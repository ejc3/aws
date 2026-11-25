"""Fixed factory request queue and broker. No caller-defined EC2 parameters.

Two tracks, each a CodeBuild project of its own (ejc3/fcvm fleet/ami/README.md): the pair (the GPU
host and the EC2 tile, four slots) and the renders host (pages mode's arm64 CPU host, two slots,
RENDERS_TRACK). A build's track is its project's; each track has its own queue, journal slots,
lease, release pointer and failure record, and neither starts, stops, retires or collects the
other's builds, instances or images."""
import base64
from contextlib import closing, contextmanager
import copy
import datetime
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
import urllib.request
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from publish import production_image_ids

SLOTS = ("host-build", "tile-build", "host-verify", "tile-verify")
TYPES = {"host": "g4dn.metal", "tile": "g4dn.xlarge"}
VOLUMES = {"host": 96, "tile": 32}
# The renders track's slots, type and root (ejc3/fcvm fleet/ami/pipeline.py RENDERS_SLOTS, build-ami.sh
# RENDERS_TYPE and `launch renders`): the type the host runs on, arm64, 128 GiB. Kept apart from the
# pair's tables: the pair's role caps its roots at the pair's largest volume.
RENDERS_SLOTS = ("renders-build", "renders-verify")
RENDERS_TYPES = {"renders": "m8gd.metal-24xl"}
RENDERS_VOLUMES = {"renders": 128}
TRACK_SLOTS = {"pair": SLOTS, "renders": RENDERS_SLOTS}
TRACK_KINDS = {"pair": ("host", "tile"), "renders": ("renders",)}
ALL_TYPES = {**TYPES, **RENDERS_TYPES}
ALL_VOLUMES = {**VOLUMES, **RENDERS_VOLUMES}
# Where the renders track keeps its queue under STATE_PREFIX. Journals (/builds/) and publication
# receipts (/publications/) are keyed by the build id, which names its project, so they share the
# pair's namespace.
RENDERS_STATE = "/renders"
# The renders executor exists only in sources with the six-hour lease (ejc3/fcvm pipeline.py LEASE_MINUTES).
RENDERS_TIMEOUT_MINUTES = 360
LEGACY_TIMEOUT_MINUTES = 240
EXTENDED_TIMEOUT_MINUTES = 360
# First fcvm revision whose executor accepts a six-hour broker lease (fcvm#1802).
EXTENDED_TIMEOUT_SOURCE = "063042ae40a1a6df3f1b4fc7d72fc3625a427058"
# This exact source was queued before the pause and boots without fleet user data (aws#850).
RETIRED_SOURCE_SHA = "3e36a3a36e13fdb03168b77ff6257a707e6654d1"
RETIRED_SOURCE_REASON = "source retired: fleet host fails to load boot user data"
PUBLICATION_SECONDS = 24 * 60 * 60
GUARD_PURPOSE = "fcvm-fleet-ami-builder"
SHA = re.compile(r"^[0-9a-f]{40}$")
INPUT = re.compile(r"^[0-9a-f]{64}$")
PUBLIC_FIELDS = ("request_id", "status", "source_sha", "input_sha256", "priority", "build_id", "not_before", "deadline_unix", "publication_deadline_unix", "message", "ci_head_sha", "ci_run_id")


# Cross-account failure status. Log text can include output from any builder subprocess.
# Classify known symptoms without copying any log text or regex capture to the Fleet account.
FAILURE_VERSION = 1
FAILURE_LOG_EVENTS = 200
FAILURE_TAIL_BYTES = 32 * 1024
PHASES = {"host": ("prep", "build", "bake"), "tile": ("prep", "image", "trace", "bake"),
          "renders": ("prep", "build", "bake")}
FAILURE_REASONS = {
    "CodeBuild definitively rejected the request before starting": "start_rejected",
    "CodeBuild execution lease does not match its journal": "invalid_execution_lease",
    "publication lease expired": "publication_lease_expired",
    "publication rolled back; automatic replay refused": "publication_rolled_back",
    "factory stopped or reached its fixed deadline": "build_failed_or_expired",
}
BUILD_STATUSES = ("FAILED", "FAULT", "STOPPED", "TIMED_OUT", "SUCCEEDED", "IN_PROGRESS", "QUEUED")
EXECUTOR_ERRORS = (
    (re.compile(r"^RuntimeError: candidate viewer path failed:"), "viewer_path_failed"),
    (re.compile(r"^(?:__main__\.)?BuilderCommandFailed:"), "builder_command_failed"),
    (re.compile(r"^(?:[A-Za-z_][\w.]*\.)?TimeoutError:"), "executor_timeout"),
    # The renders validator's verdicts (ejc3/fcvm fleet/ami/pipeline.py verify_renders).
    (re.compile(r"^RuntimeError: candidate failed the renders host check \("), "renders_host_check_failed"),
    (re.compile(r"^RuntimeError: candidate failed the agent check \("), "renders_agent_check_failed"),
    (re.compile(r"^RuntimeError: candidate failed the agent unit's own words \("), "renders_unit_check_failed"),
)
DIAGNOSTIC_CODES = (
    (re.compile(r"no space left on device|disk quota exceeded", re.I), "disk_full"),
    (re.compile(r"adb(?:: failed to| could not) install|INSTALL_FAILED_", re.I), "adb_install_failed"),
    (re.compile(r"failed to fetch|temporary failure resolving", re.I), "download_failed"),
    (re.compile(r"permission denied|access denied", re.I), "permission_denied"),
    (re.compile(r"timed out|deadline exceeded", re.I), "timeout"),
)


def diagnostic_codes(text):
    return [code for pattern, code in DIAGNOSTIC_CODES if pattern.search(text)]


class Refused(Exception):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def terminated_receipt(entry):
    instance_id = entry.get("instance_id")
    return (entry.get("attempted") is True and entry.get("terminated_seen") is True
            and isinstance(instance_id, str) and re.fullmatch(r"i-[0-9a-f]{17}", instance_id) is not None
            and not entry.get("no_instance") and not entry.get("rejected"))


def public_record(record):
    return {key: record[key] for key in PUBLIC_FIELDS if key in record}


def public_github(path):
    req = urllib.request.Request("https://api.github.com/repos/ejc3/fcvm/" + path, headers={"User-Agent": "fcvm-image-factory"})
    with urllib.request.urlopen(req, timeout=10) as response:
        raw = response.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise Refused("trusted CI metadata exceeds its fixed bound")
    return json.loads(raw)


def execution_timeout_minutes(source):
    if source == EXTENDED_TIMEOUT_SOURCE:
        return EXTENDED_TIMEOUT_MINUTES
    try:
        # Ancestry only, from page two: page one carries every changed file's patch, and over the
        # long history since EXTENDED_TIMEOUT_SOURCE it passes public_github's 2 MiB bound, which
        # gave generation fifteen's build the 240-minute limit and expired it mid-validation.
        comparison = public_github("compare/" + EXTENDED_TIMEOUT_SOURCE + "..." + source + "?per_page=1&page=2")
    except (OSError, ValueError, Refused):
        # Missing ancestry proof is not permission to extend the legacy bound.
        return LEGACY_TIMEOUT_MINUTES
    if isinstance(comparison, dict):
        base = comparison.get("merge_base_commit")
        if (comparison.get("status") == "ahead" and comparison.get("behind_by") == 0
                and isinstance(base, dict) and base.get("sha") == EXTENDED_TIMEOUT_SOURCE):
            return EXTENDED_TIMEOUT_MINUTES
    return LEGACY_TIMEOUT_MINUTES


def execution_deadline(build):
    minutes = build.get("timeoutInMinutes")
    if type(minutes) is int and minutes in (LEGACY_TIMEOUT_MINUTES, EXTENDED_TIMEOUT_MINUTES):
        return int(build["startTime"].timestamp()) + minutes * 60
    return None


def documentation_path(path):
    if not isinstance(path, str) or not path.endswith(".md"):
        return False
    pieces = path.split("/")
    return (all(piece not in {"", ".", ".."} for piece in pieces)
            and (len(pieces) == 1 or pieces[0] == "docs"))


def source_descends_from(base, head, limit=None):
    # GitHub only includes file patches on page one; this check needs ancestry alone.
    comparison = public_github("compare/" + base + "..." + head + "?per_page=1&page=2")
    distance = comparison.get("ahead_by")
    return (comparison.get("status") == "ahead" and comparison.get("behind_by") == 0
            and type(distance) is int and distance > 0 and (limit is None or distance <= limit)
            and comparison.get("merge_base_commit", {}).get("sha") == base)


def trusted_ci(source):
    # Pin the actual CI workflow, not its display name (the docs summary is also named CI).
    data = public_github("actions/workflows/ci.yml/runs?branch=main&status=success&per_page=20")
    runs = data.get("workflow_runs")
    if not isinstance(runs, list):
        raise Refused("trusted main CI is unavailable")
    # A requested ancestor's exact run can follow a newer successful run. Scan the
    # bounded page for an exact match before trying the existing docs-only fallback.
    fallback = None
    for run in runs[:20]:
        head = run.get("head_sha", "")
        if not (run.get("conclusion") == "success" and run.get("status") == "completed"
                and run.get("event") in ("push", "workflow_run") and run.get("head_branch") == "main"
                and run.get("head_repository", {}).get("full_name") == "ejc3/fcvm"
                and run.get("path", "").split("@", 1)[0] == ".github/workflows/ci.yml"
                and SHA.fullmatch(head) and type(run.get("id")) is int):
            continue
        if head == source:
            return {"ci_head_sha": head, "ci_run_id": run["id"]}
        if fallback is None:
            fallback = head, run["id"]
    if fallback is None:
        raise Refused("no successful trusted main CI covers these image inputs")
    head, run_id = fallback
    comparison = public_github("compare/" + head + "..." + source)
    files = comparison.get("files")
    if not (comparison.get("status") == "ahead" and comparison.get("behind_by") == 0
            and comparison.get("merge_base_commit", {}).get("sha") == head
            and type(comparison.get("total_commits")) is int and 0 < comparison["total_commits"] <= 250
            and isinstance(files, list) and len(files) < 300):
        raise Refused("trusted CI comparison is incomplete or outside its bound")
    for item in files:
        previous = item.get("previous_filename")
        if not documentation_path(item.get("filename")) or previous is not None and not documentation_path(previous):
            raise Refused("non-documentation changes require successful CI for this exact revision")
    return {"ci_head_sha": head, "ci_run_id": run_id}


def renders_config(raw):
    """RENDERS_TRACK, Terraform's fixed configuration of the renders track; absent, there is no track."""
    if not raw:
        return None
    value = json.loads(raw)
    names = ("project", "release_parameter", "failure_parameter", "canonical_ami", "builder_profile")
    if not (isinstance(value, dict) and set(value) == {"enabled", "templates", *names} and type(value["enabled"]) is bool
            and all(isinstance(value[name], str) and value[name] for name in names)
            and isinstance(value["templates"], list) and len(value["templates"]) <= 4
            and all(isinstance(t, dict) and set(t) == {"region", "id", "kind", "arch"} and t["kind"] == "renders"
                    and t["arch"] == "arm64" for t in value["templates"])):
        raise Refused("renders track configuration is outside its fixed scope")
    return value


def strict(event, allowed, required):
    if not isinstance(event, dict) or set(event) - set(allowed) or set(required) - set(event):
        raise Refused("invalid request fields")


class Factory:
    def __init__(self, env=None, clients=None, clock=time.time):
        self.env = dict(os.environ if env is None else env)
        self.now = clock
        self.clients = clients or {}
        self.prefix = self.env["STATE_PREFIX"]
        self.project = self.env["PROJECT_NAME"]
        self.failures = []
        # Renders-track steps that failed in this invocation (renders_step); tick raises once the pair's work is done.
        self.renders_errors = []
        try:
            self.renders = renders_config(self.env.get("RENDERS_TRACK"))
        except (Refused, ValueError, TypeError) as error:
            # A bad configuration turns the renders track off; the pair's queue, broker and cleanup run as without it.
            self.renders = None
            self.renders_errors.append("configuration")
            print(json.dumps({"renders_track_error": "configuration", "message": str(error) if isinstance(error, Refused) else type(error).__name__}))

    def track_of(self, build_id):
        """A build's track is its project's: the build id names it, and no caller can choose it."""
        if self.renders is not None and isinstance(build_id, str) and build_id.startswith(self.renders["project"] + ":"):
            return "renders"
        return "pair"

    def project_of(self, track):
        return self.renders["project"] if track == "renders" else self.project

    @staticmethod
    def state(track):
        return RENDERS_STATE if track == "renders" else ""

    def enabled(self, track):
        return self.env.get("ENABLED") == "true" and (track == "pair" or self.renders is not None and self.renders["enabled"])

    def release_parameter(self, track):
        return self.renders["release_parameter"] if track == "renders" else self.env["RELEASE_PARAMETER"]

    def builder_profile(self, track):
        """The renders builders run with an instance profile of their own, which cannot read the pair's TLS canaries."""
        return self.renders["builder_profile"] if track == "renders" else self.env["BUILDER_PROFILE"]

    def failure_parameter(self, track):
        if track == "renders":
            return self.renders["failure_parameter"] if self.renders is not None else None
        return self.env.get("FAILURE_PARAMETER")

    def client(self, service, fleet=False, region=None):
        # COMPUTE_REGION is where the factory builds. Retention also reads the release-region
        # launch templates and production AMIs to keep the originals they were copied from.
        key = (service, fleet) if region is None else (service, fleet, region)
        if key not in self.clients:
            kw = {"region_name": region or (self.env["COMPUTE_REGION"] if fleet else self.env["CONTROL_REGION"])}
            if fleet:
                creds = self.client("sts").assume_role(RoleArn=self.env["FLEET_ROLE"], RoleSessionName="image-factory-broker")["Credentials"]
                kw.update(aws_access_key_id=creds["AccessKeyId"], aws_secret_access_key=creds["SecretAccessKey"], aws_session_token=creds["SessionToken"])
            # These reads are diagnostic only and run in the single-concurrency dispatcher.
            timeouts = {"connect_timeout": 2, "read_timeout": 3} if service in ("logs", "s3") else {}
            self.clients[key] = boto3.client(service, config=Config(retries={"total_max_attempts": 1}, **timeouts), **kw)
        return self.clients[key]

    def release_ssm(self):
        # The release's parameters (/fcvm-fleet/ami-release and its copy record) are in the
        # release's region, RELEASE_REGION, which is not the build region.
        region = self.env.get("RELEASE_REGION")
        return self.client("ssm", True) if region in (None, self.env["COMPUTE_REGION"]) else self.client("ssm", True, region)

    def load(self, suffix, default=None):
        try:
            return json.loads(self.client("ssm").get_parameter(Name=self.prefix + suffix, WithDecryption=False)["Parameter"]["Value"])
        except self.client("ssm").exceptions.ParameterNotFound:
            return default

    def save(self, suffix, value):
        encoded = json.dumps(value, separators=(",", ":"))
        if len(encoded.encode()) > 4000:
            raise Refused("factory metadata exceeds its fixed bound")
        self.client("ssm").put_parameter(Name=self.prefix + suffix, Type="String", Value=encoded, Overwrite=True)

    def request_record(self, request_id, track="pair"):
        if not isinstance(request_id, str) or not INPUT.fullmatch(request_id):
            raise Refused("invalid request id")
        record = self.load(self.state(track) + "/requests/" + request_id)
        if record is None:
            raise Refused("unknown request")
        return record

    def ready(self, journal):
        track = self.track_of(journal["build_id"])
        control = self.load(self.state(track) + "/control", {})
        record = self.load(self.state(track) + "/requests/" + journal["request_id"], {})
        build = self.actual_build(journal["build_id"], active=False)
        return (control.get("active") == journal["build_id"] and record.get("status") not in (None, "superseded", "failed")
                and build["buildStatus"] == "IN_PROGRESS" and build.get("resolvedSourceVersion") == journal["source_sha"]
                and execution_deadline(build) == journal["deadline_unix"])

    def actual_build(self, build_id, active=True):
        project = self.project_of(self.track_of(build_id))
        if not isinstance(build_id, str) or not build_id.startswith(project + ":") or not re.fullmatch(r"[0-9a-f-]{36}", build_id.split(":", 1)[1]):
            raise Refused("invalid build id")
        builds = self.client("codebuild").batch_get_builds(ids=[build_id])["builds"]
        if len(builds) != 1 or builds[0]["projectName"] != project:
            raise Refused("unknown project build")
        build = builds[0]
        if active and build["buildStatus"] != "IN_PROGRESS":
            raise Refused("build is not in progress")
        return build

    def journal(self, build_id, active=True):
        build = self.actual_build(build_id, active)
        variables = {v["name"]: v["value"] for v in build.get("environment", {}).get("environmentVariables", [])}
        source = variables.get("SOURCE_SHA", "")
        input_sha = variables.get("INPUT_SHA256", "")
        resolved = build.get("resolvedSourceVersion")
        if not SHA.fullmatch(source) or not INPUT.fullmatch(input_sha) or resolved not in (None, source) or active and resolved != source:
            raise Refused("build provenance does not match the request")
        if self.track_of(build_id) == "renders" and variables.get("FLEET_AMI_TRACK") != "renders":
            raise Refused("a renders build must run the renders track")
        suffix = "/builds/" + digest(build_id)
        journal = self.load(suffix)
        if journal is None:
            started = int(build["startTime"].timestamp())
            journal = {"build_id": build_id, "source_sha": source, "input_sha256": input_sha, "request_id": input_sha,
                       # An unsupported receipt has no compute lease, but still gets cleanup.
                       "started": started, "deadline_unix": execution_deadline(build) or started, "slots": {}, "candidates": {}}
            self.save(suffix, journal)
        if journal["source_sha"] != source or journal["input_sha256"] != input_sha:
            raise Refused("immutable build provenance changed")
        return suffix, journal

    def owned(self, journal, slot):
        recorded = journal["slots"].get(slot, {})
        filters = [{"Name": "tag:Purpose", "Values": [GUARD_PURPOSE]}, {"Name": "tag:FactoryBuild", "Values": [digest(journal["build_id"])]}, {"Name": "tag:FactorySlot", "Values": [slot]}]
        instance_id = recorded.get("instance_id")
        if isinstance(instance_id, str) and re.fullmatch(r"i-[0-9a-f]{17}", instance_id):
            # An ID filter returns no rows after EC2 forgets a terminated instance.
            filters = [{"Name": "instance-id", "Values": [instance_id]}]
        reservations = self.client("ec2", True).describe_instances(Filters=filters)["Reservations"]
        if recorded.get("terminated_seen") is True and any(
                r["Instances"] and r.get("OwnerId") != self.env["FLEET_ACCOUNT"] for r in reservations):
            raise Refused("instance metadata contradicts its recorded owner")
        reported = [i for r in reservations for i in r["Instances"]]
        # EC2 may omit the profile after termination. Only an already recorded
        # instance can supply that terminal receipt without the profile proof.
        profile = self.builder_profile(self.track_of(journal["build_id"]))
        rows = [i for i in reported if all({t["Key"]: t["Value"] for t in i.get("Tags", [])}.get(key) == value
                for key, value in (("Purpose", GUARD_PURPOSE), ("FactoryBuild", digest(journal["build_id"])), ("FactorySlot", slot)))
            and i.get("InstanceType") == ALL_TYPES[slot.split("-", 1)[0]] and (
            i.get("IamInstanceProfile", {}).get("Arn") == profile or (
                "IamInstanceProfile" not in i and i["State"]["Name"] == "terminated"
                and recorded.get("instance_id") == i["InstanceId"]))]
        if recorded.get("terminated_seen") is True and reported and (
                len(rows) != len(reported) or any(i["State"]["Name"] != "terminated" for i in rows)):
            raise Refused("instance metadata contradicts its recorded termination")
        if len(rows) > 1:
            raise Refused("more than one instance in a fixed slot")
        if rows and recorded.get("instance_id") not in (None, rows[0]["InstanceId"]):
            raise Refused("slot identity changed")
        if rows and slot in journal["slots"]:
            changed = recorded.get("instance_id") is None or rows[0]["State"]["Name"] == "terminated" and not recorded.get("terminated_seen")
            recorded["instance_id"] = rows[0]["InstanceId"]
            if rows[0]["State"]["Name"] == "terminated":
                recorded["terminated_seen"] = True
            if changed:
                # Recover the paid API receipt before returning proof/status.
                self.save("/builds/" + digest(journal["build_id"]), journal)
        return rows[0] if rows else None

    def base_image(self, kind):
        # Only a sanitized factory release can replace the original Canonical base.
        image_id = self.env["CANONICAL_AMI"]
        try:
            # The active release's image in this region: its own entry, else the copy record's (a pair
            # built here is released from copies in the release's region).
            manifest = json.loads(self.release_ssm().get_parameter(Name=self.env["RELEASE_PARAMETER"], WithDecryption=False)["Parameter"]["Value"])
            candidate = manifest[kind + "_amis"].get(self.env["COMPUTE_REGION"], {}).get("x86_64")
            if candidate is None:
                record = json.loads(self.release_ssm().get_parameter(Name=self.env["RELEASE_PARAMETER"] + "-copies", WithDecryption=False)["Parameter"]["Value"])
                candidate = record["images"][self.env["COMPUTE_REGION"]][kind]
            try:
                rows = self.client("ec2", True).describe_images(ImageIds=[candidate])["Images"]
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") != "InvalidAMIID.NotFound":
                    raise
                rows = []
            if rows:
                image = rows[0]
                tags = {t["Key"]: t["Value"] for t in image.get("Tags", [])}
                if image["OwnerId"] == self.env["FLEET_ACCOUNT"] and not image.get("Public", True) and image.get("State") == "available" and image.get("Architecture") == "x86_64" and tags.get("PipelineSanitized") == "true" and tags.get("FleetImageValidated") == "true" and tags.get("FleetImageKind") == kind and tags.get("GuardHelperSHA256") == self.env["GUARD_SHA256"]:
                    image_id = candidate
        except (KeyError, self.release_ssm().exceptions.ParameterNotFound):
            pass
        images = self.client("ec2", True).describe_images(ImageIds=[image_id])["Images"]
        if len(images) != 1 or images[0].get("State") != "available" or images[0].get("Architecture") != "x86_64":
            raise Refused("factory base is unavailable")
        if image_id == self.env["CANONICAL_AMI"] and images[0]["OwnerId"] != "099720109477":
            raise Refused("initial base is not Canonical")
        return images[0]

    def renders_base_image(self):
        """The renders builder's base: the released renders image in this region, else arm64 Canonical.

        The renders pointer names the build region's original beside the release region's copy, so
        nothing else is read. Only a sanitized, validated renders image with the reviewed guard replaces
        the Canonical base, as for the pair."""
        image_id = self.renders["canonical_ami"]
        try:
            pointer = json.loads(self.release_ssm().get_parameter(Name=self.renders["release_parameter"], WithDecryption=False)["Parameter"]["Value"])
            amis = pointer.get("renders_amis") if isinstance(pointer, dict) else None
            here = amis.get(self.env["COMPUTE_REGION"], {}) if isinstance(amis, dict) else None
            if not isinstance(here, dict) or not set(here) <= {"arm64"}:
                raise Refused("the renders release pointer is malformed; no renders build starts from it")
            candidate = here.get("arm64")
            if candidate is not None:
                try:
                    rows = self.client("ec2", True).describe_images(ImageIds=[candidate])["Images"]
                except ClientError as error:
                    if error.response.get("Error", {}).get("Code") != "InvalidAMIID.NotFound":
                        raise
                    rows = []
                if rows:
                    image = rows[0]
                    tags = {t["Key"]: t["Value"] for t in image.get("Tags", [])}
                    if (image["OwnerId"] == self.env["FLEET_ACCOUNT"] and not image.get("Public", True) and image.get("State") == "available"
                            and image.get("Architecture") == "arm64" and tags.get("PipelineSanitized") == "true"
                            and tags.get("FleetImageValidated") == "true" and tags.get("FleetImageKind") == "renders"
                            and tags.get("GuardHelperSHA256") == self.env["GUARD_SHA256"]):
                        image_id = candidate
        except self.release_ssm().exceptions.ParameterNotFound:
            pass
        images = self.client("ec2", True).describe_images(ImageIds=[image_id])["Images"]
        if len(images) != 1 or images[0].get("State") != "available" or images[0].get("Architecture") != "arm64":
            raise Refused("renders factory base is unavailable")
        if image_id == self.renders["canonical_ami"] and images[0]["OwnerId"] != "099720109477":
            raise Refused("initial renders base is not Canonical")
        return images[0]

    def launch(self, suffix, journal, slot):
        if self.now() >= journal["deadline_unix"] - 60:
            raise Refused("factory deadline exhausted")
        if slot in journal["slots"]:
            # An uncertain launch is observed, never retried or replaced.
            return self.slot_status(journal, slot)
        kind, stage = slot.split("-", 1)
        if stage == "verify":
            previous = self.slot_status(journal, kind + "-build")
            if previous["state"] != "terminated":
                raise Refused("build instance must be terminated before verification")
            image_id = journal["candidates"].get(kind)
            if not image_id:
                raise Refused("no sealed candidate")
            image = self.client("ec2", True).describe_images(ImageIds=[image_id])["Images"][0]
            if image.get("State") != "available":
                raise Refused("candidate image is not available")
        else:
            image = self.renders_base_image() if kind == "renders" else self.base_image(kind)
            image_id = image["ImageId"]
        entry = {"attempted": True, "image_id": image_id, "slot": slot}
        journal["slots"][slot] = entry
        self.save(suffix, journal)  # Fence the slot before the non-transactional API call.
        tags = [{"Key": k, "Value": v} for k, v in {"Name": GUARD_PURPOSE, "Purpose": GUARD_PURPOSE,
                "FactoryBuild": digest(journal["build_id"]), "FactorySlot": slot, "SourceSHA": journal["source_sha"]}.items()]
        guard = base64.b64encode(Path(__file__).with_name("guard.py").read_bytes()).decode()
        user_data = "#!/bin/bash\nset -eu\ncommand -v nft >/dev/null || { apt-get update -qq; apt-get install -y nftables; }\nprintf '%s' '" + guard + "' | base64 -d >/run/factory-guard.py\npython3 /run/factory-guard.py --apply\nrm -f /run/factory-guard.py\n"
        try:
            response = self.client("ec2", True).run_instances(ImageId=image_id, InstanceType=ALL_TYPES[kind], MinCount=1, MaxCount=1,
            ClientToken=digest(journal["build_id"] + "/" + slot), IamInstanceProfile={"Arn": self.builder_profile(self.track_of(journal["build_id"]))},
            MetadataOptions={"HttpEndpoint": "enabled", "HttpTokens": "required", "HttpPutResponseHopLimit": 1},
            NetworkInterfaces=[{"DeviceIndex": 0, "SubnetId": self.env["SUBNET_ID"], "Groups": [self.env["SECURITY_GROUP"]], "AssociatePublicIpAddress": True, "DeleteOnTermination": True, "Ipv6PrefixCount": 1}],
            BlockDeviceMappings=[{"DeviceName": image["RootDeviceName"], "Ebs": {"VolumeSize": ALL_VOLUMES[kind], "VolumeType": "gp3", "Encrypted": True, "DeleteOnTermination": True}}],
            TagSpecifications=[{"ResourceType": t, "Tags": tags} for t in ["instance", "volume", "network-interface"]], UserData=user_data)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code")
            if code in {"InsufficientInstanceCapacity", "InstanceLimitExceeded", "VcpuLimitExceeded", "UnauthorizedOperation", "AuthFailure", "InvalidParameterValue", "Unsupported", "InsufficientFreeAddressesInSubnet"}:
                entry.update(no_instance=True, failure_code=code)
                self.save(suffix, journal)
                raise Refused("fixed launch was rejected; the slot is not retried") from None
            raise
        entry["instance_id"] = response["Instances"][0]["InstanceId"]
        self.save(suffix, journal)
        return self.slot_status(journal, slot)

    def slot_status(self, journal, slot):
        instance = self.owned(journal, slot)
        entry = journal["slots"].get(slot, {})
        instance_id = instance["InstanceId"] if instance else entry.get("instance_id")
        kind = slot.split("-", 1)[0]
        image_id = journal["candidates"].get(kind)
        image_state = None
        if image_id:
            rows = self.client("ec2", True).describe_images(ImageIds=[image_id])["Images"]
            image_state = rows[0]["State"] if rows else "missing"
        return {"slot": slot, "instance_id": instance_id,
                "state": instance["State"]["Name"] if instance else "terminated" if terminated_receipt(entry) else "rejected" if entry.get("no_instance") else "uncertain", "instance_type": ALL_TYPES[kind], "root_gib": ALL_VOLUMES[kind],
                "image_id": image_id, "image_state": image_state, "region": self.env["COMPUTE_REGION"], "deadline_unix": journal["deadline_unix"],
                "source_commit": journal["source_sha"], "input_sha256": journal["input_sha256"],
                "asset_prefix": "builders/arn:aws:ec2:" + self.env["COMPUTE_REGION"] + ":" + self.env["FLEET_ACCOUNT"] + ":instance/" + instance_id + "/" if instance_id else None}

    def seal(self, suffix, journal, slot):
        if not slot.endswith("-build") or self.now() >= journal["deadline_unix"]:
            raise Refused("only a live build slot can be sealed")
        kind = slot.split("-", 1)[0]
        instance = self.owned(journal, slot)
        if not instance or instance["State"]["Name"] != "stopped":
            raise Refused("seal requires a stopped build instance")
        if kind in journal["candidates"]:
            return self.slot_status(journal, slot)
        entry = journal["slots"][slot]
        if entry.get("seal_attempted"):
            # Recover a lost response by its immutable name, not another CreateImage.
            name = "fcvm-factory-" + digest(journal["build_id"])[:24] + "-" + kind
            images = self.client("ec2", True).describe_images(Owners=["self"], Filters=[{"Name": "name", "Values": [name]}])["Images"]
            if len(images) == 1:
                journal["candidates"][kind] = images[0]["ImageId"]
                self.save(suffix, journal)
            return self.slot_status(journal, slot)
        entry["seal_attempted"] = True
        self.save(suffix, journal)
        tags = [{"Key": k, "Value": v} for k, v in {"Purpose": "fcvm-fleet", "PipelineBuildId": journal["build_id"],
            "FleetImageKind": kind, "PipelineSanitized": "true", "SourceCommit": journal["source_sha"],
            "InputSHA256": journal["input_sha256"], "GuardHelperSHA256": self.env["GUARD_SHA256"]}.items()]
        image = self.client("ec2", True).create_image(InstanceId=instance["InstanceId"], Name="fcvm-factory-" + digest(journal["build_id"])[:24] + "-" + kind,
            NoReboot=True, TagSpecifications=[{"ResourceType": t, "Tags": tags} for t in ["image", "snapshot"]])
        journal["candidates"][kind] = image["ImageId"]
        self.save(suffix, journal)
        return self.slot_status(journal, slot)

    def cleanup(self, journal):
        instances = [self.owned(journal, slot) for slot in journal["slots"]]
        ids = [i["InstanceId"] for i in instances if i and i["State"]["Name"] not in ("terminated", "shutting-down")]
        if ids:
            self.client("ec2", True).terminate_instances(InstanceIds=ids)
        return {"slots": {slot: self.slot_status(journal, slot) for slot in journal["slots"]}, "candidates": journal["candidates"], "deadline_unix": journal["deadline_unix"]}

    def credentials(self, journal):
        if self.now() >= journal["deadline_unix"]:
            raise Refused("factory deadline exhausted")
        build_hash = digest(journal["build_id"])
        prefixes = {slot: self.slot_status(journal, slot)["asset_prefix"] for slot in journal["slots"]}
        resources = ["arn:aws:s3:::" + self.env["PIPELINE_BUCKET"] + "/runs/" + build_hash + "/*"]
        resources += ["arn:aws:s3:::" + self.env["PIPELINE_BUCKET"] + "/" + p + "*" for p in prefixes.values() if p]
        policy = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], "Resource": resources},
            # The pair's executor writes and reads its two TLS canaries; the renders executor has none
            # and gets no Secrets Manager action, so it cannot touch the pair's.
            {"Effect": "Allow", "Action": ["ssm:SendCommand", "ssm:DescribeInstanceInformation"] + (
                [] if self.track_of(journal["build_id"]) == "renders" else ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]),
             "Resource": "*"}]}
        response = self.client("sts").assume_role(RoleArn=self.env["WORKER_ROLE"], RoleSessionName="factory-" + build_hash[:24],
            DurationSeconds=min(3600, max(900, int(journal["deadline_unix"] - self.now()))),
            Tags=[{"Key": "FactoryBuild", "Value": build_hash}], Policy=json.dumps(policy, separators=(",", ":")))
        creds = response["Credentials"]
        return {"credentials": {"AccessKeyId": creds["AccessKeyId"], "SecretAccessKey": creds["SecretAccessKey"],
                "SessionToken": creds["SessionToken"], "Expiration": int(creds["Expiration"].timestamp())},
                "asset_prefix": "runs/" + build_hash + "/", "builder_prefixes": prefixes}

    def command_status(self, journal, slot, command_id):
        if not isinstance(command_id, str) or not re.fullmatch(r"[0-9a-f-]{36}", command_id):
            raise Refused("invalid command id")
        instance = self.owned(journal, slot)
        if not instance:
            raise Refused("unknown slot instance")
        try:
            result = self.client("ssm", True).get_command_invocation(CommandId=command_id, InstanceId=instance["InstanceId"])
        except self.client("ssm", True).exceptions.InvocationDoesNotExist:
            return {"status": "Pending", "response_code": -1, "standard_output": "", "standard_error": ""}
        return {"status": result["Status"], "response_code": result.get("ResponseCode"),
                "standard_output": result.get("StandardOutputContent", ""), "standard_error": result.get("StandardErrorContent", "")}

    def admitted_sources(self, control):
        last = control.get("last_source")
        if last is not None:
            sources = [last]
        else:
            # Before last_source existed, these records could already hold a newer
            # revision. Do not let the first ancestor request displace that work.
            sources = []
            pending = control.get("pending")
            if pending is not None:
                sources.append(self.request_record(pending).get("source_sha"))
            active = control.get("active")
            if isinstance(active, dict):
                sources.append(self.request_record(active.get("intent")).get("source_sha"))
            elif active is not None:
                sources.append(self.actual_build(active, active=False).get("resolvedSourceVersion"))
        if any(not isinstance(source, str) or not SHA.fullmatch(source) for source in sources):
            raise Refused("a previously admitted factory source could not be verified")
        return sorted(set(sources))

    def request(self, event):
        action = event.get("action")
        if action == "status":
            strict(event, ["action", "request_id"], ["action", "request_id"])
            self.reconcile()
            return public_record(self.request_record(event["request_id"]))
        strict(event, ["action", "source_sha", "input_sha256", "priority"], ["action", "source_sha", "input_sha256", "priority"])
        if action != "request" or not SHA.fullmatch(str(event["source_sha"])) or not INPUT.fullmatch(str(event["input_sha256"])) or event["priority"] not in ("normal", "critical"):
            raise Refused("invalid image request")
        if self.env.get("ENABLED") != "true":
            return {"request_id": event["input_sha256"], "status": "disabled", "message": "factory automation is disabled"}
        if event["source_sha"] == RETIRED_SOURCE_SHA:
            raise Refused(RETIRED_SOURCE_REASON)
        request_id = event["input_sha256"]
        existing = self.load("/requests/" + request_id)
        if existing:
            if event["priority"] == "critical" and existing["priority"] == "normal" and existing["status"] in ("queued", "building", "awaiting-publish"):
                existing.update(priority="critical", not_before=int(self.now()))
                self.save("/requests/" + request_id, existing)
                self.reconcile()
                existing = self.request_record(request_id)
            self.renders_step(self.mirror_renders, existing)
            return public_record(existing)
        current = public_github("git/ref/heads/main")["object"]["sha"]
        source = event["source_sha"]
        control = self.load("/control", {})
        if current != source:
            # A busy main rarely holds still for its CI (the next merge cancels the head's run, a
            # download fails it), so a recent ancestor of main with its own successful CI enters
            # too, but never one older than a revision already admitted: that would roll back.
            if not source_descends_from(source, current, limit=250):
                raise Refused("only main or a recent ancestor of it can enter the factory")
            for floor in self.admitted_sources(control):
                if floor != source and not source_descends_from(floor, source):
                    raise Refused("requested main revision does not descend from a source already admitted by the factory")
        ci = trusted_ci(source)
        now = int(self.now())
        record = {"request_id": request_id, "status": "queued", "source_sha": source, "input_sha256": request_id,
                  "priority": event["priority"], "build_id": None, "created": now, "not_before": now + (21600 if event["priority"] == "normal" else 0), "deadline_unix": None, **ci}
        previous = self.request_record(control["pending"]) if control.get("pending") else None
        if (previous is not None and previous["priority"] == record["priority"] == "normal"
                and previous["status"] == "queued" and not previous.get("start_attempted")):
            # Keep the first request's window while replacing its source with the newest main.
            record["not_before"] = min(record["not_before"], previous["not_before"])
        record["build_timeout_minutes"] = execution_timeout_minutes(record["source_sha"])
        self.save("/requests/" + request_id, record)
        if previous is not None:
            if previous["priority"] == "critical" and record["priority"] == "normal":
                record.update(status="superseded", message="a critical request is pending")
                self.save("/requests/" + request_id, record)
                return public_record(record)
            previous.update(status="superseded", message="a newer input superseded this queued request")
            self.save("/requests/" + previous["request_id"], previous)
        control["pending"] = request_id
        control["last_source"] = source
        self.save("/control", control)
        self.reconcile(checked_timeout=(request_id, source, record["build_timeout_minutes"]))
        admitted = self.request_record(request_id)
        self.renders_step(self.mirror_renders, admitted)
        return public_record(admitted)

    def renders_step(self, step, *arguments):
        """Run one renders-track step where the pair's work goes on regardless: a failure is logged by
        its type and recorded, so the pair's answer, queue and cleanup are what they would be without the
        track, and tick() then fails the invocation (a Lambda error) once both tracks have run."""
        try:
            step(*arguments)
        except Exception as error:
            self.renders_errors.append(type(error).__name__)
            print(json.dumps({"renders_track_error": type(error).__name__}))

    def mirror_renders(self, record):
        """Queue the renders build of a request the pair admitted: the same id, source, priority and CI
        receipt, so the source fences and the daily window are the pair's. The renders queue then runs
        on its own (its control, its records, its project); a newer request supersedes a queued older
        one there as it does in the pair's queue. Only a live pair request (queued, building or awaiting
        publication) creates a renders record: a request repeated after its pair build ended can only
        raise an existing renders record's priority, never queue an older source over a newer one.
        This writes records only; the next tick starts the build."""
        if not self.enabled("renders"):
            return
        requests, control_key = RENDERS_STATE + "/requests/", RENDERS_STATE + "/control"
        request_id = record["request_id"]
        existing = self.load(requests + request_id)
        if existing:
            if record["priority"] == "critical" and existing["priority"] == "normal" and existing["status"] in ("queued", "building", "awaiting-publish"):
                existing.update(priority="critical", not_before=int(self.now()))
                self.save(requests + request_id, existing)
            return
        if record.get("status") not in ("queued", "building", "awaiting-publish"):
            return
        mirrored = {key: record[key] for key in ("source_sha", "priority", "not_before", "ci_head_sha", "ci_run_id") if key in record}
        mirrored.update(request_id=request_id, input_sha256=request_id, track="renders", status="queued", build_id=None,
                        created=int(self.now()), deadline_unix=None, build_timeout_minutes=RENDERS_TIMEOUT_MINUTES)
        control = self.load(control_key, {})
        previous = self.request_record(control["pending"], "renders") if control.get("pending") else None
        self.save(requests + request_id, mirrored)
        if previous is not None and previous["request_id"] != request_id:
            if previous["priority"] == "critical" and mirrored["priority"] == "normal":
                mirrored.update(status="superseded", message="a critical request is pending")
                self.save(requests + request_id, mirrored)
                return
            if previous["status"] == "queued" and not previous.get("start_attempted"):
                previous.update(status="superseded", message="a newer input superseded this queued request")
                self.save(requests + previous["request_id"], previous)
        control["pending"] = request_id
        self.save(control_key, control)

    @contextmanager
    def control_lock(self):
        # Same mutex as Terraform and the publisher; never read backend state.
        info = json.dumps({"ID": str(uuid.uuid4()), "Operation": "FleetAMIControl", "Who": "factory-dispatch", "Version": "1.10.3",
                           "Created": datetime.datetime.fromtimestamp(self.now(), datetime.timezone.utc).isoformat()}, separators=(",", ":"))
        client = self.client("dynamodb")
        try:
            client.put_item(TableName=self.env["LOCK_TABLE"], Item={"LockID": {"S": self.env["LOCK_ID"]}, "Info": {"S": info}},
                            ConditionExpression="attribute_not_exists(LockID)")
        except client.exceptions.ConditionalCheckFailedException:
            yield False
            return
        try:
            yield True
        finally:
            client.delete_item(TableName=self.env["LOCK_TABLE"], Key={"LockID": {"S": self.env["LOCK_ID"]}},
                               ConditionExpression="#info = :info", ExpressionAttributeNames={"#info": "Info"},
                               ExpressionAttributeValues={":info": {"S": info}})

    def gone(self, suffix, journal):
        complete = True
        for slot, entry in journal["slots"].items():
            instance = self.owned(journal, slot)
            if instance and instance["State"]["Name"] == "terminated":
                entry["terminated_seen"] = True
            elif not terminated_receipt(entry) and not (entry.get("no_instance") is True and not entry.get("instance_id")):
                complete = False  # Includes an ambiguous launch: never infer that no instance exists.
        self.save(suffix, journal)
        return complete

    def executor_failure(self, build):
        """A fixed executor error code and a validated archive locator; the locator stays internal."""
        logs = build.get("logs") or {}
        group, stream = logs.get("groupName"), logs.get("streamName")
        if not isinstance(group, str) or not isinstance(stream, str):
            return None, None
        events = self.client("logs").get_log_events(logGroupName=group, logStreamName=stream,
            startFromHead=False, limit=FAILURE_LOG_EVENTS)["events"]
        error, receipt = None, None
        kinds = TRACK_KINDS[self.track_of(build.get("id"))]
        for event in events:
            message = str(event.get("message", "")).strip()
            if message.startswith('{"phaseDiagnostics"'):
                try:
                    value = json.loads(message)["phaseDiagnostics"]
                except (ValueError, KeyError, TypeError):
                    continue
                kind = value.get("kind") if isinstance(value, dict) else None
                if isinstance(kind, str) and kind in kinds and value.get("phase") in PHASES.get(kind, ()):
                    version = value.get("version_id")
                    if not isinstance(version, str) or not 0 < len(version) <= 1024 or version == "null":
                        version = None
                    receipt = {"kind": kind, "phase": value["phase"], "version_id": version}
            else:
                for pattern, code in EXECUTOR_ERRORS:
                    if pattern.match(message):
                        error = code
                        break
        return error, receipt

    def phase_diagnostic_codes(self, journal, receipt):
        """Classify a bounded read of one archived phase from its builder's prefix."""
        instance_id = journal["slots"].get(receipt["kind"] + "-build", {}).get("instance_id")
        if not receipt.get("version_id") or not isinstance(instance_id, str) or not re.fullmatch(r"i-[0-9a-f]{17}", instance_id):
            return None
        key = ("builders/arn:aws:ec2:" + self.env["COMPUTE_REGION"] + ":" + self.env["FLEET_ACCOUNT"] + ":instance/"
               + instance_id + "/diagnostics/" + receipt["kind"] + "-" + receipt["phase"] + ".log")
        response = self.client("s3", False, self.env["PIPELINE_REGION"]).get_object(Bucket=self.env["PIPELINE_BUCKET"],
            Key=key, VersionId=receipt["version_id"], Range="bytes=-" + str(FAILURE_TAIL_BYTES))
        with closing(response["Body"]) as body:
            return diagnostic_codes(body.read(FAILURE_TAIL_BYTES).decode("utf-8", "replace"))

    def publish_failure(self, record, journal=None, build=None):
        """Write fixed failure codes and validated build metadata; never forward free-form text."""
        track = record.get("track", "pair")
        name = self.failure_parameter(track)
        request_id, source = record.get("request_id"), record.get("source_sha")
        if (not name or record.get("status") != "failed" or not isinstance(request_id, str)
                or not INPUT.fullmatch(request_id)):
            return
        message = record.get("message")
        reason = FAILURE_REASONS.get(message, "unknown_failure") if isinstance(message, str) else "unknown_failure"
        status = (build or {}).get("buildStatus")
        value = {"version": FAILURE_VERSION, "request_id": request_id,
                 "source_sha": source if isinstance(source, str) and SHA.fullmatch(source) else None,
                 "status": "failed", "reason": reason, "failed_at": int(self.now()),
                 "build_status": status if status in BUILD_STATUSES else None,
                 "executor_error": None, "phase": None, "diagnostic_codes": [], "unavailable": []}
        if track == "renders":
            value["track"] = "renders"
        receipt = None
        if build is not None:
            try:
                value["executor_error"], receipt = self.executor_failure(build)
                if receipt:
                    value["phase"] = {"kind": receipt["kind"], "phase": receipt["phase"]}
            except Exception:
                value["unavailable"].append("executor_log")
        if receipt:
            try:
                codes = self.phase_diagnostic_codes(journal, receipt) if journal is not None else None
            except Exception:
                codes = None
            if codes is None:
                value["unavailable"].append("phase_diagnostics")
            else:
                value["diagnostic_codes"] = codes
        try:
            self.release_ssm().put_parameter(Name=name, Type="String", Overwrite=True,
                Value=json.dumps(value, separators=(",", ":")))
        except Exception as error:
            print(json.dumps({"failure_parameter_error": type(error).__name__}))

    def queue_failure(self, record, journal=None, build=None):
        # A publisher rollback can leave a request failed before the dispatcher sees it.
        # Track the best-effort report separately from the request's terminal state.
        track = record.get("track", "pair")
        if not self.failure_parameter(track) or record.get("failure_report_queued") is True:
            return
        record["failure_report_queued"] = True
        self.save(self.state(track) + "/requests/" + record["request_id"], record)
        self.failures.append((copy.deepcopy(record), copy.deepcopy(journal), build))

    def reconcile(self, checked_timeout=None, track="pair"):
        self.failures = []
        try:
            publication = self.reconcile_control(checked_timeout, track)
        finally:
            # In the order they were recorded, so the parameter ends on the newest.
            failures, self.failures = self.failures, []
            for failure in failures:
                self.publish_failure(*failure)
        if publication is not None:
            # The publisher takes this same mutex. Invoke only after releasing it.
            response = self.client("lambda").invoke(FunctionName=self.env["PUBLISH_FUNCTION"], InvocationType="Event",
                Payload=json.dumps(publication, separators=(",", ":")).encode())
            if response.get("StatusCode") != 202:
                raise Refused("publication retry was not accepted")

    def request_window_open(self, control, request):
        if request["priority"] != "normal":
            return True
        now = self.now()
        today = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date().isoformat()
        return now >= request["not_before"] and control.get("last_normal_day") != today

    @staticmethod
    def legacy_timeout_to_recheck(request):
        return (request["status"] == "queued" and request.get("build_timeout_minutes") == LEGACY_TIMEOUT_MINUTES
                and not request.get("start_attempted") and not request.get("build_id")
                and request["source_sha"] != RETIRED_SOURCE_SHA)

    def reconcile_control(self, checked_timeout=None, track="pair"):
        control_key, requests = self.state(track) + "/control", self.state(track) + "/requests/"
        project, enabled = self.project_of(track), self.enabled(track)
        control = self.load(control_key, {})
        if not control.get("active") and not control.get("pending"):
            return
        # A queued request may have received the legacy limit when the ancestry lookup failed.
        # Recheck outside the backend mutex, and bind the proof to the exact request under it.
        if control.get("pending") and enabled:
            pending = self.request_record(control["pending"], track)
            if self.legacy_timeout_to_recheck(pending) and self.request_window_open(control, pending):
                identity = (pending["request_id"], pending["source_sha"])
                if checked_timeout is None or checked_timeout[:2] != identity:
                    checked_timeout = (*identity, execution_timeout_minutes(pending["source_sha"]))
        start_record = None
        is_new = False
        with self.control_lock() as acquired:
            if not acquired:
                return
            control = self.load(control_key, {})
            pending = self.request_record(control["pending"], track) if control.get("pending") else None
            active = control.get("active")
            if isinstance(active, dict):
                start_record = self.request_record(active["intent"], track)
                if start_record.get("no_build"):
                    # A definitive rejection receipt survives a busy mutex.
                    # Clear this intent only; a newer pending request stays queued.
                    control["active"] = None
                    if control.get("pending") == start_record["request_id"]:
                        control["pending"] = None
                        pending = None
                    self.queue_failure(start_record)
                    self.save(control_key, control)
                    start_record = None
            elif active:
                if self.track_of(active) != track:
                    raise Refused("the control names another track's build")
                suffix, journal = self.journal(active, active=False)
                build = self.actual_build(active, active=False)
                record = self.request_record(journal["request_id"], track)
                preempt = pending and pending["priority"] == "critical" and pending["request_id"] != record["request_id"]
                invalid_lease = execution_deadline(build) != journal["deadline_unix"]
                succeeded = build["buildStatus"] == "SUCCEEDED" and not invalid_lease
                expired = build["buildStatus"] in ("IN_PROGRESS", "QUEUED") and self.now() >= journal["deadline_unix"]
                failed = build["buildStatus"] in ("FAILED", "FAULT", "STOPPED", "TIMED_OUT")
                publication_expired = False
                if succeeded:
                    publication = self.load("/publications/" + digest(active), {})
                    if publication.get("build_id") == active and publication.get("status") == "withdrawn":
                        release = json.loads(self.release_ssm().get_parameter(
                            Name=self.release_parameter(track), WithDecryption=False)["Parameter"]["Value"])
                        current = release.get("build_id")
                        if isinstance(current, str) and current and current != active:
                            # The publisher records intent before the pointer commits. Only a
                            # moved pointer under this same mutex makes the withdrawal terminal.
                            # The request key can now name another retry: never overwrite it.
                            if record.get("build_id") == active and record["status"] not in ("failed", "promoted", "superseded"):
                                record.update(status="failed", message="publication rolled back; automatic replay refused")
                                self.save(requests + record["request_id"], record)
                                self.queue_failure(record, journal, build)
                            self.cleanup(journal)
                            if self.gone(suffix, journal):
                                control["active"] = None
                                self.save(control_key, control)
                            return
                if succeeded and record["status"] != "failed":
                    if not build.get("buildComplete") or not isinstance(build.get("endTime"), datetime.datetime):
                        raise Refused("successful build has no final execution receipt")
                    publication_deadline = int(build["endTime"].timestamp()) + PUBLICATION_SECONDS
                    record["publication_deadline_unix"] = publication_deadline
                    if self.now() >= publication_deadline:
                        release = json.loads(self.release_ssm().get_parameter(Name=self.release_parameter(track), WithDecryption=False)["Parameter"]["Value"])
                        publication_expired = release.get("build_id") != active
                if record["status"] == "failed":
                    # Preserve the publisher's rollback reason even after its lease expires.
                    self.queue_failure(record, journal, build)
                    if build["buildStatus"] in ("IN_PROGRESS", "QUEUED"):
                        self.client("codebuild").stop_build(id=active)
                    self.cleanup(journal)
                    if not self.gone(suffix, journal):
                        return
                    control["active"] = None
                    self.save(control_key, control)
                elif preempt or expired or failed or publication_expired or invalid_lease:
                    if build["buildStatus"] in ("IN_PROGRESS", "QUEUED"):
                        self.client("codebuild").stop_build(id=active)
                    self.cleanup(journal)
                    message = ("preempted by critical work" if preempt else
                               "CodeBuild execution lease does not match its journal" if invalid_lease else
                               "publication lease expired" if publication_expired else
                               "factory stopped or reached its fixed deadline")
                    record.update(status="superseded" if preempt else "failed", message=message)
                    self.save(requests + record["request_id"], record)
                    if record["status"] == "failed":
                        self.queue_failure(record, journal, build)
                    if not self.gone(suffix, journal):
                        return
                    control["active"] = None
                    self.save(control_key, control)
                elif record["status"] in ("promoted", "superseded"):
                    self.cleanup(journal)
                    if not self.gone(suffix, journal):
                        return
                    control["active"] = None
                    self.save(control_key, control)
                else:
                    if succeeded:
                        record["status"] = "awaiting-publish"
                        self.save(requests + record["request_id"], record)
                        if not self.gone(suffix, journal):
                            return  # Final EC2 shutdown can outlast the executor's compute lease.
                        due = self.now() - record.get("publication_last_attempt", 0) >= 300
                        if due:
                            record["publication_last_attempt"] = int(self.now())
                        self.save(requests + record["request_id"], record)
                        if due:
                            return {"source": "aws.codebuild", "detail-type": "CodeBuild Build State Change",
                                "account": self.env["CONTROL_ACCOUNT"], "region": self.env["CONTROL_REGION"],
                                "detail": {"build-status": "SUCCEEDED", "project-name": project,
                                    "build-id": "arn:aws:codebuild:" + self.env["CONTROL_REGION"] + ":" + self.env["CONTROL_ACCOUNT"] + ":build/" + active}}
                    return
            if start_record is None:
                if not pending or not enabled:
                    return
                if pending.get("source_sha") == RETIRED_SOURCE_SHA:
                    if pending.get("start_attempted") or pending.get("build_id"):
                        raise Refused("retired request has an uncertain start")
                    if pending["status"] not in ("queued", "superseded"):
                        raise Refused("retired pending request has an unexpected status")
                    # Save terminal status first so a lost control write can be retried safely.
                    pending.update(status="superseded", message=RETIRED_SOURCE_REASON)
                    self.save(requests + pending["request_id"], pending)
                    control["pending"] = None
                    self.save(control_key, control)
                    return
                if not self.request_window_open(control, pending):
                    return
                # Invalidate old publication before leaving the mutex for StartBuild.
                start_record = pending
                is_new = not start_record.get("start_attempted")
                if is_new:
                    if start_record["status"] != "queued" or start_record.get("build_id"):
                        return  # Never fence a record that ceased to be an unstarted request.
                    if self.legacy_timeout_to_recheck(start_record):
                        if checked_timeout is None or checked_timeout[:2] != (start_record["request_id"], start_record["source_sha"]):
                            return  # The eligible request changed; reconcile it on the next tick.
                        if checked_timeout[2] == EXTENDED_TIMEOUT_MINUTES:
                            start_record["build_timeout_minutes"] = EXTENDED_TIMEOUT_MINUTES
                    start_record["start_attempted"] = int(self.now())
                    self.save(requests + start_record["request_id"], start_record)
                control["active"] = {"intent": start_record["request_id"]}
                self.save(control_key, control)
        # No backend lock is held through StartBuild or its reconciliation.
        if is_new:
            try:
                minutes = (EXTENDED_TIMEOUT_MINUTES if start_record.get("build_timeout_minutes") == EXTENDED_TIMEOUT_MINUTES
                           else LEGACY_TIMEOUT_MINUTES)
                # The renders build shares its request id with the pair's: its token is its own, so CodeBuild can
                # neither refuse it as a repeat nor answer with the pair's build.
                token = start_record["request_id"] if track == "pair" else digest("renders/" + start_record["request_id"])
                build = self.client("codebuild").start_build(projectName=project, sourceVersion=start_record["source_sha"],
                    idempotencyToken=token, timeoutInMinutesOverride=minutes, environmentVariablesOverride=[
                        {"name": "SOURCE_SHA", "value": start_record["source_sha"], "type": "PLAINTEXT"},
                        {"name": "INPUT_SHA256", "value": start_record["request_id"], "type": "PLAINTEXT"},
                        {"name": "FLEET_AMI_CAPABILITY", "value": secrets.token_urlsafe(32), "type": "PLAINTEXT"}])["build"]
            except ClientError as error:
                if error.response.get("Error", {}).get("Code") not in {
                        "AccountLimitExceededException", "InvalidInputException", "ResourceNotFoundException", "AccessDeniedException"}:
                    raise  # An unknown outcome still must never replay StartBuild.
                record = self.request_record(start_record["request_id"], track)
                record.update(status="failed", no_build=True, message="CodeBuild definitively rejected the request before starting")
                self.save(requests + record["request_id"], record)
                with self.control_lock() as acquired:
                    if acquired:
                        control = self.load(control_key, {})
                        if control.get("active") == {"intent": record["request_id"]}:
                            # Queue the failure while the durable no_build intent can still
                            # recover it, then clear the intent before publishing outside the lock.
                            self.queue_failure(record)
                            control["active"] = None
                            if control.get("pending") == record["request_id"]:
                                control["pending"] = None
                            self.save(control_key, control)
                return
        else:
            ids = self.client("codebuild").list_builds_for_project(projectName=project, sortOrder="DESCENDING")["ids"][:20]
            builds = self.client("codebuild").batch_get_builds(ids=ids)["builds"] if ids else []
            matches = [b for b in builds if any(v.get("name") == "INPUT_SHA256" and v.get("value") == start_record["request_id"] for v in b.get("environment", {}).get("environmentVariables", []))]
            if len(matches) != 1:
                # Retain the intent fence. Ambiguity must never permit a replacement build.
                return
            build = matches[0]
        if self.track_of(build.get("id")) != track or build.get("projectName") != project:
            # Keep the intent fence: a build of the other track is never bound to this queue.
            raise Refused("CodeBuild answered with another track's build")
        with self.control_lock() as acquired:
            if not acquired:
                return
            control = self.load(control_key, {})
            if control.get("active") != {"intent": start_record["request_id"]}:
                raise Refused("build start no longer owns the control intent")
            record = self.request_record(start_record["request_id"], track)
            record.update(status="superseded" if record["status"] == "superseded" else "building",
                          build_id=build["id"], deadline_unix=execution_deadline(build))
            control["active"] = build["id"]
            if control.get("pending") == start_record["request_id"]:
                control["pending"] = None
            if record["priority"] == "normal":
                control["last_normal_day"] = datetime.datetime.fromtimestamp(self.now(), datetime.timezone.utc).date().isoformat()
            self.save(requests + record["request_id"], record)
            self.save(control_key, control)

    def tick(self):
        # Server-held journals make cleanup independent of runner/GitHub lifetime.
        rows = [i for r in self.client("ec2", True).describe_instances(Filters=[
            {"Name": "tag:Purpose", "Values": [GUARD_PURPOSE]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}])["Reservations"] for i in r["Instances"]]
        try:
            for instance in rows:
                tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
                build_hash = tags.get("FactoryBuild", "")
                if not INPUT.fullmatch(build_hash) or tags.get("FactorySlot") not in SLOTS or instance.get("IamInstanceProfile", {}).get("Arn") != self.env["BUILDER_PROFILE"] or instance.get("InstanceType") not in TYPES.values():
                    continue
                self.reap(instance, build_hash)
            self.reconcile()
            self.collect_images()
        finally:
            # The renders track's builders, queue and images, after the pair's work and whether or not it raised.
            if self.renders is not None:
                self.renders_step(self.reap_renders, rows)
                self.renders_step(self.reconcile, None, "renders")
                self.renders_step(self.collect_renders_images)
        if self.renders_errors:
            raise RuntimeError("renders track steps failed: " + ",".join(self.renders_errors))
        return {"status": "checked"}

    def reap_renders(self, rows):
        """The renders builders: their own slots, type and profile, never the pair's."""
        for instance in rows:
            tags = {t["Key"]: t["Value"] for t in instance.get("Tags", [])}
            build_hash = tags.get("FactoryBuild", "")
            if (tags.get("FactorySlot") in RENDERS_SLOTS and INPUT.fullmatch(build_hash)
                    and instance.get("IamInstanceProfile", {}).get("Arn") == self.renders["builder_profile"]
                    and instance.get("InstanceType") == RENDERS_TYPES["renders"]):
                self.reap(instance, build_hash)

    def reap(self, instance, build_hash):
        """Terminate one owned builder whose build has ended or whose lease has run out."""
        journal = self.load("/builds/" + build_hash)
        if journal is not None:
            build = self.actual_build(journal["build_id"], active=False)
            deadline = execution_deadline(build)
            expired = deadline is None or self.now() >= min(deadline, journal["deadline_unix"])
            terminal = build["buildStatus"] not in ("IN_PROGRESS", "QUEUED")
        else:
            # A real launch has a durable journal before RunInstances; a lost journal cannot extend compute.
            expired = self.now() - instance["LaunchTime"].timestamp() >= LEGACY_TIMEOUT_MINUTES * 60
            terminal = False
        if expired or terminal:
            self.client("ec2", True).terminate_instances(InstanceIds=[instance["InstanceId"]])

    def prod_factory_images(self, owned):
        """Protect production's active and rollback releases and both ends of their build-region copy links."""
        ssm = self.release_ssm()
        try:
            prod = json.loads(ssm.get_parameter(Name=self.env["PROD_RELEASE_PARAMETER"],
                                                WithDecryption=False)["Parameter"]["Value"])
        except ssm.exceptions.ParameterNotFound:
            return set()
        build = self.env["COMPUTE_REGION"]
        by_region = production_image_ids(prod, self.env.get("RELEASE_REGION", build))
        keep = set(by_region.get(build, ()))
        for region, ids in by_region.items():
            if region == build:
                continue
            ec2 = self.client("ec2", True, region)
            if not {"SourceImageRegion", "SourceImageId"} <= set(ec2.meta.service_model.shape_for("Image").members):
                raise Refused("EC2 SDK cannot report production AMI copy sources; preserving factory images")
            copies = ec2.describe_images(Owners=[self.env["FLEET_ACCOUNT"]], ImageIds=sorted(ids))["Images"]
            if {row["ImageId"] for row in copies} != ids:
                raise Refused("production AMI inventory incomplete; preserving factory images")
            keep.update(row["SourceImageId"] for row in copies if row.get("SourceImageRegion") == build)
            keep.update(row["ImageId"] for row, _ in owned
                        if row.get("SourceImageRegion") == region and row.get("SourceImageId") in ids)
        return keep

    def collect_images(self):
        """Bounded cleanup: factory-only images, preserving every live/default/release reference."""
        self.collect(self.project, self.pair_references)

    def collect_renders_images(self):
        """The renders track's own candidates: its project's images only, keeping what its pointer and its
        template name. The pair's collection never sees them (their build ids name the renders project)."""
        self.collect(self.renders["project"], self.renders_references)

    def renders_references(self, ec2, owned):
        """The renders images in use: the renders pointer's and its previous release's (the build region's
        originals and the release region's copies), and each renders template's default image. A missing
        pointer refuses: no collection without the release it would have to keep."""
        pointer = json.loads(self.release_ssm().get_parameter(Name=self.renders["release_parameter"], WithDecryption=False)["Parameter"]["Value"])
        referenced = set()
        for bundle in [pointer, pointer.get("previous") or {}]:
            for arches in bundle.get("renders_amis", {}).values():
                referenced.update(v for v in arches.values() if v)
        for template in self.renders["templates"]:
            where = ec2 if template["region"] == self.env["COMPUTE_REGION"] else self.client("ec2", True, template["region"])
            versions = where.describe_launch_template_versions(LaunchTemplateId=template["id"], Versions=["$Default"])["LaunchTemplateVersions"]
            referenced.update(v["LaunchTemplateData"]["ImageId"] for v in versions)
        return referenced

    def collect(self, project, references):
        ec2 = self.client("ec2", True)
        rows = ec2.describe_images(Owners=["self"], Filters=[{"Name": "tag:Purpose", "Values": ["fcvm-fleet"]},
            {"Name": "tag:PipelineSanitized", "Values": ["true"]}])["Images"]
        owned = []
        for image in rows:
            tags = {t["Key"]: t["Value"] for t in image.get("Tags", [])}
            if image.get("OwnerId") == self.env["FLEET_ACCOUNT"] and tags.get("Purpose") == "fcvm-fleet" and tags.get("PipelineSanitized") == "true" and tags.get("PipelineBuildId", "").startswith(project + ":"):
                owned.append((image, tags["PipelineBuildId"]))
        if not owned or len(owned) > 80:
            return
        builds = self.client("codebuild").batch_get_builds(ids=list({build for _, build in owned}))["builds"]
        by_id = {b["id"]: b for b in builds}
        successful = sorted((b for b in builds if b["buildStatus"] == "SUCCEEDED"), key=lambda b: b["startTime"], reverse=True)
        keep_builds = {b["id"] for b in successful[:3]}
        with self.control_lock() as acquired:
            if not acquired:
                return
            referenced = references(ec2, owned)
            live = [i for r in ec2.describe_instances(Filters=[{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]}])["Reservations"] for i in r["Instances"]]
            referenced.update(i.get("ImageId") for i in live)
            for image, build_id in owned:
                build = by_id.get(build_id)
                if image["ImageId"] in referenced or build_id in keep_builds or not build or build["buildStatus"] in ("IN_PROGRESS", "QUEUED"):
                    continue
                if build["buildStatus"] != "SUCCEEDED":
                    ended = build.get("endTime")
                    if not isinstance(ended, datetime.datetime) or self.now() < ended.timestamp() + PUBLICATION_SECONDS:
                        continue  # Preserve failed candidates for one bounded day of diagnosis.
                journal = self.load("/builds/" + digest(build_id))
                if not journal or not self.gone("/builds/" + digest(build_id), journal):
                    continue
                ec2.deregister_image(ImageId=image["ImageId"])
                for mapping in image.get("BlockDeviceMappings", []):
                    snapshot = mapping.get("Ebs", {}).get("SnapshotId")
                    if snapshot:
                        try:
                            ec2.delete_snapshot(SnapshotId=snapshot)
                        except ClientError as error:
                            if error.response.get("Error", {}).get("Code") != "InvalidSnapshot.InUse":
                                raise

    def pair_references(self, ec2, owned):
        """The pair's images in use: its release and previous release, production's, the copy record's and
        every configured template's default."""
        release = json.loads(self.release_ssm().get_parameter(Name=self.env["RELEASE_PARAMETER"], WithDecryption=False)["Parameter"]["Value"])
        referenced = self.prod_factory_images(owned)
        for bundle in [release, release.get("previous") or {}]:
            for key in ("host_amis", "tile_amis"):
                for arches in bundle.get(key, {}).values():
                    referenced.update(v for v in arches.values() if v)
        # The release's images in every region: a pair built here is released from its copies
        # elsewhere, so the originals here are named only by the copy record.
        try:
            record = json.loads(self.release_ssm().get_parameter(Name=self.env["RELEASE_PARAMETER"] + "-copies", WithDecryption=False)["Parameter"]["Value"])
        except self.release_ssm().exceptions.ParameterNotFound:
            record = {}
        for field in ("images", "previous_images"):
            for kinds in record.get(field, {}).values():
                referenced.update(v for v in kinds.values() if v)
        for template in json.loads(self.env.get("TEMPLATES_JSON", "[]")):
            where = ec2 if template["region"] == self.env["COMPUTE_REGION"] else self.client("ec2", True, template["region"])
            versions = where.describe_launch_template_versions(LaunchTemplateId=template["id"], Versions=["$Default"])["LaunchTemplateVersions"]
            referenced.update(v["LaunchTemplateData"]["ImageId"] for v in versions)
        return referenced

    def broker(self, event):
        strict(event, ["action", "build_id", "slot", "command_id", "capability"], ["action", "build_id", "capability"])
        action = event["action"]
        if action not in ("launch", "status", "reboot", "stop", "start", "seal", "cleanup", "credentials", "command_status"):
            raise Refused("unknown broker action")
        build = self.actual_build(event["build_id"], active=False)
        variables = {v["name"]: v["value"] for v in build.get("environment", {}).get("environmentVariables", [])}
        capability = event.get("capability")
        expected = variables.get("FLEET_AMI_CAPABILITY", "")
        if not isinstance(capability, str) or len(expected) < 32 or not hmac.compare_digest(capability, expected):
            raise Refused("invalid factory capability")
        suffix, journal = self.journal(event["build_id"], active=action not in ("status", "cleanup", "command_status"))
        if action not in ("status", "cleanup", "command_status") and not self.ready(journal):
            raise Refused("build is awaiting its publication fence")
        if action == "credentials":
            return self.credentials(journal)
        if action == "cleanup":
            return self.cleanup(journal)
        slot = event.get("slot")
        if action == "status" and slot is None:
            return {"build_id": journal["build_id"], "deadline_unix": journal["deadline_unix"], "source_commit": journal["source_sha"], "input_sha256": journal["input_sha256"], "ready": self.ready(journal), "slots": {s: self.slot_status(journal, s) for s in journal["slots"]}, "candidates": journal["candidates"]}
        if slot not in TRACK_SLOTS[self.track_of(journal["build_id"])]:
            raise Refused("unknown fixed slot")
        if action == "command_status":
            return self.command_status(journal, slot, event.get("command_id"))
        if action == "launch":
            return self.launch(suffix, journal, slot)
        if action == "seal":
            return self.seal(suffix, journal, slot)
        if action == "status":
            return self.slot_status(journal, slot)
        if self.now() >= journal["deadline_unix"]:
            raise Refused("factory deadline exhausted")
        instance = self.owned(journal, slot)
        if not instance:
            raise Refused("unknown slot instance")
        methods = {"reboot": "reboot_instances", "stop": "stop_instances", "start": "start_instances"}
        getattr(self.client("ec2", True), methods[action])(InstanceIds=[instance["InstanceId"]])
        return self.slot_status(journal, slot)


def lambda_handler(event, context):
    factory = Factory()
    alias = context.invoked_function_arn.rsplit(":", 1)[-1]
    try:
        if alias == "build":
            return factory.broker(event)
        if alias == "request":
            return factory.request(event)
        if event.get("source") == "aws.events":
            return factory.tick()
        raise Refused("a fixed invocation alias is required")
    except Refused as error:
        return {"status": "refused", "message": str(error)}
    except Exception as error:
        # Request/SDK messages can include environment or credential material.
        print(json.dumps({"error_type": type(error).__name__}))
        raise RuntimeError("factory operation failed; inspect the bounded factory journal") from None

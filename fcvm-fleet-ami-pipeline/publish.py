"""Promote one independently validated image pair under Terraform's exact lock.

Factory publication requires a successful fixed-project CodeBuild event.
Reviewed, configuration-pinned transitions use the same image transaction.
Every new template version differs solely in ImageId.
"""
import base64
import copy
from contextlib import contextmanager
import datetime
import hashlib
import http.client
import json
import os
import re
import time
import uuid


SHA = re.compile(r"[0-9a-f]{40}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
IMAGE = re.compile(r"ami-[0-9a-f]{17}\Z")
COPY_REGION = re.compile(r"us-(east|west)-[12]\Z")
# What release_copies() and replicate() write as a copy's description.
COPY_DESCRIPTION = re.compile(r"copy of (ami-[0-9a-f]{17}) from (us-(?:east|west)-[12])\Z")
COPY_RETRY_SUFFIX = re.compile(r"-copy-[23]\Z")
COPY_ATTEMPTS = 3
FACTORY_NAME = re.compile(r"fcvm-factory-[0-9a-f]{24}-(host|tile)\Z")
HANDMADE_NAME = re.compile(r"fcvm-fleet-(host|tile)-x86_64-[A-Za-z0-9._-]{1,100}\Z")
HANDMADE_DAYS = 14
# Longer than a publication's 24-hour lease (dispatch.py PUBLICATION_SECONDS).
RETAIN_MIN_DAYS = 2
RETAIN_LIMIT = 16
COPY_SNAPSHOT_TAG = "FleetPublisherCopy"
# A published image's name: the factory's (dispatch.py seal) or a hand-built one's (build-ami.sh).
NAME = {kind: re.compile(r"(fcvm-factory-[0-9a-f]{24}-" + kind + r"|fcvm-fleet-" + kind + r"-x86_64-[0-9a-f]{8}-[0-9]{12})\Z")
        for kind in ("host", "tile")}
SLOTS = {"host-build", "tile-build", "host-verify", "tile-verify"}
ARCHIVES = {f"part-{index:02d}.tar.zst" for index in range(16)} | {"dirs.tar.zst"}
STORE_FILES = ARCHIVES | {"parts", "sha256.json"}
EXECUTION_TIMEOUT_MINUTES = (240, 360)


class Refused(Exception):
    pass


def need(value, message):
    if not value:
        raise Refused(message)


def execution_lease_seconds(build):
    minutes = build.get("timeoutInMinutes")
    need(type(minutes) is int and minutes in EXECUTION_TIMEOUT_MINUTES,
         "CodeBuild execution lease is missing or unsupported")
    return minutes * 60


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def without_image(value):
    return {key: item for key, item in value.items() if key != "ImageId"}


def copy_snapshot_tags():
    return [{"ResourceType": "snapshot", "Tags": [{"Key": COPY_SNAPSHOT_TAG, "Value": "true"}]}]


def canonical_image_name(name):
    return COPY_RETRY_SUFFIX.sub("", name)


def copy_sdk_status():
    """Check the managed runtime's SDK model without constructing any AWS client."""
    from botocore.session import get_session
    ec2 = get_session().get_service_model("ec2")
    tags = ec2.operation_model("CopyImage").input_shape.members.get("TagSpecifications")
    image = ec2.operation_model("DescribeImages").output_shape.members["Images"].member
    missing = ["DescribeImages." + field for field in ("SourceImageId", "SourceImageRegion") if field not in image.members]
    if not tags or "snapshot" not in tags.member.members["ResourceType"].enum:
        missing.append("CopyImage.TagSpecifications[snapshot]")
    return {"status": "incompatible", "missing": missing} if missing else {"status": "ready"}


# The publisher's bulky fixed configuration ships in its code bundle (Terraform writes it),
# not in the Lambda environment, which is limited to 4 KB.
BUNDLED_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "publisher-config.json")
BUNDLED_KEYS = {"TEMPLATES_JSON", "SEED_RELEASE_JSON", "PHONE_AMI_JSON", "RENDERER_AMI_JSON",
                "POTATO_AMI_JSON", "HOST_FIX_AMI_JSON"}


def bundled_config(path):
    if not os.path.exists(path):
        return {}
    with open(path) as handle:
        value = json.load(handle)
    need(isinstance(value, dict) and set(value) <= BUNDLED_KEYS
         and all(isinstance(item, str) for item in value.values()), "bundled configuration is outside its fixed scope")
    return value


class Publisher:
    def __init__(self, env=None, clients=None, clock=time.time, sleep=time.sleep, config=None):
        given = dict(os.environ if env is None else env)
        bundled = bundled_config(BUNDLED_CONFIG if config is None else config)
        need(not set(bundled) & set(given), "a value is both bundled and in the environment")
        self.env = {**bundled, **given}
        self.clients = clients or {}
        self.clock = clock
        self.sleep = sleep
        self.templates = json.loads(self.env["TEMPLATES_JSON"])
        need(len(self.templates) == 4 and len({t["id"] for t in self.templates}) == 4,
             "exactly four distinct configured templates are required")
        need(all(set(t) == {"region", "id", "kind", "arch"}
                 and t["region"] == self.env["COMPUTE_REGION"] and t["arch"] == "x86_64"
                 and re.fullmatch(r"lt-[0-9a-f]{17}", t["id"])
                 and t["kind"] in {"host", "tile"} for t in self.templates)
             and sum(t["kind"] == "host" for t in self.templates) == 1,
             "template configuration is outside the fixed image-pair scope")
        # Regions that receive a copy of every published pair, for work kept off the
        # compute region's quotas (agents' GPU boxes). Never the compute region itself.
        # Comma-separated, not JSON: the environment is close to Lambda's 4 KB limit.
        self.copy_regions = self.env["COPY_REGIONS"].split(",") if self.env["COPY_REGIONS"] else []
        need(len(set(self.copy_regions)) == len(self.copy_regions)
             and all(COPY_REGION.fullmatch(region) for region in self.copy_regions)
             and self.env["COMPUTE_REGION"] not in self.copy_regions, "image copy configuration is outside its fixed scope")
        self.copies_parameter = self.env["RELEASE_PARAMETER"] + "-copies"
        # Where the factory builds and verifies candidates. The release (the launch templates) is in
        # COMPUTE_REGION; a candidate built elsewhere is copied there before it is promoted.
        self.build_region = self.env["BUILD_REGION"]
        need(COPY_REGION.fullmatch(self.build_region), "image build region is outside its fixed scope")
        self.build_id = None
        self.lock_info = None
        self.transaction = None
        self.committed = False
        self.recovery_failed = False

    def request(self, source, input_sha):
        name = self.env["STATE_PREFIX"] + "/requests/" + input_sha
        value = self.load(name)
        need(value.get("request_id") == input_sha and value.get("input_sha256") == input_sha
             and value.get("source_sha") == source and value.get("build_id") == self.build_id
             and value.get("priority") in {"normal", "critical"}, "accepted request provenance changed")
        return name, value

    def current_request(self, source, input_sha):
        need(self.load(self.env["STATE_PREFIX"] + "/control").get("active") == self.build_id,
             "a superseded build cannot publish")
        _, request = self.request(source, input_sha)
        need(request.get("status") not in {"superseded", "failed"}, "a superseded build cannot publish")

    def reviewed_pair(self, config, *, host_only=False):
        need(set(config) == {"source_sha", "images", "expected"}
             and SHA.fullmatch(config["source_sha"]), "invalid reviewed image configuration")
        expected = config["expected"]
        need(set(expected) == {"version", "generation", "source_sha", "build_id", "guard_sha256",
                              "host_amis", "tile_amis", "versions", "previous"}
             and expected["version"] == 1 and type(expected["generation"]) is int
             and expected["guard_sha256"] == self.env["GUARD_SHA256"]
             and set(config["images"]) == {"host", "tile"}, "invalid reviewed release predecessor")
        images = []
        for kind in ("host", "tile"):
            maps = expected[kind + "_amis"]
            need(set(maps) == {self.env["COMPUTE_REGION"]}
                 and set(maps[self.env["COMPUTE_REGION"]]) == {"x86_64"}, "unexpected reviewed release topology")
            images.extend([config["images"][kind], maps[self.env["COMPUTE_REGION"]]["x86_64"]])
        need(len(set(images)) == (3 if host_only else 4) and (not host_only or images[2] == images[3])
             and all(IMAGE.fullmatch(item) for item in images), "invalid reviewed image identities")
        return config

    def reviewed_seed(self):
        config = self.reviewed_pair(json.loads(self.env["SEED_RELEASE_JSON"]))
        expected = config["expected"]
        need(expected["generation"] == 0 and expected["source_sha"] == expected["build_id"] == ""
             and expected["versions"] == {} and expected["previous"] is None,
             "reviewed transition is not an initial seed")
        return config

    def reviewed_phone_release(self):
        config = self.reviewed_pair(json.loads(self.env["PHONE_AMI_JSON"]))
        seed = self.reviewed_seed()
        expected = config["expected"]
        need(expected["generation"] == 1 and expected["source_sha"] == seed["source_sha"] != config["source_sha"]
             and expected["build_id"] == "reviewed-seed:" + digest(encoded(seed))
             and expected["previous"] == {k: v for k, v in seed["expected"].items() if k != "previous"}
             and all(expected[kind + "_amis"] == {self.env["COMPUTE_REGION"]: {"x86_64": image}}
                     for kind, image in seed["images"].items())
             and set(expected["versions"]) == {t["region"] + "/" + t["id"] for t in self.templates}
             and all(type(v) is int and v > 0 for v in expected["versions"].values()),
             "reviewed phone transition must follow the exact seed publication")
        return config

    def reviewed_renderer_release(self):
        config = json.loads(self.env["RENDERER_AMI_JSON"])
        need(set(config) == {"source_sha", "images", "versions"}, "invalid reviewed renderer configuration")
        phone = self.reviewed_phone_release()
        versions = config.pop("versions")
        need(phone["source_sha"] != config["source_sha"]
             and set(versions) == {t["region"] + "/" + t["id"] for t in self.templates}
             and all(type(v) is int and v > 0 for v in versions.values()),
             "reviewed renderer transition must follow the exact phone publication")
        config["expected"] = {
            "version": 1, "generation": 2, "source_sha": phone["source_sha"],
            "build_id": "reviewed-phone-release:" + digest(encoded(phone)),
            "guard_sha256": self.env["GUARD_SHA256"], "versions": versions,
            "previous": {k: v for k, v in phone["expected"].items() if k != "previous"},
            **{kind + "_amis": {self.env["COMPUTE_REGION"]: {"x86_64": image}}
               for kind, image in phone["images"].items()},
        }
        return self.reviewed_pair(config)

    def reviewed_potato_release(self):
        config = json.loads(self.env["POTATO_AMI_JSON"])
        need(set(config) == {"source_sha", "tile_source_sha", "images", "versions"},
             "invalid reviewed potato configuration")
        renderer = self.reviewed_renderer_release()
        versions, tile_source = config.pop("versions"), config.pop("tile_source_sha")
        need(renderer["source_sha"] != config["source_sha"]
             and SHA.fullmatch(tile_source) and tile_source != config["source_sha"]
             and isinstance(versions, dict)
             and set(versions) == {t["region"] + "/" + t["id"] for t in self.templates}
             and all(type(v) is int and v > 0 for v in versions.values()),
             "reviewed potato transition must follow the exact renderer publication")
        config["expected"] = {
            "version": 1, "generation": 3, "source_sha": renderer["source_sha"],
            "build_id": "reviewed-renderer-release:" + digest(encoded(renderer)),
            "guard_sha256": self.env["GUARD_SHA256"], "versions": versions,
            "previous": {k: v for k, v in renderer["expected"].items() if k != "previous"},
            **{kind + "_amis": {self.env["COMPUTE_REGION"]: {"x86_64": image}}
               for kind, image in renderer["images"].items()},
        }
        return self.reviewed_pair(config), tile_source

    def reviewed_potato_host_fix(self):
        fixed = json.loads(self.env["HOST_FIX_AMI_JSON"])
        need(set(fixed) == {"source_sha", "host_image", "versions"}, "invalid reviewed host fix configuration")
        potato, tile_source = self.reviewed_potato_release()
        versions = fixed["versions"]
        need(fixed["source_sha"] not in {potato["source_sha"], tile_source}
             and isinstance(versions, dict) and set(versions) == set(potato["expected"]["versions"])
             and all(type(v) is int and v > 0 for v in versions.values()),
             "reviewed host fix must follow the exact potato publication")
        return self.reviewed_pair({
            "source_sha": fixed["source_sha"],
            "images": {"host": fixed["host_image"], "tile": potato["images"]["tile"]},
            "expected": {
                "version": 1, "generation": 4, "source_sha": potato["source_sha"],
                "build_id": "reviewed-potato-release:" + digest(encoded({**potato, "tile_source_sha": tile_source})),
                "guard_sha256": self.env["GUARD_SHA256"], "versions": versions,
                "previous": {k: v for k, v in potato["expected"].items() if k != "previous"},
                **{kind + "_amis": {self.env["COMPUTE_REGION"]: {"x86_64": image}}
                   for kind, image in potato["images"].items()},
            },
        }, host_only=True)

    def reviewed_stride_release(self):
        # The reviewed #805 pair and observed generation-five host version are
        # fixed here so this transition needs no more Lambda environment space.
        prior = self.reviewed_potato_host_fix()
        host = next(t for t in self.templates if t["kind"] == "host")
        host_key = host["region"] + "/" + host["id"]
        return self.reviewed_pair({
            "source_sha": "92a26793a818b142e71ba7d96b014d033f014562",
            "images": {"host": "ami-09ac99f69700ec306", "tile": "ami-094ca9c62a284a389"},
            "expected": {
                "version": 1, "generation": 5, "source_sha": prior["source_sha"],
                "build_id": "reviewed-potato-host-fix:" + digest(encoded(prior)),
                "guard_sha256": self.env["GUARD_SHA256"],
                "versions": {**prior["expected"]["versions"], host_key: 21},
                "previous": {k: v for k, v in prior["expected"].items() if k != "previous"},
                **{kind + "_amis": {self.env["COMPUTE_REGION"]: {"x86_64": image}}
                   for kind, image in prior["images"].items()},
            },
        })

    def reviewed_retake_host_release(self):
        stride = self.reviewed_stride_release()
        host = next(t for t in self.templates if t["kind"] == "host")
        host_key = host["region"] + "/" + host["id"]
        # The generation-six receipt selected the next version on each tile
        # and version 23 on the host, after Terraform prepared version 22.
        versions = {key: number + 1 for key, number in stride["expected"]["versions"].items()}
        versions[host_key] = 23
        return self.reviewed_pair({
            "source_sha": "343b2287636d07b1726ab7a2d431c9a1ec854a67",
            "images": {"host": "ami-05923927f810a2ce9", "tile": stride["images"]["tile"]},
            "expected": {
                "version": 1, "generation": 6, "source_sha": stride["source_sha"],
                "build_id": "reviewed-stride-release:" + digest(encoded(stride)),
                "guard_sha256": self.env["GUARD_SHA256"], "versions": versions,
                "previous": {k: v for k, v in stride["expected"].items() if k != "previous"},
                **{kind + "_amis": {self.env["COMPUTE_REGION"]: {"x86_64": image}}
                   for kind, image in stride["images"].items()},
            },
        }, host_only=True)

    def reviewed_held_stamp_host_release(self):
        retake = self.reviewed_retake_host_release()
        host = next(t for t in self.templates if t["kind"] == "host")
        host_key = host["region"] + "/" + host["id"]
        # The generation-seven receipt selected version 25 on the host, after
        # Terraform prepared version 24, and kept every tile's version.
        return self.reviewed_pair({
            "source_sha": "fb2f5c361175bb8a0b5baac141e359906543e101",
            "images": {"host": "ami-0d176763f9f33416e", "tile": retake["images"]["tile"]},
            "expected": {
                "version": 1, "generation": 7, "source_sha": retake["source_sha"],
                "build_id": "reviewed-retake-host-release:" + digest(encoded(retake)),
                "guard_sha256": self.env["GUARD_SHA256"],
                "versions": {**retake["expected"]["versions"], host_key: 25},
                "previous": {k: v for k, v in retake["expected"].items() if k != "previous"},
                **{kind + "_amis": {self.env["COMPUTE_REGION"]: {"x86_64": image}}
                   for kind, image in retake["images"].items()},
            },
        }, host_only=True)

    # The publisher's us-east-1 copy of the fb2f5c36 host, which the generation-eight receipt names.
    HELD_STAMP_HOST_COPY = "ami-02217558b180d38fb"

    def reviewed_windows_host_release(self):
        held = self.reviewed_held_stamp_host_release()
        host = next(t for t in self.templates if t["kind"] == "host")
        host_key = host["region"] + "/" + host["id"]
        # The generation-eight receipt selected version 27 on the host, after Terraform prepared
        # version 26, kept every tile's version, and names the publisher's us-east-1 copy of the
        # fb2f5c36 host (the reviewed identity is the us-west-2 original).
        return self.reviewed_pair({
            "source_sha": "853eb034a4611e4e4d3b7e79caaf03cf5b2ed643",
            "images": {"host": "ami-0925206c2b9eac0fe", "tile": held["images"]["tile"]},
            "expected": {
                "version": 1, "generation": 8, "source_sha": held["source_sha"],
                "build_id": "reviewed-held-stamp-host-release:" + digest(encoded(held)),
                "guard_sha256": self.env["GUARD_SHA256"],
                "versions": {**held["expected"]["versions"], host_key: 27},
                "previous": {k: v for k, v in held["expected"].items() if k != "previous"},
                "host_amis": {self.env["COMPUTE_REGION"]: {"x86_64": self.HELD_STAMP_HOST_COPY}},
                "tile_amis": {self.env["COMPUTE_REGION"]: {"x86_64": held["images"]["tile"]}},
            },
        }, host_only=True)

    def github_source_json(self, path, proof):
        connection = http.client.HTTPSConnection("api.github.com", timeout=10)
        try:
            connection.request("GET", "/repos/ejc3/fcvm/" + path,
                               headers={"User-Agent": "fcvm-image-publisher", "Accept": "application/vnd.github+json"})
            response = connection.getresponse()
            need(response.status == 200, proof + " is unavailable")
            raw = response.read(2 * 1024 * 1024 + 1)
            need(len(raw) <= 2 * 1024 * 1024, proof + " exceeds its bound")
            return json.loads(raw)
        finally:
            connection.close()

    def compare_sources(self, base, head):
        # GitHub includes file patches only on page one; ancestry metadata remains on page two.
        return self.github_source_json("compare/" + base + "..." + head + "?per_page=1&page=2", "source ancestry")

    def strict_source_edge(self, floor, head):
        comparison = self.compare_sources(floor, head)
        need(comparison.get("status") == "ahead" and comparison.get("behind_by") == 0
             and comparison.get("base_commit", {}).get("sha") == floor
             and comparison.get("merge_base_commit", {}).get("sha") == floor,
             "factory source precedes the reviewed or active release")

    def reviewed_host_source_fence(self, predecessor, source):
        commit = self.github_source_json("commits/" + source, "reviewed host source signature")
        signature = commit.get("commit", {}).get("verification", {})
        need(commit.get("sha") == source and signature.get("verified") is True and signature.get("reason") == "valid",
             "reviewed host source signature is invalid")
        self.strict_source_edge(predecessor, source)

    def source_fence(self, source, tile_source=None):
        need(SHA.fullmatch(source), "invalid factory source")
        active = self.load(self.env["RELEASE_PARAMETER"], True)
        floors = {self.reviewed_seed()["source_sha"]}
        if active.get("source_sha"):
            need(SHA.fullmatch(active["source_sha"]), "active release source is invalid")
            floors.add(active["source_sha"])
        target = source
        if tile_source is not None:
            need(SHA.fullmatch(tile_source) and tile_source != source, "invalid reviewed tile source")
            # Prove both floors precede the reviewed tile, then the tile precedes
            # the host. GitHub can refuse the longer floor-to-host diff; the two
            # strict links also prevent publishing a tile older than either floor.
            target = tile_source
        edges = [(floor, target) for floor in sorted(floors - {target})]
        if tile_source is not None:
            edges.append((tile_source, source))
        for floor, head in edges:
            self.strict_source_edge(floor, head)

    def factory_fence(self, source, input_sha):
        self.current_request(source, input_sha)
        self.source_fence(source)

    def client(self, service, fleet=False, region=None):
        region = region or (self.env["PIPELINE_REGION"] if service == "s3" and not fleet
                            else self.env["COMPUTE_REGION" if fleet else "CONTROL_REGION"])
        key = (service, fleet, region)
        if key not in self.clients:
            import boto3
            from botocore.config import Config
            options = {"region_name": region,
                       "config": Config(retries={"total_max_attempts": 1}, connect_timeout=10, read_timeout=60)}
            if fleet:
                credentials = self.client("sts").assume_role(RoleArn=self.env["FLEET_ROLE"],
                    RoleSessionName="fleet-image-publisher", DurationSeconds=3600)["Credentials"]
                options.update(aws_access_key_id=credentials["AccessKeyId"],
                    aws_secret_access_key=credentials["SecretAccessKey"], aws_session_token=credentials["SessionToken"])
            self.clients[key] = boto3.client(service, **options)
        return self.clients[key]

    def load(self, name, fleet=False):
        response = self.client("ssm", fleet).get_parameter(Name=name, WithDecryption=False)
        raw = response["Parameter"]["Value"]
        need(len(raw.encode()) <= 4000, "publication metadata exceeds its fixed bound")
        return json.loads(raw)

    def save(self, name, value, fleet=False):
        raw = encoded(value)
        need(len(raw.encode()) <= 4000, "publication metadata exceeds its fixed bound")
        self.client("ssm", fleet).put_parameter(Name=name, Type="String", Value=raw, Overwrite=True)

    def prior_transaction(self):
        try:
            value = self.load(self.env["STATE_PREFIX"] + "/publications/" + digest(self.build_id))
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "ParameterNotFound":
                return None
            raise
        need(value.get("build_id") == self.build_id, "publication receipt identity changed")
        return value

    def acquire(self):
        # This is the same LockID used by Terraform's S3 backend, not a parallel lock.
        self.lock_info = encoded({"ID": str(uuid.uuid4()), "Operation": "OperationTypeApply",
            "Who": "fcvm-image-publisher", "Version": "1.10.3",
            "Created": datetime.datetime.fromtimestamp(self.clock(), datetime.timezone.utc).isoformat(),
            "Path": self.env["LOCK_ID"], "Info": self.build_id})
        self.client("dynamodb").put_item(TableName=self.env["LOCK_TABLE"],
            Item={"LockID": {"S": self.env["LOCK_ID"]}, "Info": {"S": self.lock_info}},
            ConditionExpression="attribute_not_exists(LockID)")

    def release(self):
        if self.lock_info is not None:
            self.client("dynamodb").delete_item(TableName=self.env["LOCK_TABLE"],
                Key={"LockID": {"S": self.env["LOCK_ID"]}}, ConditionExpression="#info = :ours",
                ExpressionAttributeNames={"#info": "Info"}, ExpressionAttributeValues={":ours": {"S": self.lock_info}})
            self.lock_info = None

    @contextmanager
    def publication_lock(self):
        self.acquire()
        try:
            yield
        except Exception:
            if self.transaction and not self.committed:
                try:
                    self.rollback()
                except Exception:
                    self.recovery_failed = True
                    raise Refused("image publication recovery needs administrator reconciliation") from None
            raise
        finally:
            if not self.recovery_failed:
                self.release()

    def reviewed_fence(self, config):
        need(self.load(self.env["RELEASE_PARAMETER"], True) == config["expected"],
             "reviewed transition requires its unchanged predecessor")
        for kind, image_id in config["images"].items():
            rows = self.client("ec2", True).describe_images(ImageIds=[image_id])["Images"]
            need(len(rows) == 1, "reviewed image is missing")
            image = rows[0]
            tags = {item["Key"]: item["Value"] for item in image.get("Tags", [])}
            need(image.get("ImageId") == image_id and image.get("OwnerId") == self.env["FLEET_ACCOUNT"]
                 and image.get("Public") is False and image.get("State") == "available"
                 and image.get("Architecture") == "x86_64" and image.get("RootDeviceType") == "ebs"
                 and all(tags.get(key) == value for key, value in {
                     "Purpose": "fcvm-fleet", "FleetImageKind": kind, "FleetImageValidated": "true",
                     "GuardHelperSHA256": self.env["GUARD_SHA256"]}.items()),
                 "reviewed image is outside its private validated scope")

    def run_seed(self, event):
        need(event == {"action": "activate-reviewed-seed"}, "reviewed seed accepts no caller configuration")
        return self.run_reviewed(self.reviewed_seed(), "reviewed-seed")

    def run_phone_release(self, event):
        need(event == {"action": "activate-reviewed-phone-release"}, "reviewed release accepts no caller configuration")
        return self.run_reviewed(self.reviewed_phone_release(), "reviewed-phone-release", check_ancestry=True)

    def run_renderer_release(self, event):
        need(event == {"action": "activate-reviewed-renderer-release"}, "reviewed release accepts no caller configuration")
        return self.run_reviewed(self.reviewed_renderer_release(), "reviewed-renderer-release", check_ancestry=True)

    def run_potato_release(self, event):
        need(event == {"action": "activate-reviewed-potato-release"}, "reviewed release accepts no caller configuration")
        config, tile_source = self.reviewed_potato_release()
        return self.run_reviewed(config, "reviewed-potato-release", check_ancestry=True, tile_source=tile_source)

    def run_potato_host_fix(self, event):
        need(event == {"action": "activate-reviewed-potato-host-fix"}, "reviewed host fix accepts no caller configuration")
        host_user_data = self.env["HOST_FIX_BOOT_SHA"]
        need(DIGEST.fullmatch(host_user_data), "invalid reviewed host bootstrap digest")
        return self.run_reviewed(self.reviewed_potato_host_fix(), "reviewed-potato-host-fix",
                                 host_user_data=host_user_data, host_only=True)

    def run_stride_release(self, event):
        need(event == {"action": "activate-reviewed-stride-release"}, "reviewed stride release accepts no caller configuration")
        host_user_data = self.env["HOST_FIX_BOOT_SHA"]
        need(DIGEST.fullmatch(host_user_data), "invalid reviewed host bootstrap digest")
        return self.run_reviewed(self.reviewed_stride_release(), "reviewed-stride-release", host_user_data=host_user_data)

    def run_retake_host_release(self, event):
        need(event == {"action": "activate-reviewed-retake-host-release"},
             "reviewed host release accepts no caller configuration")
        host_user_data = self.env["HOST_FIX_BOOT_SHA"]
        need(DIGEST.fullmatch(host_user_data), "invalid reviewed host bootstrap digest")
        return self.run_reviewed(self.reviewed_retake_host_release(), "reviewed-retake-host-release",
                                 host_user_data=host_user_data, host_only=True)

    def reviewed_release_copy(self, original, kind):
        """The release-region copy of a reviewed image built by hand in BUILD_REGION.

        The copy is made the way release_copies makes a factory candidate's (same name, a
        CopyImage that carries the original's tags, found again by name on a replay), so the
        release names a us-east-1 copy, replicate records the original as the build region's
        image, and retention sees both. Returns None while the copy is pending."""
        compute = self.env["COMPUTE_REGION"]
        need(self.build_region != compute, "a reviewed build-region image needs a distinct build region")
        rows = self.client("ec2", True, self.build_region).describe_images(ImageIds=[original])["Images"]
        need(len(rows) == 1, "reviewed image is missing")
        image = rows[0]
        tags = {item["Key"]: item["Value"] for item in image.get("Tags", [])}
        validated = {"Purpose": "fcvm-fleet", "FleetImageKind": kind, "FleetImageValidated": "true",
                     "GuardHelperSHA256": self.env["GUARD_SHA256"]}
        need(image.get("ImageId") == original and image.get("OwnerId") == self.env["FLEET_ACCOUNT"]
             and image.get("Public") is False and image.get("State") == "available"
             and image.get("Architecture") == "x86_64" and image.get("RootDeviceType") == "ebs"
             and NAME[kind].fullmatch(image.get("Name", ""))
             # build-ami.sh's CreateImage, not a copy (both report a SourceImageId: a CreateImage
             # image's is the image its builder booted)
             and image.get("Description") == "fcvm fleet: " + image.get("Name", "")
             and all(tags.get(key) == value for key, value in validated.items()),
             "reviewed image is outside its private validated scope")
        return self.regional_copy(self.client("ec2", True), compute, original, self.build_region,
                                  image["Name"], validated, factory=True)

    def run_held_stamp_host_release(self, event):
        need(event == {"action": "activate-reviewed-held-stamp-host-release"},
             "reviewed host release accepts no caller configuration")
        return self.run_build_region_host_release(self.reviewed_held_stamp_host_release(), "reviewed-held-stamp-host-release")

    def run_windows_host_release(self, event):
        need(event == {"action": "activate-reviewed-windows-host-release"},
             "reviewed host release accepts no caller configuration")
        return self.run_build_region_host_release(self.reviewed_windows_host_release(), "reviewed-windows-host-release")

    def run_build_region_host_release(self, config, identity):
        host_user_data = self.env["HOST_FIX_BOOT_SHA"]
        need(DIGEST.fullmatch(host_user_data), "invalid reviewed host bootstrap digest")
        # Built in us-west-2 (#807's build region): the release names the publisher's copy.
        copied = self.reviewed_release_copy(config["images"]["host"], "host")
        need(copied is not None, "release-region copies are still pending: host")
        # Potato mode is chosen by the boot image's id, so the reviewed bootstrap must name the copy.
        host = next(t for t in self.templates if t["kind"] == "host")
        boot = base64.b64decode(self.current(host["id"])["LaunchTemplateData"].get("UserData", "")).decode("utf-8", "replace")
        need(copied in boot, "Terraform's host bootstrap does not name the release-region copy yet")
        return self.run_reviewed(config, identity, host_user_data=host_user_data,
                                 host_only=True, release_images={**config["images"], "host": copied})

    def run_reviewed(self, config, identity, check_ancestry=False, tile_source=None, *, host_user_data=None, host_only=False,
                     release_images=None):
        transaction_config = config if tile_source is None else {**config, "tile_source_sha": tile_source}
        self.build_id = identity + ":" + digest(encoded(transaction_config))
        if release_images is not None:
            # The reviewed identity stays the build-region image; the release names its copy.
            config = self.reviewed_pair({**config, "images": release_images}, host_only=host_only)
        generation = config["expected"]["generation"] + 1

        def fence():
            self.reviewed_fence(config)
            if host_user_data is not None:
                # Bootstrap-bound transitions require the signed source to
                # descend directly from the exact published predecessor.
                self.reviewed_host_source_fence(config["expected"]["source_sha"], config["source_sha"])
            elif check_ancestry:
                self.source_fence(config["source_sha"], tile_source)

        with self.publication_lock():
            active = self.load(self.env["RELEASE_PARAMETER"], True)
            if active.get("build_id") == self.build_id:
                need(active.get("generation") == generation and active.get("source_sha") == config["source_sha"]
                     and active.get("previous") == {k: v for k, v in config["expected"].items() if k != "previous"}
                     and all(active.get(kind + "_amis") == {self.env["COMPUTE_REGION"]: {"x86_64": image}}
                             for kind, image in config["images"].items()), "committed reviewed receipt differs")
                if host_user_data is not None:
                    if host_only:
                        need(all(active["versions"].get(key) == value
                                 for key, value in self.retained_tiles(config["expected"]).items()),
                             "committed reviewed tile versions differ")
                    else:
                        self.retained_tiles(active)
                    self.check_reviewed_host_default(active, host_user_data)
                self.committed = True
                self.retain(active, self.replicate(active))
                return {"status": "promoted", "generation": generation}
            need(active == config["expected"], "reviewed transition requires its unchanged predecessor")
            prior = self.prior_transaction()
            if prior:
                need(prior.get("before") == config["expected"], "reviewed transaction identity changed")
                if prior.get("status") == "rolled_back":
                    return {"status": "failed", "reason": "rolled_back"}
                self.transaction = prior
                if not all(entry.get("candidate") for entry in prior.get("entries", {}).values()):
                    self.recovery_failed = True
                    self.transaction = None
                    raise Refused("uncertain reviewed version requires administrator reconciliation")
                raise Refused("interrupted reviewed publication must roll back before terminal replay")
            fence()
            manifest = {"images": {kind: {"image_id": image} for kind, image in config["images"].items()}}
            result = self.promote(manifest, config["source_sha"], None, [], fence=fence,
                                  host_user_data=host_user_data, host_only=host_only)
            self.retain(result, self.replicate(result))
            return {"status": "promoted", "generation": result["generation"]}

    def event(self, event):
        need(isinstance(event, dict) and event.get("source") == "aws.codebuild"
             and event.get("detail-type") == "CodeBuild Build State Change"
             and event.get("account") == self.env["CONTROL_ACCOUNT"]
             and event.get("region") == self.env["CONTROL_REGION"], "not the configured CodeBuild event")
        detail = event.get("detail", {})
        need(detail.get("build-status") == "SUCCEEDED"
             and detail.get("project-name") == self.env["PROJECT_NAME"], "only successful factory builds may publish")
        build_id = detail.get("build-id", "")
        prefix = f"arn:aws:codebuild:{self.env['CONTROL_REGION']}:{self.env['CONTROL_ACCOUNT']}:build/"
        if build_id.startswith(prefix):
            build_id = build_id[len(prefix):]
        need(re.fullmatch(re.escape(self.env["PROJECT_NAME"]) + r":[0-9a-f-]{36}", build_id), "invalid factory build identity")
        self.build_id = build_id
        builds = self.client("codebuild").batch_get_builds(ids=[build_id])["builds"]
        need(len(builds) == 1, "factory build is missing")
        build = builds[0]
        need(build["id"] == build_id and build["projectName"] == self.env["PROJECT_NAME"]
             and build["buildStatus"] == "SUCCEEDED" and build["buildComplete"] is True,
             "actual CodeBuild result is not successful")
        variables = {item["name"]: item["value"] for item in build["environment"]["environmentVariables"]}
        source, input_sha = variables.get("SOURCE_SHA", ""), variables.get("INPUT_SHA256", "")
        need(SHA.fullmatch(source) and DIGEST.fullmatch(input_sha)
             and build.get("resolvedSourceVersion") == source, "factory source provenance changed")
        need(0 < (build["endTime"] - build["startTime"]).total_seconds() <= execution_lease_seconds(build),
             "build exceeded its fixed execution lease")
        return build, source, input_sha

    def proof(self, build, source, input_sha):
        prefix = self.env["STATE_PREFIX"]
        control = self.load(prefix + "/control")
        need(control.get("active") == self.build_id, "a superseded build cannot publish")
        journal = self.load(prefix + "/builds/" + digest(self.build_id))
        need(journal.get("build_id") == self.build_id and journal.get("source_sha") == source
             and journal.get("input_sha256") == input_sha and journal.get("request_id") == input_sha
             and set(journal.get("slots", {})) == SLOTS
             and set(journal.get("candidates", {})) == {"host", "tile"}
             and journal["deadline_unix"] == int(build["startTime"].timestamp()) + execution_lease_seconds(build)
             and build["endTime"].timestamp() <= journal["deadline_unix"], "broker proof does not match this completed build")
        ids = [journal["slots"][slot].get("instance_id", "") for slot in sorted(SLOTS)]
        need(len(set(ids)) == 4 and all(re.fullmatch(r"i-[0-9a-f]{17}", item) for item in ids),
             "broker has no four distinct instance receipts")
        slots = {journal["slots"][slot]["instance_id"]: slot for slot in SLOTS}
        need(all(entry.get("attempted") is True and entry.get("terminated_seen") is True
                 and not entry.get("no_instance") and not entry.get("rejected")
                 for entry in journal["slots"].values()), "broker has no durable termination proof for every slot")
        # Filters safely omit expired IDs; API errors still fail the publication.
        reservations = self.client("ec2", True, self.build_region).describe_instances(Filters=[{"Name": "instance-id", "Values": ids}])["Reservations"]
        seen = set()
        for reservation in reservations:
            for item in reservation["Instances"]:
                instance_id = item["InstanceId"]
                need(instance_id in slots and instance_id not in seen, "unexpected factory instance metadata")
                seen.add(instance_id)
                tags = {tag["Key"]: tag["Value"] for tag in item.get("Tags", [])}
                slot = slots[instance_id]
                need(reservation.get("OwnerId") == self.env["FLEET_ACCOUNT"]
                     and item["State"]["Name"] == "terminated"
                     and item.get("InstanceType") == {"host": "g4dn.metal", "tile": "g4dn.xlarge"}[slot.split("-", 1)[0]]
                     and tags.get("Purpose") == "fcvm-fleet-ami-builder"
                     and tags.get("FactoryBuild") == digest(self.build_id) and tags.get("FactorySlot") == slot
                     and ("IamInstanceProfile" not in item or item["IamInstanceProfile"].get("Arn") == self.env["BUILDER_PROFILE"]),
                     "instance metadata contradicts its recorded termination")
        key = "runs/" + digest(self.build_id) + "/manifest.json"
        response = self.client("s3").get_object(Bucket=self.env["PIPELINE_BUCKET"], Key=key)
        raw = response["Body"].read(32769)
        need(len(raw) <= 32768, "factory manifest exceeds its fixed bound")
        manifest = json.loads(raw)
        tls = manifest.get("tls", {})
        need(manifest.get("version") == 1 and manifest.get("build_id") == self.build_id
             and manifest.get("source_commit") == source and manifest.get("input_sha256") == input_sha
             and DIGEST.fullmatch(manifest.get("source_tree_sha256", ""))
             and manifest.get("guard_sha256") == self.env["GUARD_SHA256"]
             and manifest.get("deadline_unix") == journal["deadline_unix"]
             and set(manifest.get("images", {})) == {"host", "tile"}
             and set(tls) == {"build_cert_sha256", "test_cert_sha256"}
             and all(DIGEST.fullmatch(value) for value in tls.values())
             and tls["build_cert_sha256"] != tls["test_cert_sha256"], "factory manifest provenance does not match")
        for kind in ("host", "tile"):
            image = manifest["images"][kind]
            validation = image.get("validation", {})
            boots = validation.get("boot_ids", [])
            need(image.get("image_id") == journal["candidates"][kind] and IMAGE.fullmatch(image["image_id"])
                 and DIGEST.fullmatch(image.get("renderer_sha256", ""))
                 and re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("renderer_image_id", ""))
                 and all(validation.get(field) is True for field in ("guard", "frames", "sanitized", "tls_rotation"))
                 and all(validation.get(key) == value for key, value in tls.items())
                 and (kind != "host" or validation.get("phone_clone") is True)
                 and len(boots) == len(set(boots)) == 3
                 and all(re.fullmatch(r"[0-9a-f-]{36}", value) for value in boots), "candidate lacks the full boot/security proof")
        need(manifest["images"]["host"]["image_id"] != manifest["images"]["tile"]["image_id"]
             and manifest["images"]["host"]["renderer_image_id"] == manifest["images"]["tile"]["renderer_image_id"]
             and manifest["images"]["host"]["renderer_sha256"] == manifest["images"]["tile"]["renderer_sha256"],
             "host and tile are not one paired renderer release")
        return manifest

    def images(self, manifest, source, input_sha):
        ec2 = self.client("ec2", True, self.build_region)
        resources = []
        for kind in ("host", "tile"):
            image_id = manifest["images"][kind]["image_id"]
            rows = ec2.describe_images(ImageIds=[image_id])["Images"]
            need(len(rows) == 1, "candidate image is missing")
            image = rows[0]
            expected = {"Purpose": "fcvm-fleet", "FleetImageKind": kind,
                "GuardHelperSHA256": self.env["GUARD_SHA256"], "SourceCommit": source,
                "InputSHA256": input_sha, "PipelineBuildId": self.build_id, "PipelineSanitized": "true"}
            tags = {item["Key"]: item["Value"] for item in image.get("Tags", [])}
            need(image["OwnerId"] == self.env["FLEET_ACCOUNT"] and image.get("Public") is False
                 and image.get("State") == "available" and image.get("Architecture") == "x86_64"
                 and image.get("RootDeviceType") == "ebs" and all(tags.get(key) == value for key, value in expected.items()),
                 "candidate image is outside the private validated factory lineage")
            snapshots = [item["Ebs"]["SnapshotId"] for item in image.get("BlockDeviceMappings", []) if item.get("Ebs", {}).get("SnapshotId")]
            need(0 < len(snapshots) <= 4, "candidate has no bounded EBS snapshot set")
            found = ec2.describe_snapshots(SnapshotIds=snapshots)["Snapshots"]
            need({item["SnapshotId"] for item in found} == set(snapshots), "candidate snapshot is missing")
            for snapshot in found:
                tags = {item["Key"]: item["Value"] for item in snapshot.get("Tags", [])}
                need(snapshot["OwnerId"] == self.env["FLEET_ACCOUNT"] and snapshot.get("State") == "completed"
                     and snapshot.get("Encrypted") is True and all(tags.get(key) == value for key, value in expected.items()),
                     "candidate snapshot provenance does not match")
            resources.extend([image_id, *snapshots])
        return {self.build_region: resources}

    def regional_copy(self, target, region, original, source_region, base, expected, *, reverse=None, factory=False):
        """Retry proven failed copies under deterministic names without deleting their receipts."""
        names = [base] + [f"{base}-copy-{attempt}" for attempt in range(2, COPY_ATTEMPTS + 1)]
        found = target.describe_images(Owners=[self.env["FLEET_ACCOUNT"]],
            Filters=[{"Name": "name", "Values": names}])["Images"]
        by_name = {image["Name"]: image for image in found}
        need(len(by_name) == len(found) and by_name.keys() <= set(names), "ambiguous regional copy names")
        description = "copy of " + original + " from " + source_region

        def status(image, name):
            if image.get("State") == "pending":
                return "pending"
            image_id = image.get("ImageId", "")
            tags = {tag["Key"]: tag["Value"] for tag in image.get("Tags", [])}
            forward = (image.get("SourceImageId"), image.get("SourceImageRegion")) == (original, source_region)
            state = image.get("State")
            need(image.get("Name") == name and IMAGE.fullmatch(image_id)
                 and image.get("OwnerId") == self.env["FLEET_ACCOUNT"] and image.get("Public") is False
                 and all(tags.get(key) == value for key, value in expected.items())
                 and (forward or (reverse == image_id and state == "available"))
                 and (not (factory or state == "failed") or image.get("Description") == description),
                 "a regional copy is outside its private image lineage")
            if state == "available":
                need(image.get("Architecture") == "x86_64" and image.get("RootDeviceType") == "ebs",
                     "a regional copy is not an x86_64 EBS image")
            else:
                need(state == "failed", "a regional copy is not pending, available or failed")
            return state

        # A release copied in from the target region can use its verified original, which
        # keeps its canonical name even when the released image needed a retry suffix.
        if reverse is not None and by_name.get(base, {}).get("ImageId") == reverse:
            return reverse if status(by_name[base], base) == "available" else None
        latest = max((names.index(name) for name in by_name), default=-1)
        if latest >= 0:
            image = by_name[names[latest]]
            state = status(image, names[latest])
            if state == "available":
                return image["ImageId"]
            if state == "pending":
                return None
        next_attempt = latest + 1
        need(next_attempt < COPY_ATTEMPTS, "regional copy attempts exhausted; inspect the failed AMIs")
        name = names[next_attempt]
        token = original + "/" + region + ("/" + str(next_attempt + 1) if next_attempt else "")
        copied = target.copy_image(SourceImageId=original, SourceRegion=source_region, Name=name,
            Description=description, Encrypted=True, CopyImageTags=True, TagSpecifications=copy_snapshot_tags(),
            ClientToken=digest(token))["ImageId"]
        need(IMAGE.fullmatch(copied), "image copy returned no image identity")
        try:
            rows = target.describe_images(ImageIds=[copied])["Images"]
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "InvalidAMIID.NotFound":
                raise
            return None
        need(len(rows) <= 1, "copy image returned more than one identity")
        if not rows:
            return None
        need(rows[0].get("ImageId") == copied, "copy image returned a different identity")
        return copied if status(rows[0], name) == "available" else None

    def release_copies(self, manifest, source, input_sha):
        """The candidate pair in the release region: the candidates themselves when the factory
        builds there, else their copies, started here and found again by name on a replay.

        A copy still pending refuses; the dispatcher redrives publication every five minutes
        until its fixed publication lease ends. Returns the release-region ids and the copies'
        image ids, which promote tags validated with the originals."""
        compute = self.env["COMPUTE_REGION"]
        if self.build_region == compute:
            return {kind: manifest["images"][kind]["image_id"] for kind in ("host", "tile")}, []
        source_ec2, target = self.client("ec2", True, self.build_region), self.client("ec2", True, compute)
        expected = {"Purpose": "fcvm-fleet", "GuardHelperSHA256": self.env["GUARD_SHA256"], "SourceCommit": source,
                    "InputSHA256": input_sha, "PipelineBuildId": self.build_id, "PipelineSanitized": "true"}
        ids, pending = {}, []
        for kind in ("host", "tile"):
            original = manifest["images"][kind]["image_id"]
            rows = source_ec2.describe_images(ImageIds=[original])["Images"]
            need(len(rows) == 1 and NAME[kind].fullmatch(rows[0].get("Name", "")), "candidate image has no factory name")
            copied = self.regional_copy(target, compute, original, self.build_region, rows[0]["Name"],
                                        {**expected, "FleetImageKind": kind}, factory=True)
            if copied is None:
                pending.append(kind)
            else:
                ids[kind] = copied
        need(not pending, "release-region copies are still pending: " + ",".join(pending))
        return ids, list(ids.values())

    def store(self, manifest, input_sha):
        """Copy only immutable, proof-bound source versions into runtime assets.

        The backend lock serializes the sole Fleet-prefix writer. CodeBuild and
        builders cannot write this destination; differing existing bytes refuse
        publication rather than overwriting another release.
        """
        proof = manifest.get("store", {})
        source_prefix = "runs/" + digest(self.build_id) + "/store/"
        target_prefix = "factory/" + input_sha + "/store/"
        destination = {"bucket": self.env["ASSET_BUCKET"], "region": self.env["ASSET_REGION"], "prefix": target_prefix}
        objects = proof.get("objects", {})
        need(proof.get("bucket") == self.env["PIPELINE_BUCKET"]
             and proof.get("region") == self.env["PIPELINE_REGION"] and proof.get("prefix") == source_prefix
             and proof.get("destination") == destination and set(objects) == STORE_FILES,
             "runtime store proof is outside the fixed source and destination")
        source = self.client("s3")
        target = self.client("s3", True, self.env["ASSET_REGION"])
        for name, item in objects.items():
            need(set(item) == {"sha256", "version_id", "size"} and DIGEST.fullmatch(item.get("sha256", ""))
                 and isinstance(item.get("version_id"), str) and item["version_id"] not in {"", "null"}
                 and len(item["version_id"]) <= 1024 and type(item.get("size")) is int
                 and 0 < item["size"] < (5 * 1024**3 if name in ARCHIVES else 65536),
                 "runtime store object has no bounded immutable digest receipt")
        for name in ("parts", "sha256.json"):
            item = objects[name]
            response = source.get_object(Bucket=proof["bucket"], Key=source_prefix + name,
                VersionId=item["version_id"], ExpectedBucketOwner=self.env["CONTROL_ACCOUNT"])
            data = response["Body"].read(65537)
            need(len(data) == item["size"] and hashlib.sha256(data).hexdigest() == item["sha256"],
                 "runtime store manifest digest changed")
            if name == "parts":
                need(data == ("\n".join(f"part-{i:02d}" for i in range(16)) + "\n").encode(),
                     "runtime store is not the complete sixteen-part image")
            else:
                need(json.loads(data) == {name: objects[name]["sha256"] for name in ARCHIVES},
                     "runtime archive digests disagree with their manifest")
        existing = set()
        arguments = {"Bucket": destination["bucket"], "Prefix": target_prefix,
                     "ExpectedBucketOwner": self.env["FLEET_ACCOUNT"]}
        for _ in range(5):
            page = target.list_objects_v2(**arguments)
            existing.update(item["Key"] for item in page.get("Contents", []))
            if not page.get("IsTruncated"):
                break
            arguments["ContinuationToken"] = page["NextContinuationToken"]
        else:
            raise Refused("runtime destination exceeds the fixed object bound")
        need(existing <= {target_prefix + name for name in STORE_FILES}, "runtime destination contains unrelated objects")
        for name in sorted(STORE_FILES):
            item = objects[name]
            head = source.head_object(Bucket=proof["bucket"], Key=source_prefix + name,
                VersionId=item["version_id"], ExpectedBucketOwner=self.env["CONTROL_ACCOUNT"], ChecksumMode="ENABLED")
            checksum = base64.b64encode(bytes.fromhex(item["sha256"])).decode()
            need(head.get("VersionId") == item["version_id"] and head["ContentLength"] == item["size"]
                 and head.get("Metadata", {}).get("sha256") == item["sha256"]
                 and head.get("ChecksumSHA256") == checksum, "immutable source object receipt changed")
            key = target_prefix + name
            if key not in existing:
                response = target.copy_object(Bucket=destination["bucket"], Key=key,
                    CopySource={"Bucket": proof["bucket"], "Key": source_prefix + name, "VersionId": item["version_id"]},
                    CopySourceIfMatch=head["ETag"], ExpectedBucketOwner=self.env["FLEET_ACCOUNT"],
                    ExpectedSourceBucketOwner=self.env["CONTROL_ACCOUNT"], MetadataDirective="REPLACE",
                    Metadata={"sha256": item["sha256"], "factory-input-sha256": input_sha},
                    ServerSideEncryption="AES256", ChecksumAlgorithm="SHA256")
                need(response.get("CopySourceVersionId") == item["version_id"]
                     and response.get("CopyObjectResult", {}).get("ChecksumSHA256") == checksum,
                     "runtime copy did not confirm its content checksum")
            copied = target.head_object(Bucket=destination["bucket"], Key=key,
                ExpectedBucketOwner=self.env["FLEET_ACCOUNT"], ChecksumMode="ENABLED")
            need(copied["ContentLength"] == item["size"] and copied.get("Metadata", {}).get("sha256") == item["sha256"]
                 and copied.get("Metadata", {}).get("factory-input-sha256") == input_sha
                 and copied.get("ChecksumSHA256") == checksum,
                 "runtime destination differs; existing assets were not overwritten")

    def current(self, template):
        ec2 = self.client("ec2", True)
        rows = ec2.describe_launch_templates(LaunchTemplateIds=[template])["LaunchTemplates"]
        need(len(rows) == 1 and rows[0]["LaunchTemplateId"] == template, "configured template is missing")
        number = rows[0]["DefaultVersionNumber"]
        need(type(number) is int and number > 0, "template has no explicit default version")
        versions = ec2.describe_launch_template_versions(LaunchTemplateId=template, Versions=[str(number)])["LaunchTemplateVersions"]
        need(len(versions) == 1 and versions[0]["VersionNumber"] == number, "default template version is missing")
        return versions[0]

    def version(self, template, current, image, label, entry=None):
        ec2 = self.client("ec2", True)
        description = "fleet-image-" + digest(self.build_id + "/" + template + "/" + label)
        try:
            response = ec2.create_launch_template_version(LaunchTemplateId=template,
                SourceVersion=str(current["VersionNumber"]), LaunchTemplateData={"ImageId": image},
                VersionDescription=description, ClientToken=digest(description))
            created = response["LaunchTemplateVersion"]
        except Exception:
            # Never replay an uncertain create. Discover only our immutable label.
            found = []
            arguments = {"LaunchTemplateId": template, "MaxResults": 100}
            for _ in range(20):
                page = ec2.describe_launch_template_versions(**arguments)
                found.extend(item for item in page["LaunchTemplateVersions"]
                             if item.get("VersionDescription") == description)
                if not page.get("NextToken"):
                    break
                arguments["NextToken"] = page["NextToken"]
            else:
                self.recovery_failed = True
                raise Refused("uncertain template create exceeds the discovery bound") from None
            if len(found) != 1:
                self.recovery_failed = True
                raise Refused("uncertain template create needs administrator reconciliation") from None
            created = found[0]
        if not (created.get("LaunchTemplateId") == template and created.get("VersionDescription") == description
                and type(created.get("VersionNumber")) is int):
            self.recovery_failed = True
            raise Refused("created version identity cannot be reconciled")
        if entry is not None:
            entry["candidate"] = created["VersionNumber"]
            self.record()
        need(created.get("LaunchTemplateId") == template and created.get("VersionDescription") == description
             and type(created.get("VersionNumber")) is int
             and created["LaunchTemplateData"].get("ImageId") == image
             and without_image(created["LaunchTemplateData"]) == without_image(current["LaunchTemplateData"]),
             "candidate version changed infrastructure configuration")
        return created["VersionNumber"]

    def select(self, template, version):
        ec2 = self.client("ec2", True)
        try:
            ec2.modify_launch_template(LaunchTemplateId=template, DefaultVersion=str(version),
                ClientToken=digest(self.build_id + "/" + template + "/default/" + str(version)))
        except Exception:
            # A lost response is resolved by reading the actual explicit default.
            if self.current(template)["VersionNumber"] != version:
                raise Refused("template default update was not confirmed") from None
        need(self.current(template)["VersionNumber"] == version, "template default update was not confirmed")

    def record(self):
        self.save(self.env["STATE_PREFIX"] + "/publications/" + digest(self.build_id), self.transaction)

    def rollback(self):
        ec2 = self.client("ec2", True)
        for template, entry in reversed(list(self.transaction["entries"].items())):
            current = self.current(template)
            if current["VersionNumber"] == entry.get("candidate"):
                # Preserve the CURRENT configuration, even during failure recovery.
                restored = self.version(template, current, entry["old_image"], "rollback-" + str(current["VersionNumber"]))
                self.select(template, restored)
            else:
                need(current["LaunchTemplateData"].get("ImageId") == entry["old_image"],
                     "another default writer prevents automatic rollback")
            candidate = entry.get("candidate")
            if candidate:
                need(self.current(template)["VersionNumber"] != candidate, "cannot delete a selected candidate version")
                result = ec2.delete_launch_template_versions(LaunchTemplateId=template, Versions=[str(candidate)])
                need(not result.get("UnsuccessfullyDeletedLaunchTemplateVersions"), "candidate version cleanup was not confirmed")
        self.transaction["status"] = "rolled_back"
        self.record()

    def retained_tiles(self, before):
        versions = {}
        for template in self.templates:
            if template["kind"] == "tile":
                key = template["region"] + "/" + template["id"]
                current = self.current(template["id"])
                need(current["VersionNumber"] == before["versions"][key]
                     and current["LaunchTemplateData"].get("ImageId") == before["tile_amis"][template["region"]][template["arch"]],
                     "retained tile template default differs from the reviewed release")
                versions[key] = current["VersionNumber"]
        return versions

    def check_reviewed_host_default(self, release, host_user_data):
        host = next(t for t in self.templates if t["kind"] == "host")
        current = self.current(host["id"])
        data = current["LaunchTemplateData"]
        need(current["VersionNumber"] == release["versions"].get(host["region"] + "/" + host["id"])
             and data.get("ImageId") == release["host_amis"][host["region"]][host["arch"]]
             and digest(data.get("UserData", "")) == host_user_data,
             "reviewed host template default differs from the release")

    def promote(self, manifest, source, input_sha, resources, fence=None, *, host_user_data=None, host_only=False):
        name = self.env["RELEASE_PARAMETER"]
        before = self.load(name, True)
        need(before.get("version") == 1 and before.get("guard_sha256") == self.env["GUARD_SHA256"]
             and type(before.get("generation")) is int and before["generation"] >= 0,
             "active release schema is incompatible")
        if before.get("build_id") == self.build_id:
            return before
        templates = self.templates
        versions = {}
        prepared_host = None
        if host_user_data is not None:
            retained = self.retained_tiles(before)
            host = next(t for t in self.templates if t["kind"] == "host")
            if host_only:
                versions = retained
                templates = [host]
            prepared_host = self.current(host["id"])
            key = host["region"] + "/" + host["id"]
            data = prepared_host["LaunchTemplateData"]
            need(prepared_host["VersionNumber"] > before["versions"][key]
                 and data.get("ImageId") == before["host_amis"][host["region"]][host["arch"]]
                 and digest(data.get("UserData", "")) == host_user_data,
                 "Terraform has not prepared the exact reviewed host bootstrap")
        self.transaction = {"status": "preparing", "build_id": self.build_id, "entries": {}, "before": before}
        # Persist the rollback image identities before changing any default.
        self.record()
        for template in templates:
            current = (prepared_host if template["kind"] == "host" else None) or self.current(template["id"])
            old_image = before[template["kind"] + "_amis"][template["region"]][template["arch"]]
            need(current["LaunchTemplateData"].get("ImageId") == old_image, "release pointer and template default disagree")
            entry = {"old_image": old_image, "old_version": current["VersionNumber"]}
            self.transaction["entries"][template["id"]] = entry
            self.record()
            entry["candidate"] = self.version(template["id"], current, manifest["images"][template["kind"]]["image_id"], "candidate", entry)
            versions[template["region"] + "/" + template["id"]] = entry["candidate"]
            self.record()
        for region, names in sorted((resources or {}).items()):
            if names:
                self.client("ec2", True, region).create_tags(Resources=names, Tags=[{"Key": "FleetImageValidated", "Value": "true"}])
        for template, entry in self.transaction["entries"].items():
            need(self.current(template)["VersionNumber"] == entry["old_version"], "template default changed during publication")
            self.select(template, entry["candidate"])
        after = copy.deepcopy(before)
        after.update(generation=before["generation"] + 1, source_sha=source, build_id=self.build_id,
                     previous={key: value for key, value in before.items() if key != "previous"})
        after["versions"] = versions
        for kind in ("host", "tile"):
            after[kind + "_amis"][self.env["COMPUTE_REGION"]]["x86_64"] = manifest["images"][kind]["image_id"]
        if fence is None:
            self.factory_fence(source, input_sha)
        else:
            fence()
        if host_user_data is not None:
            self.retained_tiles(before if host_only else after)
            self.check_reviewed_host_default(after, host_user_data)
        try:
            self.save(name, after, True)
        except Exception:
            need(self.load(name, True) == after, "release pointer update was not confirmed")
        need(self.load(name, True) == after, "release pointer update was not confirmed")
        self.committed = True
        self.transaction["status"] = "committed"
        self.record()
        return after

    def retire(self, release):
        """Critical only: reclaim old-image Fleet placements, never dev machines.

        This runs AFTER the pair is committed. Failure must keep the new release
        and retry reclamation, rather than rolling back to vulnerable images.
        """
        templates = {item["id"]: item for item in self.templates}
        ec2 = self.client("ec2", True)

        def eligible(instance, owner):
            tags = {item["Key"]: item["Value"] for item in instance.get("Tags", [])}
            # DescribeInstances exposes these AWS-managed, immutable tags, not
            # a LaunchTemplate object. Never trust a synthetic fallback field.
            template = templates.get(tags.get("aws:ec2launchtemplate:id"))
            if (template is None or owner != self.env["FLEET_ACCOUNT"]
                    or not re.fullmatch(r"[1-9][0-9]*", tags.get("aws:ec2launchtemplate:version", ""))):
                return False
            kind = template["kind"]
            current = release[kind + "_amis"][template["region"]][template["arch"]]
            return (instance.get("InstanceType") == {"host": "g4dn.metal", "tile": "g4dn.xlarge"}[kind]
                and tags.get("Purpose") == "fcvm-fleet-" + kind
                and IMAGE.fullmatch(instance.get("ImageId", "")) and instance["ImageId"] != current
                and instance.get("State", {}).get("Name") in {"pending", "running", "stopping", "stopped", "shutting-down"})

        ids = set()
        arguments = {"Filters": [{"Name": "tag:Purpose", "Values": ["fcvm-fleet-host", "fcvm-fleet-tile"]},
            {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]}]}
        for _ in range(20):
            page = ec2.describe_instances(**arguments)
            for reservation in page["Reservations"]:
                ids.update(item["InstanceId"] for item in reservation["Instances"] if eligible(item, reservation.get("OwnerId")))
            if not page.get("NextToken"):
                break
            arguments["NextToken"] = page["NextToken"]
        else:
            raise Refused("critical Fleet inventory exceeds its fixed bound")
        need(len(ids) <= 64, "critical Fleet inventory exceeds its fixed bound")
        if not ids:
            return
        # Recheck exact identities immediately before the only runtime mutation.
        rows = ec2.describe_instances(InstanceIds=sorted(ids))["Reservations"]
        actual = {item["InstanceId"] for reservation in rows for item in reservation["Instances"]
                  if eligible(item, reservation.get("OwnerId"))}
        need(actual == ids, "critical Fleet placement changed before reclamation")
        instances = {item["InstanceId"]: item for reservation in rows for item in reservation["Instances"]}
        spot = {item["SpotInstanceRequestId"]: item for item in instances.values() if item.get("SpotInstanceRequestId")}
        if spot:
            requests = ec2.describe_spot_instance_requests(SpotInstanceRequestIds=sorted(spot))["SpotInstanceRequests"]
            need({item["SpotInstanceRequestId"] for item in requests} == set(spot), "Fleet Spot request receipt is incomplete")
            persistent = []
            for request in requests:
                instance = spot[request["SpotInstanceRequestId"]]
                need(request.get("InstanceId") == instance["InstanceId"] and request.get("Type") in {"one-time", "persistent"},
                     "Fleet Spot request is bound to a different placement")
                if request["Type"] == "persistent":
                    tags = {item["Key"]: item["Value"] for item in request.get("Tags", [])}
                    spec = request.get("LaunchSpecification", {})
                    need(tags.get("Purpose") == "fcvm-fleet-tile" and instance["InstanceType"] == "g4dn.xlarge"
                         and spec.get("InstanceType") == instance["InstanceType"] and spec.get("ImageId") == instance["ImageId"],
                         "persistent Spot request is outside the owned tile scope")
                    need(request.get("State") in {"open", "active", "disabled", "cancelled", "closed"}, "persistent Spot request state is unknown")
                    persistent.append(request["SpotInstanceRequestId"])
            if persistent:
                # A persistent request otherwise reopens its OLD launch
                # specification when the instance is terminated.
                pending = [item["SpotInstanceRequestId"] for item in requests
                           if item["SpotInstanceRequestId"] in persistent and item["State"] not in {"cancelled", "closed"}]
                if pending:
                    ec2.cancel_spot_instance_requests(SpotInstanceRequestIds=sorted(pending))
                verified = ec2.describe_spot_instance_requests(SpotInstanceRequestIds=sorted(persistent))["SpotInstanceRequests"]
                need({item["SpotInstanceRequestId"] for item in verified} == set(persistent)
                     and all(item["State"] in {"cancelled", "closed"} for item in verified), "persistent Spot cancellation is not confirmed")
        # Cancelling a disabled request can automatically terminate its stopped
        # instance. Never mistake that expected transition for lost ownership.
        rows = ec2.describe_instances(InstanceIds=sorted(ids))["Reservations"]
        remaining = []
        for reservation in rows:
            for instance in reservation["Instances"]:
                need(instance["InstanceId"] in ids, "unexpected placement appeared during reclamation")
                if instance["State"]["Name"] not in {"terminated", "shutting-down"}:
                    need(eligible(instance, reservation.get("OwnerId")), "Fleet placement changed during cancellation")
                    remaining.append(instance["InstanceId"])
        if remaining:
            ec2.terminate_instances(InstanceIds=sorted(remaining))
        for _ in range(12):
            rows = ec2.describe_instances(InstanceIds=sorted(ids))["Reservations"]
            found = {item["InstanceId"]: item["State"]["Name"] for reservation in rows for item in reservation["Instances"]}
            if set(found) == ids and all(value == "terminated" for value in found.values()):
                return
            self.sleep(10)
        raise Refused("critical release committed; old Fleet reclamation is still pending")

    def validated_image(self, ec2, image_id, kind):
        rows = ec2.describe_images(ImageIds=[image_id])["Images"]
        need(len(rows) == 1, "published image is missing")
        image = rows[0]
        tags = {item["Key"]: item["Value"] for item in image.get("Tags", [])}
        need(image.get("ImageId") == image_id and image.get("OwnerId") == self.env["FLEET_ACCOUNT"]
             and image.get("Public") is False and image.get("State") == "available" and image.get("Architecture") == "x86_64"
             and image.get("RootDeviceType") == "ebs" and NAME[kind].fullmatch(canonical_image_name(image.get("Name", "")))
             and all(tags.get(key) == value for key, value in {
                 "Purpose": "fcvm-fleet", "FleetImageKind": kind, "FleetImageValidated": "true",
                 "GuardHelperSHA256": self.env["GUARD_SHA256"]}.items()),
             "published image is outside its private validated scope")
        return image

    def replicate(self, release):
        """Copy the committed pair into every copy region and record each region's ids.

        Runs only after the release is committed, and never changes it: a failure
        leaves the release live and is retried by the next invocation. A copy is
        found again by its name (unique per region), so a replay never starts a
        second one. The copies are not in host_amis or tile_amis, so no copy region
        gets launch templates or fleet hosts.
        """
        compute = self.env["COMPUTE_REGION"]
        need(release.get("version") == 1 and type(release.get("generation")) is int
             and release.get("guard_sha256") == self.env["GUARD_SHA256"], "active release schema is incompatible")
        sources = {kind: release[kind + "_amis"][compute]["x86_64"] for kind in ("host", "tile")}
        need(all(IMAGE.fullmatch(item) for item in sources.values()) and sources["host"] != sources["tile"],
             "invalid published image identities")
        source = self.client("ec2", True)
        published = {kind: self.validated_image(source, image_id, kind) for kind, image_id in sources.items()}
        images, pending = {compute: dict(sources)}, []
        for region in self.copy_regions:
            ec2 = self.client("ec2", True, region)
            images[region] = {}
            for kind in ("host", "tile"):
                source_image = published[kind]
                reverse = source_image.get("SourceImageId") if source_image.get("SourceImageRegion") == region else None
                copied = self.regional_copy(ec2, region, sources[kind], compute, canonical_image_name(source_image["Name"]), {
                    "Purpose": "fcvm-fleet", "FleetImageKind": kind, "FleetImageValidated": "true",
                    "GuardHelperSHA256": self.env["GUARD_SHA256"]}, reverse=reverse)
                if copied is None:
                    pending.append(region + "/" + kind)
                else:
                    images[region][kind] = copied
        need(not pending, "regional AMI copies are still pending: " + ",".join(pending))
        try:
            current = self.load(self.copies_parameter, True)
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") != "ParameterNotFound":
                raise
            current = None
        # The previous release's images in every region stay named, so retention keeps them for rollback.
        if current and current.get("build_id") != release.get("build_id", ""):
            previous = current.get("images", {})
        else:
            previous = (current or {}).get("previous_images", {})
        record = {"version": 1, "generation": release["generation"], "build_id": release.get("build_id", ""),
                  "source_sha": release.get("source_sha", ""), "images": images, "previous_images": previous}
        if current != record:
            try:
                self.save(self.copies_parameter, record, True)
            except Exception:
                # A lost acknowledgement is resolved by reading the record back.
                pass
        need(self.load(self.copies_parameter, True) == record, "image copy record was not confirmed")
        return record

    def run_replicate(self, event):
        need(event == {"action": "replicate-active-release"}, "image copy accepts no caller configuration")
        release = self.load(self.env["RELEASE_PARAMETER"], True)
        record = self.replicate(release)
        self.retain(release, record)
        return {"status": "replicated", "generation": record["generation"]}

    def retirable(self, image, region):
        """Whether retention may remove an image nothing needs. Only the fleet's own private
        images with Purpose=fcvm-fleet, and of those only:
        - a copy this publisher made with CopyImage (release-region and copy-region copies);
        - a factory candidate outside the build region (from before the factory moved; in the
          build region dispatch.py retains candidates);
        - an image built by hand with build-ami.sh that was never validated (the bootstrap
          catalog's images are validated, and Terraform tags them) and is HANDMADE_DAYS old.
        """
        tags = {item["Key"]: item["Value"] for item in image.get("Tags", [])}
        if not (IMAGE.fullmatch(image.get("ImageId", "")) and image.get("OwnerId") == self.env["FLEET_ACCOUNT"]
                and image.get("Public") is False and tags.get("Purpose") == "fcvm-fleet"
                and image.get("State") in {"available", "failed"}):
            return False
        try:
            age = self.clock() - datetime.datetime.fromisoformat(image.get("CreationDate", "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            return False
        name = image.get("Name", "")
        described = COPY_DESCRIPTION.fullmatch(image.get("Description", ""))
        if described:
            # A copy younger than a publication lease may belong to a publication still in progress.
            return ((image.get("SourceImageId"), image.get("SourceImageRegion")) == described.groups()
                    and any(NAME[kind].fullmatch(canonical_image_name(name)) for kind in NAME) and age >= RETAIN_MIN_DAYS * 86400)
        if FACTORY_NAME.fullmatch(name):
            return (region != self.build_region and tags.get("PipelineSanitized") == "true"
                    and tags.get("PipelineBuildId", "").startswith(self.env["PROJECT_NAME"] + ":")
                    and age >= RETAIN_MIN_DAYS * 86400)
        if HANDMADE_NAME.fullmatch(name) and "FleetImageValidated" not in tags and "PipelineSanitized" not in tags:
            return age >= HANDMADE_DAYS * 86400
        return False

    def retain(self, release, record):
        """Deregister the retirable images (retirable()) that nothing needs, and delete their snapshots.

        Kept: the active release and the previous one (the release pointer and the copy record
        name both), every launch template's default image, every image an instance or an open
        Spot request in the region uses, and every copy of a kept image or image a kept copy was
        made from, in any region (so a record without previous_images still keeps the previous
        release's copies). At most RETAIN_LIMIT images go per region and run. A snapshot is
        eligible for deletion only if CopyImage stamped its provenance at creation. Eligible
        snapshots are tagged FleetRetired before deregistration, so a failed deletion is retried."""
        keep = set()
        for bundle in (release, release.get("previous") or {}):
            for key in ("host_amis", "tile_amis"):
                for arches in bundle.get(key, {}).values():
                    keep.update(arches.values())
        for field in ("images", "previous_images"):
            for kinds in record.get(field, {}).values():
                keep.update(kinds.values())
        keep.update(self.current(template["id"])["LaunchTemplateData"].get("ImageId") for template in self.templates)
        regions = sorted({self.env["COMPUTE_REGION"], self.build_region, *self.copy_regions})
        listed = {}
        for region in regions:
            ec2 = self.client("ec2", True, region)
            arguments = {"Filters": [{"Name": "instance-state-name",
                                      "Values": ["pending", "running", "stopping", "stopped", "shutting-down"]}]}
            for _ in range(20):
                page = ec2.describe_instances(**arguments)
                keep.update(item.get("ImageId") for reservation in page["Reservations"] for item in reservation["Instances"])
                if not page.get("NextToken"):
                    break
                arguments["NextToken"] = page["NextToken"]
            else:
                raise Refused("instance inventory exceeds its fixed bound; nothing was removed")
            requests = ec2.describe_spot_instance_requests(Filters=[{"Name": "state", "Values": ["open", "active", "disabled"]}])
            keep.update(item.get("LaunchSpecification", {}).get("ImageId") for item in requests["SpotInstanceRequests"])
            listed[region] = ec2.describe_images(Owners=[self.env["FLEET_ACCOUNT"]],
                Filters=[{"Name": "tag:Purpose", "Values": ["fcvm-fleet"]}])["Images"]
        # Copies are linked to their sources across regions: keep both ends of every link to a kept image.
        links = [(image["ImageId"], image.get("SourceImageId")) for images in listed.values() for image in images
                 if image.get("SourceImageId")]
        for _ in range(len(links) + 1):
            grown = {a for a, b in links if b in keep} | {b for a, b in links if a in keep}
            if grown <= keep:
                break
            keep |= grown
        removed = {}
        for region in regions:
            ec2 = self.client("ec2", True, region)
            for snapshot in self.copy_snapshots(ec2):
                self.delete_retired_snapshot(ec2, snapshot)
            stale = sorted((image for image in listed[region] if self.retirable(image, region) and image["ImageId"] not in keep),
                           key=lambda image: image["ImageId"])
            removed[region] = []
            for image in stale[:RETAIN_LIMIT]:
                mapped = [item["Ebs"]["SnapshotId"] for item in image.get("BlockDeviceMappings", [])
                          if item.get("Ebs", {}).get("SnapshotId")]
                snapshots = self.copy_snapshots(ec2, mapped) if mapped else []
                if snapshots:
                    ec2.create_tags(Resources=snapshots, Tags=[{"Key": "FleetRetired", "Value": "true"}])
                ec2.deregister_image(ImageId=image["ImageId"])
                for snapshot in snapshots:
                    self.delete_retired_snapshot(ec2, snapshot)
                removed[region].append(image["ImageId"])
        return removed

    def copy_snapshots(self, ec2, mapped=None):
        scope = {"Name": "tag:FleetRetired", "Values": ["true"]} if mapped is None else {"Name": "snapshot-id", "Values": mapped}
        snapshots = ec2.describe_snapshots(OwnerIds=[self.env["FLEET_ACCOUNT"]], MaxResults=RETAIN_LIMIT, Filters=[
            {"Name": "tag:" + COPY_SNAPSHOT_TAG, "Values": ["true"]}, scope])["Snapshots"]
        owned = []
        for snapshot in snapshots:
            tags = {item["Key"]: item["Value"] for item in snapshot.get("Tags", [])}
            if (snapshot.get("OwnerId") == self.env["FLEET_ACCOUNT"] and tags.get(COPY_SNAPSHOT_TAG) == "true"
                    and (tags.get("FleetRetired") == "true" if mapped is None else snapshot["SnapshotId"] in mapped)):
                owned.append(snapshot["SnapshotId"])
        return owned

    @staticmethod
    def delete_retired_snapshot(ec2, snapshot):
        try:
            ec2.delete_snapshot(SnapshotId=snapshot)
        except Exception as error:
            # Still in use by an image whose deregistration has not landed: the next run retries.
            if getattr(error, "response", {}).get("Error", {}).get("Code") not in {"InvalidSnapshot.InUse", "InvalidSnapshot.NotFound"}:
                raise

    def finish(self, result, source, input_sha):
        name, request = self.request(source, input_sha)
        if request["priority"] == "critical":
            try:
                self.retire(result)
            except Exception:
                request["status"] = "awaiting-publish"
                self.save(name, request)
                raise
        # Before the request is promoted: a failure leaves it awaiting publication, and the
        # dispatcher redrives it until the copies and their record are confirmed.
        record = self.replicate(result)
        request.update(status="promoted", generation=result["generation"])
        self.save(name, request)
        # After promotion: a failure here is raised, and the next publication retries it.
        self.retain(result, record)
        return {"status": "promoted", "generation": result["generation"]}

    def run(self, event):
        if isinstance(event, dict) and event.get("action") == "replicate-active-release":
            return self.run_replicate(event)
        if isinstance(event, dict) and event.get("action") == "activate-reviewed-seed":
            return self.run_seed(event)
        if isinstance(event, dict) and event.get("action") == "activate-reviewed-phone-release":
            return self.run_phone_release(event)
        if isinstance(event, dict) and event.get("action") == "activate-reviewed-renderer-release":
            return self.run_renderer_release(event)
        if isinstance(event, dict) and event.get("action") == "activate-reviewed-potato-release":
            return self.run_potato_release(event)
        if isinstance(event, dict) and event.get("action") == "activate-reviewed-potato-host-fix":
            return self.run_potato_host_fix(event)
        if isinstance(event, dict) and event.get("action") == "activate-reviewed-stride-release":
            return self.run_stride_release(event)
        if isinstance(event, dict) and event.get("action") == "activate-reviewed-retake-host-release":
            return self.run_retake_host_release(event)
        if isinstance(event, dict) and event.get("action") == "activate-reviewed-held-stamp-host-release":
            return self.run_held_stamp_host_release(event)
        if isinstance(event, dict) and event.get("action") == "activate-reviewed-windows-host-release":
            return self.run_windows_host_release(event)
        build, source, input_sha = self.event(event)
        active = self.load(self.env["RELEASE_PARAMETER"], True)
        manifest = None
        resources = None
        if active.get("build_id") != self.build_id:
            prior = self.prior_transaction()
            if prior and prior.get("status") == "rolled_back":
                name, request = self.request(source, input_sha)
                request.update(status="failed", message="publication rolled back; automatic replay refused")
                self.save(name, request)
                return {"status": "failed", "reason": "rolled_back"}
            self.request(source, input_sha)
            self.source_fence(source)
            manifest = self.proof(build, source, input_sha)
            resources = self.images(manifest, source, input_sha)
            # Large content transfers need not hold the infrastructure state lock.
            self.store(manifest, input_sha)
            targets, copies = self.release_copies(manifest, source, input_sha)
            if copies:
                resources[self.env["COMPUTE_REGION"]] = copies
        with self.publication_lock():
            active = self.load(self.env["RELEASE_PARAMETER"], True)
            if active.get("build_id") == self.build_id:
                self.committed = True
                return self.finish(active, source, input_sha)
            self.request(source, input_sha)
            self.factory_fence(source, input_sha)
            need(self.proof(build, source, input_sha) == manifest,
                 "factory proof changed before the publication lock")
            released = {"images": {kind: {"image_id": image} for kind, image in targets.items()}}
            result = self.promote(released, source, input_sha, resources)
            return self.finish(result, source, input_sha)


def lambda_handler(event, context):
    try:
        if event == {"action": "check-copy-sdk"}:
            return copy_sdk_status()
        return Publisher().run(event)
    except Exception:
        # SDK errors may contain request details. Only a static failure escapes.
        raise RuntimeError("validated image publication failed; inspect the bounded publication journal") from None

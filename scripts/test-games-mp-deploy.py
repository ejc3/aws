#!/usr/bin/env python3
"""Offline checks for the automatic games multiplayer deploys: games-multiplayer/poller.py
(starts builds for new colton-games commits) and games-multiplayer/release.py (turns a finished
build into a release), and the Terraform around them (games-multiplayer-deploy.tf).

Both files are imported as they are and driven with fakes of every AWS client and of GitHub
(no credentials, no network, no boto3). What this pins:
  - main's code reaches production and a branch's code only preview, whatever a build reports;
  - a release never touches a running engine, only what the next launch runs;
  - main releases are ordered, migrations come first, and the router rolls only when its own
    files changed, putting `live` back if the rollout fails;
  - every client call a role needs is granted, and nothing more.

Run from the repo root:  python3 -S -B scripts/test-games-mp-deploy.py
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import unittest
import urllib.error
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GM = ROOT / "games-multiplayer"
DEPLOY = (ROOT / "games-multiplayer-deploy.tf").read_text()
GAMES_TF = (ROOT / "games-multiplayer.tf").read_text()
BRINGUP_TF = (ROOT / "games-multiplayer-bringup.tf").read_text()

ACCOUNT = "928413605543"
REGISTRY = "%s.dkr.ecr.us-west-1.amazonaws.com" % ACCOUNT
MAIN = "a" * 40
MAIN2 = "b" * 40
BRANCH = "c" * 40
TOKEN = "github_pat_DO_NOT_PRINT"
TD_PREFIX = "arn:aws:ecs:us-west-1:%s:task-definition/" % ACCOUNT

# games-multiplayer-deploy.tf's games_mp_engine_template: one shape for every game.
TEMPLATE = {
    "executionRoleArn": "arn:aws:iam::%s:role/games-engine-execution" % ACCOUNT,
    "taskRoleArn": "arn:aws:iam::%s:role/games-engine-task" % ACCOUNT,
    "logGroup": "/games/engines", "region": "us-west-1", "maxCpu": 4096, "maxMemory": 8192,
    "main": {"familyPrefix": "games-", "repository": "games/engines",
             "repositoryUrl": REGISTRY + "/games/engines"},
    "preview": {"familyPrefix": "games-preview-", "repository": "games-preview/engines",
                "repositoryUrl": REGISTRY + "/games-preview/engines"},
    "legacy": {"mptest": {
        "main": {"repository": "games/mptest-engine", "repositoryUrl": REGISTRY + "/games/mptest-engine"},
        "preview": {"repository": "games-preview/mptest-engine",
                    "repositoryUrl": REGISTRY + "/games-preview/mptest-engine"}}},
}


def engines_report(sim="mptest-1", **more):
    """GAMES_MP_ENGINES as bringup.py codebuild-images exports it: {game: [sim, cpu, memory]}."""
    return json.dumps(dict({"mptest": [sim, 2048, 4096]}, **more), separators=(",", ":"))


def load(name, env):
    for k, v in env.items():
        os.environ[k] = v
    sys.path.insert(0, str(GM))
    try:
        spec = importlib.util.spec_from_file_location(name + "_under_test", GM / (name + ".py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(str(GM))
    return mod


class AwsError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


# ------------------------------------------------------------------------------------------
# Fakes
# ------------------------------------------------------------------------------------------


def typed(value):
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, dict):
        return {"M": {k: typed(v) for k, v in value.items()}}
    raise TypeError(value)


def plain(value):
    (kind, inner), = value.items()
    return {"S": lambda: inner, "N": lambda: int(inner), "BOOL": lambda: inner,
            "M": lambda: {k: plain(v) for k, v in inner.items()}}[kind]()


class FakeDynamo:
    """The releases table, understanding exactly the expressions the two functions send."""

    def __init__(self):
        self.items = {}
        self.writes = []

    def get(self, key):
        item = self.items.get(key)
        return {k: plain(v) for k, v in item.items()} if item else None

    def put(self, key, **fields):
        self.items[key] = {k: typed(v) for k, v in dict(fields, id=key).items()}

    def get_item(self, TableName, Key, ConsistentRead):
        assert TableName == "games-mp-releases" and ConsistentRead
        item = self.items.get(Key["id"]["S"])
        return {"Item": item} if item else {}

    def _check(self, key, condition, names, values):
        item = self.items.get(key)
        names = names or {}
        if condition is None:
            return True
        if condition == "attribute_not_exists(id) OR (#s = :starting AND claimedAt < :stale)":
            return item is None or (item.get(names["#s"]) == values[":starting"]
                                    and int(item["claimedAt"]["N"]) < int(values[":stale"]["N"]))
        if condition == "#s = :starting":
            return item is not None and item.get(names["#s"]) == values[":starting"]
        if condition == "attribute_not_exists(seq) OR seq < :seq":
            return item is None or "seq" not in item or int(item["seq"]["N"]) < int(values[":seq"]["N"])
        if condition == "attribute_exists(id)":
            return item is not None
        raise AssertionError("unexpected condition %r" % condition)

    def put_item(self, TableName, Item, ConditionExpression=None, ExpressionAttributeNames=None,
                 ExpressionAttributeValues=None):
        key = Item["id"]["S"]
        if not self._check(key, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
            raise AwsError("ConditionalCheckFailedException")
        self.writes.append(key)
        self.items[key] = dict(Item)
        return {}

    def update_item(self, TableName, Key, UpdateExpression, ExpressionAttributeValues,
                    ExpressionAttributeNames=None, ConditionExpression=None, ReturnValues=None):
        key = Key["id"]["S"]
        if UpdateExpression == "ADD seq :one":
            item = self.items.setdefault(key, {"id": {"S": key}, "seq": {"N": "0"}})
            item["seq"] = {"N": str(int(item["seq"]["N"]) + 1)}
            self.writes.append(key)
            return {"Attributes": {"seq": item["seq"]}}
        if not self._check(key, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
            raise AwsError("ConditionalCheckFailedException")
        old = dict(self.items.get(key) or {})
        item = self.items.setdefault(key, {"id": {"S": key}})
        assert UpdateExpression.startswith("SET ")
        for part in UpdateExpression[4:].split(", "):
            name, value = part.split(" = ")
            item[(ExpressionAttributeNames or {}).get(name, name)] = ExpressionAttributeValues[value]
        self.writes.append(key)
        return {"Attributes": old if ReturnValues == "ALL_OLD" else {}}

    def delete_item(self, TableName, Key, ConditionExpression=None, ExpressionAttributeNames=None,
                    ExpressionAttributeValues=None):
        key = Key["id"]["S"]
        if not self._check(key, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
            raise AwsError("ConditionalCheckFailedException")
        self.items.pop(key, None)


class FakeCodeBuild:
    def __init__(self):
        self.started = []
        self.builds = {}
        self.running_preview = 0
        self.fail_start = None

    def start_build(self, projectName, sourceLocationOverride, environmentVariablesOverride):
        if self.fail_start:
            raise AwsError(self.fail_start)
        build = "%s:%d" % (projectName, len(self.started) + 1)
        self.started.append({"project": projectName, "source": sourceLocationOverride,
                             "env": {e["name"]: e["value"] for e in environmentVariablesOverride}})
        return {"build": {"id": build}}

    def list_builds_for_project(self, projectName, sortOrder):
        assert projectName == "games-mp-images-preview" and sortOrder == "DESCENDING"
        return {"ids": ["p:%d" % i for i in range(self.running_preview + 2)]}

    def batch_get_builds(self, ids):
        out = []
        for i in ids:
            if i.startswith("p:"):
                out.append({"id": i, "buildStatus": "IN_PROGRESS" if int(i[2:]) < self.running_preview else "SUCCEEDED"})
            elif i in self.builds:
                out.append(self.builds[i])
        return {"builds": out}

    def finish(self, build_id, project, commit, exported, status="SUCCEEDED"):
        self.builds[build_id] = {
            "id": build_id, "projectName": project, "buildStatus": status,
            "environment": {"environmentVariables": [{"name": "ACCOUNT_ID", "value": ACCOUNT},
                                                     {"name": "GAMES_MP_COMMIT", "value": commit}]},
            "exportedEnvironmentVariables": [{"name": k, "value": v} for k, v in exported.items()],
        }
        return {"source": "aws.codebuild", "detail-type": "CodeBuild Build State Change",
                "detail": {"build-id": build_id, "project-name": project, "build-status": status}}


class FakeECR:
    def __init__(self):
        self.images = {}      # (repo, tag) -> digest
        self.puts = []

    def add(self, repo, tag, digest=None):
        self.images[(repo, tag)] = digest or "sha256:" + format(abs(hash((repo, tag))), "064x")[:64]

    def describe_images(self, repositoryName, imageIds):
        tag = imageIds[0]["imageTag"]
        if (repositoryName, tag) not in self.images:
            raise AwsError("ImageNotFoundException")
        return {"imageDetails": [{"imageDigest": self.images[(repositoryName, tag)], "imageTags": [tag]}]}

    def batch_get_image(self, repositoryName, imageIds, acceptedMediaTypes):
        tag = imageIds[0]["imageTag"]
        if (repositoryName, tag) not in self.images:
            return {"images": [], "failures": [{"failureCode": "ImageNotFound"}]}
        digest = self.images[(repositoryName, tag)]
        return {"images": [{"imageId": {"imageDigest": digest, "imageTag": tag},
                            "imageManifest": "manifest:" + digest,
                            "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json"}]}

    def put_image(self, repositoryName, imageTag, imageManifest, imageManifestMediaType):
        assert repositoryName == "games/mp-router" and imageTag == "live", "only the router's live tag moves"
        digest = imageManifest.split(":", 1)[1]
        if self.images.get((repositoryName, imageTag)) == digest:
            raise AwsError("ImageAlreadyExistsException")
        self.puts.append(digest)
        self.images[(repositoryName, imageTag)] = digest
        return {}


class FakeECS:
    def __init__(self, ecr):
        self.ecr = ecr
        self.registered = []
        self.revision = {}
        self.deployments = [{"id": "ecs-svc/old", "status": "PRIMARY", "rolloutState": "COMPLETED",
                             "runningCount": 2, "desiredCount": 2}]
        self.running_digest = None
        self.updates = 0
        self.roll_fails = False
        self.service_active = True

    def register_task_definition(self, **kw):
        family = kw["family"]
        self.revision[family] = self.revision.get(family, 0) + 1
        arn = "%s%s:%d" % (TD_PREFIX, family, self.revision[family])
        self.registered.append(dict(kw, arn=arn))
        return {"taskDefinition": {"taskDefinitionArn": arn}}

    def describe_services(self, cluster, services):
        assert cluster == "games" and services == ["mp-router"]
        if not self.service_active:
            return {"services": [], "failures": [{"reason": "MISSING"}]}
        return {"services": [{"status": "ACTIVE", "deployments": [dict(d) for d in self.deployments]}]}

    def update_service(self, cluster, service, forceNewDeployment):
        assert (cluster, service, forceNewDeployment) == ("games", "mp-router", True)
        self.updates += 1
        for d in self.deployments:
            d["status"] = "ACTIVE"
        new = {"id": "ecs-svc/new%d" % self.updates, "status": "PRIMARY", "rolloutState": "IN_PROGRESS",
               "runningCount": 0, "desiredCount": 2}
        self.deployments.insert(0, new)
        self.polls = 0
        return {"service": {"deployments": [dict(d) for d in self.deployments]}}

    def tick(self):
        """Advances the newest deployment: running, then healthy (or FAILED)."""
        new = self.deployments[0]
        if self.roll_fails:
            new["rolloutState"] = "FAILED"
            new["rolloutStateReason"] = "tasks failed to start"
        else:
            new["runningCount"] = 2
            self.running_digest = self.ecr.images.get(("games/mp-router", "live"))

    def list_tasks(self, cluster, desiredStatus, serviceName=None, startedBy=None):
        if serviceName:
            return {"taskArns": ["task/r1", "task/r2"]}
        if startedBy == self.deployments[0]["id"] and self.deployments[0]["runningCount"]:
            return {"taskArns": ["task/10.0.66.5", "task/10.0.67.5"]}
        return {"taskArns": []}

    def describe_tasks(self, cluster, tasks):
        out = []
        for arn in tasks:
            if arn.startswith("task/r"):
                out.append({"containers": [{"imageDigest": self.running_digest}]})
            else:
                out.append({"attachments": [{"details": [{"name": "privateIPv4Address", "value": arn[5:]}]}]})
        return {"tasks": out}


class FakeELB:
    def describe_target_health(self, TargetGroupArn):
        return {"TargetHealthDescriptions": [
            {"Target": {"Id": ip}, "TargetHealth": {"State": "healthy"}} for ip in ("10.0.66.5", "10.0.67.5")]}


class FakeSNS:
    def __init__(self):
        self.sent = []

    def publish(self, TopicArn, Subject, Message):
        self.sent.append((Subject, Message))


class FakeS3:
    def __init__(self):
        self.objects = {}

    def upload_fileobj(self, f, bucket, key):
        self.objects[(bucket, key)] = f.read()


class FakeSecrets:
    def get_secret_value(self, SecretId):
        assert SecretId == "games/colton-games-read"
        return {"SecretString": TOKEN + "\n"}


def zipball(commit):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        top = "CoderColton-colton-games-%s/" % commit[:7]
        z.writestr(top, b"")
        z.writestr(top + "scripts/mp-images.mjs", b"// images")
        z.writestr(top + ".games-mp/bringup.py", b"print('a commit must never supply the driver')")
    return out.getvalue()


class FakeGitHub:
    def __init__(self):
        self.branches = [{"name": "main", "commit": {"sha": MAIN}},
                         {"name": "feature", "commit": {"sha": BRANCH}}]
        self.requests = []
        self.status = 200

    def urlopen(self, req, timeout=30):
        assert req.get_header("Authorization") == "Bearer " + TOKEN
        url = req.full_url
        self.requests.append(url)
        if self.status != 200:
            raise urllib.error.HTTPError(url, self.status, "no", {}, io.BytesIO(b""))
        if "/branches?" in url:
            page = int(re.search(r"[?&]page=(\d+)", url).group(1))
            body = json.dumps(self.branches[(page - 1) * 100:page * 100]).encode()
        elif "/zipball/" in url:
            body = zipball(url.rsplit("/", 1)[1])
        else:
            raise AssertionError(url)
        return contextlib.closing(io.BytesIO(body))


# ------------------------------------------------------------------------------------------
# Poller
# ------------------------------------------------------------------------------------------

POLLER_ENV = {"REPO": "CoderColton/colton-games", "TOKEN_SECRET": "games/colton-games-read",
              "RELEASES_TABLE": "games-mp-releases", "BUCKET": "games-mp-build-1",
              "MAIN_PROJECT": "games-mp-images", "PREVIEW_PROJECT": "games-mp-images-preview"}


class PollerTests(unittest.TestCase):
    def setUp(self):
        self.p = load("poller", POLLER_ENV)
        self.ddb, self.cb, self.s3, self.gh = FakeDynamo(), FakeCodeBuild(), FakeS3(), FakeGitHub()
        self.p._clients.update(dynamodb=self.ddb, codebuild=self.cb, s3=self.s3, secretsmanager=FakeSecrets())
        self.p.URLOPEN = self.gh.urlopen
        self.now = 1_000_000.0
        self.p.CLOCK = lambda: self.now
        self.log = io.StringIO()

    def run_once(self):
        with contextlib.redirect_stdout(self.log):
            return self.p.lambda_handler({}, None)

    def test_a_new_main_commit_is_built_as_main_with_our_driver(self):
        out = self.run_once()
        main = [s for s in self.cb.started if s["project"] == "games-mp-images"]
        self.assertEqual(main, [{"project": "games-mp-images", "source": "games-mp-build-1/sources/main/%s.zip" % MAIN,
                                 "env": {"GAMES_MP_COMMIT": MAIN}}])
        z = zipfile.ZipFile(io.BytesIO(self.s3.objects[("games-mp-build-1", "sources/main/%s.zip" % MAIN)]))
        self.assertEqual(z.read(".games-mp/bringup.py"), (GM / "bringup.py").read_bytes(), "ours, never the commit's")
        self.assertEqual(z.read(".games-mp/supabase-root-2021-ca.crt"), (GM / "supabase-root-2021-ca.crt").read_bytes())
        self.assertEqual(z.read(".games-mp/SOURCE_REF").decode().strip(), MAIN)
        record = self.ddb.get("build#main#" + MAIN)
        self.assertEqual((record["status"], record["seq"], record["buildId"]), ("building", 1, "games-mp-images:1"))
        self.assertNotIn("expiresAt", record, "main records never expire")
        self.assertEqual(len(out["started"]), 2)
        self.assertNotIn(TOKEN, self.log.getvalue())

    def test_every_other_branch_is_built_as_preview_and_expires(self):
        self.run_once()
        preview = [s for s in self.cb.started if s["project"] == "games-mp-images-preview"]
        self.assertEqual(preview[0]["source"], "games-mp-build-1/sources/preview/%s.zip" % BRANCH)
        record = self.ddb.get("build#preview#" + BRANCH)
        self.assertEqual(record["status"], "building")
        self.assertEqual(record["expiresAt"], int(self.now) + 30 * 86400)
        self.assertNotIn("seq", record)

    def test_a_commit_is_built_once(self):
        self.run_once()
        self.run_once()
        self.assertEqual(len(self.cb.started), 2)
        # A new main commit is a new build, with a higher sequence number.
        self.gh.branches[0]["commit"]["sha"] = MAIN2
        self.run_once()
        self.assertEqual(self.cb.started[-1]["env"], {"GAMES_MP_COMMIT": MAIN2})
        self.assertEqual(self.ddb.get("build#main#" + MAIN2)["seq"], 2)

    def test_a_commit_on_main_and_a_branch_is_built_in_both_channels(self):
        self.gh.branches[1]["commit"]["sha"] = MAIN
        self.run_once()
        self.assertEqual(sorted(s["project"] for s in self.cb.started), ["games-mp-images", "games-mp-images-preview"])

    def test_preview_builds_are_capped(self):
        self.gh.branches += [{"name": "b%d" % i, "commit": {"sha": "%040x" % i}} for i in range(1, 10)]
        self.run_once()
        self.assertEqual(len([s for s in self.cb.started if s["project"] == "games-mp-images-preview"]), 2,
                         "MAX_STARTS per run")
        self.cb.running_preview = 3
        before = len(self.cb.started)
        self.run_once()
        self.assertEqual(len(self.cb.started), before, "MAX_PREVIEW_BUILDS already running")
        self.gh.branches[0]["commit"]["sha"] = MAIN2
        self.run_once()
        self.assertEqual(self.cb.started[-1]["project"], "games-mp-images", "main is never held back by previews")

    def test_a_failed_start_leaves_no_claim_and_fails_the_run(self):
        self.cb.fail_start = "ResourceLimitExceededException"
        with self.assertRaises(AwsError):
            self.run_once()
        self.assertIsNone(self.ddb.get("build#main#" + MAIN))
        self.cb.fail_start = None
        self.run_once()
        self.assertEqual(self.ddb.get("build#main#" + MAIN)["status"], "building")

    def test_a_stale_claim_is_taken_over_a_fresh_one_is_not(self):
        self.ddb.put("build#main#" + MAIN, status="starting", claimedAt=int(self.now) - 60, channel="main", commit=MAIN)
        self.run_once()
        self.assertFalse([s for s in self.cb.started if s["project"] == "games-mp-images"])
        self.now += 1000
        self.run_once()
        self.assertTrue([s for s in self.cb.started if s["project"] == "games-mp-images"])

    def test_github_failures_fail_the_run_without_the_token(self):
        self.gh.status = 401
        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as err:
            self.run_once()
        self.assertNotIn(TOKEN, str(err.exception))
        self.assertEqual(self.cb.started, [])

    def test_branches_are_paged_and_bad_entries_skipped(self):
        self.gh.branches = [{"name": "main", "commit": {"sha": MAIN}}] + [
            {"name": "b%d" % i, "commit": {"sha": "%040x" % i}} for i in range(1, 150)] + [
            {"name": "weird", "commit": {"sha": "not-a-sha"}}]
        heads = self.p.heads()
        self.assertEqual(heads[0], ("main", MAIN, "main"))
        self.assertEqual(len(heads), 150)
        self.gh.branches = [{"name": "feature", "commit": {"sha": BRANCH}}]
        with self.assertRaisesRegex(RuntimeError, "no main"):
            self.p.heads()


# ------------------------------------------------------------------------------------------
# Release
# ------------------------------------------------------------------------------------------

RELEASE_ENV = {"RELEASES_TABLE": "games-mp-releases", "MAIN_PROJECT": "games-mp-images",
               "PREVIEW_PROJECT": "games-mp-images-preview", "MIGRATE_PROJECT": "games-mp-migrate",
               "BUCKET": "games-mp-build-1", "ENGINE_TEMPLATE": json.dumps(TEMPLATE),
               "ROUTER_REPOSITORY": "games/mp-router", "ROUTER_LIVE_TAG": "live", "CLUSTER": "games",
               "ROUTER_SERVICE": "mp-router", "TARGET_GROUP_ARN": "arn:tg", "SNS_TOPIC_ARN": "arn:sns",
               "ROLL_TIMEOUT_SEC": "600", "POLL_SEC": "15"}
INPUTS_A = "1" * 64
INPUTS_B = "2" * 64


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.r = load("release", RELEASE_ENV)
        self.ddb, self.cb, self.ecr, self.sns = FakeDynamo(), FakeCodeBuild(), FakeECR(), FakeSNS()
        self.ecs = FakeECS(self.ecr)
        self.r._clients.update(dynamodb=self.ddb, codebuild=self.cb, ecr=self.ecr, ecs=self.ecs, elbv2=FakeELB(),
                               sns=self.sns)
        self.clock = 0.0
        self.r.CLOCK = lambda: self.clock
        self.r.SLEEP = self.sleep
        self.log = io.StringIO()
        # What is live before: the router at an older commit, the database at mp revision 1.
        self.ecr.add("games/mp-router", "0" * 12, "sha256:old")
        self.ecr.add("games/mp-router", "live", "sha256:old")
        self.ecs.running_digest = "sha256:old"
        self.ddb.put("router#main", inputs=INPUTS_A, tag="0" * 12, digest="sha256:old")
        self.ddb.put("schema#main", revision=1)

    def sleep(self, s):
        self.clock += s
        self.ecs.tick()

    def invoke(self, event):
        with contextlib.redirect_stdout(self.log):
            return self.r.lambda_handler(event, None)

    def main_built(self, commit=MAIN, seq=1, inputs=INPUTS_A, schema="1", sim="mptest-1", engines=None):
        self.ddb.put("build#main#" + commit, status="building", seq=seq, channel="main", commit=commit)
        self.ecr.add("games/engines", "mptest_%s-%s" % (sim, commit[:12]))
        self.ecr.add("games/mp-router", commit[:12], "sha256:router-" + commit[:4])
        return self.cb.finish("games-mp-images:%s" % commit[:4], "games-mp-images", commit, {
            "GAMES_MP_ENGINES": engines or engines_report(sim),
            "GAMES_MP_ROUTER_INPUTS": inputs, "GAMES_MP_SCHEMA_REVISION": schema})

    def preview_built(self, commit=BRANCH, engines=None, exported=None):
        self.ddb.put("build#preview#" + commit, status="building", channel="preview", commit=commit)
        self.ecr.add("games-preview/engines", "mptest_mptest-1-" + commit[:12])
        return self.cb.finish("games-mp-images-preview:1", "games-mp-images-preview", commit,
                              exported or {"GAMES_MP_ENGINES": engines or engines_report()})

    # -- preview ------------------------------------------------------------------------------

    def test_a_preview_build_registers_only_preview_revisions_for_its_commit(self):
        self.invoke(self.preview_built())
        [td] = self.ecs.registered
        self.assertEqual(td["family"], "games-preview-mptest")
        container = td["containerDefinitions"][0]
        self.assertEqual(container["image"], REGISTRY + "/games-preview/engines:mptest_mptest-1-" + BRANCH[:12])
        self.assertIn({"name": "MP_ENV", "value": "preview"}, container["environment"])
        record = self.ddb.get("build#preview#" + BRANCH)
        self.assertEqual(record["status"], "released")
        self.assertEqual(record["games"]["mptest"]["taskDefinition"], TD_PREFIX + "games-preview-mptest:1")
        self.assertIsNone(self.ddb.get("current#main"), "production untouched")
        self.assertEqual((self.ecs.updates, self.ecr.puts, self.cb.started), (0, [], []))

    def test_a_preview_build_cannot_claim_a_production_or_another_commits_image(self):
        other = "d" * 40
        self.ecr.add("games/engines", "mptest_mptest-1-" + BRANCH[:12])            # production's repository
        self.ecr.add("games-preview/engines", "mptest_mptest-1-" + other[:12])     # another commit's
        self.ecr.add("games-preview/engines", "starfall_mptest-1-" + BRANCH[:12])  # another game's name
        for why, engines in (("its image is not in ECR", engines_report("mptest-9")),
                             ("a bad simVersion", engines_report(".x")),
                             ("not a report", json.dumps(["mptest"])),
                             ("nothing", "{}")):
            with self.subTest(why):
                event = self.preview_built(engines=engines)
                self.ecr.images.pop(("games-preview/engines", "mptest_mptest-1-" + BRANCH[:12]))
                with self.assertRaises(self.r.ReleaseError):
                    self.invoke(event)
                self.assertEqual(self.ecs.registered, [])
                self.assertNotEqual(self.ddb.get("build#preview#" + BRANCH)["status"], "released")

    def test_a_build_cannot_escape_the_fixed_shape_or_the_size_bounds(self):
        # Whatever a (preview: untrusted) build reports, a revision's family is the channel's prefix
        # plus a valid game id, its image is the channel's engine repository, and its size is a
        # Fargate size within Terraform's maximums. Refused before anything is registered.
        bad = {
            "cpu above the maximum": {"mptest": ["mptest-1", 8192, 16384]},
            "memory above the maximum": {"mptest": ["mptest-1", 4096, 16384]},
            "not a Fargate size": {"mptest": ["mptest-1", 2048, 3072]},
            "a string size": {"mptest": ["mptest-1", "2048", 4096]},
            "a boolean size": {"mptest": ["mptest-1", True, 4096]},
            "the router's family": {"mp-router": ["mptest-1", 2048, 4096]},
            "another game's preview family": {"preview-mptest": ["mptest-1", 2048, 4096]},
            "not a game id": {"../x": ["mptest-1", 2048, 4096]},
            "an extra field": {"mptest": ["mptest-1", 2048, 4096, "arn:aws:iam::1:role/admin"]},
        }
        for why, engines in bad.items():
            with self.subTest(why):
                event = self.preview_built(engines=json.dumps(engines))
                with self.assertRaises(self.r.ReleaseError):
                    self.invoke(event)
                self.assertEqual(self.ecs.registered, [])
        # Within the bounds, the commit's own size is used; everything else is the template's.
        self.ecr.add("games-preview/engines", "arena_a-2-" + BRANCH[:12])
        self.invoke(self.preview_built(engines=engines_report(arena=["a-2", 4096, 8192])))
        by_family = {td["family"]: td for td in self.ecs.registered}
        arena = by_family["games-preview-arena"]
        self.assertEqual((arena["cpu"], arena["memory"]), ("4096", "8192"))
        self.assertEqual(arena["containerDefinitions"][0]["image"], REGISTRY + "/games-preview/engines:arena_a-2-" + BRANCH[:12])
        self.assertEqual((arena["executionRoleArn"], arena["taskRoleArn"]), (TEMPLATE["executionRoleArn"], TEMPLATE["taskRoleArn"]))
        self.assertEqual(arena["networkMode"], "awsvpc")
        self.assertEqual(arena["runtimePlatform"], {"operatingSystemFamily": "LINUX", "cpuArchitecture": "ARM64"})

    def test_a_new_game_is_released_with_no_terraform_change(self):
        self.ecr.add("games/engines", "starfall-arena_arena-1-" + MAIN[:12])
        self.invoke(self.main_built(engines=engines_report(**{"starfall-arena": ["arena-1", 2048, 4096]})))
        current = self.ddb.get("current#main")
        self.assertEqual(sorted(current["games"]), ["mptest", "starfall-arena"])
        self.assertEqual(current["games"]["starfall-arena"]["taskDefinition"], TD_PREFIX + "games-starfall-arena:1")
        self.assertEqual(self.ddb.get("sim#starfall-arena#arena-1")["commit"], MAIN)

    def test_a_build_by_the_previous_driver_still_releases_mptest(self):
        # In flight while Terraform switched to dynamic games: GAMES_MP_IMAGES, legacy repository.
        self.ddb.put("build#main#" + MAIN, status="building", seq=1, channel="main", commit=MAIN)
        self.ecr.add("games/mptest-engine", "mptest-1-" + MAIN[:12])
        self.ecr.add("games/mp-router", MAIN[:12], "sha256:router-" + MAIN[:4])
        self.invoke(self.cb.finish("games-mp-images:old", "games-mp-images", MAIN, {
            "GAMES_MP_IMAGES": json.dumps({"games/mp-router": MAIN[:12], "games/mptest-engine": "mptest-1-" + MAIN[:12]}),
            "GAMES_MP_ROUTER_INPUTS": INPUTS_A, "GAMES_MP_SCHEMA_REVISION": "1"}))
        [td] = self.ecs.registered
        self.assertEqual(td["containerDefinitions"][0]["image"], REGISTRY + "/games/mptest-engine:mptest-1-" + MAIN[:12])
        self.assertEqual((td["cpu"], td["memory"]), ("2048", "4096"))
        self.assertEqual(self.ddb.get("current#main")["commit"], MAIN)
        # Only games that had their own repository: nothing else is taken from that shape.
        self.ecr.add("games/starfall-engine", "hl-1-" + MAIN2[:12])
        self.ddb.put("build#main#" + MAIN2, status="building", seq=2, channel="main", commit=MAIN2)
        with self.assertRaises(self.r.ReleaseError):
            self.invoke(self.cb.finish("games-mp-images:old2", "games-mp-images", MAIN2, {
                "GAMES_MP_IMAGES": json.dumps({"games/starfall-engine": "hl-1-" + MAIN2[:12]}),
                "GAMES_MP_ROUTER_INPUTS": INPUTS_A, "GAMES_MP_SCHEMA_REVISION": "1"}))

    def test_the_game_id_and_size_rules_agree_in_all_three_files(self):
        bu = load("bringup", {})
        la = load("launch", {})
        for mod in (bu, la):
            self.assertEqual(mod.GAME_ID.pattern, self.r.GAME_ID.pattern)
            self.assertEqual(mod.RESERVED_GAME_IDS, self.r.RESERVED_GAME_IDS)
        self.assertEqual(bu.FARGATE_MEMORY, self.r.FARGATE_MEMORY)
        self.assertEqual(bu.DEFAULT_ENGINE_SIZE, self.r.DEFAULT_ENGINE_SIZE)
        self.assertEqual(bu.SIM_VERSION.pattern, self.r.SIM_VERSION.pattern)
        for game in ("mptest", "starfall", "starfall-arena", "mp-router", "engines", "preview-x", "x_y", "A", "ab"):
            self.assertEqual({bu.valid_game_id(game), la.valid_game_id(game)}, {self.r.valid_game_id(game)}, game)

    # -- main ---------------------------------------------------------------------------------

    def test_a_main_build_becomes_productions_current_release(self):
        out = self.invoke(self.main_built())
        self.assertEqual(out, {"promoted": MAIN, "router": False})
        [td] = self.ecs.registered
        self.assertEqual(td["family"], "games-mptest")
        c = td["containerDefinitions"][0]
        self.assertEqual(c["image"], REGISTRY + "/games/engines:mptest_mptest-1-" + MAIN[:12])
        self.assertEqual([e["name"] for e in c["environment"]], ["GAME_ID", "PORT", "MP_ENV", "MP_TOKEN_VERIFIER"])
        self.assertIn({"name": "MP_TOKEN_VERIFIER", "value": "ed25519-v2"}, c["environment"])
        self.assertEqual((td["executionRoleArn"], td["taskRoleArn"]),
                         (TEMPLATE["executionRoleArn"], TEMPLATE["taskRoleArn"]))
        self.assertIn({"key": "Project", "value": "games-multiplayer"}, td["tags"])
        self.assertEqual(set(td) - {"arn"}, {"family", "requiresCompatibilities", "networkMode", "cpu", "memory",
                                             "executionRoleArn", "taskRoleArn", "runtimePlatform",
                                             "containerDefinitions", "tags"})
        current = self.ddb.get("current#main")
        self.assertEqual(current["commit"], MAIN)
        self.assertEqual(current["games"]["mptest"], {"taskDefinition": TD_PREFIX + "games-mptest:1",
                                                      "simVersion": "mptest-1"})
        self.assertEqual(self.ddb.get("sim#mptest#mptest-1")["taskDefinition"], TD_PREFIX + "games-mptest:1")
        self.assertEqual(self.ddb.get("build#main#" + MAIN)["status"], "released")
        # The router's files did not change: no rollout, `live` untouched.
        self.assertEqual((self.ecs.updates, self.ecr.puts), (0, []))
        self.assertEqual(self.sns.sent, [])

    def test_an_older_main_build_never_replaces_a_newer_release(self):
        self.invoke(self.main_built(MAIN2, seq=2))
        out = self.invoke(self.main_built(MAIN, seq=1))
        self.assertEqual(out, {"superseded": MAIN})
        self.assertEqual(self.ddb.get("current#main")["commit"], MAIN2)
        self.assertEqual(self.ddb.get("sim#mptest#mptest-1")["commit"], MAIN2)
        # An administrator's explicit promote is a rollback, and wins.
        self.ddb.put("seq#main", seq=2)
        self.invoke({"action": "promote", "commit": MAIN})
        self.assertEqual(self.ddb.get("current#main")["commit"], MAIN)
        self.assertEqual(self.ddb.get("current#main")["seq"], 3)

    def test_an_older_sim_version_keeps_its_own_newest_revision(self):
        self.invoke(self.main_built(MAIN, seq=1, sim="mptest-0"))
        self.invoke(self.main_built(MAIN2, seq=2, sim="mptest-1"))
        self.assertEqual(self.ddb.get("sim#mptest#mptest-0")["commit"], MAIN)
        self.assertEqual(self.ddb.get("sim#mptest#mptest-1")["commit"], MAIN2)

    def test_changed_router_files_roll_the_router_and_wait_for_healthy_tasks(self):
        out = self.invoke(self.main_built(inputs=INPUTS_B))
        self.assertEqual(out, {"promoted": MAIN, "router": True})
        self.assertEqual(self.ecr.images[("games/mp-router", "live")], "sha256:router-" + MAIN[:4])
        self.assertEqual(self.ecs.updates, 1)
        self.assertEqual(self.ddb.get("router#main"), {"id": "router#main", "inputs": INPUTS_B, "tag": MAIN[:12],
                                                      "digest": "sha256:router-" + MAIN[:4]})
        self.assertIn('"router-rolled"', self.log.getvalue())
        # The same inputs again: nothing moves.
        self.invoke(self.main_built(MAIN2, seq=2, inputs=INPUTS_B))
        self.assertEqual(self.ecs.updates, 1)

    def test_a_failed_router_rollout_puts_live_back_and_alerts(self):
        self.ecs.roll_fails = True
        with self.assertRaisesRegex(self.r.ReleaseError, "FAILED"):
            self.invoke(self.main_built(inputs=INPUTS_B))
        self.assertEqual(self.ecr.images[("games/mp-router", "live")], "sha256:old")
        self.assertEqual(self.ddb.get("router#main")["inputs"], INPUTS_A, "the next release tries again")
        self.assertTrue(any("release failed" in s for s, _ in self.sns.sent))
        # Nothing promoted: new matches never run this commit's engines behind the old router.
        self.assertIsNone(self.ddb.get("current#main"))
        self.assertIsNone(self.ddb.get("sim#mptest#mptest-1"))

    def test_a_retry_after_live_moved_but_the_rollout_never_ran_still_rolls(self):
        event = self.main_built(inputs=INPUTS_B)
        self.ecr.images[("games/mp-router", "live")] = "sha256:router-" + MAIN[:4]   # moved, then it failed
        out = self.invoke(event)
        self.assertEqual(out, {"promoted": MAIN, "router": True})
        self.assertEqual(self.ecs.updates, 1, "the service still ran the old image")
        self.assertEqual(self.ecs.running_digest, "sha256:router-" + MAIN[:4])

    def test_a_first_release_at_the_running_router_only_records_it(self):
        self.ddb.items.pop("router#main")
        self.ecr.add("games/mp-router", MAIN[:12], "sha256:old")   # the same image as what runs
        event = self.main_built()
        self.ecr.images[("games/mp-router", MAIN[:12])] = "sha256:old"
        self.invoke(event)
        self.assertEqual(self.ecs.updates, 0)
        self.assertEqual(self.ddb.get("router#main")["inputs"], INPUTS_A)

    def test_a_schema_change_migrates_first_then_promotes(self):
        out = self.invoke(self.main_built(schema="2"))
        self.assertEqual(out, {"migrating": MAIN})
        self.assertIsNone(self.ddb.get("current#main"), "not promoted before its migration")
        [mig] = self.cb.started
        self.assertEqual(mig, {"project": "games-mp-migrate", "source": "games-mp-build-1/sources/main/%s.zip" % MAIN,
                               "env": {"GAMES_MP_COMMIT": MAIN}})
        out = self.invoke(self.cb.finish("games-mp-migrate:1", "games-mp-migrate", MAIN, {"GAMES_MP_DB_REVISION": "2"}))
        self.assertEqual(out["promoted"], MAIN)
        self.assertEqual(self.ddb.get("schema#main")["revision"], 2)
        self.assertEqual(len(self.ecs.registered), 1, "registered once, before the migration")

    def test_a_migration_that_ends_elsewhere_promotes_nothing(self):
        self.invoke(self.main_built(schema="2"))
        with self.assertRaisesRegex(self.r.ReleaseError, "revision 1"):
            self.invoke(self.cb.finish("games-mp-migrate:1", "games-mp-migrate", MAIN, {"GAMES_MP_DB_REVISION": "1"}))
        self.assertIsNone(self.ddb.get("current#main"))

    def test_failed_builds_are_recorded_and_only_main_alerts(self):
        self.ddb.put("build#preview#" + BRANCH, status="building")
        self.invoke(self.cb.finish("games-mp-images-preview:9", "games-mp-images-preview", BRANCH, {}, "FAILED"))
        self.assertEqual(self.ddb.get("build#preview#" + BRANCH)["status"], "failed")
        self.assertEqual(self.sns.sent, [])
        self.ddb.put("build#main#" + MAIN, status="building", seq=1)
        self.invoke(self.cb.finish("games-mp-images:9", "games-mp-images", MAIN, {}, "FAILED"))
        self.assertEqual(self.ddb.get("build#main#" + MAIN)["status"], "failed")
        self.assertEqual(len(self.sns.sent), 1)
        self.invoke(self.cb.finish("games-mp-migrate:9", "games-mp-migrate", MAIN, {}, "TIMED_OUT"))
        self.assertEqual(len(self.sns.sent), 2)
        self.assertIsNone(self.ddb.get("current#main"))

    def test_events_it_does_not_know_are_errors(self):
        for event in ({"source": "aws.codebuild", "detail": {"build-id": "other:1", "build-status": "SUCCEEDED"}},
                      {"action": "promote", "commit": "main"}, {"hello": 1}):
            with self.subTest(event=event):
                self.cb.finish("other:1", "some-other-project", MAIN, {})
                with self.assertRaises(self.r.ReleaseError):
                    self.invoke(event)


# ------------------------------------------------------------------------------------------
# Terraform
# ------------------------------------------------------------------------------------------


def block(text, kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n.*?^\}' % (re.escape(kind), re.escape(name)), text, re.S | re.M)
    if m is None:
        raise AssertionError("missing %s.%s" % (kind, name))
    return m.group()


def statement(policy, sid):
    at = re.search(r'Sid\s*=\s*"%s"' % re.escape(sid), policy)
    if at is None:
        raise AssertionError("no statement %s" % sid)
    begin = policy.rindex("{", 0, at.start())
    depth = 0
    for i in range(begin, len(policy)):
        depth += {"{": 1, "}": -1}.get(policy[i], 0)
        if depth == 0:
            return policy[begin:i + 1]
    raise AssertionError("unbalanced %s" % sid)


class TerraformTests(unittest.TestCase):
    def test_preview_builds_can_push_only_preview_images(self):
        policy = block(DEPLOY, "aws_iam_role_policy", "games_mp_codebuild_preview")
        self.assertIn("Resource = local.games_mp_preview_repo_arns", statement(policy, "PushPreviewImages"))
        # Exactly the shared preview engine repository: not the legacy per-game ones, never games/*.
        self.assertIn("games_mp_preview_repo_arns    = [aws_ecr_repository.games_mp[local.mp_engine_channels.preview.repository].arn]", DEPLOY)
        self.assertIn('games_mp_production_repo_arns = [aws_ecr_repository.games_mp[local.mp_engine_channels.main.repository].arn, aws_ecr_repository.games_mp["games/mp-router"].arn]', DEPLOY)
        for name in ("GAMES_MP_ENGINE_REPOSITORY", "GAMES_MP_MAX_CPU", "GAMES_MP_MAX_MEMORY"):
            self.assertIn(name, block(DEPLOY, "aws_codebuild_project", "games_mp_images_preview"))
            self.assertIn(name, block(BRINGUP_TF, "aws_codebuild_project", "games_mp_images"))
        self.assertEqual(len(re.findall(r"ecr:BatchDeleteImage", DEPLOY)), 1, "the preview role only")
        self.assertIn('"${aws_s3_bucket.games_mp_build.arn}/sources/preview/*"', policy)
        for forbidden in ("secretsmanager", "ecs:", "iam:", "games_mp_production_repo_arns", "sources/*", "sources/main"):
            self.assertNotIn(forbidden, policy)
        project = block(DEPLOY, "aws_codebuild_project", "games_mp_images_preview")
        self.assertRegex(project, r'name  = "GAMES_MP_CHANNEL"\n      value = "preview"')
        self.assertIn('buildspec = file("${path.module}/games-multiplayer/buildspec.yml")', project)

    def test_the_preview_launch_function_is_the_only_one_that_runs_preview_revisions(self):
        self.assertIn('preview = { family_prefix = "games-preview-", repository_prefix = "games-preview/", repository = "games-preview/engines" }', GAMES_TF)
        # Game ids are checked in code now (no list of games in Terraform): a preview-* id, which
        # would name another game's preview family, is refused by the build, the release and the
        # launch function alike (test_the_game_id_and_size_rules_agree_in_all_three_files).
        self.assertNotIn("local.mp_games", GAMES_TF + DEPLOY + BRINGUP_TF)

    def test_the_poller_reads_the_token_writes_claims_and_starts_image_builds_only(self):
        policy = block(DEPLOY, "aws_iam_role_policy", "games_mp_poller")
        self.assertIn('"dynamodb:LeadingKeys" = ["build#*", "seq#main"]', statement(policy, "ClaimBuilds"))
        self.assertIn("Resource = [aws_codebuild_project.games_mp_images.arn, aws_codebuild_project.games_mp_images_preview.arn]",
                      statement(policy, "StartImageBuilds"))
        self.assertNotIn("games_mp_migrate", policy)
        self.assertIn("Resource = aws_secretsmanager_secret.games_mp_github_read.arn",
                      statement(policy, "ReadTheGitHubReadToken"))
        fn = block(DEPLOY, "aws_lambda_function", "games_mp_poller")
        self.assertIn("reserved_concurrent_executions = 1", fn)
        schedule = block(DEPLOY, "aws_scheduler_schedule", "games_mp_poller")
        self.assertIn('state      = var.games_mp_autodeploy ? "ENABLED" : "DISABLED"', schedule)

    def test_the_release_role_moves_only_the_router_live_tag(self):
        policy = block(DEPLOY, "aws_iam_role_policy", "games_mp_release")
        move = statement(policy, "MoveTheRouterLiveTag")
        self.assertIn('Action   = ["ecr:BatchGetImage", "ecr:PutImage"]', move)
        self.assertIn('Resource = aws_ecr_repository.games_mp["games/mp-router"].arn', move)
        self.assertEqual(len(re.findall(r"ecr:PutImage", policy)), 1)
        self.assertIn("Resource = [aws_iam_role.games_engine_task.arn, aws_iam_role.games_engine_execution.arn]",
                      statement(policy, "PassOnlyEngineRoles"))
        self.assertIn('"aws:RequestTag/Project" = "games-multiplayer"', statement(policy, "RegisterEngineRevisions"))
        self.assertIn(":service/${local.mp_cluster_name}/mp-router", statement(policy, "RollTheRouter"))
        self.assertNotIn("ecs:RunTask", policy)
        # Every client call release.py makes has its grant.
        calls = set(re.findall(r'_client\("(\w+)"\)\.(\w+)\(', (GM / "release.py").read_text()))
        calls |= {("ecs", m) for m in re.findall(r"\becs\.(\w+)\(", (GM / "release.py").read_text())}
        calls |= {("ecr", m) for m in re.findall(r"\becr\.(\w+)\(", (GM / "release.py").read_text())}
        actions = {"dynamodb": "dynamodb", "codebuild": "codebuild", "ecs": "ecs", "ecr": "ecr",
                   "elbv2": "elasticloadbalancing", "sns": "sns"}
        for client, method in calls:
            action = "%s:%s" % (actions[client], "".join(w.title() for w in method.split("_")))
            self.assertIn(action, policy, action)

    def test_the_router_runs_the_live_tag_which_only_it_can_move(self):
        repo = block(GAMES_TF, "aws_ecr_repository", "games_mp")
        self.assertIn('image_tag_mutability = each.key == "games/mp-router" ? "IMMUTABLE_WITH_EXCLUSION" : "IMMUTABLE"', repo)
        self.assertIn('filter_type = "WILDCARD"', repo)
        lifecycle = block(GAMES_TF, "aws_ecr_lifecycle_policy", "games_mp")
        self.assertIn('tagStatus   = "untagged"', lifecycle, "production repositories never expire a tagged image")
        self.assertNotIn("imageCountMoreThan", lifecycle)
        self.assertIn("countNumber = 45", lifecycle)

    def test_builds_reach_the_release_function_and_failures_alert(self):
        rule = block(DEPLOY, "aws_cloudwatch_event_rule", "games_mp_builds")
        self.assertIn('source        = ["aws.codebuild"]', rule)
        for project in ("games_mp_images", "games_mp_images_preview", "games_mp_migrate"):
            self.assertIn("aws_codebuild_project.%s.name" % project, rule)
        fn = block(DEPLOY, "aws_lambda_function", "games_mp_release")
        self.assertIn("reserved_concurrent_executions = 1", fn)
        self.assertIn("SNS_TOPIC_ARN     = aws_sns_topic.cost_alerts.arn", fn)
        for alarm in ("games_mp_release_errors", "games_mp_poller_errors"):
            self.assertIn("alarm_actions       = [aws_sns_topic.cost_alerts.arn]",
                          block(DEPLOY, "aws_cloudwatch_metric_alarm", alarm))

    def test_the_database_url_is_readable_only_by_administration_and_the_migration(self):
        policy = block(DEPLOY, "aws_secretsmanager_secret_policy", "games_mp_db_url")
        self.assertIn("concat(local.games_mp_admin_principals, [aws_iam_role.games_mp_migrate.arn])", policy)
        migrate = block(DEPLOY, "aws_iam_role_policy", "games_mp_migrate")
        self.assertNotIn("ecr:", migrate)
        self.assertIn("GAMES_MP_DB_URL: games/mp-db-url", (GM / "buildspec-migrate.yml").read_text())
        self.assertIn('local.games_mp_db_url_secret   = "games/mp-db-url"'.split(".", 1)[1], DEPLOY)


if __name__ == "__main__":
    unittest.main(verbosity=2)

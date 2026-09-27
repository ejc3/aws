#!/usr/bin/env python3
"""App-repo runners (runner-app.tf, runner-app/): routing, launch, caps, reaping, bootstrap, front.

Runs the real runner-app/app_runner.py against fake EC2/SSM/Secrets Manager and a fake GitHub,
and the real front function (extracted from runner-webhook-front.tf) against a fake Lambda
client. Nothing here touches AWS or GitHub.
"""
import importlib.util
import json
import re
import subprocess
import sys
import textwrap
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "runner-app" / "app_runner.py"
BOOTSTRAP = ROOT / "runner-app" / "bootstrap.sh"
APP_TF = (ROOT / "runner-app.tf").read_text()
FRONT_TF = (ROOT / "runner-webhook-front.tf").read_text()
REPOS_TF = (ROOT / "runner-repos.tf").read_text()

COLTON, DOLPHIN = "CoderColton/colton-games", "dolphin-labs-hq/dolphin-labs"
SIZES = {"xl": ["c7a.16xlarge", "c7i.16xlarge"], "l": ["c7a.8xlarge", "c7i.8xlarge"], "s": ["c7a.2xlarge"]}
REPOS = {
    COLTON: {"label": "cc-games", "max": 3, "pat_secret": f"github-runner/repo-pat/{COLTON}", "sizes": SIZES},
    DOLPHIN: {"label": "dolphin", "max": 3, "pat_secret": f"github-runner/repo-pat/{DOLPHIN}", "sizes": SIZES},
}
SUBNETS = [{"subnet_id": "subnet-c", "availability_zone": "us-west-1c"},
           {"subnet_id": "subnet-a", "availability_zone": "us-west-1a"}]
ENV = {"REPOS": json.dumps(REPOS), "LAUNCH_SUBNETS": json.dumps(SUBNETS), "SECURITY_GROUP_ID": "sg-app",
       "INSTANCE_PROFILE": "github-runner-profile", "RUNNER_ACCOUNT_ID": "123456789012",
       "CLAIMS_TABLE": "github-app-runner-claims"}
NOW = datetime(2026, 9, 27, 16, 0, tzinfo=timezone.utc)


class Refused(Exception):
    """A botocore-style ClientError: carries the EC2 error code where launch() reads it."""
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeEC2:
    def __init__(self):
        self.instances, self.launched, self.terminated, self.refuse = [], [], [], set()
        self.next = 1

    def get_paginator(self, name):
        fake = self

        class P:
            def paginate(self, Filters):
                want = {f["Name"]: f["Values"] for f in Filters}
                out = [i for i in fake.instances
                       if all(any(t["Key"] == k[4:] and t["Value"] in v for t in i["Tags"])
                              for k, v in want.items() if k.startswith("tag:"))
                       and i["State"]["Name"] in want.get("instance-state-name", [i["State"]["Name"]])]
                return [{"Reservations": [{"Instances": out}]}]
        return P()

    def run_instances(self, **kw):
        self.attempts = getattr(self, "attempts", []) + [kw]
        if getattr(self, "ambiguous", False):
            raise TimeoutError("Read timeout on endpoint URL")
        if getattr(self, "refuse_code", None):
            raise Refused(self.refuse_code)
        if kw["InstanceType"] in self.refuse:
            raise Refused("InsufficientInstanceCapacity")
        iid = "i-%017x" % self.next
        self.next += 1
        tags = kw["TagSpecifications"][0]["Tags"]
        self.instances.append({"InstanceId": iid, "Tags": tags, "LaunchTime": NOW, "State": {"Name": "pending"}})
        self.launched.append(kw)
        return {"Instances": [{"InstanceId": iid}]}

    def terminate_instances(self, InstanceIds):
        self.terminated += InstanceIds


class FakeDynamo:
    """The launch-claim table: a conditional put that only one caller can win."""
    class exceptions:
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self):
        self.items, self.deleted, self.queries = {}, [], []

    def put_item(self, TableName, Item, ConditionExpression, ExpressionAttributeValues):
        key, t = (Item["repo"]["S"], Item["job"]["S"]), int(ExpressionAttributeValues[":now"]["N"])
        held = self.items.get(key)
        if held is not None and int(held["expires_at"]["N"]) >= t:
            raise self.exceptions.ConditionalCheckFailedException(key)
        self.items[key] = Item

    def query(self, TableName, ConsistentRead, KeyConditionExpression, ExpressionAttributeValues, **kw):
        self.queries.append(ConsistentRead)
        repo = ExpressionAttributeValues[":repo"]["S"]
        return {"Items": [item for (r, _), item in self.items.items() if r == repo]}

    def delete_item(self, TableName, Key):
        key = (Key["repo"]["S"], Key["job"]["S"])
        self.deleted.append(key)
        self.items.pop(key, None)


class FakeCloudWatch:
    def __init__(self):
        self.metrics = []

    def put_metric_data(self, Namespace, MetricData):
        self.metrics.append((Namespace, MetricData))


class FakeSSM:
    class exceptions:
        ParameterNotFound = KeyError

    def __init__(self):
        self.put, self.deleted, self.fail_put = [], [], False

    def get_parameter(self, Name, WithDecryption=False):
        assert Name.startswith("/aws/service/canonical/"), Name
        return {"Parameter": {"Value": "ami-canonical"}}

    def put_parameter(self, **kw):
        if self.fail_put:
            raise RuntimeError("ssm down")
        self.put.append(kw)

    def delete_parameter(self, Name):
        self.deleted.append(Name)


class FakeSecrets:
    def __init__(self, values):
        self.values, self.read = values, []

    def get_secret_value(self, SecretId):
        self.read.append(SecretId)
        if SecretId not in self.values:
            raise Refused("ResourceNotFoundException")
        if self.values[SecretId] is PermissionError:
            raise Refused("AccessDeniedException")
        return {"SecretString": self.values[SecretId]}


class FakeGitHub:
    def __init__(self):
        self.calls, self.runs, self.jobs, self.runners = [], {}, {}, {}

    def __call__(self, method, path, pat, body=None):
        self.calls.append((method, path, pat))
        if path.endswith("/actions/runners/registration-token"):
            return {"token": "REGTOKEN", "expires_at": "2026-09-27T17:00:00Z"}
        m = re.match(r"/repos/(.+?)/actions/runs\?status=(\w+)", path)
        if m:
            return {"workflow_runs": [{"id": r} for r in self.runs.get((m.group(1), m.group(2)), [])]}
        m = re.match(r"/repos/(.+?)/actions/runs/(\d+)/jobs", path)
        if m:
            return {"jobs": self.jobs.get(int(m.group(2)), [])}
        m = re.match(r"/repos/(.+?)/actions/runners\?", path)
        if m:
            return {"runners": list(self.runners.get(m.group(1), {}).values())}
        return {}


def load_app(tokens=None):
    ec2, ssm, dynamo, cw = FakeEC2(), FakeSSM(), FakeDynamo(), FakeCloudWatch()
    secrets = FakeSecrets(tokens if tokens is not None else {
        f"github-runner/repo-pat/{COLTON}": "PAT-COLTON", f"github-runner/repo-pat/{DOLPHIN}": "PAT-DOLPHIN"})
    fake_boto3 = types.ModuleType("boto3")
    def client(name, region_name=None, config=None):
        if name == "ec2" and config is not None:
            ec2.launch_config = config
        return {"ec2": ec2, "ssm": ssm, "secretsmanager": secrets, "dynamodb": dynamo, "cloudwatch": cw}[name]
    fake_boto3.client = client
    sys.modules["boto3"] = fake_boto3
    fake_botocore, fake_config = types.ModuleType("botocore"), types.ModuleType("botocore.config")
    fake_config.Config = lambda **kw: types.SimpleNamespace(**kw)
    fake_botocore.config = fake_config
    sys.modules["botocore"], sys.modules["botocore.config"] = fake_botocore, fake_config
    for k, v in ENV.items():
        __import__("os").environ[k] = v
    spec = importlib.util.spec_from_file_location("app_runner_under_test", APP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.github = FakeGitHub()
    mod.now = lambda: NOW
    mod.fake_dynamo, mod.fake_cloudwatch = dynamo, cw
    return mod, ec2, ssm, secrets


def instance(iid, repo, job, age_min, size="xl"):
    return {"InstanceId": iid, "LaunchTime": NOW - timedelta(minutes=age_min), "State": {"Name": "running"},
            "Tags": [{"Key": "Role", "Value": "github-app-runner"}, {"Key": "Repo", "Value": repo},
                     {"Key": "JobId", "Value": job}, {"Key": "Size", "Value": size}]}


class RoutingTests(unittest.TestCase):
    def test_a_job_is_ours_only_if_every_label_is_one_our_runners_carry(self):
        app, *_ = load_app()
        cfg = REPOS[COLTON]
        self.assertEqual(app.job_size(cfg, ["self-hosted", "cc-games", "xl"]), "xl")
        self.assertEqual(app.job_size(cfg, ["Self-Hosted", "Linux", "X64", "CC-GAMES", "s"]), "s")
        for labels in (["self-hosted", "ARM64"], ["self-hosted", "cc-games"], ["self-hosted", "cc-games", "xl", "l"],
                       ["self-hosted", "cc-games", "xl", "gpu"], ["self-hosted", "dolphin", "xl"], ["ubuntu-latest"]):
            self.assertIsNone(app.job_size(cfg, labels), labels)


class LaunchTests(unittest.TestCase):
    def deliver(self, app, repo=COLTON, labels=("self-hosted", "cc-games", "xl"), job=7, action="queued"):
        return app.handler({"repo": repo, "action": action, "workflow_job": {"id": job, "labels": list(labels)}}, None)

    def test_a_queued_job_gets_one_host_from_its_size_pool_with_its_repos_token(self):
        app, ec2, ssm, secrets = load_app()
        result = self.deliver(app)
        self.assertEqual(result["outcome"], "launched")
        (kw,) = ec2.launched
        self.assertEqual(kw["InstanceType"], "c7a.16xlarge")
        self.assertEqual(kw["ImageId"], "ami-canonical")
        self.assertEqual(kw["InstanceInitiatedShutdownBehavior"], "terminate")
        self.assertEqual(kw["InstanceMarketOptions"]["MarketType"], "spot")
        self.assertEqual(kw["NetworkInterfaces"][0]["Groups"], ["sg-app"])
        tags = {t["Key"]: t["Value"] for t in kw["TagSpecifications"][0]["Tags"]}
        self.assertEqual((tags["Role"], tags["Repo"], tags["JobId"], tags["Size"]), ("github-app-runner", COLTON, "7", "xl"))
        self.assertIn(("POST", f"/repos/{COLTON}/actions/runners/registration-token", "PAT-COLTON"), app.github.calls)
        self.assertEqual(secrets.read, [f"github-runner/repo-pat/{COLTON}"])
        (put,) = ssm.put
        self.assertEqual(put["Name"], f"/github-runner/bootstrap/{ec2.instances[0]['InstanceId']}")
        self.assertIn({"Key": "InstanceArn", "Value": f"arn:aws:ec2:us-west-1:123456789012:instance/{ec2.instances[0]['InstanceId']}"}, put["Tags"])
        self.assertIn(f"REPO='{COLTON}'", kw["UserData"])
        self.assertIn("LABELS='cc-games,xl'", kw["UserData"])
        self.assertNotIn("REGTOKEN", kw["UserData"], "the registration token must never be in user data")

    def test_a_redelivered_job_launches_nothing(self):
        app, ec2, *_ = load_app()
        ec2.instances.append(instance("i-existing", COLTON, "7", 1))
        self.assertEqual(self.deliver(app)["outcome"], "exists")
        self.assertEqual(ec2.launched, [])

    def test_a_repo_at_its_cap_waits(self):
        app, ec2, *_ = load_app()
        ec2.instances += [instance(f"i-{n}", COLTON, str(100 + n), 1) for n in range(3)]
        self.assertEqual(self.deliver(app)["outcome"], "cap")
        self.assertEqual(ec2.launched, [])

    def test_caps_are_per_repo(self):
        app, ec2, *_ = load_app()
        ec2.instances += [instance(f"i-{n}", DOLPHIN, str(100 + n), 1) for n in range(3)]
        self.assertEqual(self.deliver(app)["outcome"], "launched")

    def test_a_refused_pool_falls_through_to_the_next(self):
        app, ec2, *_ = load_app()
        ec2.refuse = {"c7a.16xlarge"}
        self.assertEqual(self.deliver(app)["outcome"], "launched")
        self.assertEqual(ec2.launched[0]["InstanceType"], "c7i.16xlarge")

    def test_an_ambiguous_launch_error_tries_no_other_pool(self):
        # EC2 may have created an instance behind a timeout; another pool could be a duplicate.
        app, ec2, *_ = load_app()
        ec2.ambiguous = True
        self.assertNotEqual(self.deliver(app)["outcome"], "launched")
        self.assertEqual(len(ec2.attempts), 1, "an ambiguous error must not fall through to another pool")

    def test_every_launch_attempt_carries_its_own_client_token(self):
        app, ec2, *_ = load_app()
        ec2.refuse = {"c7a.16xlarge"}
        self.deliver(app)
        tokens = [kw.get("ClientToken") for kw in ec2.attempts]
        self.assertTrue(all(tokens), "RunInstances without a ClientToken is not idempotent under SDK retries")
        self.assertEqual(len(set(tokens)), len(tokens))

    def test_a_redelivery_before_the_host_is_listed_launches_nothing(self):
        # DescribeInstances is eventually consistent: hide the first host from the listing and
        # redeliver. The strongly consistent claim must still stop a second launch.
        app, ec2, *_ = load_app()
        self.assertEqual(self.deliver(app)["outcome"], "launched")
        ec2.instances.clear()
        self.assertEqual(self.deliver(app)["outcome"], "claimed")
        self.assertEqual(len(ec2.launched), 1)

    def test_a_definite_failure_releases_the_claim_and_an_ambiguous_one_keeps_it(self):
        app, ec2, *_ = load_app()
        ec2.refuse = {t for size in SIZES.values() for t in size}
        self.assertEqual(self.deliver(app)["outcome"], "failed")
        self.assertEqual(len(app.fake_dynamo.deleted), 1, "every pool refused: the claim must go so the job retries")
        ec2.refuse = set()
        ec2.ambiguous = True
        self.assertEqual(self.deliver(app)["outcome"], "failed")
        self.assertEqual(len(app.fake_dynamo.deleted), 1, "an ambiguous error may have launched: keep the claim")
        ec2.ambiguous = False
        self.assertEqual(self.deliver(app)["outcome"], "claimed")

    def test_launches_use_a_client_with_sdk_retries_off(self):
        # A retried InsufficientInstanceCapacity cost 7-15 s per pool on the metal controller.
        app, ec2, *_ = load_app()
        self.assertEqual(ec2.launch_config.retries, {"total_max_attempts": 1})

    def test_a_failure_before_any_launch_request_releases_the_claim(self):
        app, ec2, *_ = load_app()
        real = app.registration_token
        app.registration_token = lambda repo, pat: (_ for _ in ()).throw(RuntimeError("GitHub 503"))
        with self.assertRaises(RuntimeError):
            self.deliver(app)
        self.assertEqual(ec2.launched, [])
        self.assertEqual(len(app.fake_dynamo.deleted), 1, "nothing launched: the job must not wait out the claim")
        app.registration_token = real
        self.assertEqual(self.deliver(app)["outcome"], "launched")

    def test_an_unreadable_token_fails_loudly_but_a_missing_one_skips(self):
        app, *_ = load_app(tokens={f"github-runner/repo-pat/{DOLPHIN}": PermissionError})
        self.assertIsNone(app.repo_pat(REPOS[COLTON]), "no value yet is the expected bootstrap state")
        with self.assertRaises(Refused):
            app.repo_pat(REPOS[DOLPHIN])

    def test_the_cap_holds_in_a_burst_the_listing_has_not_caught_up_with(self):
        # Distinct jobs, each redelivered after the listing "lost" the previous launches: the
        # consistent claims still count them against the repo's cap (3 for dolphin here).
        app, ec2, *_ = load_app()
        outcomes = []
        for job in range(1, 6):
            outcomes.append(self.deliver(app, repo=DOLPHIN, labels=("self-hosted", "dolphin", "l"), job=job)["outcome"])
            ec2.instances.clear()
        self.assertEqual(outcomes, ["launched"] * 3 + ["cap"] * 2)
        self.assertTrue(all(app.fake_dynamo.queries), "the cap must be counted with a consistent read")

    def test_a_definite_launch_refusal_is_raised_and_frees_the_job(self):
        app, ec2, *_ = load_app()
        ec2.refuse_code = "UnauthorizedOperation"
        with self.assertRaises(app.LaunchRefused):
            self.deliver(app)
        self.assertEqual(len(app.fake_dynamo.deleted), 1)
        self.assertEqual(len(ec2.attempts), 1, "a definite refusal must not be tried on every other pool")

    def test_throttling_is_ambiguous_and_keeps_the_claim(self):
        app, ec2, *_ = load_app()
        ec2.refuse_code = "RequestLimitExceeded"
        self.assertEqual(self.deliver(app)["outcome"], "failed")
        self.assertEqual(app.fake_dynamo.deleted, [])

    def test_a_finished_host_frees_its_cap_slot_at_once(self):
        # Three short jobs launched and finished (listed as terminated) inside the claim window:
        # their claims must not keep the repo "full".
        app, ec2, *_ = load_app()
        launched = []
        for job in (1, 2, 3):
            # The listing has not caught up with any of them yet, so all three claims stand.
            self.assertEqual(self.deliver(app, repo=DOLPHIN, labels=("self-hosted", "dolphin", "l"), job=job)["outcome"], "launched")
            launched += ec2.instances
            ec2.instances = []
        self.assertEqual(len(app.fake_dynamo.items), 3)
        # Then they show up, already finished and terminated (a short job).
        for i in launched:
            i["State"]["Name"] = "terminated"
        ec2.instances = launched
        self.assertEqual(self.deliver(app, repo=DOLPHIN, labels=("self-hosted", "dolphin", "l"), job=4)["outcome"], "launched",
                         "finished hosts' claims kept the repo at its cap")
        self.assertEqual(len(app.fake_dynamo.deleted), 3, "listed launches release their claims")

    def test_a_failed_cleanup_after_launch_keeps_the_claim(self):
        app, ec2, ssm, _ = load_app()
        ssm.fail_put = True
        ec2.terminate_instances = lambda InstanceIds: (_ for _ in ()).throw(TimeoutError("terminate timed out"))
        self.assertEqual(self.deliver(app)["outcome"], "failed")
        self.assertEqual(app.fake_dynamo.deleted, [], "the host may be alive: releasing would allow a duplicate")

    def test_the_queue_scan_reads_every_page(self):
        app, *_ = load_app()
        pages_seen = []
        def github(method, path, pat, body=None):
            pages_seen.append(path)
            page = int(path.rsplit("page=", 1)[1])
            if "/jobs" in path:
                return {"jobs": [{"id": int(path.split("/runs/")[1].split("/")[0]) * 1000 + page,
                                  "status": "queued", "labels": ["self-hosted", "dolphin", "l"]}]}
            # Newest first, as GitHub lists them: pages 1-2 are 200 newer runs, page 3 the oldest.
            if "status=queued" in path and page <= 2:
                return {"workflow_runs": [{"id": page * 100 + n, "created_at": f"2026-09-27T1{page}:00:{n:02d}Z"}
                                          for n in range(100)]}
            if "status=queued" in path:
                return {"workflow_runs": [{"id": 999, "created_at": "2026-09-27T09:00:00Z"}]}
            return {"workflow_runs": []}
        app.github = github
        jobs = app.queued_jobs(DOLPHIN, REPOS[DOLPHIN], "PAT")
        self.assertIn("999001", [j for j, _ in jobs], "the oldest run, on page 3, was never inspected")
        jobs_calls = [p for p in pages_seen if "/jobs" in p]
        self.assertIn("/runs/999/", jobs_calls[0], "the oldest run must be inspected first")

    def test_a_single_job_whose_host_died_is_relaunched_without_waiting_out_its_claim(self):
        app, ec2, *_ = load_app()
        self.assertEqual(self.deliver(app)["outcome"], "launched")
        ec2.instances[0]["State"]["Name"] = "terminated"   # the host failed; the job is still queued
        self.assertEqual(self.deliver(app)["outcome"], "launched",
                         "a listed, terminated host's claim must not hold its own job")

    def test_a_scan_stops_at_its_time_share(self):
        app, *_ = load_app()
        calls = []
        app.github = lambda method, path, pat, body=None: calls.append(path) or {"workflow_runs": [], "jobs": []}
        app.DEADLINE[0] = 0   # this repo's share is already spent
        self.assertEqual(app.queued_jobs(DOLPHIN, REPOS[DOLPHIN], "PAT"), [])
        self.assertEqual(calls, [], "no GitHub call may start after the repo's deadline")

    def test_a_failed_credential_handoff_terminates_and_tries_nothing_else(self):
        app, ec2, ssm, _ = load_app()
        ssm.fail_put = True
        self.assertEqual(self.deliver(app)["outcome"], "failed")
        self.assertEqual(len(ec2.launched), 1)
        self.assertEqual(ec2.terminated, [ec2.instances[0]["InstanceId"]])

    def test_other_repos_completions_and_foreign_labels_are_ignored(self):
        app, ec2, *_ = load_app()
        self.assertEqual(self.deliver(app, repo="ejc3/fcvm"), {"ignored": "repo"})
        self.assertEqual(self.deliver(app, action="completed"), {"ignored": "action"})
        self.assertEqual(self.deliver(app, labels=("self-hosted", "ARM64")), {"ignored": "labels"})
        self.assertEqual(ec2.launched, [])

    def test_no_token_yet_launches_nothing(self):
        app, ec2, *_ = load_app(tokens={})
        self.assertEqual(self.deliver(app), {"skipped": "no controller token"})
        self.assertEqual(ec2.launched, [])


class ReconcileTests(unittest.TestCase):
    def test_launches_only_for_queued_jobs_with_no_host(self):
        app, ec2, *_ = load_app()
        gh = app.github
        gh.runs[(COLTON, "in_progress")] = [1]
        gh.jobs[1] = [{"id": 11, "status": "queued", "labels": ["self-hosted", "cc-games", "xl"]},
                      {"id": 12, "status": "queued", "labels": ["self-hosted", "cc-games", "l"]},
                      {"id": 13, "status": "queued", "labels": ["ubuntu-latest"]},
                      {"id": 14, "status": "in_progress", "labels": ["self-hosted", "cc-games", "xl"]}]
        ec2.instances.append(instance("i-covering12", COLTON, "12", 1, size="l"))
        app.handler({"reconcile": True}, None)
        launched = [{t["Key"]: t["Value"] for t in kw["TagSpecifications"][0]["Tags"]}["JobId"] for kw in ec2.launched]
        self.assertEqual(launched, ["11"])

    def test_one_repos_failure_does_not_stop_the_other_and_counts_are_published(self):
        app, *_ = load_app()
        real = app.reconcile
        def flaky(repo, cfg, subnets):
            if repo == COLTON:
                raise RuntimeError("HTTP Error 401: Bad credentials")
            return real(repo, cfg, subnets)
        app.reconcile = flaky
        with self.assertRaisesRegex(RuntimeError, "reconcile failed for CoderColton/colton-games"):
            app.handler({"source": "aws.events"}, None)
        namespace, data = app.fake_cloudwatch.metrics[-1]
        self.assertEqual(namespace, "GitHubAppRunner")
        repos = {d["Dimensions"][0]["Value"] for d in data}
        self.assertIn(DOLPHIN, repos, "the healthy repo was still reconciled and counted")
        self.assertIn("ALL", repos)

    def test_reaps_unregistered_idle_and_overage_hosts_and_offline_ghosts(self):
        app, ec2, *_ = load_app()
        ec2.instances += [instance("i-booting", COLTON, "1", 3), instance("i-neverup", COLTON, "2", 11),
                          instance("i-idle", COLTON, "3", 12), instance("i-busy", COLTON, "4", 60),
                          instance("i-ancient", COLTON, "5", 200)]
        label = [{"name": "cc-games"}]
        app.github.runners[COLTON] = {
            "i-idle": {"id": 31, "name": "i-idle", "busy": False, "status": "online", "labels": label},
            "i-busy": {"id": 32, "name": "i-busy", "busy": True, "status": "online", "labels": label},
            "i-ancient": {"id": 33, "name": "i-ancient", "busy": True, "status": "online", "labels": label},
            "i-gone": {"id": 34, "name": "i-gone", "busy": False, "status": "offline", "labels": label},
            "i-notours": {"id": 35, "name": "i-notours", "busy": False, "status": "offline", "labels": [{"name": "other"}]},
        }
        app.handler({"reconcile": True}, None)
        self.assertEqual(sorted(ec2.terminated), ["i-ancient", "i-idle", "i-neverup"])
        deleted = sorted(p for m, p, _ in app.github.calls if m == "DELETE")
        self.assertEqual(deleted, [f"/repos/{COLTON}/actions/runners/{n}" for n in (31, 33, 34)])


class BootstrapTests(unittest.TestCase):
    def test_bootstrap_is_ephemeral_verified_and_keeps_the_token_off_argv(self):
        text = BOOTSTRAP.read_text()
        subprocess.run(["bash", "-n", str(BOOTSTRAP)], check=True)
        self.assertIn("trap 'sync; shutdown -h now' EXIT", text)
        self.assertIn('sha256sum -c -', text)
        self.assertRegex(text, r"RUNNER_SHA256='[0-9a-f]{64}'")
        self.assertIn("--ephemeral", text)
        self.assertIn("ACTIONS_RUNNER_INPUT_TOKEN=$(cat) exec ./config.sh", text)
        self.assertNotIn('--token "$t"', text, "the registration token must never be on config.sh argv")
        self.assertIn("ssm.delete_parameter(Name=name)", text)
        self.assertIn("@@REPO@@", text)
        self.assertIn("@@LABELS@@", text)


def front_source():
    block = re.search(r'data "archive_file" "runner_webhook_front" \{.*?content\s*=\s*<<-EOF\n(.*?)\n\s*EOF\n', FRONT_TF, re.S)
    return textwrap.dedent(block.group(1))


class FakeLambda:
    def __init__(self):
        self.calls = []

    def invoke(self, **kw):
        self.calls.append(kw)


def load_front():
    client = FakeLambda()
    fake_boto3 = types.ModuleType("boto3")
    fake_boto3.client = lambda name, region_name=None: client
    sys.modules["boto3"] = fake_boto3
    import os
    os.environ.update({"WEBHOOK_SECRET": "s", "DELIVERY_TARGET": "arn:delivery", "FCVM_REPO": "ejc3/fcvm",
                       "APP_REPOS": json.dumps([COLTON, DOLPHIN]), "APP_DELIVERY_TARGET": "arn:app"})
    ns = {}
    exec(compile(front_source(), "front", "exec"), ns)
    return ns["handler"], client


def signed(payload):
    import hashlib
    import hmac
    body = json.dumps(payload)
    return {"body": body, "headers": {"X-Hub-Signature-256": "sha256=" + hmac.new(b"s", body.encode(), hashlib.sha256).hexdigest(),
                                      "X-GitHub-Delivery": "d1"}}


class FrontTests(unittest.TestCase):
    JOB = {"id": 9, "run_id": 5, "labels": ["self-hosted", "cc-games", "xl"], "runner_name": None}

    def test_fcvm_and_repo_less_deliveries_take_exactly_the_old_path(self):
        handler, client = load_front()
        handler(signed({"action": "queued", "workflow_job": self.JOB}), None)
        handler(signed({"action": "queued", "workflow_job": self.JOB, "repository": {"full_name": "ejc3/fcvm"}}), None)
        self.assertEqual([c["FunctionName"] for c in client.calls], ["arn:delivery", "arn:delivery"])
        a, b = (json.loads(c["Payload"]) for c in client.calls)
        a["delivery"].pop("received_at"), b["delivery"].pop("received_at")
        self.assertEqual(a, b, "naming ejc3/fcvm must not change what its webhook receives")

    def test_served_repos_go_to_the_app_runner_queued_only(self):
        handler, client = load_front()
        r = handler(signed({"action": "queued", "workflow_job": self.JOB, "repository": {"full_name": "codercolton/Colton-Games"}}), None)
        self.assertEqual(r["statusCode"], 202)
        (call,) = client.calls
        self.assertEqual(call["FunctionName"], "arn:app")
        event = json.loads(call["Payload"])
        self.assertEqual((event["repo"], event["action"]), (COLTON, "queued"))
        self.assertEqual(event["workflow_job"], {"id": 9, "run_id": 5, "labels": ["self-hosted", "cc-games", "xl"]})
        done = handler(signed({"action": "completed", "workflow_job": self.JOB, "repository": {"full_name": COLTON}}), None)
        self.assertEqual(done["statusCode"], 200)
        self.assertEqual(len(client.calls), 1)

    def test_other_repositories_are_dropped(self):
        handler, client = load_front()
        r = handler(signed({"action": "queued", "workflow_job": self.JOB, "repository": {"full_name": "someone/else"}}), None)
        self.assertEqual(r["statusCode"], 200)
        self.assertEqual(client.calls, [])


class WiringTests(unittest.TestCase):
    def test_pools_are_x86_only_and_diversified(self):
        sizes = re.search(r"runner_app_sizes = \{(.*?)\n  \}", APP_TF, re.S).group(1)
        for size, types_ in re.findall(r"(\w+)\s*=\s*\[([^\]]+)\]", sizes):
            names = re.findall(r'"([^"]+)"', types_)
            self.assertGreaterEqual(len({n.split(".")[0] for n in names}), 4, size)
            self.assertFalse([n for n in names if re.match(r"^[a-z]\d+g", n)], f"{size} includes arm64 (Graviton)")
            self.assertFalse([n for n in names if "metal" in n], size)

    def test_served_repos_match_their_tokens_and_the_front(self):
        served = re.findall(r'^\s+"([^"]+/[^"]+)"\s*=\s*\{ label', APP_TF, re.M)
        tokens = re.findall(r'"([^"]+/[^"]+)"', re.search(r"runner_extra_repos = \[([^\]]+)\]", REPOS_TF).group(1))
        self.assertEqual(sorted(served), sorted(tokens))
        self.assertIn("APP_REPOS           = jsonencode(keys(local.runner_app_repos))", FRONT_TF)

    def test_security_group_has_no_inbound_and_the_controller_is_serialized(self):
        sg = re.search(r'resource "aws_security_group" "runner_app" \{.*?\n\}', APP_TF, re.S).group()
        self.assertNotIn("ingress", sg)
        fn = re.search(r'resource "aws_lambda_function" "runner_app" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn("reserved_concurrent_executions = 1", fn)

    def test_controller_touches_only_app_runners(self):
        policy = re.search(r'resource "aws_iam_role_policy" "runner_app_lambda" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn('"aws:ResourceTag/Role" = "github-app-runner"', policy)
        self.assertNotIn('"aws:ResourceTag/Role" = "github-runner"\n', policy.split("TerminateAppRunnersOnly")[1].split("}")[0])
        self.assertIn('"ec2:Owner" = local.runner_app_ami_owner', policy)
        self.assertNotRegex(policy, r'Action\s*=\s*"ec2:\*"')
        self.assertIn("aws_secretsmanager_secret.github_runner_repo_pat[repo].arn", policy)


if __name__ == "__main__":
    unittest.main()

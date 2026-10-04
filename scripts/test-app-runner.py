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
import tempfile
import textwrap
import time
import types
import unittest
import urllib.error
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
       "INSTANCE_PROFILE": "github-app-runner-profile", "RUNNER_ACCOUNT_ID": "123456789012",
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
        if not getattr(self, "listing_lags", False):
            self.instances.append({"InstanceId": iid, "Tags": tags, "LaunchTime": NOW, "State": {"Name": "pending"},
                                   "ClientToken": kw["ClientToken"]})
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
        self.calls, self.runs, self.jobs, self.runners, self.job_status = [], {}, {}, {}, {}

    def __call__(self, method, path, pat, body=None):
        self.calls.append((method, path, pat))
        if path.endswith("/actions/runners/registration-token"):
            return {"token": "REGTOKEN", "expires_at": "2026-09-27T17:00:00Z"}
        m = re.match(r"/repos/(.+?)/actions/runs\?status=(\w+)", path)
        if m:
            runs = self.runs.get((m.group(1), m.group(2)), [])
            return {"total_count": len(runs), "workflow_runs": [{"id": r} for r in runs]}
        m = re.match(r"/repos/(.+?)/actions/jobs/(\d+)$", path)
        if m:
            return {"id": int(m.group(2)), "status": self.job_status.get(int(m.group(2)), "queued")}
        m = re.match(r"/repos/(.+?)/actions/runs/(\d+)/jobs", path)
        if m:
            return {"jobs": self.jobs.get(int(m.group(2)), [])}
        m = re.match(r"/repos/(.+?)/actions/runners\?(?:name=([^&]+))?", path)
        if m:
            runners = list(self.runners.get(m.group(1), {}).values())
            if m.group(2):
                runners = [r for r in runners if r.get("name") == m.group(2)]
            return {"runners": runners}
        return {}


class Hooked:
    """A fake client behind botocore's before-call event: every public method fires the
    registered handlers first, as a real client does for every operation."""
    def __init__(self, fake):
        hooks = []
        object.__setattr__(self, "_fake", fake)
        object.__setattr__(self, "_hooks", hooks)
        object.__setattr__(self, "meta", types.SimpleNamespace(
            events=types.SimpleNamespace(register=lambda event, fn: hooks.append(fn))))

    def __getattr__(self, name):
        attr = getattr(self._fake, name)
        if name.startswith("_") or isinstance(attr, type) or not callable(attr):
            return attr
        def call(*args, **kwargs):
            for hook in self._hooks:
                hook(event_name=f"before-call.{name}")
            return attr(*args, **kwargs)
        return call

    def __setattr__(self, name, value):
        setattr(self._fake, name, value)


def load_app(tokens=None):
    ec2, ssm, dynamo, cw = FakeEC2(), FakeSSM(), FakeDynamo(), FakeCloudWatch()
    secrets = FakeSecrets(tokens if tokens is not None else {
        f"github-runner/repo-pat/{COLTON}": "PAT-COLTON", f"github-runner/repo-pat/{DOLPHIN}": "PAT-DOLPHIN"})
    fake_boto3 = types.ModuleType("boto3")
    configs = []
    def client(name, region_name=None, config=None):
        configs.append((name, config))
        if name == "ec2" and config is not None and config.retries.get("total_max_attempts") == 1:
            ec2.launch_config = config
        return Hooked({"ec2": ec2, "ssm": ssm, "secretsmanager": secrets, "dynamodb": dynamo, "cloudwatch": cw}[name])
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
    mod.real_github, mod.github = mod.github, FakeGitHub()
    mod.now = lambda: NOW
    mod.fake_dynamo, mod.fake_cloudwatch, mod.client_configs = dynamo, cw, configs
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
        self.assertIn({"Key": "Fleet", "Value": "github-app-runner"}, put["Tags"])
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

    def test_every_aws_client_has_bounded_timeouts(self):
        app, *_ = load_app()
        self.assertEqual(len(app.client_configs), 6)
        for name, config in app.client_configs:
            self.assertIsNotNone(config, f"{name}: botocore's default 60 s reads can eat the publishing reserve")
            self.assertLessEqual(config.connect_timeout, 5, name)
            self.assertLessEqual(config.read_timeout, 20, name)
            self.assertLessEqual(config.retries["total_max_attempts"], 2, name)

    def test_a_repo_whose_share_is_already_spent_starts_no_call(self):
        app, ec2, *_ = load_app()
        app.DEADLINE[0] = 0
        read = []
        app.repo_pat = lambda cfg: read.append(cfg) or "PAT"
        self.assertEqual(app.reconcile(DOLPHIN, REPOS[DOLPHIN], []), {"repo": DOLPHIN, "skipped": "no time left"})
        self.assertEqual(read, [])
        self.assertEqual(app.github.calls, [])

    def test_pool_probing_stops_at_the_deadline_and_frees_the_job(self):
        app, ec2, *_ = load_app()
        ec2.refuse = {t for size in SIZES.values() for t in size}
        real = ec2.run_instances
        def slow(**kw):
            app.DEADLINE[0] = 0   # this refusal came back after the repo's share ended
            return real(**kw)
        ec2.run_instances = slow
        self.assertEqual(self.deliver(app)["outcome"], "failed")
        self.assertEqual(len(ec2.attempts), 1, "no pool may be tried after the deadline")
        self.assertEqual(len(app.fake_dynamo.deleted), 1, "nothing launched: the job must not wait out its claim")

    def test_no_launch_attempt_starts_without_time_for_one_to_finish(self):
        app, ec2, *_ = load_app()
        app.DEADLINE[0] = time.monotonic() + app.LAUNCH_ATTEMPT_SECONDS - 3
        outcome, _ = app.ensure_runner(COLTON, REPOS[COLTON], app.config()[1], "PAT", "7", "xl", [])
        self.assertEqual(outcome, "failed")
        self.assertEqual(getattr(ec2, "attempts", []), [])
        self.assertEqual(len(app.fake_dynamo.deleted), 1, "nothing launched: the claim goes")

    def test_the_reserve_outlasts_a_launch_handoff(self):
        app, *_ = load_app()
        # A failed handoff past the deadline (credential write, terminate, claim release) plus publishing.
        self.assertGreaterEqual(app.RESERVE_SECONDS, 4 * (3 + 8), "a failed handoff after the last attempt must fit")

    def test_a_failed_handoff_past_the_deadline_terminates_but_skips_the_tidy_up(self):
        app, ec2, ssm, _ = load_app()
        ssm.fail_put = True
        app.DEADLINE[0] = time.monotonic() + 100
        real = ec2.run_instances
        def accepted_late(**kw):
            out = real(**kw)
            app.DEADLINE[0] = 0
            return out
        ec2.run_instances = accepted_late
        outcome, _ = app.ensure_runner(COLTON, REPOS[COLTON], app.config()[1], "PAT", "7", "xl", [])
        self.assertEqual(outcome, "failed")
        self.assertEqual(ec2.terminated, [ec2.instances[0]["InstanceId"]])
        self.assertEqual(ssm.deleted, [], "the parameter tidy-up must not run past the deadline")

    def test_a_subnet_out_of_addresses_falls_through_to_the_next_pool(self):
        app, ec2, *_ = load_app()
        calls = []
        real = ec2.run_instances
        def full_first_subnet(**kw):
            calls.append(kw["NetworkInterfaces"][0]["SubnetId"])
            if len(calls) == 1:
                raise Refused("InsufficientFreeAddressesInSubnet")
            return real(**kw)
        ec2.run_instances = full_first_subnet
        self.assertEqual(self.deliver(app)["outcome"], "launched")

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
                return {"total_count": 201, "workflow_runs": [{"id": page * 100 + n, "created_at": f"2026-09-27T1{page}:00:{n:02d}Z"}
                                                              for n in range(100)]}
            if "status=queued" in path:
                return {"total_count": 201, "workflow_runs": [{"id": 999, "created_at": "2026-09-27T09:00:00Z"}]}
            return {"total_count": 0, "workflow_runs": []}
        app.github = github
        jobs = app.queued_jobs(DOLPHIN, REPOS[DOLPHIN], "PAT")
        self.assertIn("999001", [j for j, _ in jobs], "the oldest run, on page 3, was never inspected")
        jobs_calls = [p for p in pages_seen if "/jobs" in p]
        self.assertIn("/runs/999/", jobs_calls[0], "the oldest run must be inspected first")

    def test_a_slow_run_listing_still_reaches_the_oldest_runs_jobs(self):
        # 1,000 queued runs and a GitHub that takes 10 s per run page: listing every page first
        # would use the whole 50 s scan before reading a single job.
        app, *_ = load_app()
        clock = [0.0]
        app.time = types.SimpleNamespace(monotonic=lambda: clock[0], time=lambda: 0)
        app.DEADLINE[0] = 50
        def github(method, path, pat, body=None):
            page = int(path.rsplit("page=", 1)[1])
            if "/jobs" in path:
                run = int(path.split("/runs/")[1].split("/")[0])
                return {"jobs": [{"id": run, "status": "queued", "labels": ["self-hosted", "dolphin", "l"]}]}
            if "status=queued" not in path:
                return {"total_count": 0, "workflow_runs": []}
            clock[0] += 10
            return {"total_count": 1000, "workflow_runs": [
                {"id": (10 - page) * 100 + n, "created_at": f"2026-09-{27 - page:02d}T00:00:{n:02d}Z"} for n in range(100)]}
        app.github = github
        jobs = [int(j) for j, _ in app.queued_jobs(DOLPHIN, REPOS[DOLPHIN], "PAT")]
        self.assertIn(0, jobs, "the oldest run (last page) must be reached")
        self.assertEqual(jobs[:3], [0, 1, 2], "oldest first")

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

    def backlog(self, app, jobs=50, scan_uses_its_whole_share=False, share_ends_after=None):
        """reconcile() over a queue of `jobs`, on a fake clock with 100 s left in the repo's share."""
        clock, calls, scan_deadline = [1000.0], [], []
        app.time = types.SimpleNamespace(monotonic=lambda: clock[0], time=time.time)
        app.DEADLINE[0] = clock[0] + 100
        app.repo_pat = lambda cfg: "PAT"
        app.reap = lambda repo, cfg, pat, instances, runners, complete=True: []
        app.app_instances = lambda repo: []
        app.list_runners = lambda repo, pat: ({}, True)
        def queued(repo, cfg, pat):
            scan_deadline.append(app.DEADLINE[0] - clock[0])
            if scan_uses_its_whole_share:
                clock[0] = app.DEADLINE[0]
            return [(str(n), "s") for n in range(jobs)]
        app.queued_jobs = queued
        def ensure(repo, cfg, subnets, pat, job_id, size, live, token=None, busy=frozenset()):
            calls.append(job_id)
            if share_ends_after is not None and len(calls) >= share_ends_after:
                clock[0] += 1000
            return ("launched" if len(calls) <= int(cfg["max"]) else "cap"), "tok"
        app.ensure_runner = ensure
        return calls, app.reconcile(DOLPHIN, REPOS[DOLPHIN], ["subnet-a"]), scan_deadline

    def test_a_backlog_stops_at_the_cap(self):
        app, *_ = load_app()
        calls, result, _ = self.backlog(app)
        cap = int(REPOS[DOLPHIN]["max"])
        self.assertEqual(len(calls), cap + 1, "every job after the first 'cap' would only repeat the same scans")
        self.assertEqual(result["outcomes"], {"launched": cap, "cap": 1})

    def test_a_backlog_stops_at_the_time_share(self):
        app, *_ = load_app()
        calls, result, _ = self.backlog(app, share_ends_after=1)
        self.assertEqual(calls, ["0"], "no job may be processed after the repo's deadline")
        self.assertEqual(result["outcomes"], {"launched": 1, "deferred": 1})

    def test_a_scan_that_uses_its_whole_budget_still_leaves_time_to_launch(self):
        app, *_ = load_app()
        calls, result, scan = self.backlog(app, scan_uses_its_whole_share=True)
        self.assertEqual(scan, [50.0], "the scan gets half the share")
        self.assertEqual(result["outcomes"]["launched"], int(REPOS[DOLPHIN]["max"]),
                         "jobs the scan found must be launched, not dropped at the deadline")

    def test_a_stale_snapshot_does_not_relaunch_a_listed_host(self):
        # The invocation's `live` was taken before the host was listed; the claim-cleanup listing
        # sees it. The job has a host: releasing the claim and launching again is a duplicate.
        app, ec2, *_ = load_app()
        self.assertEqual(self.deliver(app)["outcome"], "launched")
        outcome, _ = app.ensure_runner(COLTON, REPOS[COLTON], app.config()[1], "PAT", "7", "xl", [])
        self.assertEqual(outcome, "exists")
        self.assertEqual(len(ec2.launched), 1)

    def test_an_older_host_of_the_same_job_does_not_release_a_newer_claim(self):
        app, ec2, *_ = load_app()
        self.assertEqual(self.deliver(app)["outcome"], "launched")
        first = ec2.instances[0]
        first["State"]["Name"] = "terminated"   # the host died; the job is still queued
        self.assertEqual(self.deliver(app)["outcome"], "launched")
        ec2.instances = [first]   # the relaunch is not listed yet; only the dead host is
        self.assertEqual(self.deliver(app)["outcome"], "claimed",
                         "the dead host's JobId must not release the relaunch's claim")
        self.assertEqual(len(ec2.launched), 2)

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

    def test_a_stale_delivery_for_a_job_no_longer_queued_launches_nothing(self):
        app, ec2, *_ = load_app()
        app.github.job_status[7] = "completed"   # the reconcile already ran it; the delivery waited
        self.assertEqual(self.deliver(app), {"ignored": "no longer queued"})
        self.assertEqual(ec2.launched, [])

    def test_instance_metadata_exposes_tags_so_inspector_honours_its_exclusion(self):
        app, ec2, *_ = load_app()
        self.deliver(app)
        (kw,) = ec2.launched
        self.assertEqual(kw["MetadataOptions"]["InstanceMetadataTags"], "enabled")
        self.assertEqual(kw["MetadataOptions"]["HttpTokens"], "required")

    def test_a_delivery_is_bounded_by_the_invocation_and_leaves_no_claim(self):
        app, ec2, *_ = load_app()
        context = types.SimpleNamespace(get_remaining_time_in_millis=lambda: (app.RESERVE_SECONDS - 5) * 1000)
        event = {"repo": COLTON, "action": "queued", "workflow_job": {"id": 7, "labels": ["self-hosted", "cc-games", "xl"]}}
        self.assertEqual(app.handler(event, context)["outcome"], "out of time")
        self.assertEqual(ec2.launched, [])
        self.assertEqual(app.fake_dynamo.items, {}, "a claim left behind would block the job for 15 minutes")
        self.assertEqual(app.DEADLINE[0], float("inf"))

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

    def test_a_busy_host_tagged_for_a_still_queued_job_does_not_cover_it(self):
        # GitHub gave the host launched for job 11 another job with the same labels. It is busy
        # while 11 is still queued, and an ephemeral runner takes one job: 11 needs its own host.
        app, ec2, *_ = load_app()
        gh = app.github
        gh.runs[(COLTON, "in_progress")] = [1]
        gh.jobs[1] = [{"id": 11, "status": "queued", "labels": ["self-hosted", "cc-games", "xl"]}]
        ec2.instances.append(instance("i-tagged11", COLTON, "11", 5))
        gh.runners[COLTON] = {"i-tagged11": {"id": 41, "name": "i-tagged11", "busy": True, "status": "online",
                                             "labels": [{"name": "cc-games"}]}}
        app.handler({"reconcile": True}, None)
        launched = [{t["Key"]: t["Value"] for t in kw["TagSpecifications"][0]["Tags"]}["JobId"] for kw in ec2.launched]
        self.assertEqual(launched, ["11"])

    def test_the_cap_counts_hosts_not_job_tags(self):
        # Dolphin's cap is 3: two hosts tagged for job 5 (one busy on another job) and one for 6.
        app, ec2, *_ = load_app()
        hosts = [instance("i-a", DOLPHIN, "5", 5, size="l"), instance("i-b", DOLPHIN, "5", 5, size="l"),
                 instance("i-c", DOLPHIN, "6", 5, size="l")]
        ec2.instances += hosts
        outcome, _ = app.ensure_runner(DOLPHIN, REPOS[DOLPHIN], app.config()[1], "PAT", "5", "l", list(hosts),
                                       busy=frozenset({"i-a", "i-b"}))
        self.assertEqual(outcome, "cap", "three hosts are three slots, whatever their tags")
        self.assertEqual(ec2.launched, [])

    def test_a_burst_the_listing_has_not_caught_up_with_fills_the_cap_exactly(self):
        # Each launch is in `live` and holds a claim until EC2 lists it: one slot, not two.
        app, ec2, *_ = load_app()
        ec2.listing_lags = True
        gh = app.github
        gh.runs[(DOLPHIN, "queued")] = [1]
        gh.jobs[1] = [{"id": 100 + n, "status": "queued", "labels": ["self-hosted", "dolphin", "l"]} for n in range(5)]
        result = app.reconcile(DOLPHIN, REPOS[DOLPHIN], app.config()[1])
        self.assertEqual(len(ec2.launched), int(REPOS[DOLPHIN]["max"]))
        self.assertEqual(result["outcomes"], {"launched": int(REPOS[DOLPHIN]["max"]), "cap": 1})

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
        self.assertNotIn("ALL", repos, "a total that counts the failed repo as zero would hide its hosts")

    def test_a_round_that_counted_every_repo_publishes_the_total(self):
        app, ec2, *_ = load_app()
        ec2.instances.append(instance("i-busy", COLTON, "4", 30))
        app.github.runners[COLTON] = {"i-busy": {"id": 32, "name": "i-busy", "busy": True, "status": "online",
                                                 "labels": [{"name": "cc-games"}]}}
        app.handler({"reconcile": True}, None)
        _, data = app.fake_cloudwatch.metrics[-1]
        counts = {d["Dimensions"][0]["Value"]: d["Value"] for d in data}
        self.assertEqual(counts, {COLTON: 1, DOLPHIN: 0, "ALL": 1})

    def test_no_aws_call_or_github_request_starts_after_the_deadline(self):
        app, *_ = load_app()
        app.DEADLINE[0] = 0
        with self.assertRaises(app.OutOfTime):
            app.listed_instances(COLTON)
        with self.assertRaises(app.OutOfTime):
            app.real_github("GET", f"/repos/{COLTON}/actions/runners", "PAT")
        app.fake_dynamo.items[(COLTON, "7")] = {"expires_at": {"N": "0"}}
        app.release(COLTON, "7")
        self.assertEqual(app.fake_dynamo.deleted, [(COLTON, "7")], "releasing a claim must still finish")

    def test_a_reconcile_that_runs_out_of_time_mid_job_stops_and_reports_its_count(self):
        app, ec2, *_ = load_app()
        gh = app.github
        gh.runs[(COLTON, "queued")] = [1]
        gh.jobs[1] = [{"id": 11, "status": "queued", "labels": ["self-hosted", "cc-games", "xl"]}]
        ec2.instances.append(instance("i-busy", COLTON, "4", 30))
        gh.runners[COLTON] = {"i-busy": {"id": 32, "name": "i-busy", "busy": True, "status": "online",
                                         "labels": [{"name": "cc-games"}]}}
        app.DEADLINE[0] = time.monotonic() + 100
        def slow_claims(repo):
            app.DEADLINE[0] = 0   # the claims query returned after the repo's deadline
            return {}
        app.active_claims = slow_claims
        result = app.reconcile(COLTON, REPOS[COLTON], app.config()[1])
        self.assertEqual(result["stopped"], "no time left")
        self.assertEqual(result["live"], 1)
        self.assertEqual(ec2.launched, [])

    def test_a_launch_ec2_accepted_is_handed_its_credential_past_the_deadline(self):
        app, ec2, ssm, _ = load_app()
        app.DEADLINE[0] = time.monotonic() + 100
        real = ec2.run_instances
        def accepted_late(**kw):
            out = real(**kw)
            app.DEADLINE[0] = 0
            return out
        ec2.run_instances = accepted_late
        outcome, _ = app.ensure_runner(COLTON, REPOS[COLTON], app.config()[1], "PAT", "7", "xl", [])
        self.assertEqual(outcome, "launched", "a host without its credential would idle until reaped")
        self.assertEqual(ec2.terminated, [])

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


    def test_a_terminate_that_ends_past_the_deadline_leaves_the_runner_record_for_next_round(self):
        app, ec2, *_ = load_app()
        real = ec2.terminate_instances
        def slow(InstanceIds):
            real(InstanceIds)
            app.DEADLINE[0] = 0   # the call returned after the repo's share ended
        ec2.terminate_instances = slow
        # Over its lifetime (terminated first, even while busy); its record is deleted after.
        runner = {"i-ancient": {"id": 33, "name": "i-ancient", "busy": True, "status": "online", "labels": [{"name": "cc-games"}]}}
        app.reap(COLTON, REPOS[COLTON], "PAT", [instance("i-ancient", COLTON, "5", 200)], runner)
        self.assertEqual(ec2.terminated, ["i-ancient"])
        self.assertEqual(app.github.calls, [], "no DELETE may start after the deadline")

    def test_an_idle_runner_is_deregistered_before_its_host_is_terminated(self):
        app, ec2, *_ = load_app()
        order = []
        real_terminate, real_github = ec2.terminate_instances, app.github
        ec2.terminate_instances = lambda InstanceIds: order.append("terminate") or real_terminate(InstanceIds)
        app.github = lambda method, path, pat, body=None: order.append(method) or real_github(method, path, pat, body)
        runner = {"i-idle": {"id": 31, "name": "i-idle", "busy": False, "status": "online", "labels": [{"name": "cc-games"}]}}
        app.reap(COLTON, REPOS[COLTON], "PAT", [instance("i-idle", COLTON, "3", 12)], runner)
        self.assertEqual(order, ["DELETE", "terminate"], "terminating first can kill a job taken since the listing")

    def test_a_runner_that_took_a_job_since_the_listing_keeps_its_host(self):
        # GitHub refuses to deregister a runner that is running a job.
        app, ec2, *_ = load_app()
        def github(method, path, pat, body=None):
            raise urllib.error.HTTPError(path, 422, "runner is running a job", {}, None)
        app.github = github
        runner = {"i-idle": {"id": 31, "name": "i-idle", "busy": False, "status": "online", "labels": [{"name": "cc-games"}]}}
        keep = app.reap(COLTON, REPOS[COLTON], "PAT", [instance("i-idle", COLTON, "3", 12)], runner)
        self.assertEqual(ec2.terminated, [])
        self.assertEqual([i["InstanceId"] for i in keep], ["i-idle"])

    def test_no_ec2_scan_starts_after_a_runner_listing_that_ended_past_the_deadline(self):
        app, *_ = load_app()
        scanned = []
        def listing(repo, pat):
            app.DEADLINE[0] = 0
            return {}, True
        app.list_runners = listing
        app.app_instances = lambda repo: scanned.append(repo) or []
        self.assertEqual(app.reconcile(COLTON, REPOS[COLTON], []), {"repo": COLTON, "skipped": "no time left"})
        self.assertEqual(scanned, [])

    def test_a_host_that_registered_after_the_listing_is_not_killed(self):
        app, ec2, *_ = load_app()
        app.github.runners[COLTON] = {"i-late": {"id": 51, "name": "i-late", "busy": True, "status": "online",
                                                 "labels": [{"name": "cc-games"}]}}
        keep = app.reap(COLTON, REPOS[COLTON], "PAT", [instance("i-late", COLTON, "8", 11)], {})
        self.assertEqual(ec2.terminated, [], "it registered after the round's listing and may be starting a job")
        self.assertEqual([i["InstanceId"] for i in keep], ["i-late"])

    def test_a_partial_runner_listing_never_counts_a_host_as_unregistered(self):
        app, ec2, *_ = load_app()
        hosts = [instance("i-neverup", COLTON, "2", 11), instance("i-ancient", COLTON, "5", 200)]
        keep = app.reap(COLTON, REPOS[COLTON], "PAT", hosts, {}, complete=False)
        self.assertEqual(ec2.terminated, ["i-ancient"], "absence from a partial listing proves nothing")
        self.assertEqual([i["InstanceId"] for i in keep], ["i-neverup"])

    def test_reaping_and_the_runner_listing_stop_at_the_repos_deadline(self):
        app, ec2, *_ = load_app()
        app.DEADLINE[0] = 0
        self.assertEqual(app.list_runners(COLTON, "PAT"), ({}, False))
        label = [{"name": "cc-games"}]
        hosts = [instance("i-neverup", COLTON, "2", 11), instance("i-ancient", COLTON, "5", 200)]
        ghost = {"i-gone": {"id": 34, "name": "i-gone", "busy": False, "status": "offline", "labels": label}}
        keep = app.reap(COLTON, REPOS[COLTON], "PAT", hosts, ghost)
        self.assertEqual(ec2.terminated, [])
        self.assertEqual(app.github.calls, [], "no GitHub request may start after the deadline")
        self.assertEqual(len(keep), 2, "hosts not reaped this round still count as live")


class BootstrapTests(unittest.TestCase):
    def test_the_credential_poll_waits_through_access_denied(self):
        # Before the controller writes the parameter there is no tag for the role's condition to
        # match, so SSM answers AccessDenied. That must mean "not yet", not "give up".
        text = BOOTSTRAP.read_text()
        poll = text.split("REG_TOKEN=$(python3 - \"$INSTANCE_ID\" <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        tmp = Path(tempfile.mkdtemp(prefix="poll."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        (tmp / "botocore").mkdir()
        (tmp / "botocore" / "__init__.py").write_text("")
        (tmp / "botocore" / "exceptions.py").write_text(
            "class ClientError(Exception):\n"
            "    def __init__(self, code):\n"
            "        super().__init__(code); self.response = {'Error': {'Code': code}}\n")
        (tmp / "boto3.py").write_text(
            "import botocore.exceptions as e\n"
            "class _NotFound(Exception): pass\n"
            "class _SSM:\n"
            "    class exceptions: ParameterNotFound = _NotFound\n"
            "    calls = 0\n"
            "    def get_parameter(self, Name, WithDecryption):\n"
            "        _SSM.calls += 1\n"
            "        if _SSM.calls == 1: raise e.ClientError('AccessDeniedException')\n"
            "        return {'Parameter': {'Value': 'TOKEN'}}\n"
            "    def delete_parameter(self, Name): pass\n"
            "def client(name): return _SSM()\n")
        out = subprocess.run(["python3", "-c", poll, "i-0123"], capture_output=True, text=True,
                             env={"PYTHONPATH": str(tmp), "PATH": "/usr/bin:/bin"}, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "TOKEN")

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

    def test_the_tool_cache_is_where_setup_python_interpreters_expect_it(self):
        # setup-python's prebuilt Pythons are linked against /opt/hostedtoolcache. From the
        # runner's default _work/_tool, a subprocess with a clean environment failed to load
        # libpython (dolphin-labs run 36367645699). The directory must exist, belong to the
        # runner, and be named in .env after config.sh and before run.sh starts the job.
        text = BOOTSTRAP.read_text()
        self.assertIn("TOOL_CACHE=/opt/hostedtoolcache\n", text)
        self.assertIn('install -d -o runner -g runner "$TOOL_CACHE"', text)
        env = text.index("printf 'AGENT_TOOLSDIRECTORY=%s\\nRUNNER_TOOL_CACHE=%s\\n' \"$TOOL_CACHE\" \"$TOOL_CACHE\" >> \"$DIR/.env\"")
        self.assertLess(text.index("exec ./config.sh"), env, "config.sh writes .env; append after it")
        self.assertLess(env, text.index("sudo -u runner -H ./run.sh"))
        self.assertLess(text.index('install -d -o runner -g runner "$TOOL_CACHE"'), env)

    def test_jobs_run_in_the_hosted_runners_work_directory(self):
        # actions/cache records a path outside the workspace relative to the workspace. From
        # actions-runner/_work, a Playwright cache saved on a hosted runner (workspace
        # /home/runner/work/<repo>/<repo>) restored one level too deep and the browsers were
        # missing (dolphin-labs run 36368925976).
        text = BOOTSTRAP.read_text()
        self.assertIn("WORK=/home/runner/work\n", text)
        self.assertIn('install -d -o runner -g runner "$WORK"', text)
        self.assertIn('--work "$4"\' \\\n  _ "$REPO" "$INSTANCE_ID" "$LABELS" "$WORK"\n', text)
        self.assertNotIn("--work _work", text)
        self.assertLess(text.index('install -d -o runner -g runner "$WORK"'), text.index("exec ./config.sh"))


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
    def test_the_image_statement_matches_how_iam_sees_canonical_images(self):
        # Live: ami-0150109d3dd43737d is OwnerId 099720109477 with ImageOwnerAlias "amazon",
        # and RunInstances was refused on it until "amazon" was allowed.
        stmt = APP_TF.split('Sid      = "LaunchCanonicalImagesOnly"', 1)[1].split("\n      },", 1)[0]
        self.assertIn('"ec2:Owner" = [local.runner_app_ami_owner, "amazon"]', stmt)

    def test_the_controller_may_create_the_spot_request_every_launch_needs(self):
        # Every launch is spot; RunInstances then also authorizes a spot-instances-request.
        # Missing it made every launch UnauthorizedOperation on the first real job.
        stmt = APP_TF.split('Sid      = "LaunchAppSpotRequest"', 1)[1].split("},", 1)[0]
        self.assertIn('Action   = "ec2:RunInstances"', stmt)
        self.assertIn("spot-instances-request/*", stmt)
        self.assertIn("'MarketType': 'spot'", (ROOT / "runner-app" / "app_runner.py").read_text())

    def test_the_controller_has_room_beyond_its_first_measured_memory(self):
        block = APP_TF.split('resource "aws_lambda_function" "runner_app"', 1)[1].split("\n}\n", 1)[0]
        self.assertRegex(block, r"memory_size\s*=\s*256", "104 of 128 MB was used by an empty reconcile")

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

    def test_repos_that_share_a_label_still_get_their_own_alarm(self):
        # CloudWatch alarm names are unique per region: two repos named after one label would be
        # one alarm that each apply rewrites for the other repo, so one cap would go unwatched.
        rows = re.findall(r'^\s+"([^"]+/[^"]+)"\s*=\s*\{ label = "([^"]+)", max = \d+(?:, alarm = "([^"]+)")? \}', APP_TF, re.M)
        served = re.findall(r'^\s+"([^"]+/[^"]+)"\s*=\s*\{ label', APP_TF, re.M)
        self.assertEqual([repo for repo, _, _ in rows], served, "every served repo's row must parse")
        names = [alarm or label for _, label, alarm in rows]
        self.assertEqual(len(names), len(set(names)), names)
        alarm = re.search(r'resource "aws_cloudwatch_metric_alarm" "too_many_app_runners_per_repo" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn('alarm_name          = "too-many-app-runners-${try(local.runner_app_repos[each.key].alarm, each.value.label)}"', alarm)

    def test_every_served_repo_has_a_hook_made_with_its_own_token(self):
        # A controller token is limited to one repo, so a hook made through another repo's
        # provider alias fails at apply, and without a hook the repo waits for the reconcile.
        served = re.findall(r'^\s+"([^"]+/[^"]+)"\s*=\s*\{ label', APP_TF, re.M)
        hooks = re.findall(r'resource "github_repository_webhook" "\w+" \{.*?\n\}', APP_TF, re.S)
        self.assertEqual(len(hooks), len(served))
        for repo in served:
            owner, name = repo.split("/")
            hook = [h for h in hooks if 'repository = "%s"' % name in h]
            self.assertEqual(len(hook), 1, repo)
            alias = re.search(r"provider   = github\.(\w+)", hook[0]).group(1)
            provider = re.search(r'provider "github" \{\n  alias = "%s"\n.*?\n\}' % alias, APP_TF, re.S).group()
            self.assertIn('owner = "%s"' % owner, provider, repo)
            self.assertIn('runner_repo_pat["%s"].secret_string' % repo, provider, repo)
            self.assertIn('events     = ["workflow_job"]', hook[0])

    def test_security_group_has_no_inbound_and_the_controller_is_serialized(self):
        sg = re.search(r'resource "aws_security_group" "runner_app" \{.*?\n\}', APP_TF, re.S).group()
        self.assertNotIn("ingress", sg)
        fn = re.search(r'resource "aws_lambda_function" "runner_app" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn("reserved_concurrent_executions = 1", fn)

    def test_controller_touches_only_app_runners(self):
        policy = re.search(r'resource "aws_iam_role_policy" "runner_app_lambda" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn('"aws:ResourceTag/Role" = "github-app-runner"', policy)
        self.assertNotIn('"aws:ResourceTag/Role" = "github-runner"\n', policy.split("TerminateAppRunnersOnly")[1].split("}")[0])
        self.assertIn('"ec2:Owner" = [local.runner_app_ami_owner, "amazon"]', policy)
        self.assertNotRegex(policy, r'Action\s*=\s*"ec2:\*"')
        self.assertIn("aws_secretsmanager_secret.github_runner_repo_pat[repo].arn", policy)
        # fcvm's bootstrap credentials share the path and Role tag: only Fleet tells them apart.
        for sid, key in (("DeleteBrokeredCredentialOnly", "aws:ResourceTag/Fleet"),
                         ("BrokerInstanceBoundBootstrapCredential", "aws:RequestTag/Fleet")):
            block = policy.split('Sid      = "%s"' % sid, 1)[1].split("},\n      {", 1)[0]
            self.assertIn('"%s" = "github-app-runner"' % key, block, sid)

    def test_app_hosts_get_their_own_role_that_reads_only_their_own_credential(self):
        """App runners run outside code; fcvm's github-runner-role can write security records
        and describe the fleet's ENIs. The app role's only Allow is the host's own credential."""
        policy = re.search(r'resource "aws_iam_role_policy" "runner_app_instance" \{.*?\n\}', APP_TF, re.S).group()
        allows = re.findall(r'Effect\s*=\s*"Allow"\s*\n\s*Action\s*=\s*(\[[^\]]*\]|"[^"]*")', policy)
        self.assertEqual(allows, ['["ssm:GetParameter", "ssm:DeleteParameter"]'], allows)
        allow = policy.split('Sid      = "ConsumeOwnBootstrapCredential"', 1)[1].split("},\n      {", 1)[0]
        self.assertIn("parameter/github-runner/bootstrap/*", allow)
        self.assertIn('"ssm:resourceTag/InstanceArn" = "$${ec2:SourceInstanceARN}"', allow)
        self.assertIn('"ec2:SourceInstanceARN" = "false"', allow)
        self.assertIn("DenyOtherBootstrapCredentials", policy)
        self.assertIn("DenyEveryOtherParameterPayload", policy)
        self.assertNotRegex(policy, r"s3:|ec2:Describe|ssm:\*|\"\*\"\s*\n\s*Resource", "no S3, EC2 or wildcard actions")
        attachments = re.findall(r'resource "aws_iam_role_policy_attachment" "[^"]+" \{[^}]*runner_app_instance', APP_TF)
        self.assertEqual(attachments, [], "no managed policies on the app runner role")
        profile = re.search(r'resource "aws_iam_instance_profile" "runner_app" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn("role  = aws_iam_role.runner_app_instance[0].name", profile)

    def test_controller_launches_and_passes_only_the_app_role(self):
        policy = re.search(r'resource "aws_iam_role_policy" "runner_app_lambda" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn('"ec2:InstanceProfile" = aws_iam_instance_profile.runner_app[0].arn', policy)
        passrole = policy.split('Action   = "iam:PassRole"', 1)[1].split("}\n      },", 1)[0]
        self.assertIn("Resource = aws_iam_role.runner_app_instance[0].arn", passrole)
        self.assertIn('"iam:PassedToService" = "ec2.amazonaws.com"', passrole)
        self.assertIn("NotResource = aws_iam_role.runner_app_instance[0].arn", policy)
        self.assertNotIn("aws_iam_role.runner[0]", APP_TF, "fcvm's runner role must not reach app hosts")
        self.assertNotIn("aws_iam_instance_profile.runner[0]", APP_TF)
        self.assertIn("INSTANCE_PROFILE  = aws_iam_instance_profile.runner_app[0].name", APP_TF)

    def test_each_repo_alarms_above_its_own_cap(self):
        alarm = re.search(r'resource "aws_cloudwatch_metric_alarm" "too_many_app_runners_per_repo" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn("for_each            = var.enable_github_runner ? local.runner_app_config : {}", alarm)
        self.assertIn("threshold           = each.value.max", alarm)
        self.assertIn('metric_name         = "LiveRunners"', alarm)
        self.assertIn("dimensions          = { Repo = each.key }", alarm)
        self.assertIn("'Dimensions': [{'Name': 'Repo', 'Value': r['repo']}]", APP.read_text(),
                      "the controller must publish the per-repo series this alarm reads")


if __name__ == "__main__":
    unittest.main()

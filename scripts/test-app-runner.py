#!/usr/bin/env python3
"""App-repo runners (runner-app.tf, runner-app/): routing, launch, caps, reaping, bootstrap, front.

Runs the real runner-app/app_runner.py against fake EC2/SSM/Secrets Manager and a fake GitHub,
and the real front function (extracted from runner-webhook-front.tf) against a fake Lambda
client. Nothing here touches AWS or GitHub.
"""
import importlib.util
import base64
import contextlib
import io
import math
import os
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
AGENTS_MD_TF = (ROOT / "agents-md.tf").read_text()
REPOS_TF = (ROOT / "runner-repos.tf").read_text()

COLTON, DOLPHIN = "CoderColton/colton-games", "dolphin-labs-hq/dolphin-labs"
# ejc3/imagine shares its owner with ejc3/fcvm (the metal runners), so its separation from fcvm is tested by repository.
IMAGINE, FCVM = "ejc3/imagine", "ejc3/fcvm"
SIZES = {"xl": ["c7a.16xlarge", "c7i.16xlarge"], "l": ["c7a.8xlarge", "c7i.8xlarge"], "s": ["c7a.2xlarge"]}
# runner-app.tf's runner_app_size_vcpus, which every repo's config carries as `vcpus`.
VCPUS = {"xl": 64, "l": 32, "s": 8}
# Every repo's config carries `vcpus` and `max_vcpus` as Terraform passes them (null: no vCPU cap). These three have none;
# VcpuCapTests gives Imagine its vCPU cap.
REPOS = {
    COLTON: {"label": "cc-games", "max": 3, "max_vcpus": None, "pat_secret": f"github-runner/repo-pat/{COLTON}", "sizes": SIZES, "vcpus": VCPUS},
    DOLPHIN: {"label": "dolphin", "max": 3, "max_vcpus": None, "pat_secret": f"github-runner/repo-pat/{DOLPHIN}", "sizes": SIZES, "vcpus": VCPUS},
    IMAGINE: {"label": "imagine", "max": 3, "max_vcpus": None, "pat_secret": f"github-runner/repo-pat/{IMAGINE}", "sizes": SIZES, "vcpus": VCPUS},
}
SUBNETS = [{"subnet_id": "subnet-c", "availability_zone": "us-west-1c"},
           {"subnet_id": "subnet-a", "availability_zone": "us-west-1a"}]
ENV = {"REPOS": json.dumps(REPOS), "LAUNCH_SUBNETS": json.dumps(SUBNETS), "SECURITY_GROUP_ID": "sg-app",
       "INSTANCE_PROFILE": "github-app-runner-profile", "RUNNER_ACCOUNT_ID": "123456789012",
       "CLAIMS_TABLE": "github-app-runner-claims",
       # No budget groups unless a test gives some (BudgetTests): every repo above is capped by its own caps alone.
       "BUDGET_TABLE": "github-app-runner-budget", "BUDGET_GROUPS": "{}",
       "VCPU_HOUR_USD": "0.027", "HOST_HOUR_USD": "0.016"}
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

    def describe_images(self, Owners, Filters):
        """As EC2 answers: Owners by account id, each filter a match on any of its values. With `ignore_image_filters`, a
        listing that lets everything through, so the controller's own check is what is tested."""
        self.image_queries = getattr(self, "image_queries", []) + [{"Owners": Owners, "Filters": Filters}]
        if getattr(self, "image_error", None):
            raise self.image_error
        images = list(getattr(self, "images", []))
        if getattr(self, "ignore_image_filters", False):
            return {"Images": images}
        def matches(image, name, values):
            if name.startswith("tag:"):
                return any(t["Key"] == name[4:] and t["Value"] in values for t in image.get("Tags", []))
            return {"state": image.get("State"), "architecture": image.get("Architecture")}[name] in values
        return {"Images": [i for i in images if i["OwnerId"] in Owners
                           and all(matches(i, f["Name"], f["Values"]) for f in Filters)]}


class FakeDynamo:
    """The launch-claim table: a conditional put that only one caller can win. And the budget table: one item per group,
    a consistent get and a put conditional on the item's absence or its version. `conflicts` makes that many bucket
    writes lose to another writer, which `rival(item)` (when set) stands for by storing its own newer item first."""
    class exceptions:
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self):
        self.items, self.deleted, self.queries = {}, [], []
        self.buckets, self.bucket_reads, self.bucket_writes, self.conflicts, self.rival = {}, [], [], 0, None

    def get_item(self, TableName, Key, ConsistentRead=False):
        assert TableName == "github-app-runner-budget" and ConsistentRead, (TableName, ConsistentRead)
        self.bucket_reads.append(Key["group"]["S"])
        item = self.buckets.get(Key["group"]["S"])
        return {"Item": json.loads(json.dumps(item))} if item else {}

    def put_item(self, TableName, Item, ConditionExpression, ExpressionAttributeValues=None, ExpressionAttributeNames=None):
        if TableName == "github-app-runner-budget":
            group = Item["group"]["S"]
            self.bucket_writes.append(Item)
            if self.conflicts:
                self.conflicts -= 1
                if self.rival:
                    self.buckets[group] = self.rival(self.buckets.get(group))
                raise self.exceptions.ConditionalCheckFailedException(group)
            held = self.buckets.get(group)
            if ConditionExpression == "attribute_not_exists(#g)":
                ok = held is None and ExpressionAttributeNames == {"#g": "group"}
            else:
                ok = (ConditionExpression == "#v = :v" and held is not None
                      and held["version"]["N"] == ExpressionAttributeValues[":v"]["N"])
            if not ok:
                raise self.exceptions.ConditionalCheckFailedException(group)
            self.buckets[group] = Item
            return
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
        f"github-runner/repo-pat/{COLTON}": "PAT-COLTON", f"github-runner/repo-pat/{DOLPHIN}": "PAT-DOLPHIN",
        f"github-runner/repo-pat/{IMAGINE}": "PAT-IMAGINE"})
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

    def test_each_repos_labels_select_only_its_own_config(self):
        # A job is offered to a repo's controller only if every label is that repo's. Imagine's labels never select
        # another repo's config and theirs never select Imagine's; fcvm's metal labels select none of them.
        app, *_ = load_app()
        for repo, cfg in REPOS.items():
            for other, other_cfg in REPOS.items():
                labels = ["self-hosted", other_cfg["label"], "l"]
                self.assertEqual(app.job_size(cfg, labels), "l" if other == repo else None, (repo, labels))
            for fcvm_labels in (["self-hosted", "ARM64"], ["self-hosted", "X64"], ["self-hosted", "Linux", "X64"],
                                ["self-hosted", "imagine", "ARM64", "l"], ["self-hosted", "imagine", "l", "dolphin"]):
                self.assertIsNone(app.job_size(cfg, fcvm_labels), (repo, fcvm_labels))
        self.assertEqual(app.job_size(REPOS[IMAGINE], ["self-hosted", "Linux", "X64", "imagine", "xl"]), "xl")


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

    def test_an_imagine_job_gets_a_host_registered_to_imagine_alone(self):
        app, ec2, ssm, secrets = load_app()
        result = self.deliver(app, repo=IMAGINE, labels=("self-hosted", "imagine", "l"), job=11)
        self.assertEqual(result["outcome"], "launched")
        (kw,) = ec2.launched
        self.assertEqual(kw["InstanceType"], "c7a.8xlarge")
        tags = {t["Key"]: t["Value"] for t in kw["TagSpecifications"][0]["Tags"]}
        self.assertEqual((tags["Role"], tags["Repo"], tags["JobId"], tags["Size"]), ("github-app-runner", IMAGINE, "11", "l"))
        self.assertEqual(kw["IamInstanceProfile"], {"Name": "github-app-runner-profile"})
        # Its registration token is Imagine's, asked with Imagine's token, and nothing of any other repo is read.
        self.assertIn(("POST", f"/repos/{IMAGINE}/actions/runners/registration-token", "PAT-IMAGINE"), app.github.calls)
        self.assertEqual(secrets.read, [f"github-runner/repo-pat/{IMAGINE}"])
        self.assertFalse([c for c in app.github.calls if not c[1].startswith(f"/repos/{IMAGINE}/")], app.github.calls)
        self.assertIn(f"REPO='{IMAGINE}'", kw["UserData"])
        self.assertIn("LABELS='imagine,l'", kw["UserData"])
        self.assertNotIn("REGTOKEN", kw["UserData"])

    def test_a_job_naming_another_repos_label_is_not_launched_for(self):
        app, ec2, *_ = load_app()
        for repo, labels in ((IMAGINE, ("self-hosted", "cc-games", "l")), (COLTON, ("self-hosted", "imagine", "l")),
                             (IMAGINE, ("self-hosted", "ARM64"))):
            self.assertEqual(self.deliver(app, repo=repo, labels=labels), {"ignored": "labels"}, (repo, labels))
        self.assertEqual(ec2.launched, [])

    def test_a_delivery_for_fcvm_reaching_the_app_runner_launches_nothing(self):
        app, ec2, *_ = load_app()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.deliver(app, repo=FCVM, labels=("self-hosted", "imagine", "l")), {"ignored": "repo"})
        self.assertEqual(ec2.launched, [])

    def test_imagines_cap_is_its_own(self):
        app, ec2, *_ = load_app()
        ec2.instances += [instance(f"i-{n}", IMAGINE, str(100 + n), 1) for n in range(3)]
        self.assertEqual(self.deliver(app, repo=IMAGINE, labels=("self-hosted", "imagine", "l"))["outcome"], "cap")
        self.assertEqual(self.deliver(app)["outcome"], "launched", "Imagine at its cap holds no other repo back")

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

    def test_every_repo_goes_first_in_turn(self):
        app, *_ = load_app()
        repos = ["a", "b", "c", "d", "e"]
        firsts = [app.rotated(repos, n)[0] for n in range(10)]
        self.assertEqual(firsts, repos + repos, "a reversal alone never puts the middle repo first")
        for n in range(5):
            self.assertEqual(sorted(app.rotated(repos, n)), repos, "a rotation keeps every repo exactly once")
        self.assertEqual(app.rotated([], 3), [])

    def test_the_round_number_picks_the_first_repo(self):
        app, *_ = load_app()
        order = []
        app.reconcile = lambda repo, cfg, subnets: order.append(repo) or {"repo": repo, "live": 0}
        served = list(app.config()[0])
        for n in range(len(served)):
            order.clear()
            app.time = types.SimpleNamespace(monotonic=time.monotonic, time=lambda n=n: n * 120.0 + 1)
            app.handler({"reconcile": True}, None)
            self.assertEqual(order[0], served[n % len(served)])
            self.assertEqual(sorted(order), sorted(served))

    def test_a_round_that_counted_every_repo_publishes_the_total(self):
        app, ec2, *_ = load_app()
        ec2.instances.append(instance("i-busy", COLTON, "4", 30))
        app.github.runners[COLTON] = {"i-busy": {"id": 32, "name": "i-busy", "busy": True, "status": "online",
                                                 "labels": [{"name": "cc-games"}]}}
        app.handler({"reconcile": True}, None)
        _, data = app.fake_cloudwatch.metrics[-1]
        counts = {d["Dimensions"][0]["Value"]: d["Value"] for d in data if d["MetricName"] == "LiveRunners"}
        self.assertEqual(counts, {COLTON: 1, DOLPHIN: 0, IMAGINE: 0, "ALL": 1})

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

    def test_a_host_registers_to_its_own_repository_only(self):
        # Run bootstrap's real registration step, rendered for Imagine, against a fake sudo and config.sh: the runner is
        # registered at the repository level, to https://github.com/ejc3/imagine, and a repository-level runner is never
        # given another repository's job (ejc3/fcvm's included, though both have one owner).
        app, *_ = load_app()
        rendered = app.user_data(IMAGINE, "imagine,l")
        start = rendered.index("printf '%s' \"$REG_TOKEN\" | sudo -u runner")
        step = rendered[start:rendered.index("unset REG_TOKEN", start)]
        tmp = Path(tempfile.mkdtemp(prefix="register."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        (tmp / "bin").mkdir()
        (tmp / "bin" / "sudo").write_text('#!/bin/bash\n[ "$1 $2 $3" = "-u runner -H" ] || exit 9\nshift 3\nexec "$@"\n')
        (tmp / "config.sh").write_text('#!/bin/bash\nprintf "%s\\n" "$@" > args\nprintf %s "$ACTIONS_RUNNER_INPUT_TOKEN" > token\n')
        for f in (tmp / "bin" / "sudo", tmp / "config.sh"):
            f.chmod(0o755)
        script = ("REPO=$(sed -n \"s/^REPO='\\(.*\\)'$/\\1/p\" user-data); LABELS=$(sed -n \"s/^LABELS='\\(.*\\)'$/\\1/p\" user-data)\n"
                  "INSTANCE_ID=i-0abc WORK=/home/runner/work REG_TOKEN=REGTOKEN\n" + step)
        (tmp / "user-data").write_text(rendered)
        out = subprocess.run(["bash", "-c", script], cwd=tmp, capture_output=True, text=True, timeout=30,
                             env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin"})
        self.assertEqual(out.returncode, 0, out.stderr)
        args = (tmp / "args").read_text().split("\n")
        self.assertEqual(args[args.index("--url") + 1], f"https://github.com/{IMAGINE}")
        self.assertEqual(args[args.index("--labels") + 1], "imagine,l")
        self.assertIn("--ephemeral", args)
        self.assertNotIn("REGTOKEN", args)
        self.assertEqual((tmp / "token").read_text(), "REGTOKEN")

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
                       "APP_REPOS": json.dumps([COLTON, DOLPHIN, IMAGINE]), "APP_DELIVERY_TARGET": "arn:app"})
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

    def test_imagine_goes_to_the_app_runner_and_fcvm_still_to_its_metal_webhook(self):
        # One owner, two paths: the front decides by the full repository name, never by the owner or the labels.
        handler, client = load_front()
        job = dict(self.JOB, labels=["self-hosted", "imagine", "l"])
        handler(signed({"action": "queued", "workflow_job": job, "repository": {"full_name": "ejc3/Imagine"}}), None)
        handler(signed({"action": "queued", "workflow_job": job, "repository": {"full_name": FCVM}}), None)
        handler(signed({"action": "completed", "workflow_job": job, "repository": {"full_name": IMAGINE}}), None)
        self.assertEqual([c["FunctionName"] for c in client.calls], ["arn:app", "arn:delivery"])
        self.assertEqual(json.loads(client.calls[0]["Payload"])["repo"], IMAGINE)
        self.assertNotIn("repo", json.loads(client.calls[1]["Payload"]), "fcvm's webhook is given no repository to act on")

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
        # The front forwards only what the controller serves: a repo whose token gate is off is out of both.
        self.assertIn("APP_REPOS           = jsonencode(keys(local.runner_app_config))", FRONT_TF)

    def test_only_hooks_made_with_a_pat_read_one(self):
        # Every plan reads each PAT in the set, so a PAT no provider uses would fail every plan once it is retired.
        eph = re.search(r'ephemeral "aws_secretsmanager_secret_version" "runner_repo_pat" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn("toset(local.runner_app_pat_hook_repos)", eph)
        pat_hooks = re.search(r"runner_app_pat_hook_repos = \[.*?\n  \]", APP_TF, re.S).group()
        self.assertIn('!contains(["dolphin-labs-hq", "ejc3"], split("/", r)[0])', pat_hooks)
        used = set(re.findall(r'runner_repo_pat\["([^"]+)"\]\.secret_string', APP_TF))
        self.assertEqual(used, {COLTON})

    def test_imagine_runaway_alarm_is_a_backstop_past_what_its_bucket_allows_in_a_day(self):
        # The bucket enforces Imagine's spend; this alarm, on the controller's own vCPU count, catches the bucket's
        # arithmetic going wrong: every hour of a day above what the bucket can let out in one.
        alarm = re.search(r'resource "aws_cloudwatch_metric_alarm" "imagine_runner_hours_daily" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn("period              = 3600", alarm)
        self.assertIn("evaluation_periods  = 24", alarm)
        self.assertIn("datapoints_to_alarm = 24", alarm)
        self.assertIn('statistic           = "Average"', alarm)
        self.assertIn("threshold           = local.imagine_runner_alarm_vcpus", alarm)

    def test_repos_that_share_a_label_still_get_their_own_alarm(self):
        # CloudWatch alarm names are unique per region: two repos named after one label would be
        # one alarm that each apply rewrites for the other repo, so one cap would go unwatched.
        rows = re.findall(r'^\s+"([^"]+/[^"]+)"\s*=\s*\{ label = "([^"]+)", max = \d+(?:, max_vcpus = \d+)?(?:, throttled_max = \d+)?(?:, throttled_max_vcpus = \d+)?(?:, image = "[^"]+")?(?:, alarm = "([^"]+)")? \}', APP_TF, re.M)
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
            provider = (re.search(r'provider "github" \{\n  alias = "%s"\n.*?\n\}' % alias, APP_TF, re.S) or
                        re.search(r'provider "github" \{\n  alias = "%s"\n.*?\n\}' % alias, AGENTS_MD_TF, re.S)).group()
            self.assertIn('owner = "%s"' % owner, provider, repo)
            if owner in ("dolphin-labs-hq", "ejc3"):
                # Managed as the administration agent's GitHub App, so the repos' controller PATs can be retired. For an
                # ejc3 repo that is also what keeps it off the default provider, whose token is limited to ejc3/fcvm.
                # One provider per owner: ejc3's is agents-md.tf's (the same app and installation).
                if owner == "ejc3":
                    self.assertEqual(alias, "agents_md", repo)
                    provider = re.search(r'provider "github" \{\n  alias = "agents_md"\n.*?\n\}', AGENTS_MD_TF, re.S).group()
                    self.assertIn('installation_id = tostring(local.agents_md_github_app.installations["ejc3"])', provider)
                else:
                    self.assertEqual(alias, "dolphin_labs", repo)
                    self.assertIn('installation_id = tostring(local.github_app_admin.installations["%s"])' % owner, provider, repo)
                self.assertIn("app_auth", provider, repo)
                self.assertNotIn("runner_repo_pat", provider, repo)
                self.assertNotIn("github_webhook_admin_pat", provider, repo)
            else:
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
        self.assertIn("threshold           = each.value.peak_max", alarm)
        self.assertIn('metric_name         = "LiveRunners"', alarm)
        self.assertIn("dimensions          = { Repo = each.key }", alarm)
        self.assertIn("'Dimensions': [{'Name': 'Repo', 'Value': r['repo']}]", APP.read_text(),
                      "the controller must publish the per-repo series this alarm reads")


def vcpu_cfg(max_vcpus=192, max_hosts=24):
    """Imagine's config as runner-app.tf passes it: a vCPU cap with a host-count backstop."""
    return dict(REPOS[IMAGINE], max=max_hosts, max_vcpus=max_vcpus)


def hosts(repo, sizes, first=0):
    """Listed hosts of these sizes, tagged by Size only (as hosts launched before the Vcpus tag)."""
    return [instance(f"i-{repo.split('/')[1]}-{first + n}", repo, str(9000 + first + n), 5, size=size)
            for n, size in enumerate(sizes)]


def old_claim(app, repo, job, nonce):
    """A claim made before claims recorded vCPUs: repo, job and nonce only."""
    t = int(NOW.timestamp())
    app.fake_dynamo.items[(repo, job)] = {"repo": {"S": repo}, "job": {"S": job}, "nonce": {"S": nonce},
                                          "expires_at": {"N": str(t + 600)}, "ttl": {"N": str(t + 86400)}}


class VcpuCapTests(unittest.TestCase):
    """A repo with `max_vcpus` (Imagine) waits when the vCPUs in use plus the job's own would pass it; `max` stays as a
    host-count backstop; a repo without `max_vcpus` is capped by hosts alone, exactly as before."""

    def ensure(self, app, cfg, job, size, live=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            outcome, _ = app.ensure_runner(IMAGINE, cfg, SUBNETS, "PAT", str(job), size, list(live or []))
        return outcome, out.getvalue()

    def launched_tags(self, ec2):
        return [{t["Key"]: t["Value"] for t in kw["TagSpecifications"][0]["Tags"]} for kw in ec2.launched]

    def test_at_the_vcpu_cap_an_s_job_waits_and_launches_while_its_room_is_left(self):
        app, ec2, *_ = load_app()
        cfg = vcpu_cfg()
        ec2.instances += hosts(IMAGINE, ["l"] * 5 + ["s"] * 3)          # 160 + 24 = 184 of 192: 8 left, room for one s
        self.assertEqual(self.ensure(app, cfg, 1, "s")[0], "launched", "8 vCPUs left fit an s host exactly")
        outcome, log = self.ensure(app, cfg, 2, "s")                     # 192 of 192 in use: nothing left
        self.assertEqual(outcome, "cap", "at the vCPU cap (9 hosts, under the 24-host backstop) an s job waits")
        self.assertIn("at its cap of 192 vCPUs (192 in use, an s runner needs 8); job 2 waits", log)
        self.assertEqual(len(ec2.launched), 1)

    def test_an_l_job_waits_when_only_s_sized_room_is_left(self):
        app, ec2, *_ = load_app()
        cfg = vcpu_cfg()
        ec2.instances += hosts(IMAGINE, ["l"] * 5 + ["s"])               # 168 of 192: 24 left, under an l's 32
        outcome, log = self.ensure(app, cfg, 1, "l")
        self.assertEqual(outcome, "cap")
        self.assertIn("(168 in use, an l runner needs 32)", log)
        self.assertEqual(ec2.launched, [])
        self.assertEqual(self.ensure(app, cfg, 2, "s")[0], "launched", "the same room still fits an s host")
        self.assertEqual([t["Size"] for t in self.launched_tags(ec2)], ["s"])

    def test_launches_the_listing_has_not_caught_up_with_count_their_own_size(self):
        # Separate deliveries, each before EC2 lists any of the earlier launches: only the claims stand for them, and
        # each must count its own host's vCPUs (not one slot, and not the largest size).
        app, ec2, *_ = load_app()
        ec2.listing_lags = True
        cfg = vcpu_cfg()
        for job, size in enumerate(["l"] * 5 + ["s"] * 3):                # 184 vCPUs claimed, none listed
            self.assertEqual(self.ensure(app, cfg, job, size)[0], "launched", (job, size))
        self.assertEqual(sorted(int(item["vcpus"]["N"]) for item in app.fake_dynamo.items.values()), [8] * 3 + [32] * 5,
                         "each claim records the vCPUs of the host it launches")
        outcome, log = self.ensure(app, cfg, 100, "l")
        self.assertEqual(outcome, "cap", "five l and three s claims are 184 vCPUs: an l does not fit")
        self.assertIn("(184 in use, an l runner needs 32)", log)
        self.assertEqual(self.ensure(app, cfg, 101, "s")[0], "launched", "an s does: claims count their size, not the largest")
        self.assertEqual(self.ensure(app, cfg, 102, "s")[0], "cap")
        self.assertEqual(len(ec2.launched), 9)

    def test_a_claim_made_before_claims_recorded_vcpus_counts_as_the_largest_size(self):
        app, ec2, *_ = load_app()
        cfg = vcpu_cfg()
        ec2.instances += hosts(IMAGINE, ["s"] * 7)                        # 56
        old_claim(app, IMAGINE, "500", "aaaa")                            # 64 each, as xl: 184 in all
        old_claim(app, IMAGINE, "501", "bbbb")
        outcome, log = self.ensure(app, cfg, 1, "l")
        self.assertEqual(outcome, "cap", "counted as s (72 in use) an l would launch; as the largest it must wait")
        self.assertIn("(184 in use, an l runner needs 32)", log)
        self.assertEqual(self.ensure(app, cfg, 2, "s")[0], "launched")
        self.assertEqual(self.ensure(app, cfg, 3, "s")[0], "cap")

    def test_a_repo_without_max_vcpus_is_capped_by_hosts_alone(self):
        # Colton has no vCPU cap: two xl hosts (128 vCPUs) and an xl job launch, as before, up to its 3 hosts. Its
        # launch still records its vCPUs (the host's Vcpus tag and its claim), which the LiveVcpus metric counts.
        app, ec2, *_ = load_app()
        cfg = REPOS[COLTON]
        ec2.instances += hosts(COLTON, ["xl", "xl"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            first, _ = app.ensure_runner(COLTON, cfg, SUBNETS, "PAT", "1", "xl", [])
            (item,) = app.fake_dynamo.items.values()   # the next check sees this launch listed and releases its claim
            second, _ = app.ensure_runner(COLTON, cfg, SUBNETS, "PAT", "2", "s", [])
        self.assertEqual((first, second), ("launched", "cap"))
        self.assertIn("at its cap of 3 runners; job 2 waits", out.getvalue())
        self.assertNotIn("vCPUs", out.getvalue())
        (tags,) = self.launched_tags(ec2)
        self.assertEqual((tags["Size"], tags["Vcpus"]), ("xl", "64"))
        self.assertEqual(item["vcpus"], {"N": "64"})

    def test_the_host_backstop_holds_under_a_vcpu_cap(self):
        app, ec2, *_ = load_app()
        cfg = vcpu_cfg(max_vcpus=192, max_hosts=3)
        ec2.instances += hosts(IMAGINE, ["s", "s"])                       # 16 of 192 vCPUs, 2 of 3 hosts
        self.assertEqual(self.ensure(app, cfg, 1, "s")[0], "launched")
        (item,) = app.fake_dynamo.items.values()
        self.assertEqual(item["vcpus"], {"N": "8"}, "the claim records its host's vCPUs")
        outcome, log = self.ensure(app, cfg, 2, "s")                     # 24 of 192 vCPUs, 3 of 3 hosts
        self.assertEqual(outcome, "cap", "176 vCPUs are free, but the host backstop is reached")
        self.assertIn("at its cap of 3 runners; job 2 waits", log)

    def test_a_host_counts_by_its_vcpus_tag_then_its_size_then_as_the_largest(self):
        app, *_ = load_app()
        cfg = vcpu_cfg()
        tagged = instance("i-t", IMAGINE, "1", 1, size="s")
        tagged["Tags"].append({"Key": "Vcpus", "Value": "12"})
        bare = instance("i-b", IMAGINE, "2", 1)
        bare["Tags"] = [t for t in bare["Tags"] if t["Key"] != "Size"]
        self.assertEqual([app.host_vcpus(i, cfg) for i in (tagged, instance("i-l", IMAGINE, "3", 1, size="l"), bare)],
                         [12, 32, 64])

    def test_each_repos_live_vcpus_are_published_next_to_its_host_count(self):
        app, ec2, *_ = load_app()
        busy = hosts(IMAGINE, ["l", "s", "s"])
        ec2.instances += busy
        app.github.runners[IMAGINE] = {i["InstanceId"]: {"id": n, "name": i["InstanceId"], "busy": True, "status": "online",
                                                         "labels": [{"name": "imagine"}]} for n, i in enumerate(busy)}
        app.handler({"reconcile": True}, None)
        _, data = app.fake_cloudwatch.metrics[-1]
        vcpus = {d["Dimensions"][0]["Value"]: d["Value"] for d in data if d["MetricName"] == "LiveVcpus"}
        self.assertEqual(vcpus, {COLTON: 0, DOLPHIN: 0, IMAGINE: 48})
        self.assertEqual({d["Unit"] for d in data if d["MetricName"] == "LiveVcpus"}, {"Count"})


class VcpuCapTerraformTests(unittest.TestCase):
    def test_imagine_throttles_to_8_hosts_bursts_to_its_backstop_and_colton_is_unchanged(self):
        rows = dict(re.findall(r'^\s+"([^"]+/[^"]+)"\s*=\s*(\{ label[^\n]*\})', APP_TF, re.M))
        self.assertEqual(rows[IMAGINE], '{ label = "imagine", max = 64, throttled_max = 8, image = "imagine-runner" }')
        self.assertEqual(rows[COLTON], '{ label = "cc-games", max = 8, image = "colton-games-runner" }')
        self.assertEqual(rows[DOLPHIN], '{ label = "dolphin", max = 64, throttled_max = 8, image = "dolphin-labs-runner" }')
        self.assertEqual([r for r, row in rows.items() if "max_vcpus" in row], [])
        config = re.search(r"runner_app_config = \{.*?\n  \}", APP_TF, re.S).group()
        self.assertIn("max_vcpus  = try(c.max_vcpus, null)", config)
        self.assertIn("vcpus      = local.runner_app_size_vcpus", config)

    def test_the_vcpu_map_has_every_size_and_matches_every_type_listed_under_it(self):
        vcpus = dict((k, int(v)) for k, v in re.findall(r"(\w+) = (\d+)", re.search(r"runner_app_size_vcpus = \{([^}]*)\}", APP_TF).group(1)))
        sizes = re.search(r"runner_app_sizes = \{(.*?)\n  \}", APP_TF, re.S).group(1)
        listed = {size: re.findall(r'"([^"]+)"', types_) for size, types_ in re.findall(r"(\w+)\s*=\s*\[([^\]]+)\]", sizes)}
        self.assertEqual(set(vcpus), set(listed))
        for size, names in listed.items():
            for name in names:
                self.assertEqual(4 * int(re.search(r"\.(\d+)xlarge$", name).group(1)), vcpus[size], name)
        fn = re.search(r'resource "aws_lambda_function" "runner_app" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn("precondition {", fn, "a plan refuses a type whose vCPUs differ from its size's")
        self.assertIn('== lookup(local.runner_app_size_vcpus, size, 0)', fn)

    def test_a_host_may_be_tagged_with_its_vcpus_at_launch(self):
        policy = re.search(r'resource "aws_iam_role_policy" "runner_app_lambda" \{.*?\n\}', APP_TF, re.S).group()
        keys = re.findall(r'"([^"]+)"', re.search(r'"aws:TagKeys" = \[([^\]]+)\]', policy).group(1))
        self.assertEqual(keys, ["Name", "Role", "Repo", "Size", "Vcpus", "JobId", "InspectorEc2Exclusion"])

    def test_each_vcpu_capped_repo_alarms_above_its_vcpu_cap(self):
        self.assertIn("runner_app_vcpu_capped = { for repo, c in local.runner_app_config : repo => c if c.peak_max_vcpus != null }", APP_TF)
        alarm = re.search(r'resource "aws_cloudwatch_metric_alarm" "too_many_app_runner_vcpus_per_repo" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn("for_each            = var.enable_github_runner ? local.runner_app_vcpu_capped : {}", alarm)
        self.assertIn('alarm_name          = "too-many-app-runner-vcpus-${try(local.runner_app_repos[each.key].alarm, each.value.label)}"', alarm)
        self.assertIn("threshold           = each.value.peak_max_vcpus", alarm)
        self.assertIn('metric_name         = "LiveVcpus"', alarm)
        self.assertIn("dimensions          = { Repo = each.key }", alarm)
        self.assertIn('statistic           = "Maximum"', alarm)
        self.assertIn("'MetricName': 'LiveVcpus', 'Dimensions': [{'Name': 'Repo', 'Value': r['repo']}]", APP.read_text())
        # The host alarms stay: the combined one sums the most hosts each repo may hold in any mode.
        self.assertIn("runner_app_max_total = sum([for c in values(local.runner_app_config) : c.peak_max])", APP_TF)
        combined = re.search(r'resource "aws_cloudwatch_metric_alarm" "too_many_app_runners" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn("threshold           = local.runner_app_max_total", combined)
        self.assertIn('metric_name         = "LiveRunners"', combined)

    def test_imagines_spend_alarm_counts_vcpus_past_its_buckets_daily_most(self):
        # $50 a day with two days banked lets out at most $150 in a day: 232 vCPUs all day at $0.027 per vCPU-hour; the
        # hosts running when it throttles add 3 h of 512 vCPUs, 64 averaged over the day. 296.
        alarm = re.search(r'resource "aws_cloudwatch_metric_alarm" "imagine_runner_hours_daily" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn('metric_name         = "LiveVcpus"', alarm)
        local = re.search(r"imagine_runner_alarm_vcpus = \((.*?)\n  \)", APP_TF, re.S).group(1)
        self.assertIn("ceil(local.runner_budget.imagine.daily_usd * (1 + local.runner_budget.imagine.capacity_days) / 24 / local.runner_cost.vcpu_hour_usd)", local)
        self.assertIn("+ ceil(local.runner_budget.imagine.burst_max_vcpus * 3 / 24)", local)
        import math
        self.assertEqual(math.ceil(50 * (1 + 2) / 24 / 0.027) + math.ceil(512 * 3 / 24), 296)
        self.assertIn('dimensions          = { Repo = "ejc3/imagine" }', alarm)
        self.assertIn('statistic           = "Average"', alarm)


# ---------------------------------------------------------------- budget buckets
FILMS = "dolphin-labs-hq/dolphin-films"
# runner-app.tf's runner_budget_defaults, as BUDGET_GROUPS carries them to the controller.
GROUP = {"daily_usd": 50, "capacity_days": 2, "burst_max": 64, "burst_max_vcpus": 512, "lookahead_hours": 2,
         "reserve_fraction": 0.15, "resume_fraction": 0.30}
# Each repo's config as runner-app.tf passes it once the budget groups exist. Colton is in no group, as there.
BUDGET_REPOS = {
    COLTON: dict(REPOS[COLTON], budget_group="", throttled_max=3, throttled_max_vcpus=None),
    DOLPHIN: dict(REPOS[DOLPHIN], max=64, budget_group="dolphin", throttled_max=8, throttled_max_vcpus=None),
    FILMS: dict(REPOS[DOLPHIN], max=64, budget_group="dolphin", throttled_max=2, throttled_max_vcpus=None,
                pat_secret=f"github-runner/repo-pat/{FILMS}"),
    IMAGINE: dict(REPOS[IMAGINE], max=64, budget_group="imagine", throttled_max=8),
}
# runner-app.tf's throttled_vcpus at $50 a day: (50/24 - hosts x 0.016) / 0.027, rounded down, with imagine's 8 throttled
# hosts and dolphin's 14 (8 + 2 + 2 + 2).
GROUPS = {"imagine": dict(GROUP, throttled_vcpus=72), "dolphin": dict(GROUP, throttled_vcpus=68)}
# Dollars an hour per host at $0.027 per vCPU-hour plus $0.016 per host-hour.
RATE = {"s": 8 * 0.027 + 0.016, "l": 32 * 0.027 + 0.016, "xl": 64 * 0.027 + 0.016}
REFILL_H = 50 / 24


def load_budget_app(groups=None):
    app, ec2, ssm, secrets = load_app({f"github-runner/repo-pat/{r}": f"PAT-{r}" for r in BUDGET_REPOS})
    os.environ["REPOS"] = json.dumps(BUDGET_REPOS)
    os.environ["BUDGET_GROUPS"] = json.dumps(groups or GROUPS)
    return app, ec2


def bucket(app, group, level, at=NOW, throttled=False, rate=0.0, spend=None, version=1):
    app.fake_dynamo.buckets[group] = {
        "group": {"S": group}, "level_usd": {"N": str(level)}, "updated_at": {"N": str(at.timestamp())},
        "rate_usd_h": {"N": str(rate)}, "throttled": {"BOOL": throttled},
        "spend": {"M": {h: {"N": str(v)} for h, v in (spend or {}).items()}}, "version": {"N": str(version)}}


def level_of(app, group):
    return float(app.fake_dynamo.buckets[group]["level_usd"]["N"])


def item(level, at, rate=0.0, throttled=False, spend=None, version=1):
    """A stored bucket as read_bucket() returns it."""
    return {"level_usd": level, "updated_at": at, "rate_usd_h": rate, "throttled": throttled, "spend": spend or {},
            "version": version}


def aged(repo, sizes, minutes, state="running", first=0):
    out = hosts(repo, sizes, first)
    for host in out:
        host["LaunchTime"], host["State"] = NOW - timedelta(minutes=minutes), {"Name": state}
    return out


class BudgetArithmeticTests(unittest.TestCase):
    """bucket_step(): refill at $50/86,400 s, drain by what the hosts cost, capped at two days' budget."""

    def step(self, app, stored, listed, now=NOW, g=GROUP):
        return app.bucket_step(stored, now.timestamp(), listed, lambda i: BUDGET_REPOS[IMAGINE], g)

    def test_an_hour_refills_a_twenty_fourth_of_the_budget_and_drains_what_ran(self):
        app, _ = load_budget_app()
        t = NOW.timestamp()
        # An l host live across the whole hour: charged the hour.
        state = self.step(app, item(40, t - 3600, rate=RATE["l"]), aged(IMAGINE, ["l"], 120))
        self.assertAlmostEqual(state["drained"], RATE["l"], places=9)
        self.assertAlmostEqual(state["level_usd"], 40 + REFILL_H - RATE["l"], places=9)
        # One launched half-way through: charged from its launch.
        state = self.step(app, item(40, t - 3600), aged(IMAGINE, ["l"], 30))
        self.assertAlmostEqual(state["drained"], RATE["l"] / 2, places=9)
        # One live at the last update and gone now: charged the whole interval, through the rate stored then.
        state = self.step(app, item(40, t - 3600, rate=RATE["l"]), [])
        self.assertAlmostEqual(state["drained"], RATE["l"], places=9)
        # One launched since the last update and terminated already (EC2 still lists it): charged from launch to now.
        state = self.step(app, item(40, t - 3600), aged(IMAGINE, ["l"], 20, state="terminated"))
        self.assertAlmostEqual(state["drained"], RATE["l"] * 20 / 60, places=9)
        self.assertAlmostEqual(state["rate_usd_h"], 0.0, places=9, msg="a terminated host burns nothing from now on")

    def test_the_level_is_capped_at_two_days_budget_and_never_goes_below_zero(self):
        app, _ = load_budget_app()
        t = NOW.timestamp()
        self.assertEqual(self.step(app, item(99, t - 3600), [])["level_usd"], 100, "capacity is 2 x $50")
        self.assertEqual(self.step(app, item(1, t - 3600, rate=8 * RATE["xl"]), aged(IMAGINE, ["xl"] * 8, 90))["level_usd"], 0)
        one_day = dict(GROUP, capacity_days=1)
        self.assertEqual(self.step(app, item(49, t - 3600), [], g=one_day)["level_usd"], 50)

    def test_time_moving_backwards_counts_as_none_and_a_huge_gap_as_three_hours(self):
        app, _ = load_budget_app()
        t = NOW.timestamp()
        state = self.step(app, item(40, t + 600, rate=RATE["s"]), aged(IMAGINE, ["s"], 30))
        self.assertEqual((state["level_usd"], state["drained"], state["elapsed"], state["updated_at"]), (40, 0.0, 0.0, t))
        state = self.step(app, item(0, t - 10 * 86400), [])
        self.assertEqual(state["elapsed"], 3 * 3600, "a gap is clamped to MAX_LIFETIME")
        self.assertAlmostEqual(state["level_usd"], 50 * 3 / 24, places=9)

    def test_the_7_day_spend_keeps_168_hours_of_drain(self):
        app, _ = load_budget_app()
        t = NOW.timestamp()
        old, recent = str(int(t - 8 * 86400) // 3600 * 3600), str(int(t - 6 * 86400) // 3600 * 3600)
        state = self.step(app, item(40, t - 3600, rate=RATE["l"], spend={old: 30.0, recent: 12.5}), aged(IMAGINE, ["l"], 120))
        self.assertNotIn(old, state["spend"])
        self.assertAlmostEqual(sum(state["spend"].values()), 12.5 + RATE["l"], places=9)

    def test_a_group_idle_two_days_bursts_on_day_three_past_a_one_day_bucket_and_stops_at_two_days(self):
        app, _ = load_budget_app()

        def run(g):
            t, stored = NOW.timestamp(), item(0, NOW.timestamp())
            for _ in range(3 * 720):                                    # three idle days of 2-minute reconciles
                t += 120
                stored = self.step(app, stored, [], now=datetime.fromtimestamp(t, timezone.utc), g=g)
                self.assertLessEqual(stored["level_usd"], 50 * g["capacity_days"] + 1e-9)
            banked, start = stored["level_usd"], t
            burst = [dict(h, LaunchTime=datetime.fromtimestamp(t, timezone.utc)) for h in aged(IMAGINE, ["s"] * 64, 0)]
            spent = 0.0
            while not stored["throttled"]:                              # 64 s hosts, 512 vCPUs, until it throttles
                t += 120
                stored = self.step(app, stored, burst, now=datetime.fromtimestamp(t, timezone.utc), g=g)
                spent += stored["drained"]
            return banked, spent, (t - start) / 3600

        banked, spent, hours = run(GROUP)
        self.assertAlmostEqual(banked, 100, places=6, msg="three idle days bank two days' budget, not three")
        one_day_banked, one_day_spent, _ = run(dict(GROUP, capacity_days=1))
        self.assertAlmostEqual(one_day_banked, 50, places=6)
        self.assertGreater(spent, 50 + REFILL_H * hours, "more than a one-day bucket could ever let out in that time")
        self.assertGreater(spent, 2.5 * one_day_spent)
        self.assertLessEqual(spent, 100 + REFILL_H * hours + 1e-6, "no more than the two-day bank plus the refill")

    def test_the_mode_throttles_at_the_reserve_or_the_projection_and_resumes_only_at_the_higher_mark(self):
        app, _ = load_budget_app()
        throttle = app.would_throttle
        self.assertTrue(throttle(14.99, False, 0, GROUP), "under the 15% reserve")
        self.assertFalse(throttle(15.0, False, 0, GROUP))
        burn = 9 * RATE["xl"]                                           # $15.70/h: empties $25 within two hours
        self.assertTrue(throttle(25.0, False, burn, GROUP))
        self.assertFalse(throttle(30.0, False, burn, GROUP))
        self.assertTrue(throttle(29.99, True, 0, GROUP), "throttled stays throttled below the 30% mark")
        self.assertFalse(throttle(30.0, True, 0, GROUP))
        self.assertTrue(throttle(80.0, True, 50.0, GROUP), "and resumes only with a clear projection")


class BudgetLaunchTests(unittest.TestCase):
    """The bucket deciding launches through ensure_runner() and the reconcile."""

    def ensure(self, app, repo, job, size, live=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            outcome, _ = app.ensure_runner(repo, BUDGET_REPOS[repo], SUBNETS, "PAT", str(job), size, list(live or []))
        return outcome, out.getvalue()

    def reconcile(self, app):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = app.handler({"reconcile": True}, None)
        return result, out.getvalue()

    def test_burst_under_budget_lets_imagine_past_its_old_cap_of_8(self):
        app, ec2 = load_budget_app()
        ec2.instances += aged(IMAGINE, ["s"] * 8, 5)
        outcome, _ = self.ensure(app, IMAGINE, 9, "s")
        self.assertEqual(outcome, "launched", "a full bucket bursts past the throttled cap of 8")
        self.assertEqual(len(ec2.launched), 1)
        stored = app.fake_dynamo.buckets["imagine"]
        self.assertEqual((stored["throttled"], level_of(app, "imagine")), ({"BOOL": False}, 100.0))
        self.assertEqual(app.limits(BUDGET_REPOS[IMAGINE], False), (64, 512))
        self.assertEqual(app.limits(BUDGET_REPOS[IMAGINE], True), (8, None), "throttled vCPUs are the group's (72)")
        self.assertEqual(app.limits(dict(BUDGET_REPOS[IMAGINE], max=0), False), (0, 512), "max = 0 still stops it")

    def test_a_projection_that_would_empty_within_the_lookahead_throttles_and_refuses_the_9th_host(self):
        app, ec2 = load_budget_app()
        live = aged(IMAGINE, ["xl"] * 8, 5)
        ec2.instances += live
        bucket(app, "imagine", 25.0, rate=8 * RATE["xl"])
        self.assertFalse(app.would_throttle(25.0, False, 8 * RATE["xl"], GROUP), "without the 9th the projection is clear")
        outcome, log = self.ensure(app, IMAGINE, 9, "xl")
        self.assertEqual(outcome, "cap")
        self.assertIn("ejc3/imagine: at its cap of 8 runners; job 9 waits (throttled, budget imagine at $25.00 of $100.00)", log)
        self.assertEqual(ec2.launched, [])
        self.assertEqual(app.fake_dynamo.buckets["imagine"]["throttled"], {"BOOL": True})
        self.assertEqual(app.fake_dynamo.items, {}, "a job held back by the bucket takes no claim")

    def test_a_throttled_group_stays_throttled_until_the_level_clears_the_resume_mark(self):
        app, ec2 = load_budget_app()
        ec2.instances += aged(IMAGINE, ["s"] * 8, 5)
        bucket(app, "imagine", 28.0, throttled=True, rate=8 * RATE["s"])
        self.assertFalse(app.would_throttle(28.0, False, 9 * RATE["s"], GROUP), "in burst, $28 would let the 9th through")
        outcome, log = self.ensure(app, IMAGINE, 9, "s")
        self.assertEqual(outcome, "cap")
        self.assertIn("(throttled, budget imagine at $28.00 of $100.00)", log)
        bucket(app, "imagine", 31.0, throttled=True, rate=8 * RATE["s"], version=7)
        self.assertEqual(self.ensure(app, IMAGINE, 9, "s")[0], "launched")
        self.assertEqual(app.fake_dynamo.buckets["imagine"]["throttled"], {"BOOL": False})

    def test_a_missing_bucket_starts_full(self):
        app, ec2 = load_budget_app()
        self.reconcile(app)
        self.assertEqual({g: level_of(app, g) for g in app.fake_dynamo.buckets}, {"imagine": 100.0, "dolphin": 100.0})
        bucket(app, "imagine", 3.0, at=NOW - timedelta(minutes=2))
        del app.fake_dynamo.buckets["imagine"]                          # deleted by hand
        self.reconcile(app)
        self.assertEqual(level_of(app, "imagine"), 100.0)

    def test_a_lost_write_rereads_and_counts_the_interval_once(self):
        app, ec2 = load_budget_app()
        bucket(app, "imagine", 50.0, at=NOW - timedelta(hours=1), version=4)
        # Another writer refilled the same hour first; the loser must start from its state, not refill the hour again.
        app.fake_dynamo.conflicts = 1
        app.fake_dynamo.rival = lambda held: dict(held, level_usd={"N": str(50 + REFILL_H)},
                                                  updated_at={"N": str(NOW.timestamp())}, version={"N": "5"})
        self.assertEqual(self.ensure(app, IMAGINE, 1, "s")[0], "launched")
        self.assertAlmostEqual(level_of(app, "imagine"), 50 + REFILL_H, places=5)
        self.assertEqual(len(app.fake_dynamo.bucket_writes), 2)
        self.assertEqual(app.fake_dynamo.buckets["imagine"]["version"], {"N": "6"})

    def test_a_bucket_that_keeps_changing_defers_the_launch(self):
        app, ec2 = load_budget_app()
        bucket(app, "imagine", 50.0, at=NOW - timedelta(minutes=2), version=4)
        app.fake_dynamo.conflicts = 3
        app.fake_dynamo.rival = lambda held: held and dict(held, version={"N": str(int(held["version"]["N"]) + 1)})
        outcome, log = self.ensure(app, IMAGINE, 1, "s")
        self.assertEqual(outcome, "deferred")
        self.assertIn("budget imagine: the bucket changed under 3 writes in a row; job 1 waits for the next round", log)
        self.assertEqual((ec2.launched, app.fake_dynamo.items), ([], {}), "no launch and no claim")
        # In a reconcile the backlog stops there, as at a cap.
        app.github.runs[(IMAGINE, "queued")] = [1]
        app.github.jobs[1] = [{"id": n, "status": "queued", "labels": ["self-hosted", "imagine", "s"]} for n in (21, 22)]
        # Every write loses: the round's own updates (dolphin's, then imagine's, three each, skipped and logged) and
        # the first job's three.
        app.fake_dynamo.conflicts = 9
        result, _ = self.reconcile(app)
        self.assertEqual([r["outcomes"] for r in result if r["repo"] == IMAGINE], [{"deferred": 1}])

    def test_the_dolphin_repos_share_one_bucket(self):
        app, ec2 = load_budget_app()
        ec2.instances += aged(DOLPHIN, ["s"] * 3, 120) + aged(FILMS, ["l"] * 2, 120, first=10)
        bucket(app, "dolphin", 60.0, at=NOW - timedelta(hours=1), rate=3 * RATE["s"] + 2 * RATE["l"])
        self.reconcile(app)
        self.assertEqual(sorted(app.fake_dynamo.buckets), ["dolphin", "imagine"], "one bucket for every dolphin repo")
        self.assertAlmostEqual(level_of(app, "dolphin"), 60 + REFILL_H - 3 * RATE["s"] - 2 * RATE["l"], places=5)
        # dolphin-labs' burn throttles dolphin-films: films has two small hosts, and its xl job meets its throttled cap.
        app, ec2 = load_budget_app()
        ec2.instances += aged(DOLPHIN, ["xl"] * 8, 5) + aged(FILMS, ["s"] * 2, 5, first=10)
        bucket(app, "dolphin", 25.0, rate=8 * RATE["xl"] + 2 * RATE["s"])
        outcome, log = self.ensure(app, FILMS, 9, "xl")
        self.assertEqual(outcome, "cap")
        self.assertIn("at its cap of 2 runners; job 9 waits (throttled, budget dolphin at $25.00 of $100.00)", log)

    def test_throttled_spend_cannot_outrun_the_refill(self):
        # Throttled, the group fills to its 72 vCPUs whatever the sizes, and what it then burns is under the refill, so
        # the bucket cannot drain while throttled.
        for sizes in (["s"] * 12, ["l"] * 4, ["xl", "s", "s", "l"], ["l", "s"] * 6):
            app, ec2 = load_budget_app()
            bucket(app, "imagine", 5.0, throttled=True)
            for job, size in enumerate(sizes):
                self.ensure(app, IMAGINE, job, size)
            live = [{t["Key"]: t["Value"] for t in kw["TagSpecifications"][0]["Tags"]} for kw in ec2.launched]
            vcpus = sum(int(t["Vcpus"]) for t in live)
            burn = sum(RATE[t["Size"]] for t in live)
            self.assertLessEqual(vcpus, 72, sizes)
            self.assertLessEqual(burn, REFILL_H, (sizes, burn))
            self.assertLessEqual(len(live), 8, sizes)
            # The bucket then rises an hour later, never falls.
            app.now = lambda: NOW + timedelta(hours=1)
            state = app.update_bucket("imagine", BUDGET_REPOS, [dict(h, State={"Name": "running"}) for h in ec2.instances])
            self.assertGreaterEqual(state["level_usd"], 5.0, sizes)
        # The cap the reconcile holds the group to, from Terraform's formula at these constants.
        self.assertEqual(math.floor((50 / 24 - 8 * 0.016) / 0.027), 72)
        self.assertEqual(math.floor((50 / 24 - 14 * 0.016) / 0.027), 68)

    def test_a_single_large_job_runs_when_its_throttled_group_holds_nothing(self):
        # A $20 a day group's throttled cap is under one xl host's 64 vCPUs; the xl still runs, alone.
        small = dict(GROUP, daily_usd=20, throttled_vcpus=26)
        app, ec2 = load_budget_app({"imagine": small, "dolphin": small})
        bucket(app, "imagine", 1.0, throttled=True)
        self.assertEqual(self.ensure(app, IMAGINE, 1, "xl")[0], "launched", "an empty group takes one host of any size")
        outcome, log = self.ensure(app, IMAGINE, 2, "s")
        self.assertEqual(outcome, "cap")
        self.assertIn("at its group's throttled cap of 26 vCPUs (64 in use across budget imagine, an s runner needs 8); job 2 waits"
                      " (throttled, budget imagine at $1.00 of $40.00)", log)
        # Not listed yet, the xl still counts by its claim: the group is not empty.
        app, ec2 = load_budget_app({"imagine": small, "dolphin": small})
        ec2.listing_lags = True
        bucket(app, "imagine", 1.0, throttled=True)
        self.assertEqual(self.ensure(app, IMAGINE, 1, "l")[0], "launched")
        self.assertEqual(self.ensure(app, IMAGINE, 2, "l")[0], "cap")
        # At $50 a day an l and an xl each fit on their own, and an xl fits beside an s.
        app, ec2 = load_budget_app()
        bucket(app, "imagine", 1.0, throttled=True)
        self.assertEqual([self.ensure(app, IMAGINE, j, z)[0] for j, z in enumerate(["xl", "s", "s"])], ["launched", "launched", "cap"])

    def test_the_dolphin_repos_share_the_throttled_cap(self):
        # dolphin-labs holds 64 of the group's 68 throttled vCPUs; dolphin-films, holding nothing of its own cap of 2, waits.
        app, ec2 = load_budget_app()
        ec2.instances += aged(DOLPHIN, ["s"] * 8, 5)
        bucket(app, "dolphin", 5.0, throttled=True, rate=8 * RATE["s"])
        outcome, log = self.ensure(app, FILMS, 9, "s")
        self.assertEqual(outcome, "cap")
        self.assertIn("dolphin-labs-hq/dolphin-films: at its group's throttled cap of 68 vCPUs (64 in use across budget dolphin, "
                      "an s runner needs 8); job 9 waits (throttled", log)
        # A launch of dolphin-labs' that EC2 does not list yet counts too (its claim), for the other repos.
        app, ec2 = load_budget_app()
        ec2.listing_lags = True
        bucket(app, "dolphin", 5.0, throttled=True)
        self.assertEqual(self.ensure(app, DOLPHIN, 1, "xl")[0], "launched")
        self.assertEqual(self.ensure(app, FILMS, 2, "s")[0], "cap", "64 claimed by dolphin-labs + 8 passes 68")
        # In burst the same hosts hold nothing back: the cap is the throttled mode's.
        app, ec2 = load_budget_app()
        ec2.instances += aged(DOLPHIN, ["s"] * 8, 5)
        self.assertEqual(self.ensure(app, FILMS, 9, "s")[0], "launched")

    def test_colton_games_is_in_no_bucket_and_keeps_its_own_cap(self):
        app, ec2 = load_budget_app()
        ec2.instances += aged(COLTON, ["xl"] * 3, 5)
        app.github.runs[(COLTON, "queued")] = [1]
        app.github.jobs[1] = [{"id": 31, "status": "queued", "labels": ["self-hosted", "cc-games", "s"]}]
        _, log = self.reconcile(app)
        self.assertIn("CoderColton/colton-games: at its cap of 3 runners; job 31 waits\n", log)
        self.assertEqual(sorted(set(app.fake_dynamo.bucket_reads)), ["dolphin", "imagine"])
        _, data = app.fake_cloudwatch.metrics[-1]
        self.assertEqual({d["Dimensions"][0]["Value"] for d in data if d["MetricName"] == "BucketLevelUsd"}, {"imagine", "dolphin"})
        self.assertEqual(ec2.launched, [])

    def test_throttling_never_terminates_a_running_host(self):
        app, ec2 = load_budget_app()
        busy = aged(IMAGINE, ["s"] * 20, 30)                            # 20 hosts, past the throttled cap of 8
        ec2.instances += busy
        app.github.runners[IMAGINE] = {i["InstanceId"]: {"id": n, "name": i["InstanceId"], "busy": True, "status": "online",
                                                         "labels": [{"name": "imagine"}]} for n, i in enumerate(busy)}
        app.github.runs[(IMAGINE, "queued")] = [1]
        app.github.jobs[1] = [{"id": 41, "status": "queued", "labels": ["self-hosted", "imagine", "s"]}]
        bucket(app, "imagine", 5.0, at=NOW - timedelta(minutes=2), throttled=True, rate=20 * RATE["s"])
        result, log = self.reconcile(app)
        self.assertEqual(ec2.terminated, [])
        self.assertEqual(ec2.launched, [])
        self.assertIn("at its cap of 8 runners; job 41 waits (throttled", log)
        self.assertEqual([r["live"] for r in result if r["repo"] == IMAGINE], [20])

    def test_each_group_publishes_its_level_its_mode_and_its_7_day_spend(self):
        app, ec2 = load_budget_app()
        ec2.instances += aged(IMAGINE, ["l"], 120)
        recent = str(int(NOW.timestamp() - 86400) // 3600 * 3600)
        bucket(app, "imagine", 20.0, at=NOW - timedelta(hours=1), rate=RATE["l"], spend={recent: 40.0})
        self.reconcile(app)
        _, data = app.fake_cloudwatch.metrics[-1]
        got = {(d["MetricName"], d["Dimensions"][0]["Value"]): d["Value"] for d in data if d["Dimensions"][0]["Name"] == "Group"}
        self.assertAlmostEqual(got[("BucketLevelUsd", "imagine")], 20 + REFILL_H - RATE["l"], places=3)
        self.assertAlmostEqual(got[("SpendUsd7d", "imagine")], 40 + RATE["l"], places=3)
        self.assertEqual((got[("Throttled", "imagine")], got[("Throttled", "dolphin")]), (0, 0))
        self.assertEqual(got[("BucketLevelUsd", "dolphin")], 100.0)


class BudgetTerraformTests(unittest.TestCase):
    def test_two_50_dollar_buckets_two_days_deep_imagine_alone_and_the_dolphin_repos_together(self):
        groups = re.search(r"runner_budget_groups = \{(.*?)\n  \}", APP_TF, re.S).group(1)
        self.assertIn('imagine = { daily_usd = 50, repos = ["ejc3/imagine"] }', groups)
        self.assertIn('dolphin = { daily_usd = 50, repos = ["dolphin-labs-hq/dolphin-labs", "dolphin-labs-hq/dolphin-films", '
                      '"dolphin-labs-hq/dolphin-maps", "dolphin-labs-hq/dolphin-family"] }', groups)
        self.assertNotIn(COLTON, groups)
        defaults = dict(re.findall(r"^\s+(\w+)\s+= ([\d.]+)", re.search(r"runner_budget_defaults = \{(.*?)\n  \}", APP_TF, re.S).group(1), re.M))
        self.assertEqual(defaults, {"capacity_days": "2", "burst_max": "64", "burst_max_vcpus": "512", "lookahead_hours": "2",
                                    "reserve_fraction": "0.15", "resume_fraction": "0.30", "throttled_alarm_hours": "8"})
        rows = dict(re.findall(r'^\s+"([^"]+/[^"]+)"\s*=\s*(\{ label[^\n]*\})', APP_TF, re.M))
        for repo, throttled in ((DOLPHIN, 8), (FILMS, 2), ("dolphin-labs-hq/dolphin-maps", 2), ("dolphin-labs-hq/dolphin-family", 2), (IMAGINE, 8)):
            self.assertIn(f"max = 64, throttled_max = {throttled}", rows[repo], repo)

    def test_the_cost_constants_are_stated_with_their_sources_and_passed_to_the_controller(self):
        cost = re.search(r"runner_cost = \{(.*?)\n  \}", APP_TF, re.S).group(1)
        self.assertIn("vcpu_hour_usd = 0.027", cost)
        self.assertIn("host_hour_usd = 0.016", cost)
        for source in ("describe-spot-price-history", "$0.005/h", "$0.096/GB-month"):
            self.assertIn(source, cost)
        fn = re.search(r'resource "aws_lambda_function" "runner_app" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn("VCPU_HOUR_USD = tostring(local.runner_cost.vcpu_hour_usd)", fn)
        self.assertIn("HOST_HOUR_USD = tostring(local.runner_cost.host_hour_usd)", fn)
        self.assertIn("BUDGET_TABLE = aws_dynamodb_table.runner_app_budget[0].name", fn)
        self.assertRegex(APP_TF, r'budget_group\s+= lookup\(local.runner_app_budget_group, repo, ""\)')

    def test_the_throttled_vcpu_cap_is_the_refill_rate_less_the_throttled_hosts_overhead(self):
        budget = re.search(r"runner_budget = \{ for name, g in local.runner_budget_groups : name => merge\(.*?\n  \}\) \}", APP_TF, re.S).group()
        self.assertIn("throttled_vcpus = max(0, floor((g.daily_usd / 24 - sum(concat([0], [for r in g.repos : "
                      "try(local.runner_app_repos[r].throttled_max, 0)])) * local.runner_cost.host_hour_usd) / "
                      "local.runner_cost.vcpu_hour_usd))", budget)
        fn = re.search(r'resource "aws_lambda_function" "runner_app" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn("throttled_vcpus = g.throttled_vcpus", fn)
        self.assertNotIn("throttled_max_vcpus", APP_TF, "the throttled vCPU cap is the group's, not a repo's")

    def test_the_bucket_table_and_the_controllers_access_to_it(self):
        table = re.search(r'resource "aws_dynamodb_table" "runner_app_budget" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn('hash_key     = "group"', table)
        policy = re.search(r'resource "aws_iam_role_policy" "runner_app_lambda" \{.*?\n\}', APP_TF, re.S).group()
        stmt = policy.split('Sid      = "BudgetBuckets"', 1)[1].split("},", 1)[0]
        self.assertIn('Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]', stmt)
        self.assertIn("Resource = aws_dynamodb_table.runner_app_budget[0].arn", stmt)

    def test_each_group_alarms_when_throttled_too_long_and_when_a_week_costs_more_than_its_budget(self):
        throttled = re.search(r'resource "aws_cloudwatch_metric_alarm" "runner_budget_throttled" \{.*?\n\}', APP_TF, re.S).group()
        for line in ('metric_name         = "Throttled"', "dimensions          = { Group = each.key }", "period              = 3600",
                     "evaluation_periods  = 24", "datapoints_to_alarm = each.value.throttled_alarm_hours + 1",
                     'statistic           = "Average"', "threshold           = 0.5"):
            self.assertIn(line, throttled)
        weekly = re.search(r'resource "aws_cloudwatch_metric_alarm" "runner_budget_weekly" \{.*?\n\}', APP_TF, re.S).group()
        for line in ('metric_name         = "SpendUsd7d"', "dimensions          = { Group = each.key }",
                     "threshold           = 7 * each.value.daily_usd", 'statistic           = "Maximum"'):
            self.assertIn(line, weekly)
        source = APP.read_text()
        for metric in ("BucketLevelUsd", "Throttled", "SpendUsd7d"):
            self.assertIn(f"'MetricName': '{metric}', 'Dimensions': dims", source)
        self.assertIn("dims = [{'Name': 'Group', 'Value': state['group']}]", source)
        # The per-repo alarms watch the burst ceilings: a burst within them is the bucket's to allow.
        self.assertIn("peak_max = contains(keys(local.runner_app_budget_group), repo) ? min(c.max, local.runner_budget[local.runner_app_budget_group[repo]].burst_max) : c.max", APP_TF)


class RunnersAppTokenTests(unittest.TestCase):
    """The runners GitHub App: a pure-Python RS256 JWT that openssl verifies, an installation token narrowed to the one repo,
    reuse while it lasts, and the repo's own PAT whenever the app is not set up or minting fails."""

    @classmethod
    def setUpClass(cls):
        import subprocess, tempfile
        cls.dir = tempfile.mkdtemp()
        cls.pkcs1, cls.pkcs8, cls.pub = (os.path.join(cls.dir, n) for n in ("k1.pem", "k8.pem", "k.pub"))
        subprocess.run(["openssl", "genrsa", "-traditional", "-out", cls.pkcs1, "2048"], check=True, capture_output=True)
        subprocess.run(["openssl", "pkcs8", "-topk8", "-nocrypt", "-in", cls.pkcs1, "-out", cls.pkcs8], check=True, capture_output=True)
        subprocess.run(["openssl", "rsa", "-in", cls.pkcs1, "-pubout", "-out", cls.pub], check=True, capture_output=True)

    def verify(self, message, signature):
        import subprocess, tempfile
        with tempfile.NamedTemporaryFile(delete=False) as s:
            s.write(signature)
        r = subprocess.run(["openssl", "dgst", "-sha256", "-verify", self.pub, "-signature", s.name], input=message, capture_output=True)
        os.unlink(s.name)
        return r.returncode == 0

    def test_rs256_verifies_with_openssl_for_both_key_formats(self):
        app, *_ = load_app()
        for path in (self.pkcs1, self.pkcs8):
            pem = open(path).read()
            self.assertTrue(self.verify(b"hello", app.rs256(b"hello", pem)), path)
            self.assertFalse(self.verify(b"hellO", app.rs256(b"hello", pem)), "a different message must not verify")
        self.assertIn("BEGIN RSA PRIVATE KEY", open(self.pkcs1).read(), "GitHub issues PKCS#1 keys")

    def test_the_jwt_is_signed_and_valid_nine_minutes(self):
        app, *_ = load_app()
        jwt = app.app_jwt(4242, open(self.pkcs1).read(), at=1_800_000_000)
        head, body, sig = jwt.split(".")
        pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
        self.assertEqual(json.loads(pad(body)), {"iat": 1_800_000_000 - 60, "exp": 1_800_000_000 + 540, "iss": "4242"})
        self.assertTrue(self.verify(f"{head}.{body}".encode(), pad(sig)))

    # What each installation covers, as GitHub reports it (account, selection, repositories): 777 is dolphin-labs-hq's,
    # with the three repos live today, 888 is ejc3's.
    SCOPES = {"777": ("dolphin-labs-hq", "selected", ["dolphin-labs-hq/dolphin-labs", "dolphin-labs-hq/dolphin-films",
                                                       "dolphin-labs-hq/dolphin-maps"]),
              "888": ("ejc3", "selected", ["ejc3/imagine"])}
    APP_INSTALLATION_REPOS = ["dolphin-labs-hq/dolphin-labs", "dolphin-labs-hq/dolphin-films", "dolphin-labs-hq/dolphin-maps",
                              "ejc3/imagine"]

    def app_with(self, secret_value, mint=None, scopes=None):
        tokens = {f"github-runner/repo-pat/{DOLPHIN}": "PAT-DOLPHIN", f"github-runner/repo-pat/{COLTON}": "PAT-COLTON",
                  f"github-runner/repo-pat/{IMAGINE}": "PAT-IMAGINE"}
        if secret_value is not None:
            tokens["github-app/runners"] = secret_value
        app, ec2, ssm, secrets = load_app(tokens=tokens)
        app.RUNNER_APP_SECRET = "github-app/runners"
        app._APP_TOKENS.clear()
        app._APP_SCOPE_OK.clear()
        # As in production, the controller's config names app_repo for the repos the app serves (not Colton's).
        served = {repo: dict(cfg, app_repo=repo if repo != COLTON else "") for repo, cfg in REPOS.items()}
        app.config = lambda: (served, SUBNETS)
        os.environ["APP_INSTALLATION_REPOS"] = json.dumps(self.APP_INSTALLATION_REPOS)
        scopes = scopes or self.SCOPES
        calls, scope_calls, probed = [], [], []

        def github(method, path, pat, body=None):
            installation = path.split("/")[3] if path.startswith("/app/installations/") else None
            if method == "GET" and path.startswith("/app/installations/"):
                scope_calls.append(path)
                return {"account": {"login": scopes[installation][0]}, "repository_selection": scopes[installation][1]}
            if method == "DELETE" and path == "/installation/token":
                scope_calls.append(path)
                self.assertEqual(pat, "ghs_probe", "only the listing token is revoked")
                return {}
            if method == "POST" and path.endswith("/access_tokens") and "repositories" not in (body or {}):
                scope_calls.append(path)
                self.assertEqual(body, {"permissions": {"metadata": "read"}}, "the listing token reads metadata only")
                probed.append(installation)
                return {"token": "ghs_probe"}
            if method == "GET" and path.startswith("/installation/repositories"):
                scope_calls.append(path)
                self.assertEqual(pat, "ghs_probe")
                names = scopes[probed[-1]][2]
                return {"total_count": len(names), "repositories": [{"full_name": n, "name": n.split("/")[1]} for n in names]}
            calls.append((method, path, body))
            if mint is not None:
                return mint(method, path, pat, body)
            return {"token": "ghs_minted", "expires_at": "2099-01-01T00:00:00Z"}
        app.github = github
        app.scope_calls = scope_calls
        return app, calls, secrets

    def creds(self):
        return json.dumps({"app_id": 9, "private_key": open(self.pkcs1).read(), "installations": {"dolphin-labs-hq": 777}})

    def test_an_app_repo_gets_a_narrowed_installation_token_reused_while_it_lasts(self):
        app, calls, _ = self.app_with(self.creds())
        cfg = dict(REPOS[DOLPHIN], app_repo=DOLPHIN)
        self.assertEqual(app.repo_pat(cfg), "ghs_minted")
        self.assertEqual(app.repo_pat(cfg), "ghs_minted")
        self.assertEqual(len(calls), 1, "the token is reused, not minted per call")
        method, path, body = calls[0]
        self.assertEqual((method, path), ("POST", "/app/installations/777/access_tokens"))
        self.assertEqual(body, {"repositories": ["dolphin-labs"], "permissions": {"administration": "write", "actions": "read", "metadata": "read"}})

    def test_without_the_app_secret_value_the_repo_pat_is_used(self):
        app, calls, _ = self.app_with(None)
        self.assertEqual(app.repo_pat(dict(REPOS[DOLPHIN], app_repo=DOLPHIN)), "PAT-DOLPHIN")
        self.assertEqual(calls, [])

    def test_an_owner_without_an_installation_uses_its_pat(self):
        app, calls, _ = self.app_with(self.creds())
        self.assertEqual(app.repo_pat(dict(REPOS[COLTON], app_repo=COLTON)), "PAT-COLTON")
        self.assertEqual(calls, [])

    def test_imagine_uses_its_pat_until_the_app_has_an_ejc3_installation_then_a_token_for_imagine_only(self):
        # The app is installed on dolphin-labs-hq only until the operator installs it on ejc3: until then Imagine's PAT.
        app, calls, _ = self.app_with(self.creds())
        self.assertEqual(app.repo_pat(dict(REPOS[IMAGINE], app_repo=IMAGINE)), "PAT-IMAGINE")
        self.assertEqual(calls, [])
        both = json.dumps({"app_id": 9, "private_key": open(self.pkcs1).read(),
                           "installations": {"dolphin-labs-hq": 777, "ejc3": 888}})
        app, calls, _ = self.app_with(both)
        self.assertEqual(app.repo_pat(dict(REPOS[IMAGINE], app_repo=IMAGINE)), "ghs_minted")
        ((method, path, body),) = calls
        self.assertEqual((method, path), ("POST", "/app/installations/888/access_tokens"))
        self.assertEqual(body["repositories"], ["imagine"], "narrowed to imagine, never every ejc3 repository")

    def test_a_failed_mint_falls_back_to_the_pat_and_says_why(self):
        def mint(*a):
            raise RuntimeError("GitHub 502")
        app, calls, _ = self.app_with(self.creds(), mint)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(app.repo_pat(dict(REPOS[DOLPHIN], app_repo=DOLPHIN)), "PAT-DOLPHIN")
        self.assertIn("runners app token failed", out.getvalue())

    def test_a_repo_without_app_repo_never_reads_the_app_secret(self):
        app, calls, secrets = self.app_with(self.creds())
        self.assertEqual(app.repo_pat(REPOS[DOLPHIN]), "PAT-DOLPHIN")
        self.assertNotIn("github-app/runners", secrets.read)
        self.assertEqual(calls, [])

    def test_an_installation_on_all_repositories_is_refused_and_the_pat_used(self):
        # GitHub's default choice when installing an app: every repository of the owner, ejc3/aws included.
        scopes = dict(self.SCOPES, **{"777": ("dolphin-labs-hq", "all", ["dolphin-labs-hq/dolphin-labs", "dolphin-labs-hq/dolphin-aws"])})
        app, calls, _ = self.app_with(self.creds(), scopes=scopes)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(app.repo_pat(dict(REPOS[DOLPHIN], app_repo=DOLPHIN)), "PAT-DOLPHIN")
        self.assertEqual(calls, [], "no administration token is minted from it")
        self.assertIn("covers all repositories", out.getvalue())

    def test_an_installation_that_also_selects_another_repository_is_refused(self):
        scopes = dict(self.SCOPES, **{"888": ("ejc3", "selected", ["ejc3/imagine", "ejc3/aws"])})
        both = json.dumps({"app_id": 9, "private_key": open(self.pkcs1).read(),
                           "installations": {"dolphin-labs-hq": 777, "ejc3": 888}})
        app, calls, _ = self.app_with(both, scopes=scopes)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(app.repo_pat(dict(REPOS[IMAGINE], app_repo=IMAGINE)), "PAT-IMAGINE")
        self.assertEqual(calls, [])
        self.assertIn("also covers ['ejc3/aws']", out.getvalue())

    def test_the_scope_check_runs_once_an_hour_per_installation(self):
        app, calls, _ = self.app_with(self.creds())
        cfg = dict(REPOS[DOLPHIN], app_repo=DOLPHIN)
        self.assertEqual(app.repo_pat(cfg), "ghs_minted")
        self.assertEqual(len(app.scope_calls), 4, "installation, listing token, listing, revoke")
        self.assertEqual(app.scope_calls[-1], "/installation/token")
        app._APP_TOKENS.clear()
        self.assertEqual(app.repo_pat(cfg), "ghs_minted")
        self.assertEqual(len(app.scope_calls), 4, "the passed check is remembered")
        app._APP_TOKENS.clear()
        app._APP_SCOPE_OK[(777, "dolphin-labs-hq")] = (0, None)  # the hour is up
        self.assertEqual(app.repo_pat(cfg), "ghs_minted")
        self.assertEqual(len(app.scope_calls), 8, "and checked again once it expires")

    def test_a_covered_repo_whose_gate_is_off_does_not_make_the_installation_too_broad(self):
        # dolphin-maps' token gate off (it is then missing from REPOS), while the installation still selects it.
        app, calls, _ = self.app_with(self.creds())
        app.config = lambda: ({DOLPHIN: dict(REPOS[DOLPHIN], app_repo=DOLPHIN)}, SUBNETS)
        self.assertEqual(app.repo_pat(dict(REPOS[DOLPHIN], app_repo=DOLPHIN)), "ghs_minted")

    def test_another_accounts_installation_is_refused_even_with_a_same_named_repository(self):
        both = json.dumps({"app_id": 9, "private_key": open(self.pkcs1).read(),
                           "installations": {"dolphin-labs-hq": 777, "ejc3": 888}})
        scopes = dict(self.SCOPES, **{"888": ("someone-else", "selected", ["someone-else/imagine"])})
        app, calls, _ = self.app_with(both, scopes=scopes)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(app.repo_pat(dict(REPOS[IMAGINE], app_repo=IMAGINE)), "PAT-IMAGINE")
        self.assertEqual(calls, [])
        self.assertIn("belongs to someone-else, not ejc3", out.getvalue())

    def test_a_passed_check_does_not_carry_over_to_another_owner_mapped_to_the_same_installation(self):
        # The same installation id recorded for two owners by mistake: dolphin-labs-hq's pass must not cover ejc3.
        same = json.dumps({"app_id": 9, "private_key": open(self.pkcs1).read(),
                           "installations": {"dolphin-labs-hq": 777, "ejc3": 777}})
        app, calls, _ = self.app_with(same)
        self.assertEqual(app.repo_pat(dict(REPOS[DOLPHIN], app_repo=DOLPHIN)), "ghs_minted")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(app.repo_pat(dict(REPOS[IMAGINE], app_repo=IMAGINE)), "PAT-IMAGINE")
        self.assertIn("belongs to dolphin-labs-hq, not ejc3", out.getvalue())
        self.assertEqual(len(calls), 1, "no administration token minted for ejc3 from dolphin-labs-hq's installation")

    def test_a_passed_check_expires_before_the_token_it_allowed(self):
        # Tokens are reused until ten minutes of their hour are left; the pass must be shorter, so each mint re-checks.
        app, calls, _ = self.app_with(self.creds())
        self.assertEqual(app.repo_pat(dict(REPOS[DOLPHIN], app_repo=DOLPHIN)), "ghs_minted")
        until, _ = app._APP_SCOPE_OK[(777, "dolphin-labs-hq")]
        self.assertLess(until - time.time(), 50 * 60)

    def test_a_refusal_is_remembered_rather_than_checked_on_every_call(self):
        scopes = dict(self.SCOPES, **{"777": ("dolphin-labs-hq", "all", ["dolphin-labs-hq/dolphin-labs"])})
        app, calls, _ = self.app_with(self.creds(), scopes=scopes)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(app.repo_pat(dict(REPOS[DOLPHIN], app_repo=DOLPHIN)), "PAT-DOLPHIN")
            first = len(app.scope_calls)
            self.assertEqual(app.repo_pat(dict(REPOS[DOLPHIN], app_repo=DOLPHIN)), "PAT-DOLPHIN")
        self.assertEqual(len(app.scope_calls), first)
        self.assertEqual(calls, [])

    def test_running_out_of_time_is_not_swallowed(self):
        app, *_ = load_app()
        app.RUNNER_APP_SECRET = "github-app/runners"

        def boom(repo):
            raise app.OutOfTime("deadline")
        app.app_token = boom
        with self.assertRaises(app.OutOfTime):
            app.repo_pat(dict(REPOS[DOLPHIN], app_repo=DOLPHIN))


class RunnersAppTerraformTests(unittest.TestCase):
    TF = (ROOT / "runner-app.tf").read_text()

    def test_exactly_the_dolphin_repos_and_imagine_take_app_tokens_and_colton_keeps_its_pat(self):
        m = re.search(r"runner_app_github_app_repos = \[(.*?)\]", self.TF)
        repos = re.findall(r'"([^"]+)"', m.group(1))
        self.assertEqual(sorted(repos), ["dolphin-labs-hq/dolphin-films", "dolphin-labs-hq/dolphin-labs", "dolphin-labs-hq/dolphin-maps",
                                         "ejc3/imagine"])
        self.assertNotIn(COLTON, repos)
        self.assertIn('app_repo = contains(local.runner_app_github_app_repos, repo) ? repo : ""', self.TF)
        self.assertIn('pat_secret = "github-runner/repo-pat/${repo}"', self.TF, "every repo keeps its PAT as the fallback")

    def test_a_refused_installation_alarms(self):
        flt = self.TF[self.TF.index('resource "aws_cloudwatch_log_metric_filter" "runner_app_installation_refused"'):]
        self.assertIn('pattern        = "\\"refusing it\\""', flt)
        self.assertIn("log_group_name = aws_cloudwatch_log_group.runner_app[0].name", flt)
        self.assertIn('name  = "/aws/lambda/github-app-runner"', self.TF)
        self.assertIn("to       = aws_cloudwatch_log_group.runner_app[each.value]", self.TF, "the existing group is adopted (no function call: an import address may not have one)")
        imp = self.TF[self.TF.index("  for_each = var.enable_github_runner && contains("):]
        self.assertIn('contains(data.aws_cloudwatch_log_groups.runner_app.log_group_names, "/aws/lambda/github-app-runner")', imp.split("}")[0],
                      "only where the group exists and the runners are on")
        alarm = self.TF[self.TF.index('resource "aws_cloudwatch_metric_alarm" "runner_app_installation_refused"'):]
        self.assertIn('metric_name         = "InstallationRefused"', alarm)
        self.assertIn("alarm_actions       = [aws_sns_topic.cost_alerts.arn]", alarm)
        app_src = (ROOT / "runner-app" / "app_runner.py").read_text()
        self.assertIn("refusing it", app_src, "the filter matches the controller's own refusal message")

    def test_the_controller_knows_every_repo_the_app_may_cover_gates_or_not(self):
        self.assertIn("APP_INSTALLATION_REPOS = jsonencode(local.runner_app_github_app_repos)", self.TF)

    def test_only_the_app_controller_and_administration_can_read_the_repo_tokens(self):
        # fcvm's metal controller (runner_lambda) serves ejc3/fcvm with /github-runner/pat and never reads these.
        autoscale = (ROOT / "runner-autoscale.tf").read_text()
        self.assertNotIn("aws_secretsmanager_secret.github_runner_repo_pat", autoscale)
        policy = REPOS_TF[REPOS_TF.index('resource "aws_secretsmanager_secret_policy" "github_runner_repo_pat"'):]
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.runner_app_lambda[0].arn])', policy)
        self.assertNotIn("aws_iam_role.runner_lambda", policy)

    def test_the_controller_reads_the_app_secret_and_only_it_and_the_jumpboxes_can(self):
        self.assertIn("RUNNER_APP_SECRET = aws_secretsmanager_secret.github_app_runners[0].name", self.TF)
        self.assertIn("[aws_secretsmanager_secret.github_app_runners[0].arn])", self.TF)
        self.assertIn('"aws:PrincipalArn" = concat([aws_iam_role.runner_app_lambda[0].arn], local.github_admin_token_readers)', self.TF)
        self.assertIn('count                   = var.enable_github_runner ? 1 : 0\n  name                    = "github-app/runners"', self.TF)


# ---------------------------------------------------------------- prebuilt images (runner-app-image.tf)
ACCOUNT, CANONICAL = "123456789012", "099720109477"
IMAGE_PURPOSE = "github-app-runner-image"


def ami(image_id, created, owner=ACCOUNT, purpose=IMAGE_PURPOSE, name="imagine-runner", repo=IMAGINE,
        state="available", arch="x86_64"):
    tags = [{"Key": k, "Value": v} for k, v in (("Purpose", purpose), ("Image", name), ("Repo", repo)) if v is not None]
    return {"ImageId": image_id, "OwnerId": owner, "CreationDate": created, "State": state, "Architecture": arch,
            "RootDeviceName": "/dev/sda1", "Tags": tags}


def image_cfg(image="imagine-runner"):
    """Imagine's config as runner-app.tf passes it once it sets `image`."""
    return dict(REPOS[IMAGINE], image=image)


class PrebuiltImageTests(unittest.TestCase):
    """A repo that sets `image` boots the newest build of it made and tagged by this account's pipeline, for that repo;
    every other repo, and an opted-in repo with no such image, boots Canonical's Ubuntu 24.04."""

    def launch(self, app, cfg, repo=IMAGINE, job=31):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            outcome, _ = app.ensure_runner(repo, cfg, SUBNETS, "PAT", str(job), "s", [])
        return outcome, out.getvalue()

    def test_an_opted_in_repo_launches_the_newest_tagged_image_this_account_built_for_it(self):
        app, ec2, *_ = load_app()
        ec2.images = [ami("ami-old", "2026-10-05T09:20:00.000Z"), ami("ami-new", "2026-10-12T09:20:00.000Z"),
                      ami("ami-middle", "2026-10-08T09:20:00.000Z")]
        outcome, log = self.launch(app, image_cfg())
        self.assertEqual(outcome, "launched")
        (kw,) = ec2.launched
        self.assertEqual(kw["ImageId"], "ami-new")
        self.assertEqual(kw["BlockDeviceMappings"][0]["DeviceName"], "/dev/sda1")
        self.assertIn("launching from imagine-runner image ami-new", log)
        # Asked of EC2 as this account's own images, by the pipeline's tags, for this repo, available and x86.
        (query,) = ec2.image_queries
        self.assertEqual(query["Owners"], [ACCOUNT])
        filters = {f["Name"]: f["Values"] for f in query["Filters"]}
        self.assertEqual(filters, {"tag:Purpose": [IMAGE_PURPOSE], "tag:Image": ["imagine-runner"], "tag:Repo": [IMAGINE],
                                   "state": ["available"], "architecture": ["x86_64"]})

    def test_a_repo_that_has_not_opted_in_still_launches_canonicals_image(self):
        # The opt-in is per repo: with images on hand, the repo that names one gets it, and a repo that does not (another
        # repo, or Imagine's own entry without `image`) gets Canonical's, without listing any image.
        app, ec2, *_ = load_app()
        ec2.images = [ami("ami-new", "2026-10-12T09:20:00.000Z"), ami("ami-colton", "2026-10-12T09:20:00.000Z", repo=COLTON)]
        for repo, cfg in ((IMAGINE, image_cfg()), (IMAGINE, REPOS[IMAGINE]), (COLTON, REPOS[COLTON]),
                          (IMAGINE, image_cfg(image=""))):
            self.assertEqual(self.launch(app, cfg, repo=repo, job=len(ec2.launched) + 40)[0], "launched")
        self.assertEqual([kw["ImageId"] for kw in ec2.launched], ["ami-new"] + ["ami-canonical"] * 3)
        self.assertEqual(len(getattr(ec2, "image_queries", [])), 1, "only the opted-in launch lists images")

    def test_with_no_image_it_falls_back_to_canonicals_and_says_so(self):
        app, ec2, *_ = load_app()
        ec2.images = []
        outcome, log = self.launch(app, image_cfg())
        self.assertEqual(outcome, "launched")
        self.assertEqual(ec2.launched[0]["ImageId"], "ami-canonical")
        self.assertIn("no available imagine-runner image built by this account; launching Canonical's Ubuntu 24.04 instead", log)

    def test_an_image_that_cannot_be_listed_falls_back_to_canonicals_and_says_why(self):
        app, ec2, *_ = load_app()
        ec2.image_error = Refused("RequestLimitExceeded")
        outcome, log = self.launch(app, image_cfg())
        self.assertEqual((outcome, ec2.launched[0]["ImageId"]), ("launched", "ami-canonical"))
        self.assertIn("listing its imagine-runner images failed (Refused", log)

    def test_an_untagged_foreign_or_unfinished_image_is_never_chosen(self):
        # Every one of these is newer than the one good image, and the listing lets them all through: the controller's
        # own check still picks the good one, and with the good one gone, Canonical's.
        newer = "2026-10-14T09:20:00.000Z"
        decoys = [ami("ami-no-purpose", newer, purpose=None), ami("ami-fcvm-purpose", newer, purpose="github-runner"),
                  ami("ami-other-account", newer, owner="210987654321"), ami("ami-canonical-owner", newer, owner=CANONICAL),
                  ami("ami-other-name", newer, name="dolphin-runner"), ami("ami-other-repo", newer, repo=COLTON),
                  ami("ami-pending", newer, state="pending"), ami("ami-arm", newer, arch="arm64")]
        for ignore in (False, True):
            app, ec2, *_ = load_app()
            ec2.ignore_image_filters = ignore
            ec2.images = decoys + [ami("ami-good", "2026-10-01T09:20:00.000Z")]
            self.launch(app, image_cfg())
            ec2.images = decoys
            self.launch(app, image_cfg(), job=32)
            self.assertEqual([kw["ImageId"] for kw in ec2.launched], ["ami-good", "ami-canonical"], f"filters ignored: {ignore}")


class PrebuiltImageTerraformTests(unittest.TestCase):
    POLICY = re.search(r'resource "aws_iam_role_policy" "runner_app_lambda" \{.*?\n\}', APP_TF, re.S).group()

    def statement(self, sid):
        return self.POLICY.split(f'Sid      = "{sid}"', 1)[1].split("\n      },", 1)[0]

    def test_the_launch_policy_admits_only_this_accounts_images_with_the_pipelines_tag(self):
        stmt = self.statement("LaunchPrebuiltRunnerImages")
        self.assertIn('Action   = "ec2:RunInstances"', stmt)
        self.assertIn('Resource = "arn:aws:ec2:us-west-1::image/*"', stmt)
        self.assertIn('"ec2:Owner"               = data.aws_caller_identity.current.account_id', stmt)
        self.assertIn('"aws:ResourceTag/Purpose" = local.runner_app_image_purpose', stmt)
        self.assertNotIn("github-runner\"", stmt, "never fcvm's Purpose=github-runner images")
        # Every image statement is one of the two: Canonical's, or this account's tagged ones.
        image_statements = re.findall(r'Sid\s+= "([^"]+)"[^}]*?Resource\s+= "arn:aws:ec2:us-west-1::image/\*"', self.POLICY, re.S)
        self.assertEqual(image_statements, ["LaunchCanonicalImagesOnly", "LaunchPrebuiltRunnerImages"])
        image_tf = (ROOT / "runner-app-image.tf").read_text()
        self.assertIn('runner_app_image_purpose           = "github-app-runner-image"', image_tf)

    def test_image_is_optional_and_defaults_to_canonicals(self):
        config = re.search(r"runner_app_config = \{.*?\n  \}", APP_TF, re.S).group()
        self.assertIn('image      = try(c.image, "")', config)
        fn = re.search(r'resource "aws_lambda_function" "runner_app" \{.*?\n\}', APP_TF, re.S).group()
        self.assertIn('c.image == "" || try(local.runner_app_images[c.image].repo, "") == repo', fn,
                      "a plan refuses an image that is unknown or built for another repo")
        rows = dict(re.findall(r'^\s+"([^"]+/[^"]+)"\s*=\s*(\{ label[^\n]*\})', APP_TF, re.M))
        # Each opted-in repo boots the image built for it; dolphin-family has no workflows yet and no image.
        images = {r: re.search(r'image = "([^"]+)"', row).group(1) for r, row in rows.items() if "image" in row}
        self.assertEqual(images, {IMAGINE: "imagine-runner", COLTON: "colton-games-runner",
                                  DOLPHIN: "dolphin-labs-runner", "dolphin-labs-hq/dolphin-films": "dolphin-films-runner",
                                  "dolphin-labs-hq/dolphin-maps": "dolphin-maps-runner"})

    def test_imagine_boots_its_prebuilt_image(self):
        rows = dict(re.findall(r'^\s+"([^"]+/[^"]+)"\s*=\s*(\{ label[^\n]*\})', APP_TF, re.M))
        self.assertIn('image = "imagine-runner"', rows[IMAGINE])
        image_tf = (ROOT / "runner-app-image.tf").read_text()
        self.assertIn('"imagine-runner" = {\n      repo   = "ejc3/imagine"', image_tf, "built for Imagine itself")


class BootstrapOnAPrebuiltImageTests(unittest.TestCase):
    """bootstrap.sh's install steps skip what a prebuilt image already has, and install it on Canonical's image."""

    def run_block(self, start, end, setup, stubs):
        text = BOOTSTRAP.read_text()
        block = text[text.index(start):text.index(end, text.index(start))]
        tmp = Path(tempfile.mkdtemp(prefix="bootstrap-block."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        (tmp / "bin").mkdir()
        for name, body in stubs.items():
            (tmp / "bin" / name).write_text("#!/bin/bash\n" + body)
            (tmp / "bin" / name).chmod(0o755)
        out = subprocess.run(["bash", "-c", "set -euo pipefail\n" + setup + "\n" + block], cwd=tmp, capture_output=True,
                             text=True, timeout=30, env={"PATH": f"{tmp / 'bin'}:/usr/bin:/bin", "T": str(tmp)})
        self.assertEqual(out.returncode, 0, out.stderr)
        return tmp

    def test_packages_are_installed_only_when_one_is_missing(self):
        log = 'echo "$(basename "$0") $*" >> "$T/calls"\n'
        start, end = "PACKAGES=(", "id -u runner"
        installed = {"dpkg-query": "echo 'install ok installed'\n", "apt-get": log}
        tmp = self.run_block(start, end, "", installed)
        self.assertFalse((tmp / "calls").exists(), "a prebuilt image runs no apt at all")
        missing = {"dpkg-query": '[ "${@: -1}" = jq ] && exit 1; echo "install ok installed"\n', "apt-get": log}
        tmp = self.run_block(start, end, "", missing)
        calls = (tmp / "calls").read_text().splitlines()
        self.assertEqual(calls[0], "apt-get update -q")
        self.assertTrue(calls[1].startswith("apt-get install -y -q python3-boto3 git curl jq"), calls)

    def test_the_runner_is_downloaded_unless_this_exact_version_is_there(self):
        text = BOOTSTRAP.read_text()
        version = re.search(r"^RUNNER_VERSION='([^']+)'$", text, re.M).group(1)
        start, end = 'if [ "$(cat "$DIR/.runner-version"', "# The tool cache lives"
        stubs = {"curl": 'echo "curl $*" >> "$T/calls"\n', "sha256sum": "cat >/dev/null\n", "tar": "true\n",
                 "chown": "true\n"}
        setup = (f"RUNNER_VERSION='{version}' RUNNER_SHA256=x DIR=$T/actions-runner\n"
                 'mkdir -p "$DIR/bin"; printf "#!/bin/bash\\necho deps >> $T/calls\\n" > "$DIR/bin/installdependencies.sh"; '
                 'chmod +x "$DIR/bin/installdependencies.sh"\n')
        for marker, downloads in ((version, False), ("2.300.0", True), (None, True)):
            write = f'echo {marker} > "$DIR/.runner-version"' if marker else "true"
            # A fresh download unpacks a new installdependencies.sh; the stub tar unpacks nothing, so put one back.
            stubs["tar"] = ('mkdir -p "$T/actions-runner/bin"; printf "#!/bin/bash\\necho deps >> $T/calls\\n" '
                            '> "$T/actions-runner/bin/installdependencies.sh"; chmod +x "$T/actions-runner/bin/installdependencies.sh"\n')
            tmp = self.run_block(start, end, setup + write, stubs)
            calls = (tmp / "calls").read_text().splitlines() if (tmp / "calls").exists() else []
            self.assertEqual(any(c.startswith("curl ") for c in calls), downloads, (marker, calls))
            self.assertEqual("deps" in calls, downloads, (marker, calls))
            self.assertEqual((tmp / "actions-runner" / ".runner-version").read_text().strip(), version)

    def test_a_prebuilt_images_tool_directories_go_first_on_the_jobs_path(self):
        text = BOOTSTRAP.read_text()
        block = text[text.index("if [ -s /etc/github-app-runner/path ]"):text.index("sudo -u runner -H ./run.sh")]
        tmp = Path(tempfile.mkdtemp(prefix="path."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        (tmp / "path").write_text("/home/runner/.cargo/bin\n/opt/node/bin\n")
        config_path = "/usr/local/sbin:/usr/local/bin:/usr/bin"
        for listed, expected in ((True, "/home/runner/.cargo/bin:/opt/node/bin:" + config_path), (False, config_path)):
            (tmp / ".path").write_text(config_path + "\n")
            script = block.replace("/etc/github-app-runner/path", str(tmp / "path") if listed else str(tmp / "absent"))
            subprocess.run(["bash", "-c", f"set -euo pipefail\nDIR={tmp}\n{script}"], check=True, timeout=30)
            self.assertEqual((tmp / ".path").read_text().strip(), expected, f"listed: {listed}")
        self.assertLess(text.index("exec ./config.sh"), text.index("if [ -s /etc/github-app-runner/path ]"),
                        "config.sh writes .path; prepend after it")

    def test_prepare_only_installs_and_stops_before_registration_and_never_powers_off(self):
        text = BOOTSTRAP.read_text()
        stop = text.index('if [ "$PREPARE_ONLY" = 1 ]; then')
        self.assertLess(text.index('install -d -o runner -g runner "$WORK"'), stop)
        self.assertLess(stop, text.index("REG_TOKEN=$(python3"))
        self.assertIn("exit 0", text[stop:text.index("fi", stop)])
        self.assertIn("PREPARE_ONLY=${GITHUB_APP_RUNNER_PREPARE_ONLY:-0}", text)
        trap = text.index("trap 'sync; shutdown -h now' EXIT")
        self.assertIn('if [ "$PREPARE_ONLY" != 1 ]; then\n  trap', text[trap - 40:trap + 5])
        # User data for a job host never sets it: outside comments, it is only ever read.
        app, *_ = load_app()
        code = [l for l in app.user_data(IMAGINE, "imagine,s").splitlines() if not l.lstrip().startswith("#")]
        self.assertEqual([l for l in code if "GITHUB_APP_RUNNER_PREPARE_ONLY" in l],
                         ["PREPARE_ONLY=${GITHUB_APP_RUNNER_PREPARE_ONLY:-0}"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""`pbox up N FLAVOR --lease DURATION` (scripts/parallel-box.sh, parallel-box-watchdog.tf): the launch tags the instance
PboxLeaseUntil=<epoch>, the idle watchdog leaves an owner's box running until then (capped at LaunchTime + 12 h) and
applies the normal rule after, and `status` shows the lease. Offline: the real script against a fake `aws` and `ssh`, the
real watchdog Lambda body against a fake boto3, and source checks of the Terraform."""
import datetime
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = Path(os.environ.get("PBOX_SCRIPT", ROOT / "scripts" / "parallel-box.sh"))


def source(name):
    return (ROOT / name).read_text()


POINTER = {
    "region": "us-east-2",
    "x86_image": "ami-x86-home",
    "fallback": {
        "template": "parallel-box-fallback",
        "regions": {"us-west-2": {"subnets": {"us-west-2a": "subnet-w2a"}, "x86_image": "ami-x86-w2"}},
    },
}

# Calls go to $FAKE_LOG. A launched (or pre-set) box is "region az" in $FAKE_STATE. FAKE_HOME_FULL=1: no capacity at home.
# FAKE_LEASE is what describe-instances answers for [LaunchTime, the PboxLeaseUntil tag] ("<iso>\t<value>").
FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
a = " ".join(args)
open(os.environ["FAKE_LOG"], "a").write(json.dumps(args) + "\n")
def opt(name):
    return args[args.index(name) + 1] if name in args else ""
region = opt("--region")
sp = os.environ["FAKE_STATE"]
state = open(sp).read().split() if os.path.exists(sp) else []
here = bool(state) and state[0] == region
if "get-parameter" in a:
    print(os.environ["FAKE_POINTER"])
elif "describe-instances" in a and "PboxLeaseUntil" in a:
    print(os.environ.get("FAKE_LEASE", "2026-10-22T00:00:00+00:00\tNone") if here else "None\tNone")
elif "describe-launch-template-versions" in a:
    print("spot")
elif "describe-instances" in a and "PublicIpAddress" in a:
    print("203.0.113.9" if here else "None")
elif "describe-instances" in a and "InstanceId" in a:
    print("i-0lease" if here else "None")
elif "describe-instances" in a and "AvailabilityZone" in a:
    print(state[1] if here else "None")
elif "describe-instances" in a and "Fallback" in a:
    print("true" if here and region != "us-east-2" else "None")
elif "describe-instances" in a and "InstanceType" in a:
    print("c8g.8xlarge" if here else "None")
elif "describe-instances" in a:
    print("None")
elif "describe-volumes" in a and "Attachments[0].InstanceId]" in a and "[VolumeId" in a:
    print("vol-1\tin-use\tus-east-2c\ti-0lease" if here else "vol-1\tavailable\tus-east-2c\tNone")
elif "describe-volumes" in a and "[State," in a:
    print("available\tNone")
elif "describe-volumes" in a and "AvailabilityZone" in a:
    print("us-east-2c")
elif "describe-volumes" in a:
    print("vol-1\t100\tavailable")
elif "describe-instance-types" in a:
    print("32")
elif "describe-spot-price-history" in a:
    print("0.35")
elif "describe-spot-instance-requests" in a:
    pass
elif "get-spot-placement-scores" in a or "describe-instance-type-offerings" in a:
    sys.exit(254)
elif "run-instances" in a:
    if region == "us-east-2" and os.environ.get("FAKE_HOME_FULL"):
        print("An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation")
        sys.exit(254)
    open(sp, "w").write(region + " " + ("us-west-2a" if opt("--subnet-id") else "us-east-2c"))
    print("i-0lease")
elif "attach-volume" in a:
    pass
else:
    print("None")
'''


class LeaseScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, body in (("aws", FAKE_AWS), ("ssh", "#!/bin/sh\nexit 0\n"), ("sleep", "#!/bin/sh\nexit 0\n")):
            p = self.tmp / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "calls"

    def pbox(self, *args, running=None, **extra):
        if running is not None:
            (self.tmp / "state").write_text(running)
        env = dict(os.environ, PATH=f"{self.tmp}:{os.environ['PATH']}", HOME=str(self.tmp), FAKE_LOG=str(self.log),
                   FAKE_STATE=str(self.tmp / "state"), FAKE_POINTER=json.dumps(POINTER), **extra)
        env.pop("PARALLEL_BOX_REGION", None)
        env.setdefault("MEMBER_BOX_USER", "example-owner")
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120)
        return r.returncode, r.stdout, r.stderr

    def runs(self):
        if not self.log.exists():
            return []
        return [c for c in map(json.loads, self.log.read_text().splitlines()) if "run-instances" in c]

    def instance_tags(self, run):
        """The instance tag specification of one run-instances call, as {key: value}, or None when it passes none."""
        specs = [run[i + 1:] for i, x in enumerate(run) if x == "--tag-specifications"]
        if not specs:
            return None
        inst = [s for s in specs[0] if s.startswith("ResourceType=instance,")]
        self.assertEqual(len(inst), 1, run)
        # The whole value must be well-formed AWS CLI shorthand, or the CLI refuses the launch
        # ("Error parsing parameter '--tag-specifications'"); a stray brace once broke every fallback launch.
        tag = r"\{Key=[^,{}\[\]]+,Value=[^,{}\[\]]*\}"
        self.assertRegex(inst[0], rf"\AResourceType=instance,Tags=\[{tag}(,{tag})*\]\Z", run)
        return dict(re.findall(r"\{Key=([^,]+),Value=([^}]*)\}", inst[0]))

    def test_valid_durations_tag_the_instance_with_the_lease_end(self):
        for arg, secs in ((("--lease", "90m"), 5400), (("--lease", "2h"), 7200), (("--lease=1h30m",), 5400),
                          (("--lease", "30m"), 1800), (("--lease", "12h"), 43200), (("--lease=0h45m",), 2700)):
            with self.subTest(arg=arg):
                self.log.unlink(missing_ok=True)
                (self.tmp / "state").unlink(missing_ok=True)
                before = int(time.time())
                rc, out, err = self.pbox("up", "3", "compute-32", *arg)
                after = int(time.time())
                self.assertEqual(rc, 0, err)
                runs = self.runs()
                self.assertEqual(len(runs), 1)
                self.assertIn("LaunchTemplateName=parallel-box-3,Version=$Latest", runs[0])
                tags = self.instance_tags(runs[0])
                self.assertEqual(tags["Name"], "parallel-box-3")
                self.assertEqual(tags["DevEBS"], "true")
                self.assertTrue(before + secs <= int(tags["PboxLeaseUntil"]) <= after + secs, tags)
                self.assertIn("costs money", err)
                self.assertIn("  lease: until ", err)

    def test_plain_up_passes_no_tags(self):
        rc, _, err = self.pbox("up", "3", "compute-32")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(self.instance_tags(self.runs()[0]))
        self.assertNotIn("LEASE:", err)
        self.assertNotIn("  lease:", err)

    def test_a_fallback_launch_carries_the_lease_beside_its_own_tags(self):
        before = int(time.time())
        rc, out, err = self.pbox("up", "3", "compute-32", "--fallback", "--lease", "2h", FAKE_HOME_FULL="1")
        self.assertEqual(rc, 0, err)
        fb = [r for r in self.runs() if "--subnet-id" in r]
        self.assertEqual(len(fb), 1)
        tags = self.instance_tags(fb[0])
        self.assertEqual({k: v for k, v in tags.items() if k != "PboxLeaseUntil"},
                         {"Name": "parallel-box-3", "Fallback": "true", "DevEBS": "true"})
        self.assertTrue(before + 7200 <= int(tags["PboxLeaseUntil"]) <= int(time.time()) + 7200)
        # without --lease the fallback tags are as before
        self.log.unlink()
        (self.tmp / "state").unlink()
        self.pbox("up", "3", "compute-32", "--fallback", FAKE_HOME_FULL="1")
        fb = [r for r in self.runs() if "--subnet-id" in r]
        self.assertEqual(self.instance_tags(fb[0]), {"Name": "parallel-box-3", "Fallback": "true", "DevEBS": "true"})

    def refused(self, *args, member=None):
        extra = {"MEMBER_BOX_USER": member} if member else {}
        rc, _, err = self.pbox(*args, **extra)
        self.assertEqual(rc, 1, (args, err))
        self.assertFalse(self.log.exists(), "called aws before refusing")
        return err

    def test_too_short(self):
        for d in ("20m", "29m", "0h"):
            self.assertIn("under 30 minutes", self.refused("up", "3", "compute-32", "--lease", d))

    def test_too_long(self):
        for d in ("13h", "12h1m", "721m", "9999h"):
            self.assertIn("over the 12-hour maximum", self.refused("up", "3", "compute-32", "--lease", d))

    def test_bad_syntax(self):
        for d in ("2", "2d", "h", "1.5h", "-1h", "2h30", "2H", "1m2h", "90 m", "999999m", "12345h"):
            self.assertIn("a duration like 90m, 2h or 1h30m", self.refused("up", "3", "compute-32", f"--lease={d}"))
        self.assertIn("--lease needs a duration", self.refused("up", "3", "compute-32", "--lease"))
        self.assertIn("--lease needs a duration", self.refused("up", "3", "compute-32", "--lease="))

    def test_only_up_takes_a_lease(self):
        for cmd in (("status",), ("down", "3"), ("ssh", "3"), ("ip", "3"), ("capacity", "compute-32"), ("flavors",)):
            self.assertIn("--lease only applies to 'up'", self.refused(*cmd, "--lease", "2h"))

    def test_a_members_box_takes_no_lease(self):
        self.assertIn("--lease is not available for pbox-skevh",
                      self.refused("up", "compute-32", "--lease", "2h", member="skevh"))

    def test_up_with_a_lease_on_a_running_box_launches_nothing_and_says_so(self):
        rc, out, err = self.pbox("up", "3", "compute-32", "--lease", "2h", running="us-east-2 us-east-2c")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.runs(), [])
        self.assertIn("--lease applies only to a new launch", err)

    def status(self, lease):
        _, out, _ = self.pbox("status", "3", running="us-east-2 us-east-2c", FAKE_LEASE=lease)
        return [line for line in out.splitlines() if line.startswith("  lease:")]

    @staticmethod
    def iso(epoch):
        return datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc).isoformat()

    def test_status_shows_the_lease(self):
        now = int(time.time())
        until = now + 2 * 3600 + 30 * 60 + 30
        hhmm = datetime.datetime.fromtimestamp(until, datetime.timezone.utc).strftime("%H:%M")
        self.assertEqual(self.status(f"{self.iso(now - 600)}\t{until}"), [f"  lease:  until {hhmm} UTC (2h 30m left)"])
        self.assertEqual(self.status(f"{self.iso(now - 7200)}\t{now - 60}"), ["  lease:  ended"])

    def test_status_shows_no_lease_for_a_box_without_one(self):
        now = int(time.time())
        for value in ("None", "soon", "-5", "1e9", ""):
            with self.subTest(value=value):
                self.assertEqual(self.status(f"{self.iso(now - 600)}\t{value}"), [])
        _, out, _ = self.pbox("status", "3", running="us-east-2 us-east-2c")
        self.assertIn("RUNNING at 203.0.113.9", out)

    def test_status_caps_the_lease_at_12_hours_from_launch(self):
        now = int(time.time())
        launch = now - 3600 + 30  # 30 s of margin, so the script's own clock still reads 11h 0m left
        cap = launch + 12 * 3600
        hhmm = datetime.datetime.fromtimestamp(cap, datetime.timezone.utc).strftime("%H:%M")
        self.assertEqual(self.status(f"{self.iso(launch)}\t{now + 20 * 3600}"), [f"  lease:  until {hhmm} UTC (11h 0m left)"])
        self.assertEqual(self.status(f"{self.iso(now - 13 * 3600)}\t{now + 3600}"), ["  lease:  ended"])

    def test_help_documents_the_lease(self):
        _, out, _ = self.pbox("help")
        self.assertIn("pbox up [N] FLAVOR --lease DURATION", out)
        self.assertIn("30m to 12h", out)
        self.assertIn("COSTS MONEY FOR THE WHOLE LEASE", out)


class LeaseWatchdog(unittest.TestCase):
    """The real Lambda body (parallel-box-watchdog.tf) against fake boto3, with the environment the owner's boxes get."""

    NAMES = ["parallel-box"] + ["parallel-box-%d" % n for n in range(2, 9)]

    def run_watchdog(self, instances, cpu=1.0, lease_names=None):
        """instances: (id, name, age in minutes, PboxLeaseUntil value or None). Every box is idle at `cpu`."""
        code = textwrap.dedent(re.search(r"parallel_watchdog_code = <<-PY\n(.*?)\n  PY", source("parallel-box-watchdog.tf"),
                                         re.S).group(1))
        self.now = datetime.datetime.now(datetime.timezone.utc)
        calls = {"terminate": [], "sns": []}
        launch = {iid: self.now - datetime.timedelta(minutes=age) for iid, _, age, _ in instances}
        self.launch = launch

        class EC2:
            def describe_instances(self, Filters):
                return {"Reservations": [{"Instances": [
                    {"InstanceId": iid, "LaunchTime": launch[iid], "InstanceType": "c8g.8xlarge",
                     "Tags": [{"Key": "Name", "Value": name}] + ([{"Key": "PboxLeaseUntil", "Value": tag}] if tag is not None else [])}
                    for iid, name, _, tag in instances]}]}

            def terminate_instances(self, InstanceIds):
                calls["terminate"].extend(InstanceIds)

        class CW:
            def get_metric_statistics(self, **kwargs):
                if kwargs["MetricName"] == "CPUUtilization":
                    return {"Datapoints": [{"Maximum": cpu}, {"Maximum": cpu}]}
                return {"Datapoints": []}

        class SNS:
            def publish(self, **kwargs):
                calls["sns"].append(kwargs["Message"])

        fake = types.ModuleType("boto3")
        fake.client = lambda name, region_name=None: {"ec2": EC2(), "cloudwatch": CW(), "sns": SNS()}[name]
        saved_mod, saved_env = sys.modules.get("boto3"), dict(os.environ)
        sys.modules["boto3"] = fake
        os.environ.update(TAG_NAMES=",".join(self.NAMES + ["pbox-colton", "gpu-box"]), IDLE_MINUTES="30", IDLE_CPU_PCT="5",
                          SNS_TOPIC_ARN="arn:aws:sns:test", IO_THRESHOLDS="{}",
                          LEASE_NAMES=",".join(self.NAMES if lease_names is None else lease_names))
        try:
            env = {}
            exec(compile(code, "index.py", "exec"), env)
            result = env["lambda_handler"]({}, None)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
            if saved_mod is None:
                del sys.modules["boto3"]
            else:
                sys.modules["boto3"] = saved_mod
        return {r["id"]: r for r in result["results"]}, calls

    def epoch(self, minutes_from_now):
        return str(int(time.time() + minutes_from_now * 60))

    def test_a_lease_in_the_future_keeps_an_idle_box(self):
        results, calls = self.run_watchdog([("i-l", "parallel-box-3", 120, self.epoch(90)),
                                            ("i-1", "parallel-box", 45, self.epoch(5))])
        self.assertEqual({k: r["action"] for k, r in results.items()}, {"i-l": "leased", "i-1": "leased"})
        self.assertEqual(calls["terminate"], [])
        self.assertAlmostEqual(results["i-l"]["left_min"], 90, delta=1)

    def test_a_lease_in_the_past_leaves_the_normal_rule(self):
        results, calls = self.run_watchdog([("i-l", "parallel-box-3", 300, self.epoch(-10)),
                                            ("i-busy", "parallel-box-4", 300, self.epoch(-10))])
        self.assertEqual(results["i-l"]["action"], "terminated")
        self.assertEqual(calls["terminate"], ["i-l", "i-busy"])
        self.assertIn("Its lease ended at", calls["sns"][0])
        results, calls = self.run_watchdog([("i-busy", "parallel-box-4", 300, self.epoch(-10))], cpu=50.0)
        self.assertEqual(results["i-busy"]["action"], "busy")

    def test_a_missing_or_garbage_tag_is_no_lease(self):
        boxes = [("i-none", "parallel-box-2", 45, None)] + [
            ("i-g%d" % n, "parallel-box-%d" % (n + 3), 45, v) for n, v in enumerate(
                ("", "soon", "-5", "1e12", "4102444800.5", "9" * 40, "１２"))]
        results, calls = self.run_watchdog(boxes)
        self.assertEqual({r["action"] for r in results.values()}, {"terminated"}, results)
        self.assertEqual(sorted(calls["terminate"]), sorted(b[0] for b in boxes))
        self.assertTrue(all("lease" not in m for m in calls["sns"]))

    def test_a_lease_past_12_hours_from_launch_is_capped(self):
        # 60 minutes old, tagged 20 hours past launch: leased only until LaunchTime + 12 h.
        results, calls = self.run_watchdog([("i-young", "parallel-box-3", 60, self.epoch(19 * 60))])
        self.assertEqual(results["i-young"]["action"], "leased")
        self.assertEqual(results["i-young"]["until"], (self.launch["i-young"] + datetime.timedelta(hours=12)).isoformat())
        # 13 hours old, tagged an hour from now: past the cap, so the idle rule applies.
        results, calls = self.run_watchdog([("i-old", "parallel-box-3", 13 * 60, self.epoch(60))])
        self.assertEqual(results["i-old"]["action"], "terminated")
        self.assertEqual(calls["terminate"], ["i-old"])

    def test_a_lease_counts_only_on_the_names_in_lease_names(self):
        # A member's box and a GPU test box (not in LEASE_NAMES) keep the normal rule whatever they are tagged.
        results, calls = self.run_watchdog([("i-m", "pbox-colton", 45, self.epoch(120)), ("i-g", "gpu-box", 45, self.epoch(120)),
                                            ("i-p", "parallel-box", 45, self.epoch(120))])
        self.assertEqual({k: r["action"] for k, r in results.items()},
                         {"i-m": "terminated", "i-g": "terminated", "i-p": "leased"})
        # The members' Windows watchdog sets no LEASE_NAMES at all.
        results, _ = self.run_watchdog([("i-p", "parallel-box", 45, self.epoch(120))], lease_names=[])
        self.assertEqual(results["i-p"]["action"], "terminated")


def block(text, header):
    m = re.search(re.escape(header) + r" \{\n.*?\n\}\n", text, re.S)
    assert m, header
    return m.group(0)


class LeaseTerraform(unittest.TestCase):
    def test_every_watchdog_of_the_owners_boxes_honours_their_leases_and_only_theirs(self):
        lease = r'LEASE_NAMES\s*=\s*join\(",", local\.parallel_box_names\)'
        for path, header in (("parallel-box-watchdog.tf", 'resource "aws_lambda_function" "parallel_watchdog"'),
                             ("ohio-pbox.tf", 'resource "aws_lambda_function" "parallel_watchdog_ohio"')):
            with self.subTest(path=path):
                self.assertRegex(block(source(path), header), lease)
        fallback = re.search(r"pbox_fallback_watchdog = \{.*?\n  \}\n", source("pbox-fallback.tf"), re.S).group(0)
        self.assertRegex(fallback, lease)
        self.assertNotIn("LEASE_NAMES", source("member-wbox.tf"))
        self.assertEqual(len(re.findall(lease, "".join(source(p.name) for p in ROOT.glob("*.tf")))), 3)

    def test_the_launch_grants_allow_the_lease_tag_key(self):
        # The home and fallback grants tag at RunInstances with no aws:TagKeys list, so PboxLeaseUntil needs no grant. A
        # list added there must name it, or every leased launch is denied.
        for path in ("parallel-box-launch.tf", "pbox-fallback.tf"):
            with self.subTest(path=path):
                text = source(path)
                if "aws:TagKeys" in text:
                    self.assertIn("PboxLeaseUntil", text)

    def test_the_script_and_the_watchdog_cap_a_lease_at_the_same_12_hours(self):
        self.assertRegex(source("scripts/parallel-box.sh"), r"\nLEASE_MAX_HOURS=12 ")
        self.assertRegex(source("parallel-box-watchdog.tf"), r"\nLEASE_MAX_HOURS = 12\n")


if __name__ == "__main__":
    unittest.main(buffer=True)

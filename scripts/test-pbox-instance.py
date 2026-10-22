#!/usr/bin/env python3
"""pbox acts on the instance a caller launched, not on whatever holds the slot now (ejc3/aws#621).

`up`'s machine-readable last line carries `id=i-...`, also when the box was launched but did not come up (`failed=...`).
`ip`, `ssh` and `down` take `--instance i-...` and refuse when the slot holds another instance. The owner's launches
tag the box `PboxLaunchedBy`, and `down` without `--instance` refuses a box another caller launched in the last hour
unless `--force`. Offline: the real scripts/parallel-box.sh against a fake `aws`, `ssh` and `sleep`."""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = Path(os.environ.get("PBOX_SCRIPT", ROOT / "scripts" / "parallel-box.sh"))
POINTER = {"region": "us-east-2", "x86_image": "ami-x86-home", "fallback": {"template": "parallel-box-fallback", "regions": {}}}

# The slot's current instance is $FAKE_STATE ("<instance id>"), launched by $FAKE_BY at $FAKE_LT. A launch writes
# i-0new there. Every call is appended to $FAKE_LOG.
FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
a = " ".join(args)
open(os.environ["FAKE_LOG"], "a").write(json.dumps(args) + "\n")
def opt(name):
    return args[args.index(name) + 1] if name in args else ""
sp = os.environ["FAKE_STATE"]
cur = open(sp).read().strip() if os.path.exists(sp) else ""
here = bool(cur) and opt("--region") == "us-east-2"
if "get-parameter" in a:
    print(os.environ["FAKE_POINTER"])
elif "describe-launch-template-versions" in a:
    print("spot")
elif "describe-instances" in a and "--instance-ids" in a and "PboxLaunchedBy" in a:
    if os.environ.get("FAKE_BY_FAIL"):
        sys.exit(255)
    print(os.environ.get("FAKE_BY", "None") + "\t" + os.environ.get("FAKE_LT", "2020-01-01T00:00:00+00:00"))
elif "describe-instances" in a and "--instance-ids" in a and "PublicIpAddress" in a:
    print("203.0.113.9" if opt("--instance-ids") == cur and here else "None")
elif "describe-instances" in a and "PublicIpAddress" in a:
    print("203.0.113.9" if here else "None")
elif "describe-instances" in a and "InstanceId" in a:
    print(cur if here else "None")
elif "describe-instances" in a and "AvailabilityZone" in a:
    print("us-east-2c" if here else "None")
elif "describe-instances" in a and "InstanceType" in a:
    print("c8g.48xlarge" if here else "None")
elif "describe-instances" in a:
    print("None")
elif "describe-volumes" in a and "Attachments[0].InstanceId]" in a and "[VolumeId" in a:
    print("vol-1\tin-use\tus-east-2c\t" + cur if here else "vol-1\tavailable\tus-east-2c\tNone")
elif "describe-volumes" in a and "[State," in a:
    print("available\tNone")
elif "describe-volumes" in a and "AvailabilityZone" in a:
    print("us-east-2c")
elif "describe-volumes" in a:
    print("vol-1\t100\tavailable")
elif "describe-instance-types" in a:
    print("192")
elif "describe-spot-price-history" in a:
    print("1.2")
elif "run-instances" in a:
    open(sp, "w").write("i-0new")
    print("i-0new")
elif "terminate-instances" in a:
    print("{}")
else:
    print("None")
'''


class InstanceScoped(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.make("aws", FAKE_AWS)
        self.make("ssh", "#!/bin/sh\nexit 0\n")
        self.make("sleep", "#!/bin/sh\nexit 0\n")
        self.log = self.tmp / "calls"

    def make(self, name, body):
        p = self.tmp / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)

    def pbox(self, *args, current=None, **extra):
        if current is not None:
            (self.tmp / "state").write_text(current)
        env = dict(os.environ, PATH=f"{self.tmp}:{os.environ['PATH']}", HOME=str(self.tmp), FAKE_LOG=str(self.log),
                   FAKE_STATE=str(self.tmp / "state"), FAKE_POINTER=json.dumps(POINTER), PBOX_CALLER="job-a")
        env.update(extra)
        env.pop("PARALLEL_BOX_REGION", None)
        env.setdefault("MEMBER_BOX_USER", "example-owner")
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120)
        return r.returncode, r.stdout, r.stderr

    def calls(self, word):
        if not self.log.exists():
            return []
        return [c for c in map(json.loads, self.log.read_text().splitlines()) if word in c]

    # ---------------------------------------------------------------------------------------------- up's last line
    def test_up_reports_the_instance_id(self):
        rc, out, err = self.pbox("up", "3", "compute-192")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip().splitlines()[-1],
                         "launched box=3 region=us-east-2 az=us-east-2c type=c8g.48xlarge ip=203.0.113.9 id=i-0new work=persistent")

    def test_a_box_that_never_answers_ssh_still_reports_its_id(self):
        self.make("ssh", "#!/bin/sh\nexit 255\n")
        rc, out, err = self.pbox("up", "3", "compute-192")
        self.assertEqual(rc, 1, err)
        self.assertIn("SSH did not come up", err)
        self.assertIn("--instance i-0new", err)
        self.assertEqual(out.strip().splitlines()[-1],
                         "launched box=3 region=us-east-2 az=us-east-2c type=c8g.48xlarge ip=203.0.113.9 id=i-0new "
                         "work=persistent failed=ssh-timeout")

    def test_up_waits_on_the_instance_it_launched_not_the_slot(self):
        rc, _, err = self.pbox("up", "3", "compute-192")
        self.assertEqual(rc, 0, err)
        by_id = [c for c in self.calls("describe-instances") if "--instance-ids" in c and "i-0new" in c]
        self.assertTrue(by_id, "wait_ready looks the address up by instance id")

    # ---------------------------------------------------------------------------------------------- --instance
    def test_ip_ssh_and_down_refuse_another_instance_in_the_slot(self):
        for cmd in ("ip", "ssh", "down"):
            with self.subTest(cmd=cmd):
                rc, out, err = self.pbox(cmd, "3", "--instance", "i-0aaaaaaaaaaaaaaaa", current="i-0bbbbbbbbbbbbbbbb")
                self.assertEqual(rc, 1, err)
                self.assertIn("refusing: parallel-box-3 holds i-0bbbbbbbbbbbbbbbb now, not i-0aaaaaaaaaaaaaaaa", err)
                self.assertEqual(out, "")
        self.assertEqual(self.calls("terminate-instances"), [])

    def test_a_matching_instance_is_acted_on(self):
        rc, out, err = self.pbox("ip", "3", "--instance=i-0bbbbbbbbbbbbbbbb", current="i-0bbbbbbbbbbbbbbbb")
        self.assertEqual((rc, out.strip()), (0, "203.0.113.9"), err)
        rc, _, err = self.pbox("down", "3", "--instance", "i-0bbbbbbbbbbbbbbbb", current="i-0bbbbbbbbbbbbbbbb",
                               FAKE_BY="job-b", FAKE_LT="2099-01-01T00:00:00+00:00")
        self.assertEqual(rc, 0, err)
        term = self.calls("terminate-instances")
        self.assertEqual(len(term), 1)
        self.assertEqual(term[0][term[0].index("--instance-ids") + 1], "i-0bbbbbbbbbbbbbbbb")

    def test_a_slot_with_nothing_running_is_refused_too(self):
        rc, _, err = self.pbox("down", "3", "--instance", "i-0aaaaaaaaaaaaaaaa", current="")
        self.assertEqual(rc, 1, err)
        self.assertIn("holds no running instance now", err)

    def test_instance_is_refused_elsewhere_and_malformed(self):
        for args in (("status", "3", "--instance", "i-0aaaaaaaaaaaaaaaa"), ("up", "3", "compute-192", "--instance=i-0aaaaaaaaaaaaaaaa")):
            with self.subTest(args=args):
                rc, _, err = self.pbox(*args)
                self.assertEqual(rc, 1, err)
                self.assertIn("--instance only applies to ip, ssh and down", err)
        rc, _, err = self.pbox("ip", "3", "--instance=")
        self.assertEqual(rc, 1, err)
        self.assertIn("--instance needs an instance id", err)
        rc, _, err = self.pbox("ip", "3", "--instance", "box3")
        self.assertEqual(rc, 1, err)
        self.assertIn("--instance takes an instance id", err)
        rc, _, err = self.pbox("up", "3", "compute-192", "--force")
        self.assertEqual(rc, 1, err)
        self.assertIn("--force only applies to 'down'", err)
        self.assertEqual(self.calls("run-instances"), [])

    # ---------------------------------------------------------------------------------------------- who launched it
    def test_launches_tag_the_launcher(self):
        self.pbox("up", "3", "compute-192")
        run = self.calls("run-instances")[0]
        spec = [s for s in run if s.startswith("ResourceType=instance,")][0]
        self.assertIn("{Key=PboxLaunchedBy,Value=job-a}", spec)
        src = SCRIPT.read_text()
        self.assertIn('{Key=DevEBS,Value=true},{Key=PboxLaunchedBy,Value=$PBOX_CALLER}$LEASE_TAG]', src)  # fallback
        self.assertIn('if [ -z "$MEMBER" ]; then\n        EXTRA_TAGS="{Key=PboxLaunchedBy,Value=$PBOX_CALLER}"', src)  # not members

    def test_down_refuses_a_young_box_another_caller_launched(self):
        rc, _, err = self.pbox("down", "3", current="i-0bbbbbbbbbbbbbbbb", FAKE_BY="job-b", FAKE_LT="2099-01-01T00:00:00+00:00")
        self.assertEqual(rc, 1, err)
        self.assertIn("launched", err)
        self.assertIn("by job-b, not by job-a", err)
        self.assertIn("--instance i-0bbbbbbbbbbbbbbbb", err)
        self.assertEqual(self.calls("terminate-instances"), [])

    def test_down_goes_ahead_with_force_for_its_own_or_for_an_old_box(self):
        cases = (
            ({"FAKE_BY": "job-b", "FAKE_LT": "2099-01-01T00:00:00+00:00"}, ("--force",)),
            ({"FAKE_BY": "job-a", "FAKE_LT": "2099-01-01T00:00:00+00:00"}, ()),
            ({"FAKE_BY": "job-b", "FAKE_LT": "2020-01-01T00:00:00+00:00"}, ()),
            ({"FAKE_BY": "None"}, ()),
        )
        for env, extra in cases:
            with self.subTest(env=env, extra=extra):
                if self.log.exists():
                    self.log.unlink()
                rc, _, err = self.pbox("down", "3", *extra, current="i-0bbbbbbbbbbbbbbbb", **env)
                self.assertEqual(rc, 0, err)
                self.assertEqual(len(self.calls("terminate-instances")), 1)

    def test_down_fails_closed_when_the_launcher_cannot_be_read(self):
        cases = (
            ({"FAKE_BY_FAIL": "1"}, "could not read who launched"),
            ({"FAKE_BY": "job-b", "FAKE_LT": "not-a-time"}, "unreadable launch time"),
        )
        for env, msg in cases:
            with self.subTest(env=env):
                rc, _, err = self.pbox("down", "3", current="i-0bbbbbbbbbbbbbbbb", **env)
                self.assertEqual(rc, 1, err)
                self.assertIn(msg, err)
                self.assertIn("--instance i-0bbbbbbbbbbbbbbbb", err)
        self.assertEqual(self.calls("terminate-instances"), [])

    def test_a_caller_name_with_a_space_is_one_field(self):
        # The tag is read back tab-separated, so a space in it must not split the launcher from the launch time.
        self.pbox("up", "3", "compute-192", PBOX_CALLER="job a")
        spec = [s for s in self.calls("run-instances")[0] if s.startswith("ResourceType=instance,")][0]
        self.assertIn("{Key=PboxLaunchedBy,Value=job_a}", spec)
        self.assertIn("IFS=$'\\t' read -r BY LT_AT", SCRIPT.read_text())

    def test_docs_say_to_carry_the_id(self):
        for f in ("AGENTS.md", "README.md"):
            with self.subTest(f=f):
                text = (ROOT / f).read_text()
                self.assertIn("--instance", text)
                self.assertIn("never re-resolve a slot by name", text)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

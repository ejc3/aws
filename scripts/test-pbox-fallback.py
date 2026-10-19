#!/usr/bin/env python3
"""`pbox up N --fallback` (pbox-fallback.tf, modules/pbox-fallback-site): plain `up` stays home and exits 3 naming
--fallback; with the flag, the other US AZs are tried best spot placement score first, with a fresh work volume; the
box is then found wherever it runs. Offline: the real scripts/parallel-box.sh against a fake `aws` and `ssh`, plus
source checks of the Terraform."""
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "parallel-box.sh"
FALLBACK = (ROOT / "pbox-fallback.tf").read_text()
MODULE = (ROOT / "modules" / "pbox-fallback-site" / "main.tf").read_text()
OHIO = (ROOT / "ohio-pbox.tf").read_text()
LAUNCH = (ROOT / "parallel-box-launch.tf").read_text()

POINTER = {
    "region": "us-east-2",
    "fallback": {
        "template": "parallel-box-fallback",
        "regions": {
            "us-east-1": {"subnets": {"us-east-1a": "subnet-e1a", "us-east-1b": "subnet-e1b"}},
            "us-west-2": {"subnets": {"us-west-2a": "subnet-w2a", "us-west-2b": "subnet-w2b"}},
        },
    },
}

# Calls are appended to $FAKE_LOG. FAKE_CAPACITY lists the "region/az" pools that have capacity (home is
# us-east-2/us-east-2c); FAKE_HOME_ERROR replaces the home failure with another error; a launched box is remembered in
# $FAKE_STATE as "region az" so the describe calls afterwards find it there.
FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
a = " ".join(args)
open(os.environ["FAKE_LOG"], "a").write(json.dumps(args) + "\n")
def opt(name):
    return args[args.index(name) + 1] if name in args else ""
region = opt("--region")
state = open(os.environ["FAKE_STATE"]).read().split() if os.path.exists(os.environ["FAKE_STATE"]) else []
here = bool(state) and state[0] == region
if "get-parameter" in a:
    print(os.environ["FAKE_POINTER"])
elif "get-spot-placement-scores" in a:
    # zone IDs, as the API answers; us-west-2b scores best
    print("9\tus-west-2\tusw2-az1\n3\tus-east-1\tuse1-az1\n5\tus-west-2\tusw2-az2\n2\tus-east-1\tuse1-az2")
elif "describe-availability-zones" in a:
    print({"us-east-1": "use1-az1\tus-east-1a\nuse1-az2\tus-east-1b",
           "us-west-2": "usw2-az2\tus-west-2a\nusw2-az1\tus-west-2b",
           "us-east-2": "use2-az3\tus-east-2c"}.get(region, ""))
elif "describe-instance-type-offerings" in a:
    print("c8g.48xlarge\tm8g.48xlarge")
elif "describe-launch-template-versions" in a:
    print("spot")
elif "describe-instances" in a and "PublicIpAddress" in a:
    print("203.0.113.9" if here else "None")
elif "describe-instances" in a and "InstanceId" in a:
    print("i-0fallback" if here else "None")
elif "describe-instances" in a and "AvailabilityZone" in a:
    print(state[1] if here else "None")
elif "describe-instances" in a and "Fallback" in a:
    print("true" if here and state[0] + "/" + state[1] != "us-east-2/us-east-2c" else "None")
elif "describe-instances" in a:
    print("None")
elif "describe-volumes" in a and "[State," in a:
    print("available\tNone")
elif "describe-volumes" in a and "AvailabilityZone" in a:
    print("us-east-2c")
elif "describe-volumes" in a:
    print("vol-1\t100\tavailable")
elif "describe-instance-types" in a:
    print("192")
elif "describe-spot-price-history" in a:
    print("2.2198")
elif "run-instances" in a:
    subnet = opt("--subnet-id")
    az = {"subnet-e1a": "us-east-1a", "subnet-e1b": "us-east-1b", "subnet-w2a": "us-west-2a",
          "subnet-w2b": "us-west-2b", "": "us-east-2c"}[subnet]
    if not subnet and os.environ.get("FAKE_HOME_ERROR"):
        print("An error occurred (UnauthorizedOperation) when calling the RunInstances operation")
        sys.exit(254)
    if subnet and region in os.environ.get("FAKE_QUOTA", "").split():
        print("An error occurred (VcpuLimitExceeded) when calling the RunInstances operation")
        sys.exit(254)
    if region + "/" + az in os.environ.get("FAKE_CAPACITY", "").split():
        open(os.environ["FAKE_STATE"], "w").write(region + " " + az)
        print("i-0fallback")
    else:
        print("An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation")
        sys.exit(254)
elif "terminate-instances" in a:
    pass
else:
    print("None")
'''


class FallbackScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, body in (("aws", FAKE_AWS), ("ssh", "#!/bin/sh\nexit 0\n"), ("sleep", "#!/bin/sh\nexit 0\n")):
            p = self.tmp / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "calls"

    def pbox(self, *args, capacity="", home_error=False, launched=None, quota=""):
        if launched:
            (self.tmp / "state").write_text(launched)
        env = dict(os.environ, PATH=f"{self.tmp}:{os.environ['PATH']}", HOME=str(self.tmp),
                   FAKE_LOG=str(self.log), FAKE_STATE=str(self.tmp / "state"), FAKE_POINTER=json.dumps(POINTER),
                   FAKE_CAPACITY=capacity, FAKE_HOME_ERROR="1" if home_error else "", FAKE_QUOTA=quota,
                   PARALLEL_BOX_TYPES="c8g.48xlarge m8g.48xlarge")
        env.pop("PARALLEL_BOX_REGION", None)
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120)
        return r.returncode, r.stdout, r.stderr

    def calls(self, word):
        if not self.log.exists():
            return []
        return [c for c in map(json.loads, self.log.read_text().splitlines()) if word in c]

    def test_plain_up_stays_home_and_exits_3_naming_the_fallback(self):
        rc, out, err = self.pbox("up", "4", capacity="us-west-2/us-west-2b")
        self.assertEqual(rc, 3, err)
        self.assertIn("pbox up 4 --fallback", err.replace("parallel-box.sh", "pbox"))
        self.assertIn("EMPTY work volume", err)
        self.assertEqual([c for c in self.calls("run-instances") if "--subnet-id" in c], [])
        self.assertEqual(out, "")

    def test_fallback_tries_the_best_scored_az_first_with_a_fresh_disk(self):
        rc, out, err = self.pbox("up", "4", "--fallback", capacity="us-west-2/us-west-2b us-east-1/us-east-1a")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.strip().splitlines()[-1],
                         "launched box=4 region=us-west-2 az=us-west-2b type=c8g.48xlarge ip=203.0.113.9 work=fresh")
        runs = self.calls("run-instances")
        self.assertNotIn("--subnet-id", runs[0])  # home first
        fb = [c for c in runs if "--subnet-id" in c]
        self.assertEqual(len(fb), 1)  # usw2-az1 = us-west-2b scores 9: tried first, and it had capacity
        self.assertEqual(fb[0][fb[0].index("--subnet-id") + 1], "subnet-w2b")
        self.assertIn("LaunchTemplateName=parallel-box-fallback,Version=$Latest", fb[0])
        tags = [a for a in fb[0] if a.startswith("ResourceType=")]
        self.assertEqual([t.split(",")[0] for t in tags],
                         ["ResourceType=instance", "ResourceType=volume", "ResourceType=network-interface"])
        for t in tags:
            self.assertIn("{Key=Name,Value=parallel-box-4}", t)
        self.assertIn("{Key=Fallback,Value=true}", tags[0])
        self.assertEqual(self.calls("attach-volume"), [])  # never the home volume
        self.assertIn("FRESH, EMPTY work volume", err)

    def test_regions_limits_the_search(self):
        rc, out, err = self.pbox("up", "2", "--regions", "us-east-1", capacity="us-west-2/us-west-2b us-east-1/us-east-1b")
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.strip().endswith("region=us-east-1 az=us-east-1b type=c8g.48xlarge ip=203.0.113.9 work=fresh"))
        self.assertFalse(any("subnet-w2b" in c for c in self.calls("run-instances")))

    def test_regions_still_finds_a_box_running_outside_them(self):
        # --regions narrows where a new box is searched for, never where an existing one is looked for.
        rc, out, _ = self.pbox("up", "4", "--regions", "us-east-1", launched="us-west-2 us-west-2b",
                               capacity="us-east-1/us-east-1a")
        self.assertEqual(rc, 0)
        self.assertIn("running box=4 region=us-west-2 az=us-west-2b", out)
        self.assertEqual(self.calls("run-instances"), [])

    def test_regions_needs_a_value(self):
        for args in (("up", "2", "--regions"), ("up", "2", "--regions=")):
            rc, _, err = self.pbox(*args)
            self.assertEqual(rc, 1, args)
            self.assertIn("--regions needs a list", err)
        self.assertEqual(self.calls("run-instances"), [])

    def test_a_quota_refusal_is_no_capacity_there_not_a_real_error(self):
        rc, _, err = self.pbox("up", "1", "--fallback", quota="us-west-2", capacity="us-east-1/us-east-1b")
        self.assertEqual(rc, 0, err)
        self.assertIn("over the account's quota", err)
        (self.tmp / "state").unlink()  # the fake remembers one launched box, whatever its number
        rc, _, err = self.pbox("up", "2", "--fallback", quota="us-east-1 us-west-2")
        self.assertEqual(rc, 4, err)

    def test_unknown_region_is_refused(self):
        rc, _, err = self.pbox("up", "2", "--regions=us-east-9")
        self.assertEqual(rc, 1)
        self.assertIn("no fallback site in 'us-east-9'", err)
        self.assertEqual(self.calls("run-instances"), [])

    def test_no_capacity_anywhere_exits_4(self):
        rc, out, err = self.pbox("up", "3", "--fallback")
        self.assertEqual(rc, 4, err)
        self.assertEqual(len([c for c in self.calls("run-instances") if "--subnet-id" in c]), 8)  # 4 AZs x 2 types

    def test_a_real_error_at_home_is_not_a_capacity_failure(self):
        rc, _, err = self.pbox("up", "1", "--fallback", home_error=True, capacity="us-west-2/us-west-2b")
        self.assertEqual(rc, 1)
        self.assertIn("UnauthorizedOperation", err)
        self.assertEqual([c for c in self.calls("run-instances") if "--subnet-id" in c], [])

    def test_ip_status_down_find_the_box_where_it_runs(self):
        rc, out, _ = self.pbox("ip", "4", launched="us-west-2 us-west-2b")
        self.assertEqual((rc, out.strip()), (0, "203.0.113.9"))
        _, out, _ = self.pbox("status", "4", launched="us-west-2 us-west-2b")
        self.assertIn("RUNNING at 203.0.113.9", out)
        self.assertIn("FALLBACK site us-west-2b", out)
        rc, _, err = self.pbox("down", "4", launched="us-west-2 us-west-2b")
        self.assertEqual(rc, 0)
        term = self.calls("terminate-instances")
        self.assertEqual(term[0][term[0].index("--region") + 1], "us-west-2")
        self.assertIn("fresh work volume is deleted with it", err)

    def test_up_of_a_box_running_elsewhere_launches_nothing(self):
        rc, out, _ = self.pbox("up", "4", launched="us-west-2 us-west-2b")
        self.assertEqual(rc, 0)
        self.assertIn("running box=4 region=us-west-2 az=us-west-2b", out)
        self.assertEqual(self.calls("run-instances"), [])

    def test_fallback_only_applies_to_up(self):
        rc, _, err = self.pbox("status", "--fallback")
        self.assertEqual(rc, 1)
        self.assertIn("only apply to 'up'", err)

    def test_capacity_marks_home_and_fallback_azs(self):
        rc, out, _ = self.pbox("capacity")
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0], "9 us-west-2 us-west-2b fallback (fresh work volume)")
        self.assertEqual(self.calls("run-instances"), [])


def block(text, header):
    m = re.search(re.escape(header) + r" \{\n.*?\n\}\n", text, re.S)
    assert m, header
    return m.group(0)


class FallbackTerraform(unittest.TestCase):
    def test_four_us_regions_and_never_the_home_az(self):
        self.assertEqual(re.findall(r'^\s+"(us-[a-z]+-\d)" = \{ cidr', FALLBACK, re.M),
                         ["us-east-1", "us-east-2", "us-west-1", "us-west-2"])
        self.assertIn("az if az != local.ohio_pbox_az", FALLBACK)
        for r in ("us_east_1", "us_east_2", "us_west_1", "us_west_2"):
            self.assertIn(f'module "pbox_fallback_{r}"', FALLBACK)

    def test_cidrs_are_their_own(self):
        for c in ("10.13.0.0/16", "10.14.0.0/16", "10.15.0.0/16", "10.16.0.0/16"):
            hits = [p.name for p in ROOT.glob("*.tf") if c in p.read_text()]
            self.assertEqual(hits, ["pbox-fallback.tf"], c)

    def test_the_work_volume_is_fresh_and_dies_with_the_box(self):
        lt = block(MODULE, 'resource "aws_launch_template" "box"')
        self.assertEqual(lt.count("delete_on_termination = true"), 2)
        self.assertIn('device_name = "/dev/sdf"', lt)
        self.assertNotIn("network_interfaces", lt)  # the caller's --subnet-id picks the AZ
        self.assertIn('Fallback = "true"', lt)
        self.assertNotIn("Name     =", lt)  # the caller names the box; IAM requires one of the box names
        self.assertNotIn("aws_ebs_volume", MODULE)
        self.assertNotIn("snapshot", MODULE)

    def test_the_boot_script_formats_only_a_disk_without_a_filesystem_and_skips_nfs(self):
        ud = re.search(r"pbox_fallback_user_data = base64encode\(<<-INIT\n(.*?)\n  INIT", FALLBACK, re.S).group(1)
        self.assertEqual(ud.count("mkfs.ext4"), 1)
        self.assertIn('if ! blkid "$DEV"', ud)
        self.assertIn("refusing to format anything", ud)
        self.assertIn("$1 != r", ud)  # never the root disk
        self.assertNotIn("io_box_client_setup", ud)
        self.assertIn("tls_private_key.dev_hop.public_key_openssh", ud)

    def test_the_policy_pins_the_templates_and_the_box_names_and_is_its_own(self):
        doc = block(FALLBACK, 'data "aws_iam_policy_document" "parallel_box_fallback"')
        self.assertEqual(doc.count("values   = local.pbox_fallback_template_arns"), 2)
        self.assertEqual(doc.count("values   = local.parallel_box_names"), 2)
        self.assertNotIn("iam:PassRole", doc)
        self.assertNotIn("AttachVolume", doc)
        self.assertNotIn('"*"', doc)
        self.assertIn('resource "aws_iam_policy" "parallel_box_fallback"', FALLBACK)
        for role in ("aws_iam_role.dev_server.name", "aws_iam_role.nextjs_dev.name"):
            self.assertIn(role, FALLBACK)

    def test_watchdogs_where_none_runs_yet(self):
        for r, on in (("us_east_1", True), ("us_east_2", False), ("us_west_1", True), ("us_west_2", False)):
            m = block(FALLBACK, f'module "pbox_fallback_{r}"')
            self.assertIn("local.pbox_fallback_watchdog" if on else "local.pbox_no_watchdog", m)
        self.assertIn('TAG_NAMES     = join(",", local.parallel_box_names)', FALLBACK)

    def test_the_pointer_publishes_the_sites_and_pbox_may_score_them(self):
        self.assertIn("jsonencode({ region = local.parallel_box_region, fallback = local.pbox_fallback_pointer })", OHIO)
        describe = re.search(r'sid    = "DescribeForPbox".*?\]', LAUNCH, re.S).group(0)
        for action in ("ec2:GetSpotPlacementScores", "ec2:DescribeAvailabilityZones", "ec2:DescribeInstanceTypeOfferings"):
            self.assertIn(action, describe)


if __name__ == "__main__":
    unittest.main()

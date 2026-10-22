#!/usr/bin/env python3
"""`pbox up N FLAVOR --open` (ohio-pbox.tf, parallel-box-launch.tf): the home launch replaces the template's interface 0
with one that carries the SSH group and the open group and asks for a delegated IPv6 /80, and the /80 is reported. It is
refused for other commands, with --fallback / --regions, and for a member's box; plain `up` is unchanged. Offline: the
real scripts/parallel-box.sh against a fake `aws` and `ssh`, plus source checks of the Terraform."""
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = Path(os.environ.get("PBOX_SCRIPT", ROOT / "scripts" / "parallel-box.sh"))
OHIO = (ROOT / "ohio-pbox.tf").read_text()
LAUNCH = (ROOT / "parallel-box-launch.tf").read_text()
MODULE = (ROOT / "modules" / "pbox-fallback-site" / "main.tf").read_text()
MEMBER_TF = (ROOT / "member-pbox.tf").read_text()

PREFIX = "2001:db8:0:1:a::/80"  # documentation range
POINTER = {
    "region": "us-east-2",
    "x86_image": "ami-x86-home",
    "open": {"subnet": "subnet-home", "security_groups": ["sg-ssh", "sg-open"]},
    "fallback": {
        "template": "parallel-box-fallback",
        "regions": {"us-west-2": {"subnets": {"us-west-2a": "subnet-w2a"}, "x86_image": "ami-x86-w2"}},
    },
}

# Calls are appended to $FAKE_LOG. A launched (or FAKE_RUNNING) box is remembered in $FAKE_STATE as "region az prefix",
# prefix "-" for none. A launch gets PREFIX when it passes --network-interfaces asking for one, unless FAKE_NO_PREFIX.
# FAKE_CAPACITY=0 makes every launch fail for capacity.
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
elif "describe-launch-template-versions" in a:
    print("spot")
elif "describe-instances" in a and "NetworkInterfaceId" in a:
    print("eni-0open")
elif "assign-ipv6-addresses" in a:
    print("2001:db8:0:1::5")
elif "describe-instances" in a and "Ipv6Prefixes" in a:
    print(state[2] if here and state[2] != "-" else "None")
elif "describe-instances" in a and "PublicIpAddress" in a:
    print("203.0.113.9" if here else "None")
elif "describe-instances" in a and "InstanceId" in a:
    print("i-0open" if here else "None")
elif "describe-instances" in a and "AvailabilityZone" in a:
    print(state[1] if here else "None")
elif "describe-instances" in a and "InstanceType" in a:
    print("c8g.metal-48xl" if here else "None")
elif "describe-instances" in a:
    print("None")
elif "describe-volumes" in a and "Attachments[0].InstanceId]" in a and "[VolumeId" in a:
    print("vol-1\tin-use\tus-east-2c\ti-0open" if here else "vol-1\tavailable\tus-east-2c\tNone")
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
    if os.environ.get("FAKE_CAPACITY") == "0":
        print("An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation")
        sys.exit(254)
    nic = json.loads(opt("--network-interfaces") or "[]")
    prefix = "-"
    if nic and nic[0].get("Ipv6PrefixCount") and not os.environ.get("FAKE_NO_PREFIX"):
        prefix = os.environ["FAKE_PREFIX"]
    open(sp, "w").write(region + " us-east-2c " + prefix)
    print("i-0open")
elif "attach-volume" in a:
    open(sp + ".attached", "w").write("1")
else:
    print("None")
'''


class OpenScript(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, body in (("aws", FAKE_AWS), ("ssh", "#!/bin/sh\nexit 0\n"), ("sleep", "#!/bin/sh\nexit 0\n")):
            p = self.tmp / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IXUSR)
        self.log = self.tmp / "calls"

    def pbox(self, *args, pointer=None, running=None, **extra):
        if running is not None:
            (self.tmp / "state").write_text("us-east-2 us-east-2c " + running)
        env = dict(os.environ, PATH=f"{self.tmp}:{os.environ['PATH']}", HOME=str(self.tmp), FAKE_LOG=str(self.log),
                   FAKE_STATE=str(self.tmp / "state"), FAKE_POINTER=json.dumps(pointer or POINTER), FAKE_PREFIX=PREFIX,
                   **extra)
        env.pop("PARALLEL_BOX_REGION", None)
        env.setdefault("MEMBER_BOX_USER", "example-owner")
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120)
        return r.returncode, r.stdout, r.stderr

    def runs(self):
        if not self.log.exists():
            return []
        return [c for c in map(json.loads, self.log.read_text().splitlines()) if "run-instances" in c]

    def test_open_launch_asks_for_the_prefix_only_then_gives_the_box_its_own_address(self):
        # EC2 creates an interface with an address count OR a prefix count, never both (checked live 2026-10-22).
        rc, out, err = self.pbox("up", "3", "kvm-192", "--open")
        self.assertEqual(rc, 0, err)
        nic = json.loads(self.runs()[0][self.runs()[0].index("--network-interfaces") + 1])
        self.assertNotIn("Ipv6AddressCount", nic[0])
        calls = [c for c in map(json.loads, self.log.read_text().splitlines()) if "assign-ipv6-addresses" in c]
        self.assertEqual(len(calls), 1)
        c = calls[0]
        self.assertEqual(c[c.index("--network-interface-id") + 1], "eni-0open")
        self.assertEqual(c[c.index("--ipv6-address-count") + 1], "1")
        self.assertIn("the box's own IPv6 address: 2001:db8:0:1::5", err)

    def test_plain_up_assigns_no_ipv6(self):
        rc, out, err = self.pbox("up", "3", "kvm-192")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("assign-ipv6-addresses", self.log.read_text())

    def test_a_gpu_flavor_with_open_stays_at_home(self):
        # A GPU flavor implies --fallback, except with --open, which launches only in the home AZ (kvm-gpu-96 is offered there).
        rc, out, err = self.pbox("up", "3", "kvm-gpu-96", "--open")
        self.assertNotIn("Drop --fallback", err)
        for run in self.runs():
            self.assertEqual(run[run.index("--region") + 1], "us-east-2")

    def test_open_launch_replaces_interface_0_with_both_groups_and_a_prefix(self):
        rc, out, err = self.pbox("up", "3", "kvm-192", "--open")
        self.assertEqual(rc, 0, err)
        runs = self.runs()
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertIn("LaunchTemplateName=parallel-box-3,Version=$Latest", run)  # still the box's own template
        self.assertNotIn("--subnet-id", run)
        self.assertNotIn("--security-group-ids", run)
        self.assertEqual(run.count("--network-interfaces"), 1)
        nic = json.loads(run[run.index("--network-interfaces") + 1])
        self.assertEqual(nic, [{"DeviceIndex": 0, "SubnetId": "subnet-home", "Groups": ["sg-ssh", "sg-open"],
                                "AssociatePublicIpAddress": True, "DeleteOnTermination": True,
                                "Ipv6PrefixCount": 1}])
        self.assertEqual(out.strip().splitlines()[-1],
                         "launched box=3 region=us-east-2 az=us-east-2c type=c8g.metal-48xl ip=203.0.113.9 "
                         "work=persistent ipv6_prefix=" + PREFIX)
        self.assertIn(f"  ipv6:  {PREFIX} (fcvm --network routed --ipv6-prefix {PREFIX})", err)
        self.assertIn("every port of this box and its VMs is reachable from the internet", err)

    def test_plain_up_is_unchanged(self):
        rc, out, err = self.pbox("up", "3", "kvm-192")
        self.assertEqual(rc, 0, err)
        run = self.runs()[0]
        self.assertNotIn("--network-interfaces", run)
        self.assertEqual(out.strip().splitlines()[-1],
                         "launched box=3 region=us-east-2 az=us-east-2c type=c8g.metal-48xl ip=203.0.113.9 work=persistent")
        self.assertNotIn("ipv6", err)

    def test_an_open_launch_without_a_prefix_fails_loudly(self):
        rc, out, err = self.pbox("up", "3", "kvm-192", "--open", FAKE_NO_PREFIX="1")
        self.assertEqual(rc, 1, err)
        self.assertIn("EC2 reports no IPv6 prefix", err)
        self.assertNotIn("launched box=", out)

    def test_open_is_refused_with_fallback_and_regions(self):
        for extra in (("--fallback",), ("--regions", "us-west-2"), ("--regions=us-west-2",)):
            rc, _, err = self.pbox("up", "3", "kvm-192", "--open", *extra)
            self.assertEqual(rc, 1, extra)
            self.assertIn("--open launches only in the home AZ", err)
        self.assertEqual(self.runs(), [])

    def test_open_only_applies_to_up(self):
        for cmd in (("status",), ("down", "3"), ("ssh", "3"), ("ip", "3"), ("capacity", "kvm-192")):
            rc, _, err = self.pbox(*cmd, "--open")
            self.assertEqual(rc, 1, cmd)
            self.assertIn("--open only applies to 'up'", err)
        self.assertEqual(self.runs(), [])

    def test_a_members_box_refuses_open_before_calling_aws(self):
        rc, _, err = self.pbox("up", "compute-48", "--open", MEMBER_BOX_USER="skevh")
        self.assertEqual(rc, 1)
        self.assertIn("--open is not available for pbox-skevh", err)
        self.assertFalse(self.log.exists())

    def test_no_open_interface_in_the_pointer_launches_nothing(self):
        pointer = json.loads(json.dumps(POINTER))
        del pointer["open"]
        rc, _, err = self.pbox("up", "3", "kvm-192", "--open", pointer=pointer)
        self.assertEqual(rc, 1)
        self.assertIn("names no open network interface", err)
        self.assertEqual(self.runs(), [])
        pointer["open"] = {"subnet": "subnet-home", "security_groups": ["sg-ssh"]}  # the open group missing
        rc, _, _ = self.pbox("up", "3", "kvm-192", "--open", pointer=pointer)
        self.assertEqual(rc, 1)
        self.assertEqual(self.runs(), [])

    def test_no_home_capacity_with_open_exits_3_without_offering_the_fallback(self):
        rc, _, err = self.pbox("up", "3", "kvm-192", "--open", FAKE_CAPACITY="0")
        self.assertEqual(rc, 3, err)
        self.assertIn("--fallback is not an option with it", err)
        self.assertNotIn("--fallback      (or --regions", err)
        self.assertEqual([c for c in self.runs() if "--subnet-id" in c], [])
        self.assertTrue(all("--network-interfaces" in c for c in self.runs()))

    def test_up_open_of_a_box_running_without_it_launches_nothing_and_says_so(self):
        (self.tmp / "state.attached").write_text("1")
        rc, out, err = self.pbox("up", "3", "kvm-192", "--open", running="-")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.runs(), [])
        self.assertIn("already running without --open", err)
        self.assertNotIn("ipv6_prefix=", out)

    def test_status_and_a_rerun_up_report_the_prefix(self):
        _, out, _ = self.pbox("status", "3", running=PREFIX)
        self.assertIn(f"  ipv6:   {PREFIX} (fcvm --network routed --ipv6-prefix {PREFIX})", out)
        self.assertIn("launched with --open", out)
        rc, out, err = self.pbox("up", "3", "kvm-192", running=PREFIX)
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.strip().splitlines()[-1].endswith(" ipv6_prefix=" + PREFIX), out)
        self.assertEqual(self.runs(), [])

    def test_status_of_a_box_without_a_prefix_shows_no_ipv6(self):
        _, out, _ = self.pbox("status", "3", running="-")
        self.assertIn("RUNNING at 203.0.113.9", out)
        self.assertNotIn("ipv6", out)

    def test_help_documents_open(self):
        _, out, _ = self.pbox("help")
        self.assertIn("pbox up [N] FLAVOR --open", out)
        self.assertIn("--network routed --ipv6-prefix <prefix>/80", out)
        self.assertIn("EVERY PORT IS PUBLIC", out)


def block(text, header):
    m = re.search(re.escape(header) + r" \{\n.*?\n\}\n", text, re.S)
    assert m, header
    return m.group(0)


class OpenTerraform(unittest.TestCase):
    def test_the_open_group_lets_everything_in_and_is_in_the_box_vpc(self):
        sg = block(OHIO, 'resource "aws_security_group" "ohio_pbox_open"')
        self.assertIn("vpc_id      = aws_vpc.ohio_pbox.id", sg)
        self.assertEqual(sg.count("ingress {"), 1)
        ingress = re.search(r"ingress \{.*?\n  \}", sg, re.S).group(0)
        self.assertIn('protocol         = "-1"', ingress)
        self.assertIn('cidr_blocks      = ["0.0.0.0/0"]', ingress)
        self.assertIn('ipv6_cidr_blocks = ["::/0"]', ingress)
        self.assertIn("pbox up --open", sg)

    def test_the_templates_still_attach_only_the_ssh_group(self):
        lt = block(LAUNCH, 'resource "aws_launch_template" "parallel_box_ohio"')
        self.assertIn("security_groups             = [aws_security_group.ohio_pbox.id]", lt)
        self.assertNotIn("ohio_pbox_open", lt)

    def test_iam_allows_the_open_group_by_exact_arn_through_the_templates_only(self):
        sites = re.search(r"parallel_box_sites = \{.*?\n  \}\n", LAUNCH, re.S).group(0)
        self.assertRegex(sites, r"\n      open_security_group_id = aws_security_group\.ohio_pbox_open\.id\n")
        referenced = re.search(r"parallel_box_referenced_arns = flatten\(\[.*?\n  \]\)", LAUNCH, re.S).group(0)
        self.assertIn('"arn:aws:ec2:${r}:${local.parallel_box_account}:security-group/${site.open_security_group_id}",',
                      referenced)
        stmt = re.search(r'sid       = "RunInstancesReferencedResources".*?\n  \}', LAUNCH, re.S).group(0)
        self.assertIn("local.parallel_box_referenced_arns", stmt)
        self.assertIn("values   = local.parallel_box_template_arns", stmt)

    def test_the_members_grant_does_not_name_the_open_group(self):
        self.assertNotIn("ohio_pbox_open", MEMBER_TF)
        self.assertNotIn("open_security_group_id", MEMBER_TF)

    def test_the_pointer_publishes_the_home_subnet_and_both_groups_ssh_first(self):
        ptr = re.search(r"parallel_box_open_pointer = \{.*?\n  \}", OHIO, re.S).group(0)
        self.assertIn("subnet          = local.parallel_box_sites[local.parallel_box_region].subnet_id", ptr)
        self.assertIn("security_groups = [local.parallel_box_sites[local.parallel_box_region].security_group_id, "
                      "local.parallel_box_sites[local.parallel_box_region].open_security_group_id]", ptr)
        self.assertIn("open = local.parallel_box_open_pointer", OHIO)

    def test_the_home_subnet_has_ipv6_to_delegate_and_the_fallback_sites_do_not(self):
        # A /80 is carved from the subnet's IPv6 /64, and routed out through the VPC's ::/0 route.
        subnet = block(OHIO, 'resource "aws_subnet" "ohio_pbox"')
        self.assertIn("ipv6_cidr_block                 = cidrsubnet(aws_vpc.ohio_pbox.ipv6_cidr_block, 8, 1)", subnet)
        self.assertIn("assign_generated_ipv6_cidr_block = true", block(OHIO, 'resource "aws_vpc" "ohio_pbox"'))
        self.assertIn('ipv6_cidr_block = "::/0"', block(OHIO, 'resource "aws_route_table" "ohio_pbox"'))
        # The fallback sites are IPv4 only, which is why the script refuses --open with --fallback. Adding IPv6 there
        # is the change that lifts the refusal.
        self.assertNotIn("ipv6", block(MODULE, 'resource "aws_subnet" "this"'))
        self.assertNotIn("ipv6", block(MODULE, 'resource "aws_vpc" "this"'))


if __name__ == "__main__":
    unittest.main(buffer=True)

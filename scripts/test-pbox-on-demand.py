#!/usr/bin/env python3
"""`pbox up [N] FLAVOR --on-demand` (scripts/parallel-box.sh, parallel-box-launch.tf, modules/pbox-fallback-site): one launch is
bought on-demand from the box's always on-demand sibling launch template ("<box>-ondemand" at home,
"parallel-box-fallback-ondemand" at a fallback site), while every other launch stays spot. The price shown is the on-demand
list price, the quota named is the on-demand one, `status` reports the box ON-DEMAND, and the flag is refused for other
commands and for a member's box. Offline: the real script against a fake `aws` and `ssh`, plus source checks of the
Terraform, and the three IAM policies involved rendered with placeholder ARNs against IAM's 6,144-character limit.

Live proof needs the apply: `aws ec2 describe-launch-template-versions --launch-template-name parallel-box-ondemand
--versions '$Latest'` shows no InstanceMarketOptions, and `aws iam simulate-principal-policy` against nextjs-dev-role allows
RunInstances through the -ondemand templates."""
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
LAUNCH = (ROOT / "parallel-box-launch.tf").read_text()
FALLBACK = (ROOT / "pbox-fallback.tf").read_text()
FLAVORS_TF = (ROOT / "pbox-flavors.tf").read_text()
MODULE = (ROOT / "modules" / "pbox-fallback-site" / "main.tf").read_text()
WATCHDOG = (ROOT / "parallel-box-watchdog.tf").read_text()
WORKFLOW = (ROOT / ".github" / "workflows" / "lambda-tests.yml").read_text()

POINTER = {
    "region": "us-east-2",
    "x86_image": "ami-x86-home",
    "open": {"subnet": "subnet-home", "security_groups": ["sg-ssh", "sg-open"]},
    "fallback": {
        "template": "parallel-box-fallback",
        "ondemand_template": "parallel-box-fallback-ondemand",
        "regions": {"us-west-2": {"subnets": {"us-west-2a": "subnet-w2a"}, "x86_image": "ami-x86-w2"}},
    },
}

# Calls are appended to $FAKE_LOG. A launched (or FAKE_RUNNING) box is remembered in $FAKE_STATE as
# "region az lifecycle": lifecycle "None" (on-demand) when it was launched from a template whose name ends in -ondemand,
# else "spot". FAKE_CAPACITY lists the "region/az" pools with capacity; FAKE_VCPU_LIMIT the regions that refuse with
# VcpuLimitExceeded. A template named *-ondemand reads as on-demand (no market options), any other as spot; FAKE_TEMPLATE
# overrides the answer for every template ("spot", "None", or "missing" for an unreadable one).
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
    forced = os.environ.get("FAKE_TEMPLATE", "")
    if forced == "missing":
        print("An error occurred (InvalidLaunchTemplateName.NotFoundException)", file=sys.stderr)
        sys.exit(254)
    print(forced or ("None" if opt("--launch-template-name").endswith("-ondemand") else "spot"))
elif "get-products" in a:
    t = [f for f in args if "Field=instanceType" in f][0].rsplit("=", 1)[1]
    usd = {"g4dn.metal": "7.8240000000", "g5g.metal": "2.7440000000"}.get(t, "1.2340000000")
    print(json.dumps({"product": {"attributes": {"instanceType": t}},
                      "terms": {"OnDemand": {"X": {"priceDimensions": {"X.Y": {"unit": "Hrs", "pricePerUnit": {"USD": usd}}}}}}}))
elif "get-service-quota" in a:
    print({"L-DB2E81BA": "128.0", "L-1216C47A": "512.0"}.get(opt("--quota-code"), "96.0"))
elif "get-spot-placement-scores" in a:
    print("9\tus-west-2\tusw2-az1")
elif "describe-availability-zones" in a:
    print({"us-west-2": "usw2-az1\tus-west-2a", "us-east-2": "use2-az3\tus-east-2c"}.get(region, ""))
elif "describe-instance-type-offerings" in a:
    print("\t".join([f for f in args if f.startswith("Name=instance-type,Values=")][0].split("=", 2)[2].split(",")))
elif "describe-spot-instance-requests" in a:
    print("sir-stale\ti-0gone")
elif "describe-instances" in a and "State.Name" in a:
    print("terminated")
elif "describe-instances" in a and "InstanceLifecycle" in a:
    print(state[2] if here else "None")
elif "describe-instances" in a and "Ipv6Prefixes" in a:
    print("None")
elif "describe-instances" in a and "PublicIpAddress" in a:
    print("203.0.113.9" if here else "None")
elif "describe-instances" in a and "InstanceId" in a:
    print("i-0od" if here else "None")
elif "describe-instances" in a and "AvailabilityZone" in a:
    print(state[1] if here else "None")
elif "describe-instances" in a and "Fallback" in a:
    print("true" if here and state[1] != "us-east-2c" else "None")
elif "describe-instances" in a and "InstanceType" in a:
    print("c8g.12xlarge" if here else "None")
elif "describe-instances" in a:
    print("None")
elif "describe-volumes" in a and "Attachments[0].InstanceId]" in a and "[VolumeId" in a:
    print("vol-1\tin-use\tus-east-2c\ti-0od" if here else "vol-1\tavailable\tus-east-2c\tNone")
elif "describe-volumes" in a and "[State," in a:
    print("available\tNone")
elif "describe-volumes" in a and "AvailabilityZone" in a:
    print("us-east-2c")
elif "describe-volumes" in a:
    print("vol-1\t100\tavailable")
elif "describe-instance-types" in a:
    print("48")
elif "describe-spot-price-history" in a:
    print("0.4321")
elif "run-instances" in a:
    lt = [f for f in args if f.startswith("LaunchTemplateName=")][0].split(",")[0].split("=", 1)[1]
    az = {"subnet-w2a": "us-west-2a", "": "us-east-2c"}[opt("--subnet-id")]
    if region in os.environ.get("FAKE_VCPU_LIMIT", "").split():
        print("An error occurred (VcpuLimitExceeded) when calling the RunInstances operation")
        sys.exit(254)
    if region + "/" + az not in os.environ.get("FAKE_CAPACITY", "us-east-2/us-east-2c").split():
        print("An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation")
        sys.exit(254)
    open(sp, "w").write(" ".join([region, az, "None" if lt.endswith("-ondemand") else "spot"]))
    print("i-0od")
else:
    print("None")
'''


class OnDemandScript(unittest.TestCase):
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
                   FAKE_STATE=str(self.tmp / "state"), FAKE_POINTER=json.dumps(pointer or POINTER), **extra)
        env.pop("PARALLEL_BOX_REGION", None)
        env.setdefault("MEMBER_BOX_USER", "example-owner")
        r = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env, timeout=120)
        return r.returncode, r.stdout, r.stderr

    def calls(self, word):
        if not self.log.exists():
            return []
        return [c for c in map(json.loads, self.log.read_text().splitlines()) if word in c]

    def templates(self):
        return [[f for f in c if f.startswith("LaunchTemplateName=")][0] for c in self.calls("run-instances")]

    def test_up_on_demand_launches_from_the_boxs_ondemand_sibling(self):
        rc, out, err = self.pbox("up", "3", "compute-48", "--on-demand")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.templates(), ["LaunchTemplateName=parallel-box-3-ondemand,Version=$Latest"])
        # the template decides the market; the call passes no market options of its own
        self.assertNotIn("--instance-market-options", self.calls("run-instances")[0])
        lookups = self.calls("describe-launch-template-versions")
        self.assertEqual([c[c.index("--launch-template-name") + 1] for c in lookups], ["parallel-box-3-ondemand"])
        self.assertTrue(out.strip().splitlines()[-1].startswith("launched box=3 region=us-east-2 az=us-east-2c"), out)
        self.assertIn("Work volume vol-1 is attached to i-0od.", err)  # the persistent work volume, as for a spot box

    def test_box_1_uses_parallel_box_ondemand(self):
        rc, _, err = self.pbox("up", "compute-48", "--on-demand")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.templates(), ["LaunchTemplateName=parallel-box-ondemand,Version=$Latest"])

    def test_plain_up_still_launches_spot_from_the_boxs_own_template(self):
        rc, _, err = self.pbox("up", "3", "compute-48")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.templates(), ["LaunchTemplateName=parallel-box-3,Version=$Latest"])
        self.assertIn("PURCHASE MODE: spot", err)
        self.assertEqual(self.calls("get-products"), [])

    def test_the_price_is_the_on_demand_list_price_not_spot(self):
        rc, _, err = self.pbox("up", "3", "compute-48", "--on-demand")
        self.assertEqual(rc, 0, err)
        self.assertIn("--> trying c8g.12xlarge (48 cores, on-demand $1.234/hr) in us-east-2c ...", err)
        self.assertEqual(self.calls("describe-spot-price-history"), [])
        self.assertNotIn("0.4321", err)
        self.assertNotIn("spot reference", err)
        products = self.calls("get-products")
        self.assertEqual(products[0][products[0].index("--region") + 1], "us-east-1")  # the Price List API's region
        self.assertIn("Type=TERM_MATCH,Field=regionCode,Value=us-east-2", products[0])
        self.assertIn("Type=TERM_MATCH,Field=marketoption,Value=OnDemand", products[0])
        self.assertIn("PURCHASE MODE: ON-DEMAND for this launch only", err)
        self.assertIn("about 3x spot", err)

    def test_the_on_demand_quota_is_named_before_the_launch(self):
        _, _, err = self.pbox("up", "3", "compute-48", "--on-demand")
        self.assertIn('Standard instances ("Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances": '
                      "L-1216C47A = 512 vCPU), not the spot quota.", err)
        (self.tmp / "state").unlink()  # the fake remembers one launched box, whatever its number
        _, _, err = self.pbox("up", "4", "kvm-gpu-96", "--on-demand", "--open")
        self.assertIn('G-and-VT instances ("Running On-Demand G and VT instances": L-DB2E81BA = 128 vCPU)', err)
        self.assertIn("on-demand $7.824/hr", err)

    def test_an_on_demand_quota_refusal_names_that_quota_and_cancels_no_spot_request(self):
        rc, _, err = self.pbox("up", "3", "compute-48", "--on-demand", FAKE_VCPU_LIMIT="us-east-2")
        self.assertEqual(rc, 3, err)
        self.assertIn("VcpuLimitExceeded: c8g.12xlarge would put us-east-2 over its on-demand vCPU quota for Standard "
                      'instances ("Running On-Demand Standard (A, C, D, H, I, M, R, T, Z) instances")', err)
        self.assertIn("(L-1216C47A = 512 vCPU) -- trying the next one", err)
        # spot requests do not count against the on-demand quota: none is looked up or cancelled, nothing retried
        self.assertEqual(self.calls("describe-spot-instance-requests"), [])
        self.assertEqual(self.calls("cancel-spot-instance-requests"), [])
        self.assertIn("FAILED: no on-demand capacity in us-east-2c", err)

    def test_a_spot_quota_refusal_still_clears_stale_spot_requests(self):
        rc, _, err = self.pbox("up", "3", "compute-48", FAKE_VCPU_LIMIT="us-east-2")
        self.assertEqual(rc, 3, err)
        self.assertNotEqual(self.calls("describe-spot-instance-requests"), [])

    def test_a_fallback_launch_uses_the_sites_ondemand_template(self):
        rc, out, err = self.pbox("up", "4", "compute-48", "--on-demand", "--fallback", FAKE_CAPACITY="us-west-2/us-west-2a")
        self.assertEqual(rc, 0, err)
        home = [c for c in self.calls("run-instances") if "--subnet-id" not in c]
        self.assertEqual(len(home), 9)  # every compute-48 type at home first
        self.assertEqual(self.templates(), ["LaunchTemplateName=parallel-box-4-ondemand,Version=$Latest"] * 9
                         + ["LaunchTemplateName=parallel-box-fallback-ondemand,Version=$Latest"])
        fb = self.calls("run-instances")[-1]
        self.assertEqual(fb[fb.index("--subnet-id") + 1], "subnet-w2a")
        self.assertIn("--> trying c8g.12xlarge (on-demand $1.234/hr) in us-west-2a ...", err)
        self.assertIn("used only to order the AZs; each launch uses parallel-box-fallback-ondemand", err)
        self.assertTrue(out.strip().splitlines()[-1].endswith("work=fresh"), out)

    def test_a_gpu_flavor_searches_the_fallback_sites_on_demand(self):
        # A GPU flavor implies --fallback; with --on-demand both home and the fallback sites use the on-demand templates.
        rc, _, err = self.pbox("up", "4", "kvm-gpu-64", "--on-demand", FAKE_CAPACITY="us-west-2/us-west-2a")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.templates(), ["LaunchTemplateName=parallel-box-4-ondemand,Version=$Latest",
                                            "LaunchTemplateName=parallel-box-fallback-ondemand,Version=$Latest"])
        self.assertIn("on-demand $2.744/hr", err)

    def test_open_and_on_demand_together(self):
        rc, _, err = self.pbox("up", "3", "compute-48", "--on-demand", "--open")
        run = self.calls("run-instances")[0]
        self.assertIn("LaunchTemplateName=parallel-box-3-ondemand,Version=$Latest", run)
        self.assertIn("--network-interfaces", run)

    def test_no_ondemand_fallback_template_in_the_pointer_launches_nothing(self):
        pointer = json.loads(json.dumps(POINTER))
        del pointer["fallback"]["ondemand_template"]
        rc, _, err = self.pbox("up", "4", "compute-48", "--on-demand", "--fallback", pointer=pointer)
        self.assertEqual(rc, 1)
        self.assertIn("names no on-demand fallback template", err)
        self.assertEqual(self.calls("run-instances"), [])

    def test_a_missing_or_spot_ondemand_template_launches_nothing(self):
        for answer, words in (("missing", "cannot be read"), ("spot", "which is spot, not on-demand")):
            with self.subTest(template=answer):
                rc, _, err = self.pbox("up", "3", "compute-48", "--on-demand", FAKE_TEMPLATE=answer)
                self.assertEqual(rc, 1, err)
                self.assertIn("--on-demand launches from the launch template parallel-box-3-ondemand", err)
                self.assertIn(words, err)
                self.assertEqual(self.calls("run-instances"), [])

    def test_status_of_an_on_demand_launch_says_on_demand(self):
        rc, _, err = self.pbox("up", "3", "compute-48", "--on-demand")
        self.assertEqual(rc, 0, err)
        _, out, _ = self.pbox("status", "3")
        self.assertIn("RUNNING at 203.0.113.9 (c8g.12xlarge, flavor compute-48, ON-DEMAND (about 3x spot))", out)
        # and the next launch is spot again
        self.assertIn("next launch: spot (launch template;", out)
        self.assertIn("up 3 FLAVOR --on-demand` buys that one launch on-demand)", out)

    def test_up_on_demand_of_a_box_running_as_spot_launches_nothing_and_says_so(self):
        (self.tmp / "state.attached").write_text("1")
        rc, _, err = self.pbox("up", "3", "compute-48", "--on-demand", running="spot")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.calls("run-instances"), [])
        self.assertIn("NOTE: parallel-box-3 is already running as spot; it is kept.", err)
        rc, _, err = self.pbox("up", "3", "compute-48", "--on-demand", running="None")
        self.assertNotIn("already running as spot", err)

    def test_on_demand_only_applies_to_up(self):
        for cmd in (("status",), ("down", "3"), ("ssh", "3"), ("ip", "3"), ("capacity", "compute-48"), ("flavors",)):
            rc, _, err = self.pbox(*cmd, "--on-demand")
            self.assertEqual(rc, 1, cmd)
            self.assertIn("--on-demand only applies to 'up'", err)
        self.assertFalse(self.log.exists())  # refused before any AWS call

    def test_a_members_box_refuses_on_demand_before_calling_aws(self):
        rc, _, err = self.pbox("up", "compute-48", "--on-demand", MEMBER_BOX_USER="skevh")
        self.assertEqual(rc, 1)
        self.assertIn("--on-demand is not available for pbox-skevh: its IAM grant (member-pbox.tf) allows spot launches only", err)
        self.assertFalse(self.log.exists())

    def test_help_documents_on_demand(self):
        _, out, _ = self.pbox("help")
        self.assertIn("pbox up [N] FLAVOR --on-demand", out)
        self.assertIn("about 3x the spot price", out)
        self.assertIn('"Running On-Demand G and VT instances"', out)
        self.assertIn("The idle watchdog terminates it like a spot box", out)


def block(text, header):
    m = re.search(re.escape(header) + r" \{\n.*?\n\}\n", text, re.S)
    assert m, header
    return m.group(0)


def sids(doc):
    return re.findall(r'^\s+sid\s+= "(\w+)"', doc, re.M)


class OnDemandTerraform(unittest.TestCase):
    def test_every_home_box_has_an_always_on_demand_sibling_from_the_same_resource(self):
        self.assertEqual(re.findall(r'resource "aws_launch_template" "(\w+)"', LAUNCH), ["parallel_box_ohio"])
        lt = block(LAUNCH, 'resource "aws_launch_template" "parallel_box_ohio"')
        self.assertIn("for_each = local.parallel_box_templates", lt)
        self.assertIn("name                   = each.value.template", lt)
        self.assertIn("for_each = each.value.spot ? [1] : []", lt)
        self.assertIn('user_data = local.parallel_box_user_data["us-east-2/${each.value.box}"]', lt)
        # the box's own keeps its key, name and switch (nothing existing moves); the sibling is never spot
        self.assertIn('{ for k, b in local.parallel_boxes : k => { box = k, name = b.name, template = b.name, '
                      'spot = var.parallel_box_spot } }', LAUNCH)
        self.assertIn('{ for k, b in local.parallel_boxes : "${k}-ondemand" => { box = k, name = b.name, '
                      'template = "${b.name}-ondemand", spot = false } }', LAUNCH)

    def test_both_templates_tag_the_box_with_its_name_so_iam_status_and_the_watchdog_see_it(self):
        lt = block(LAUNCH, 'resource "aws_launch_template" "parallel_box_ohio"')
        self.assertEqual(lt.count("Name    = each.value.name"), 1)  # the instance
        self.assertEqual(lt.count("Name = each.value.name"), 2)      # its root volume and its ENI

    def test_the_idle_watchdog_finds_boxes_by_name_whatever_their_market(self):
        handler = re.search(r"def lambda_handler.*?instances = ", WATCHDOG, re.S).group(0)
        self.assertEqual(re.findall(r'\{"Name": "([^"]+)"', handler), ["tag:Name", "instance-state-name"])
        self.assertNotIn("InstanceLifecycle", WATCHDOG)
        self.assertNotIn("instance-lifecycle", WATCHDOG)

    def test_both_home_templates_are_pinned_by_exact_arn(self):
        self.assertIn("parallel_box_template_arns = [for lt in aws_launch_template.parallel_box_ohio : lt.arn]", LAUNCH)
        doc = block(LAUNCH, 'data "aws_iam_policy_document" "parallel_box_control"')
        self.assertEqual(doc.count("values   = local.parallel_box_template_arns"), 2)
        self.assertIn("resources = concat(local.parallel_box_referenced_arns, local.parallel_box_template_arns)", doc)

    def test_every_fallback_site_has_an_on_demand_sibling_from_the_same_resource(self):
        self.assertEqual(re.findall(r'resource "aws_launch_template" "(\w+)"', MODULE), ["box"])
        self.assertIn('    default  = { name = var.template_name, spot = var.spot }\n'
                      '    ondemand = { name = "${var.template_name}-ondemand", spot = false }\n', MODULE)
        lt = block(MODULE, 'resource "aws_launch_template" "box"')
        self.assertIn("for_each = local.templates", lt)
        self.assertIn("for_each = each.value.spot ? [1] : []", lt)
        self.assertIn("name                   = each.value.name", lt)
        # the existing template keeps its state: moved, not destroyed and created again
        self.assertIn('moved {\n  from = aws_launch_template.box\n  to   = aws_launch_template.box["default"]\n}', MODULE)
        self.assertIn('value = aws_launch_template.box["default"].arn', MODULE)
        self.assertIn('value       = aws_launch_template.box["ondemand"].arn', MODULE)

    def test_the_fallback_policy_pins_both_templates_and_the_pointer_names_the_sibling(self):
        self.assertIn("pbox_fallback_template_arns = flatten([for s in values(local.pbox_fallback_sites) : "
                      "[s.template_arn, s.ondemand_template_arn]])", FALLBACK)
        doc = block(FALLBACK, 'data "aws_iam_policy_document" "parallel_box_fallback"')
        self.assertEqual(doc.count("values   = local.pbox_fallback_template_arns"), 2)
        self.assertIn("ondemand_template = one(distinct([for s in values(local.pbox_fallback_sites) : "
                      "s.ondemand_template_name]))", FALLBACK)

    def test_the_lambda_tests_workflow_runs_this_file(self):
        self.assertIn("run: python3 -S -B scripts/test-pbox-on-demand.py", WORKFLOW)


ACCOUNT = "123456789012"
NAMES = ["parallel-box"] + ["parallel-box-%d" % n for n in range(2, 9)]


def arn(region, kind, n):
    return "arn:aws:ec2:%s:%s:%s/%s-%017x" % (region, ACCOUNT, kind, {"launch-template": "lt"}.get(kind, kind[:3]), n)


def size(statements):
    return len(json.dumps({"Version": "2012-10-17", "Statement": statements}, separators=(",", ":")))


class PolicySizes(unittest.TestCase):
    """IAM counts a managed policy's characters without whitespace, up to 6,144. Each policy is modelled with ARN-length
    placeholders, statement by statement; the model's statement ids must be the Terraform's, so a statement added there
    without being added here fails instead of going uncounted."""

    LIMIT = 6144
    MARGIN = 300  # for the placeholders being a few characters off the real ids

    def test_parallel_box_control_fits_with_both_templates_of_every_box(self):
        n_boxes = len(re.findall(r"\d", re.search(r"for n in \[([\d, ]+)\]", LAUNCH).group(1)))
        templates = [arn("us-east-2", "launch-template", i) for i in range(2 * n_boxes)]
        created = ["arn:aws:ec2:us-east-2:%s:%s/*" % (ACCOUNT, k) for k in ("instance", "volume", "network-interface")]
        referenced = ["arn:aws:ec2:us-east-2::image/ami-%017x" % 1, "arn:aws:ec2:us-east-2::image/ami-%017x" % 2,
                      arn("us-east-2", "subnet", 1), arn("us-east-2", "security-group", 1), arn("us-east-2", "security-group", 2),
                      "arn:aws:ec2:us-east-2:%s:key-pair/fcvm-ec2-pbox-ohio" % ACCOUNT]
        instance = "arn:aws:ec2:us-east-2:%s:instance/*" % ACCOUNT
        model = [
            {"Sid": "RunTaggedParallelBoxOnly", "Effect": "Allow", "Action": "ec2:RunInstances", "Resource": created,
             "Condition": {"StringEquals": {"aws:RequestTag/Name": NAMES}, "ArnEquals": {"ec2:LaunchTemplate": templates}}},
            {"Sid": "RunInstancesReferencedResources", "Effect": "Allow", "Action": "ec2:RunInstances",
             "Resource": referenced + templates, "Condition": {"ArnEquals": {"ec2:LaunchTemplate": templates}}},
            {"Sid": "TagAtLaunchOnly", "Effect": "Allow", "Action": "ec2:CreateTags", "Resource": created,
             "Condition": {"StringEquals": {"ec2:CreateAction": "RunInstances"}}},
            {"Sid": "ManageTaggedParallelBoxOnly", "Effect": "Allow",
             "Action": ["ec2:TerminateInstances", "ec2:StopInstances", "ec2:StartInstances"], "Resource": instance,
             "Condition": {"StringEquals": {"ec2:ResourceTag/Name": NAMES}}},
            {"Sid": "PassInstanceProfileRole", "Effect": "Allow", "Action": "iam:PassRole",
             "Resource": "arn:aws:iam::%s:role/dev-ebs-only-role" % ACCOUNT,
             "Condition": {"StringEquals": {"iam:PassedToService": "ec2.amazonaws.com"}}},
        ]
        doc = block(LAUNCH, 'data "aws_iam_policy_document" "parallel_box_control"')
        self.assertEqual(sids(doc), [s["Sid"] for s in model])
        self.assertLess(size(model), self.LIMIT - self.MARGIN)

    def test_parallel_box_fallback_fits_with_both_templates_of_every_site(self):
        sites = dict(re.findall(r'"(us-[a-z]+-\d)" = \{ cidr = "[^"]+", azs = (.*?) \}\n', FALLBACK))
        self.assertEqual(sorted(sites), ["us-east-1", "us-east-2", "us-west-1", "us-west-2"])
        templates = [arn(r, "launch-template", i) for r in sites for i in range(2)]
        created = ["arn:aws:ec2:us-*:%s:%s/*" % (ACCOUNT, k) for k in ("instance", "volume", "network-interface")]
        referenced = []
        for r, azs in sites.items():
            n_azs = max(1, len(re.findall(r'"us-[a-z]+-\d[a-z]"', azs)))
            referenced += ["arn:aws:ec2:%s::image/ami-%017x" % (r, 1), "arn:aws:ec2:%s::image/ami-%017x" % (r, 2),
                           arn(r, "security-group", 1), "arn:aws:ec2:%s:%s:key-pair/fcvm-ec2-pbox-fallback" % (r, ACCOUNT)]
            referenced += [arn(r, "subnet", i) for i in range(n_azs)]
        model = [
            {"Sid": "RunTaggedFallbackBoxOnly", "Effect": "Allow", "Action": "ec2:RunInstances", "Resource": created,
             "Condition": {"StringEquals": {"aws:RequestTag/Name": NAMES}, "ArnEquals": {"ec2:LaunchTemplate": templates}}},
            {"Sid": "RunFallbackReferencedResources", "Effect": "Allow", "Action": "ec2:RunInstances",
             "Resource": referenced + templates, "Condition": {"ArnEquals": {"ec2:LaunchTemplate": templates}}},
            {"Sid": "TagFallbackAtLaunchOnly", "Effect": "Allow", "Action": "ec2:CreateTags", "Resource": created,
             "Condition": {"StringEquals": {"ec2:CreateAction": "RunInstances"}}},
            {"Sid": "ManageTaggedFallbackBoxOnly", "Effect": "Allow",
             "Action": ["ec2:TerminateInstances", "ec2:StopInstances", "ec2:StartInstances"],
             "Resource": "arn:aws:ec2:us-*:%s:instance/*" % ACCOUNT, "Condition": {"StringEquals": {"ec2:ResourceTag/Name": NAMES}}},
        ]
        doc = block(FALLBACK, 'data "aws_iam_policy_document" "parallel_box_fallback"')
        self.assertEqual(sids(doc), [s["Sid"] for s in model])
        self.assertLess(size(model), self.LIMIT - self.MARGIN)

    def test_pbox_flavor_types_fits_with_the_statements_moved_into_it(self):
        menu = re.search(r"^FLAVORS='\n(.*?)\n'\n", SCRIPT.read_text(), re.S | re.M).group(1)
        rows = [line.split() for line in menu.splitlines()]
        owner = list(dict.fromkeys(t for r in rows for t in r[5:]))
        member = list(dict.fromkeys(t for r in rows if not r[0].startswith("kvm-") for t in r[5:]))
        instance = "arn:aws:ec2:*:%s:instance/*" % ACCOUNT
        members = ["pbox-colton", "pbox-connor", "pbox-colin", "pbox-skevh"]
        model = [
            {"Sid": "CancelStaleSpotRequests", "Effect": "Allow", "Action": "ec2:CancelSpotInstanceRequests",
             "Resource": "arn:aws:ec2:*:%s:spot-instances-request/*" % ACCOUNT},
            {"Sid": "AssignIpv6ToOpenParallelBoxes", "Effect": "Allow", "Action": "ec2:AssignIpv6Addresses",
             "Resource": "arn:aws:ec2:us-east-2:%s:network-interface/*" % ACCOUNT,
             "Condition": {"StringEquals": {"ec2:ResourceTag/Name": NAMES}}},
            {"Sid": "NeverThePersistentBoxesRequests", "Effect": "Deny", "Action": "ec2:CancelSpotInstanceRequests",
             "Resource": ["arn:aws:ec2:*:%s:spot-instances-request/sir-%08x" % (ACCOUNT, n) for n in range(4)]},
            {"Sid": "OwnerPboxFlavorTypesOnly", "Effect": "Deny", "Action": "ec2:RunInstances", "Resource": instance,
             "Condition": {"StringEquals": {"aws:RequestTag/Name": NAMES}, "StringNotEquals": {"ec2:InstanceType": owner}}},
            {"Sid": "MemberPboxFlavorTypesOnly", "Effect": "Deny", "Action": "ec2:RunInstances", "Resource": instance,
             "Condition": {"StringEquals": {"aws:RequestTag/Name": members}, "StringNotEquals": {"ec2:InstanceType": member}}},
            {"Sid": "AttachWorkVolume", "Effect": "Allow", "Action": ["ec2:AttachVolume", "ec2:DetachVolume"],
             "Resource": [arn("us-east-2", "volume", n) for n in range(len(NAMES))]},
            {"Sid": "AttachWorkVolumeToTaggedInstance", "Effect": "Allow", "Action": ["ec2:AttachVolume", "ec2:DetachVolume"],
             "Resource": "arn:aws:ec2:us-east-2:%s:instance/*" % ACCOUNT,
             "Condition": {"StringEquals": {"ec2:ResourceTag/Name": NAMES}}},
            {"Sid": "DescribeForPbox", "Effect": "Allow", "Resource": "*",
             "Action": re.findall(r'"(ec2:[A-Za-z]+)"', re.search(r'sid    = "DescribeForPbox".*?\]', FLAVORS_TF, re.S).group(0))},
        ]
        doc = block(FLAVORS_TF, 'data "aws_iam_policy_document" "pbox_flavor_types"')
        self.assertEqual(sorted(sids(doc)), sorted(s["Sid"] for s in model))
        self.assertLess(size(model), self.LIMIT - self.MARGIN)


if __name__ == "__main__":
    unittest.main(buffer=True)

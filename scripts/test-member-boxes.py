#!/usr/bin/env python3
"""Every member's own wbox, pbox and gbox (member-boxes.tf, member-wbox.tf, member-pbox.tf, member-gbox.tf) behind the
owner's `wbox`, `pbox` and `gbox` commands: one box of each kind per person, everything durable per person and named
"<kind>-<person>" so a session can be narrowed to one person, a fixed private address each, reaped by the watchdogs and
by the box's own deadline, and the owner's slots untouched.

Offline: reads the Terraform source, runs the real watchdog Lambda body against fake boto3, runs the boxes' own lifetime
check, and runs the real scripts against a fake `aws` (and `ssh`) on PATH. Live proof is the jumpbox's apply (a plan with
no destroy or replace of an existing slot, volume or wbox), `aws iam simulate-principal-policy` against nextjs-dev-role,
and one `up` / `down` of each kind from a member's account."""
import datetime
import ipaddress
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMMON = (ROOT / "member-boxes.tf").read_text()
WTF = (ROOT / "member-wbox.tf").read_text()
PTF = (ROOT / "member-pbox.tf").read_text()
GTF = (ROOT / "member-gbox.tf").read_text()
ALL = COMMON + WTF + PTF + GTF
WATCHDOG = (ROOT / "parallel-box-watchdog.tf").read_text()
OHIO = (ROOT / "ohio-pbox.tf").read_text()
ALARMS = (ROOT / "watchdog-alarms.tf").read_text()
SCRIPTS = ROOT / "scripts"
WMEMBER = (SCRIPTS / "wbox-member.sh").read_text()
WBOX = (SCRIPTS / "wbox.sh").read_text()
GBOX = (SCRIPTS / "gpu-box.sh").read_text()
PBOX = (SCRIPTS / "parallel-box.sh").read_text()
PEOPLE = ["colton", "connor", "colin", "skevh"]


def block(kind, name, text=ALL):
    m = re.search(r'^(?:resource|data) "%s" "%s" \{\n.*?^\}' % (kind, name), text, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


def statement(sid, text=ALL):
    m = re.search(r'statement \{\s*sid\s*=\s*"' + re.escape(sid) + r'".*?\n  \}\n', text, re.S)
    assert m, "statement %s missing" % sid
    return m.group()


def run_statements(doc):
    return re.findall(r'statement \{\s*sid\s*=\s*"(\w+)"\s*\n\s*actions\s*=\s*\["ec2:RunInstances"\]', doc)


def user_data():
    return re.search(r"slot => <<-PS\n(.*?)\nPS\n", WTF, re.S).group(1)


# ============================================================ who has boxes
class People(unittest.TestCase):
    def test_four_members_by_their_account_names_and_every_script_agrees(self):
        self.assertIn('member_box_people = ["colton", "connor", "colin", "skevh"]', COMMON)
        users = re.search(r"nextjs_users = \[(.*?)\]", (ROOT / "nextjs-user-data.tf").read_text()).group(1)
        self.assertEqual(sorted(re.findall(r'"(\w+)"', users)), sorted(PEOPLE + ["ejc3"]))
        for name, text in (("wbox.sh", WBOX), ("wbox-member.sh", WMEMBER), ("gpu-box.sh", GBOX), ("parallel-box.sh", PBOX)):
            with self.subTest(script=name):
                self.assertEqual(re.search(r'^MEMBERS="([^"]+)"', text, re.M).group(1).split(), PEOPLE)
                self.assertIn('MEMBER="${MEMBER_BOX_USER:-$(id -un 2>/dev/null)}"', text)

    def test_each_kind_is_named_kind_dash_person(self):
        for kind, text in (("wbox", WTF), ("pbox", PTF), ("gbox", GTF)):
            with self.subTest(kind=kind):
                self.assertRegex(text, r'member_%s_slots\s+= \{ for p in local\.member_box_people : p => "%s-\$\{p\}" \}' % (kind, kind))
        self.assertIn('NAME="wbox-$MEMBER"', WMEMBER)
        self.assertIn('NAME="gbox-$MEMBER"', GBOX)
        self.assertIn('NAME="pbox-$MEMBER"', PBOX)
        self.assertIn('tags = { Name = "${each.value}-data", Purpose = "windows-playtest" }', block("aws_ebs_volume", "member_wbox_data"))
        self.assertIn('VOLTAG="$NAME-data"', WMEMBER)
        self.assertIn('tags = { Name = "${each.value}-work"', block("aws_ebs_volume", "member_pbox_work"))
        self.assertIn('VOLTAG="$NAME-work"', PBOX)


# ============================================================ the owner's boxes do not move
class OwnerUnchanged(unittest.TestCase):
    def test_the_owners_slots_and_volumes_are_defined_as_before(self):
        launch = (ROOT / "parallel-box-launch.tf").read_text()
        gpu = (ROOT / "gpu-box.tf").read_text()
        self.assertIn('parallel_boxes     = { for n in [1, 2, 3, 4, 5, 6, 7, 8] : tostring(n) => { name = n == 1 ? "parallel-box" : "parallel-box-${n}" } }', launch)
        self.assertIn('{ for n in [1, 2, 3, 4] : tostring(n) => n == 1 ? local.gpu_box_name : "${local.gpu_box_name}-${n}" },', gpu)
        for n, (size, name) in {"1": (300, "parallel-box-work"),
                                **{str(k): (100, "parallel-box-%d-work" % k) for k in range(2, 9)}}.items():
            self.assertIn('"%s" = { size = %d, name = "%s" }' % (n, size, name), OHIO)
        # the member boxes are their own resources, never more keys in the owner's for_each maps
        for owner in ("parallel_boxes", "gpu_box_slots", "ohio_pbox_volumes"):
            self.assertNotRegex(ALL, r"for_each\s*=\s*local\.%s\b" % owner)
        self.assertNotIn("member", launch + gpu + (ROOT / "wbox.tf").read_text() + (ROOT / "pbox-fallback.tf").read_text())

    def test_the_owners_grants_name_no_member_box(self):
        for f, doc in (("parallel-box-launch.tf", "parallel_box_control"), ("gpu-box.tf", "gpu_box_control"),
                       ("wbox.tf", None)):
            text = (ROOT / f).read_text()
            body = block("aws_iam_policy_document", doc, text) if doc else text
            self.assertNotRegex(body, r"member_|(wbox|pbox|gbox)-(colton|connor|colin|skevh)")

    def test_owner_commands_keep_their_slots(self):
        self.assertIn('    1|2|3|4|5|6|7|8) BOX="$1" ;;', PBOX)
        self.assertIn('SLOTS="1 2 3 4 metal native" # the keys of local.gpu_box_slots', GBOX)
        self.assertIn('NAME="wbox"', WBOX)


# ============================================================ fixed addresses
class Addresses(unittest.TestCase):
    def expected(self, reserved):
        net = ipaddress.ip_network(reserved)
        return {p: str(net[2 + i]) for i, p in enumerate(PEOPLE)}

    def test_each_kind_reserves_its_addresses_explicitly(self):
        for kind, text in (("wbox", WTF), ("pbox", PTF), ("gbox", GTF)):
            with self.subTest(kind=kind):
                r = block("aws_ec2_subnet_cidr_reservation", "member_%s" % kind, text)
                self.assertIn('reservation_type = "explicit"', r)
                self.assertIn("cidr_block       = local.member_%s_reserved_cidr" % kind, r)
                self.assertIn("member_%s_ips           = { for p, i in local.member_box_index : p => cidrhost(local.member_%s_reserved_cidr, 2 + i) }"
                              % (kind, kind), text)
                lt = block("aws_launch_template", "member_%s" % kind, text)
                self.assertIn("private_ip_address          = local.member_%s_ips[each.key]" % kind, lt)
                self.assertIn("aws_ec2_subnet_cidr_reservation.member_%s" % kind, lt)
        self.assertIn('member_box_index  = { for i, p in local.member_box_people : p => i }', COMMON)

    def test_the_addresses_themselves(self):
        # wbox: cidrsubnet(10.0.0.0/16, 8, 69) = 10.0.69.0/24, then its .8/29
        self.assertIn("cidr_block              = cidrsubnet(data.aws_vpc.selected.cidr_block, 8, 69)", block("aws_subnet", "member_wbox"))
        self.assertIn("member_wbox_reserved_cidr = cidrsubnet(aws_subnet.member_wbox.cidr_block, 5, 1)", WTF)
        self.assertEqual(self.expected("10.0.69.8/29"), {"colton": "10.0.69.10", "connor": "10.0.69.11", "colin": "10.0.69.12", "skevh": "10.0.69.13"})
        self.assertEqual(str(list(ipaddress.ip_network("10.0.69.0/24").subnets(new_prefix=29))[1]), "10.0.69.8/29")
        # pbox: the members' own Ohio subnet 10.12.2.0/24, its .8/29
        self.assertIn('cidr_block              = "10.12.2.0/24"', block("aws_subnet", "member_pbox"))
        self.assertIn("member_pbox_reserved_cidr = cidrsubnet(aws_subnet.member_pbox.cidr_block, 5, 1)", PTF)
        self.assertEqual(self.expected("10.12.2.8/29")["skevh"], "10.12.2.13")
        # gbox: the us-west-2a default subnet (172.31.32.0/20 on 2026-10-18), the /28 at offset 4064
        self.assertIn("member_gbox_reserved_cidr = cidrsubnet(data.aws_subnet.member_gbox.cidr_block, 8, 254)", GTF)
        res = list(ipaddress.ip_network("172.31.32.0/20").subnets(new_prefix=28))[254]
        self.assertEqual(str(res), "172.31.47.224/28")
        self.assertNotIn(ipaddress.ip_address("172.31.47.255"), res, "never the subnet's last, reserved address")
        self.assertEqual(self.expected(str(res))["colton"], "172.31.47.226")
        self.assertIn("member_gbox_subnet_id = local.gpu_box_subnets[0]", GTF)
        self.assertIn('"subnet-047683926b94c92c7", # us-west-2a', (ROOT / "gpu-box.tf").read_text().split("gpu_box_subnets = [", 1)[1])

    def test_the_gbox_is_reached_privately_from_the_fleet_only(self):
        sg = block("aws_security_group", "member_gbox")
        ingress = sg.split("dynamic \"egress\"", 1)[0]
        self.assertIn("cidr_blocks = [for s in local.dev_fleet_subnets : s.cidr_block]", ingress)
        self.assertNotRegex(ingress, r"0\.0\.0\.0/0|public_ip")
        self.assertNotIn("2049", sg)
        self.assertIn('IPQ="PrivateIpAddress"', GBOX)
        # the fleet routes the whole us-west-2 default VPC over the I/O box peering, and that VPC routes back
        self.assertIn("cidr_block                = data.aws_vpc.west2_default.cidr_block", (ROOT / "main.tf").read_text())
        self.assertIn("destination_cidr_block    = data.aws_vpc.selected.cidr_block", (ROOT / "io-box.tf").read_text())


# ============================================================ per person
class PerPerson(unittest.TestCase):
    def test_every_durable_thing_is_keyed_by_person(self):
        for kind, name in (("aws_launch_template", "member_wbox"), ("aws_ebs_volume", "member_wbox_data"),
                           ("aws_iam_role", "member_wbox"), ("aws_iam_instance_profile", "member_wbox"),
                           ("aws_secretsmanager_secret", "member_wbox_admin"), ("aws_launch_template", "member_pbox"),
                           ("aws_ebs_volume", "member_pbox_work"), ("aws_launch_template", "member_gbox")):
            with self.subTest(resource="%s.%s" % (kind, name)):
                self.assertRegex(block(kind, name), r"for_each\s+= local\.member_[pgw]box_slots\n")
        for kind, name in (("tls_private_key", "member_box"), ("aws_secretsmanager_secret", "member_box_ssh"),
                           ("aws_secretsmanager_secret_policy", "member_box_ssh")):
            self.assertRegex(block(kind, name, COMMON), r"for_each\s+= toset\(local\.member_box_people\)\n")

    def test_one_role_per_windows_box_reading_only_its_own_password(self):
        pol = block("aws_iam_role_policy", "member_wbox")
        self.assertIn("for_each = local.member_wbox_slots", pol)
        self.assertIn("Resource = aws_secretsmanager_secret.member_wbox_admin[each.key].arn", pol)
        self.assertNotIn("member_wbox_secret_arns", pol)
        self.assertIn('name     = "${each.value}-role"', block("aws_iam_role", "member_wbox"))
        self.assertIn("arn = aws_iam_instance_profile.member_wbox[each.key].arn", block("aws_launch_template", "member_wbox"))
        sp = block("aws_secretsmanager_secret_policy", "member_wbox_admin")
        self.assertIn("concat(local.games_mp_admin_principals, [aws_iam_role.member_wbox[each.key].arn, aws_iam_role.nextjs_dev.arn])", sp)
        self.assertNotIn("dev_server", sp)

    def test_each_persons_ssh_key_opens_only_their_linux_boxes(self):
        self.assertIn('name                    = "member-box/${each.key}/ssh-key"', block("aws_secretsmanager_secret", "member_box_ssh", COMMON))
        self.assertIn("concat(local.games_mp_admin_principals, [aws_iam_role.nextjs_dev.arn])",
                      block("aws_secretsmanager_secret_policy", "member_box_ssh", COMMON))
        self.assertIn("tls_private_key.member_box[p].public_key_openssh", PTF)
        self.assertIn("tls_private_key.member_box[each.key].public_key_openssh", GTF)
        for text in (PTF, GTF):
            self.assertNotIn("dev_hop", text)
        for script in (GBOX, PBOX):
            self.assertIn('--secret-id "member-box/$MEMBER/ssh-key"', script)
            self.assertIn('KEY="$HOME/.ssh/member-box"', script)

    def test_the_output_lists_what_a_session_policy_must_name(self):
        out = re.search(r'^output "member_boxes" \{\n.*?^\}', COMMON, re.S | re.M).group()
        for key in ("launch_template", "data_volume", "password_secret", "instance_role", "instance_profile", "work_volume",
                    "ssh_key_secret", "private_ip", "name", "region"):
            self.assertIn(key, out)


# ============================================================ the grants
class Grants(unittest.TestCase):
    KINDS = {"wbox": WTF, "pbox": PTF, "gbox": GTF}

    def doc(self, kind):
        return block("aws_iam_policy_document", "member_%s_control" % kind)

    def test_every_launch_statement_pins_the_members_templates_and_names(self):
        for kind in self.KINDS:
            doc = self.doc(kind)
            sids = run_statements(doc)
            self.assertEqual(len(sids), 4, (kind, sids))
            for sid in sids:
                with self.subTest(kind=kind, sid=sid):
                    s = statement(sid, doc)
                    self.assertRegex(s, r'"ec2:LaunchTemplate"\s*\n\s*values\s*=\s*local\.member_%s_template_arns\n' % kind)
                    if "Referenced" not in sid:
                        self.assertRegex(s, r'"aws:RequestTag/Name"\s*\n\s*values\s*=\s*local\.member_%s_names\n' % kind)

    def test_types_market_profile_and_imds(self):
        p = statement("RunMemberPboxInstance")
        # the pbox's instance types are pbox-flavor-types' (pbox-flavors.tf, tested in test-pbox-flavors.py)
        self.assertNotIn('"ec2:InstanceType"', p)
        self.assertRegex(p, r'"ec2:InstanceMarketType"\s*\n\s*values\s*=\s*\["spot"\]')
        self.assertRegex(p, r'test     = "Null"\n\s+variable = "ec2:InstanceProfile"\n\s+values   = \["true"\]')
        g = statement("RunMemberGboxInstance")
        self.assertIn("values   = local.gpu_box_types", g)
        self.assertRegex(g, r'test     = "Null"\n\s+variable = "ec2:InstanceProfile"')
        w = statement("RunMemberWboxInstanceOnly")
        self.assertIn("values   = local.member_wbox_types", w)
        self.assertRegex(w, r'"ec2:InstanceMarketType"\s*\n\s*values\s*=\s*\["spot"\]')
        self.assertIn("values   = local.member_wbox_profile_arns", w)
        for s in (p, g, w):
            self.assertIn('"ec2:MetadataHttpTokens"', s)
            self.assertIn('values   = ["dedicated", "host"]', s)
        # a member's pbox types are every flavor's but kvm's (no metal: the kvm flavors are the owner's)
        flavors = (ROOT / "pbox-flavors.tf").read_text()
        self.assertIn('member_pbox_types = distinct(flatten([for f, types in local.pbox_flavors : types if !startswith(f, "kvm-")]))',
                      flavors)
        self.assertNotIn("member_pbox_types =", PTF)
        self.assertEqual(re.search(r'^TYPES="([^"]+)"', WMEMBER, re.M).group(1).split(),
                         re.findall(r'"([^"]+)"', re.search(r"member_wbox_types = \[(.*?)\]", WTF).group(1)))

    def test_passrole_only_where_a_profile_is_passed(self):
        self.assertIn("resources = local.member_wbox_role_arns", statement("PassMemberWboxRole"))
        self.assertNotIn("iam:PassRole", self.doc("pbox") + self.doc("gbox"))
        for kind in ("pbox", "gbox"):
            self.assertNotIn("iam_instance_profile", block("aws_launch_template", "member_%s" % kind))

    def test_terminate_attach_secrets_and_describe_are_scoped(self):
        for kind in self.KINDS:
            doc = self.doc(kind)
            t = re.search(r'sid\s*=\s*"TerminateTaggedMember%sOnly".*?\n  \}\n' % kind.capitalize(), doc, re.S).group()
            self.assertRegex(t, r'"ec2:ResourceTag/Name"\s*\n\s*values\s*=\s*local\.member_%s_names\n' % kind)
            d = re.search(r'sid\s*=\s*"DescribeFor\w+".*?\n  \}\n', doc, re.S).group()
            self.assertIn('"aws:RequestedRegion"', d)
            self.assertNotRegex(d, r'"ec2:(Run|Terminate|Create|Attach|Delete|Modify|Detach|Stop|Start)')
            self.assertNotRegex(doc, r'"ec2:(DetachVolume|StopInstances|StartInstances|DeleteVolume|CreateVolume)"')
        self.assertIn("resources = local.member_pbox_work_arns", statement("AttachMemberPboxWorkVolume"))
        self.assertIn("resources = local.member_wbox_data_arns", statement("AttachMemberWboxDataVolume"))
        self.assertIn("resources = local.member_box_ssh_arns", statement("ReadMemberBoxSshKeys", self.doc("pbox")))
        self.assertIn("resources = local.member_wbox_secret_arns", statement("ReadMemberWboxPasswords"))

    def test_attached_to_nextjs_dev_only_and_within_the_role_quota(self):
        for kind in self.KINDS:
            a = block("aws_iam_role_policy_attachment", "member_%s_control" % kind)
            self.assertIn("role       = aws_iam_role.nextjs_dev.name", a)
            self.assertNotIn("for_each", a)
        # IAM allows 20 managed policies per role: count what nextjs-dev-role carries across the repo
        n = 0
        for f in ROOT.glob("*.tf"):
            for m in re.finditer(r'^resource "aws_iam_role_policy_attachment" "\w+" \{\n(.*?)^\}', f.read_text(), re.S | re.M):
                n += "aws_iam_role.nextjs_dev." in m.group(1)
        self.assertLessEqual(n, 20)

    def test_the_grants_hold_nothing_else(self):
        allowed = {"ec2:RunInstances", "ec2:CreateTags", "ec2:TerminateInstances", "ec2:AttachVolume", "iam:PassRole",
                   "secretsmanager:GetSecretValue", "ec2:DescribeInstances", "ec2:DescribeVolumes",
                   "ec2:DescribeSpotPriceHistory", "ec2:DescribeInstanceTypes", "ec2:DescribeLaunchTemplateVersions"}
        keys = {"ec2:LaunchTemplate", "ec2:InstanceType", "ec2:Tenancy", "ec2:MetadataHttpTokens", "ec2:InstanceProfile",
                "ec2:VolumeType", "ec2:VolumeSize", "ec2:Encrypted", "ec2:CreateAction", "iam:PassedToService",
                "ec2:InstanceMarketType", "ec2:IsLaunchTemplateResource"}
        for kind in self.KINDS:
            actions = set(re.findall(r'"((?:ec2|iam|secretsmanager|ssm|s3):\w+)"', self.doc(kind))) - keys
            self.assertLessEqual(actions, allowed, kind)


# ============================================================ cost bounds
class Watchdogs(unittest.TestCase):
    def test_every_member_box_is_watched_and_has_a_lifetime(self):
        self.assertIn("parallel_watchdog_tag_names = concat(local.parallel_box_names, local.gpu_box_names, local.member_gbox_names, local.member_pbox_names)", WATCHDOG)
        fn = block("aws_lambda_function", "parallel_watchdog", WATCHDOG)
        self.assertIn("TAG_NAMES    = join(\",\", local.parallel_watchdog_tag_names)", fn)
        self.assertIn('MAX_AGE_MINUTES = jsonencode({ for n in concat(local.gpu_box_names, local.member_gbox_names) : n => n == local.gpu_box_slots["native"] ? 30 : var.gpu_box_max_hours * 60 })', fn)
        ohio = block("aws_lambda_function", "parallel_watchdog_ohio", OHIO)
        self.assertIn('TAG_NAMES       = join(",", concat(local.parallel_box_names, local.member_pbox_names))', ohio)
        self.assertIn("MAX_AGE_MINUTES = jsonencode({ for n in local.member_pbox_names : n => local.member_box_max_hours * 60 })", ohio)
        role = block("aws_iam_role_policy", "parallel_watchdog", WATCHDOG)
        self.assertIn('StringEquals = { "ec2:ResourceTag/Name" = local.parallel_watchdog_tag_names }', role)
        w = block("aws_lambda_function", "member_wbox_watchdog")
        self.assertIn('TAG_NAMES       = join(",", local.member_wbox_names)', w)
        self.assertIn("MAX_AGE_MINUTES = jsonencode({ for n in local.member_wbox_names : n => local.member_box_max_hours * 60 })", w)
        self.assertIn('Condition = { StringEquals = { "ec2:ResourceTag/Name" = local.member_wbox_names } }', block("aws_iam_role_policy", "member_wbox_watchdog"))
        self.assertRegex(COMMON, r"member_box_max_hours = 6\n")

    def test_alarms_when_the_wbox_watchdog_errors_or_stops_running(self):
        for name, metric, op, missing in (("member_wbox_watchdog_errors", "Errors", "GreaterThanOrEqualToThreshold", "notBreaching"),
                                          ("member_wbox_watchdog_not_running", "Invocations", "LessThanThreshold", "breaching")):
            a = block("aws_cloudwatch_metric_alarm", name)
            self.assertRegex(a, r'metric_name\s+= "%s"\n' % metric)
            self.assertRegex(a, r'comparison_operator\s+= "%s"\n' % op)
            self.assertRegex(a, r'threshold\s+= 1\n')
            self.assertRegex(a, r'period\s+= 900\n')
            self.assertRegex(a, r'treat_missing_data\s+= "%s"\n' % missing)
            self.assertRegex(a, r'alarm_actions\s+= \[aws_sns_topic\.cost_alerts\.arn\]\n')
            self.assertIn("FunctionName = aws_lambda_function.member_wbox_watchdog.function_name", a)

    def run_watchdog(self, instances, cpu, names, max_age, single=()):
        code = textwrap.dedent(re.search(r"parallel_watchdog_code = <<-PY\n(.*?)\n  PY", WATCHDOG, re.S).group(1))
        now = datetime.datetime.now(datetime.timezone.utc)
        calls = {"terminate": [], "body": []}

        class EC2:
            def describe_instances(self, Filters):
                return {"Reservations": [{"Instances": [
                    {"InstanceId": iid, "LaunchTime": now - datetime.timedelta(minutes=age), "Tags": [{"Key": "Name", "Value": name}]}
                    for iid, name, age in instances]}]}

            def terminate_instances(self, InstanceIds):
                calls["terminate"].extend(InstanceIds)

        class CW:
            def get_metric_statistics(self, **kw):
                if kw["MetricName"] != "CPUUtilization":
                    return {"Datapoints": []}  # no disk or network series: no activity
                return {"Datapoints": [{"Maximum": p} for p in cpu[kw["Dimensions"][0]["Value"]]]}

        class SNS:
            def publish(self, **kw):
                calls["body"].append(kw["Message"])

        fake = types.ModuleType("boto3")
        fake.client = lambda name, region_name=None: {"ec2": EC2(), "cloudwatch": CW(), "sns": SNS()}[name]
        saved_mod, saved_env = sys.modules.get("boto3"), dict(os.environ)
        sys.modules["boto3"] = fake
        os.environ.update(TAG_NAMES=",".join(names), IDLE_MINUTES="60", IDLE_CPU_PCT="15",
                          MAX_AGE_MINUTES=json.dumps({n: max_age for n in names}), SNS_TOPIC_ARN="arn:aws:sns:test",
                          SINGLE_NAMES=",".join(single))
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
        return {r["id"]: r["action"] for r in result["results"]}, calls

    IDLE_DESKTOP = [6.2, 6.4, 11.7, 6.3, 6.4, 11.2, 6.4, 6.3, 6.5, 6.4, 6.1, 6.4]  # wbox, 2026-10-03

    def test_an_idle_member_box_ends_and_its_notice_names_the_person_and_command(self):
        for kind, kept in (("wbox", "Its D: disk (Roblox and its caches) is untouched"), ("pbox", "Its work volume is untouched"),
                           ("gbox", "Nothing on it persists")):
            with self.subTest(kind=kind):
                actions, calls = self.run_watchdog([("i-1", "%s-connor" % kind, 90)], {"i-1": self.IDLE_DESKTOP},
                                                   ["%s-connor" % kind], 360)
                self.assertEqual(actions, {"i-1": "terminated"})
                # a pbox needs a flavor, which the watchdog does not know: it says so
                cmd = "pbox up FLAVOR" if kind == "pbox" else kind + " up"
                self.assertIn("%s   (run by connor)\n" % cmd, calls["body"][0])
                self.assertEqual("pbox flavors" in calls["body"][0], kind == "pbox")
                self.assertIn(kept, calls["body"][0])

    def test_a_game_being_played_keeps_it_but_the_lifetime_still_ends_it(self):
        busy = self.IDLE_DESKTOP[:5] + [38.0] + self.IDLE_DESKTOP[6:]
        names = ["wbox-colton", "wbox-colin", "wbox-skevh"]
        actions, calls = self.run_watchdog([("i-1", "wbox-colton", 120), ("i-3", "wbox-colin", 365), ("i-y", "wbox-skevh", 30)],
                                           {"i-1": busy, "i-3": busy, "i-y": self.IDLE_DESKTOP}, names, 360)
        self.assertEqual(actions, {"i-1": "busy", "i-3": "terminated_lifetime", "i-y": "too_young"})
        self.assertEqual(calls["terminate"], ["i-3"])

    def test_the_owners_notices_are_unchanged(self):
        _, calls = self.run_watchdog([("i-1", "gpu-box-3", 90)], {"i-1": [1.0, 1.0]}, ["gpu-box-3"], 9999)
        self.assertIn("  gbox up 3\n", calls["body"][0])
        _, calls = self.run_watchdog([("i-2", "parallel-box", 90)], {"i-2": [1.0, 1.0]}, ["parallel-box"], 9999)
        self.assertIn("  pbox up FLAVOR\n  (it ran as ?; `pbox flavors` lists the flavors)\n", calls["body"][0])


class OneInstancePerMember(unittest.TestCase):
    """IAM cannot cap RunInstances' count, nor stop a user-data override: the watchdogs keep one instance per member."""

    wd = Watchdogs.run_watchdog
    IDLE_DESKTOP = Watchdogs.IDLE_DESKTOP

    def test_every_watchdog_names_the_members_as_single(self):
        self.assertIn('SINGLE_NAMES    = join(",", local.member_wbox_names)', block("aws_lambda_function", "member_wbox_watchdog"))
        self.assertRegex(block("aws_lambda_function", "parallel_watchdog", WATCHDOG), r'SINGLE_NAMES\s+= join\(",", concat\(local\.member_gbox_names, \[local\.gpu_box_slots\["native"\]\]\)\)')
        self.assertIn('SINGLE_NAMES    = join(",", local.member_pbox_names)', block("aws_lambda_function", "parallel_watchdog_ohio", OHIO))

    def test_extra_instances_are_terminated_keeping_the_oldest_with_an_alert(self):
        busy = [40.0, 40.0]
        actions, calls = self.wd(
            [("i-new", "pbox-colin", 50), ("i-old", "pbox-colin", 200), ("i-mid", "pbox-colin", 90), ("i-sk", "pbox-skevh", 120)],
            {"i-old": busy, "i-sk": busy}, ["pbox-colin", "pbox-skevh"], 9999, single=["pbox-colin", "pbox-skevh"])
        self.assertEqual(sorted(calls["terminate"]), ["i-mid", "i-new"])
        self.assertEqual(actions, {"i-new": "terminated_duplicate", "i-mid": "terminated_duplicate", "i-old": "busy", "i-sk": "busy"})
        self.assertEqual(len(calls["body"]), 1)
        self.assertIn("Kept the oldest, i-old, and terminated i-mid, i-new.", calls["body"][0])

    def test_the_kept_one_still_meets_the_lifetime_and_idle_rules(self):
        actions, calls = self.wd([("i-1", "wbox-connor", 400), ("i-2", "wbox-connor", 10)], {}, ["wbox-connor"], 360,
                                  single=["wbox-connor"])
        self.assertEqual(actions, {"i-2": "terminated_duplicate", "i-1": "terminated_lifetime"})

    def test_names_not_listed_are_left_alone(self):
        actions, calls = self.wd([("i-a", "parallel-box", 100), ("i-b", "parallel-box", 80)], {"i-a": [40.0, 40.0], "i-b": [40.0, 40.0]},
                                  ["parallel-box"], 9999, single=["pbox-colin"])
        self.assertEqual(actions, {"i-a": "busy", "i-b": "busy"})
        self.assertEqual(calls["terminate"], [])

    def test_the_eni_cannot_be_overridden(self):
        for kind in ("wbox", "pbox", "gbox"):
            doc = block("aws_iam_policy_document", "member_%s_control" % kind)
            sid = next(s for s in run_statements(doc) if s.endswith("Eni"))
            self.assertRegex(statement(sid, doc), r'test     = "Bool"\n\s+variable = "ec2:IsLaunchTemplateResource"\n\s+values   = \["true"\]')


class MemberPboxNetwork(unittest.TestCase):
    def test_own_subnet_with_no_peer_route_and_no_nfs(self):
        rt = block("aws_route_table", "member_pbox")
        self.assertEqual(rt.count("route {"), 1)
        self.assertIn("gateway_id = aws_internet_gateway.ohio_pbox.id", rt)
        self.assertNotRegex(rt, r"vpc_peering|transit_gateway")
        self.assertNotRegex(PTF + OHIO, r'aws_route"\s+"\w+"\s*\{[^}]*aws_route_table\.member_pbox')
        self.assertIn("route_table_id = aws_route_table.member_pbox.id", block("aws_route_table_association", "member_pbox"))
        self.assertNotIn("member_pbox", re.search(r"io_box_nfs_client_cidrs = concat\((.*?)\n  \)", (ROOT / "io-box.tf").read_text(), re.S).group(1))
        self.assertIn("subnet_id                   = aws_subnet.member_pbox.id", block("aws_launch_template", "member_pbox"))
        self.assertIn("subnet_id        = aws_subnet.member_pbox.id", block("aws_ec2_subnet_cidr_reservation", "member_pbox"))
        self.assertIn(":subnet/${aws_subnet.member_pbox.id}", statement("RunMemberPboxReferencedResources"))
        self.assertNotIn("aws_subnet.ohio_pbox", PTF)
        self.assertIn("availability_zone       = local.ohio_pbox_az", block("aws_subnet", "member_pbox"))


class WatchdogAlarms(unittest.TestCase):
    SITES = (("parallel_watchdog", "aws.west2", "aws_lambda_function.parallel_watchdog"),
             ("parallel_watchdog_ohio", "aws.ohio", "aws_lambda_function.parallel_watchdog_ohio"))

    def test_both_parallel_watchdogs_have_the_two_alarms(self):
        for prefix, provider, fn in self.SITES:
            for suffix, metric, op, missing in (("errors", "Errors", "GreaterThanOrEqualToThreshold", "notBreaching"),
                                                ("not_running", "Invocations", "LessThanThreshold", "breaching")):
                with self.subTest(alarm="%s_%s" % (prefix, suffix)):
                    a = block("aws_cloudwatch_metric_alarm", "%s_%s" % (prefix, suffix), ALARMS)
                    self.assertIn("provider            = %s" % provider, a)
                    self.assertRegex(a, r'metric_name\s+= "%s"\n' % metric)
                    self.assertRegex(a, r'comparison_operator\s+= "%s"\n' % op)
                    self.assertRegex(a, r'threshold\s+= 1\n')
                    self.assertRegex(a, r'period\s+= 900\n')
                    self.assertRegex(a, r'treat_missing_data\s+= "%s"\n' % missing)
                    self.assertIn("dimensions          = { FunctionName = %s.function_name }" % fn, a)

    def test_each_regions_alarms_are_relayed_to_the_alert_topic(self):
        for region, prefix in (("west2", "parallel_watchdog"), ("ohio", "parallel_watchdog_ohio")):
            rule = block("aws_cloudwatch_event_rule", "watchdog_alarm_relay_%s" % region, ALARMS)
            for suffix in ("errors", "not_running"):
                self.assertIn("aws_cloudwatch_metric_alarm.%s_%s.arn" % (prefix, suffix), rule)
                self.assertIn("aws_cloudwatch_metric_alarm.%s_%s.arn" % (prefix, suffix), block("aws_cloudwatch_event_rule", "watchdog_alarms", ALARMS))
            target = block("aws_cloudwatch_event_target", "watchdog_alarm_relay_%s" % region, ALARMS)
            self.assertIn("arn       = local.watchdog_alarm_bus_arn", target)
            self.assertIn("role_arn  = aws_iam_role.watchdog_alarm_relay.arn", target)
        self.assertIn('"arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:event-bus/default"', ALARMS)
        self.assertIn('detail        = { state = { value = ["ALARM"] } }', ALARMS)
        self.assertIn("arn       = aws_sns_topic.cost_alerts.arn", block("aws_cloudwatch_event_target", "watchdog_alarms", ALARMS))
        role = block("aws_iam_role_policy", "watchdog_alarm_relay", ALARMS)
        self.assertIn('Statement = [{ Effect = "Allow", Action = "events:PutEvents", Resource = local.watchdog_alarm_bus_arn }]', role)
        self.assertIn('"aws:SourceAccount"', block("aws_iam_role", "watchdog_alarm_relay", ALARMS))
        topic = (ROOT / "cost-alerts.tf").read_text()
        self.assertIn('Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.watchdog_alarms.arn } }', topic)
        # the alarms carry no action of their own: an alarm may only notify a topic in its region
        for prefix, _, _ in self.SITES:
            for suffix in ("errors", "not_running"):
                self.assertNotIn("alarm_actions", block("aws_cloudwatch_metric_alarm", "%s_%s" % (prefix, suffix), ALARMS))


class LinuxDeadline(unittest.TestCase):
    """The member pbox's and gbox's own lifetime timer (member-boxes.tf), run for real."""

    def snippet(self):
        return textwrap.dedent(re.search(r"member_box_deadline_setup = <<-SH\n(.*?)\n  SH\n", COMMON, re.S).group(1))

    def test_both_linux_templates_set_it_first_with_their_lifetime(self):
        for text, hours in ((PTF, "${local.member_box_max_hours}"), (GTF, "${var.gpu_box_max_hours}")):
            ud = text.split("<<-INIT", 1)[1]
            self.assertLess(ud.index("HOURS=%s" % hours), ud.index("${local.member_box_deadline_setup}"))
            self.assertLess(ud.index("${local.member_box_deadline_setup}"), ud.index("authorized_keys"))
        for kind in ("pbox", "gbox"):
            self.assertIn('instance_initiated_shutdown_behavior = "terminate"', block("aws_launch_template", "member_%s" % kind))
        s = self.snippet()
        self.assertIn("OnBootSec=1min", s)
        self.assertIn("OnUnitActiveSec=15min", s)
        self.assertIn("systemctl enable --now member-box-deadline.timer", s)
        self.assertNotRegex(s, r"shutdown -h \+")

    def check(self, deadline):
        script = re.search(r"<<'DEADLINE'\n(.*?)\nDEADLINE\n", self.snippet(), re.S).group(1)
        with tempfile.TemporaryDirectory() as d:
            dl, log = Path(d) / "deadline", Path(d) / "shutdown.log"
            if deadline is not None:
                dl.write_text(deadline)
            fake = Path(d) / "shutdown"
            fake.write_text("#!/bin/sh\necho \"$@\" > %s\n" % log)
            fake.chmod(0o755)
            body = script.replace("/etc/member-box-deadline", str(dl)).replace("/sbin/shutdown", str(fake))
            subprocess.run(["sh", "-c", body], check=True, timeout=10)
            return log.read_text() if log.exists() else ""

    def test_it_powers_off_only_past_the_deadline_and_on_an_unreadable_one(self):
        import time
        self.assertEqual(self.check(str(int(time.time()) + 3600)), "")
        self.assertIn("-h now", self.check(str(int(time.time()) - 1)))
        self.assertIn("-h now", self.check("garbage"))
        self.assertIn("-h now", self.check(None))

    def test_the_deadline_is_written_once_from_the_boot_time(self):
        s = self.snippet()
        self.assertIn("BOOT=$(( $(date +%s) - $(cut -d. -f1 /proc/uptime) ))", s)
        self.assertIn("[ -s /etc/member-box-deadline ] || echo $(( BOOT + HOURS * 3600 )) > /etc/member-box-deadline", s)


class LinuxBoxes(unittest.TestCase):
    def test_the_member_pbox_has_no_nfs_no_profile_and_formats_only_blank_disks(self):
        lt = block("aws_launch_template", "member_pbox")
        self.assertNotIn("io_box_client_setup", PTF)
        self.assertNotIn("DevEBS", lt)
        self.assertIn('market_type = "spot"', lt)
        self.assertIn('http_tokens   = "required"', lt)
        self.assertIn("encrypted             = true", lt)
        ud = PTF.split("<<-INIT", 1)[1]
        self.assertEqual(ud.count("mkfs"), 1)
        self.assertLess(ud.index('if ! blkid "$DEV"'), ud.index("mkfs"))
        self.assertIn("prevent_destroy = true", block("aws_ebs_volume", "member_pbox_work"))
        self.assertNotIn("DevEBS", block("aws_ebs_volume", "member_pbox_work"))
        self.assertIn("provider          = aws.ohio", block("aws_ebs_volume", "member_pbox_work"))
        self.assertIn("availability_zone = local.ohio_pbox_az", block("aws_ebs_volume", "member_pbox_work"))

    def test_installed_where_the_members_work(self):
        sel = (ROOT / "dev-selfupdate.tf").read_text()
        self.assertIn('${base64encode(file("${path.module}/scripts/wbox-member.sh"))}', sel)
        self.assertIn("/usr/local/libexec/wbox-member", sel)
        self.assertEqual((ROOT / "nextjs-user-data.tf").read_text().count("${local.wbox_member_setup}"), 1)
        self.assertNotIn("playbox", (ROOT / "dev-user-data.tf").read_text() + sel)


# ============================================================ the Windows box's boot (fixes from review)
class WindowsBoot(unittest.TestCase):
    def test_found_by_its_volume_id_and_formatted_only_when_blank(self):
        ud = user_data()
        self.assertIn('$serial = \'${replace(aws_ebs_volume.member_wbox_data[slot].id, "-", "")}\'', ud)
        self.assertEqual(ud.count("Format-Volume"), 1)
        self.assertLess(ud.index("if ($disk.PartitionStyle -eq 'RAW') {"), ud.index("Format-Volume"))

    def test_roblox_and_the_installers_live_on_d(self):
        ud = user_data()
        self.assertIn("(Join-Path $env:LOCALAPPDATA 'Roblox') = 'D:\\Roblox\\LocalAppData'", ud)
        self.assertIn("$cache = 'D:\\wbox\\cache'", ud)
        self.assertIn("Rename-Item -LiteralPath $link", ud)
        self.assertNotRegex(ud, r"Remove-Item[^\n]*\$link")

    def test_no_template_sequences_but_the_four_intended(self):
        found = re.findall(r"\$\{[^}]*\}|%\{", user_data())
        self.assertEqual(found, ["${local.member_box_max_hours}", "${aws_secretsmanager_secret.member_wbox_admin[slot].name}",
                                 "${local.member_wbox_region}", '${replace(aws_ebs_volume.member_wbox_data[slot].id, "-", "")}'])

    def test_a_failed_nvidia_install_fails_the_setup(self):
        ud = user_data()
        start = ud.index("$p = Start-Process $exe -ArgumentList '-s', '-noreboot', '-clean' -Wait -PassThru")
        check = ud.index("if ($p.ExitCode -notin 0, 1) { throw \"NVIDIA driver install failed: exit code $($p.ExitCode)\" }")
        self.assertLess(start, check)
        self.assertLess(check, ud.index("GridLicensing"))
        self.assertLess(check, ud.index("New-Item 'C:\\wbox-setup.done'"))

    def test_a_failed_setup_is_retried_once_then_the_box_shuts_down(self):
        ud = user_data()
        tail = ud[ud.index("if (Test-Path 'C:\\wbox-setup.done') { Stop-Transcript; exit 0 }"):]
        self.assertIn("for ($attempt = 1; $attempt -le 2 -and -not $done; $attempt++) {", tail)
        ok, failed = tail.split("} else {", 1)
        self.assertIn("New-Item 'C:\\wbox-setup.done' -ItemType File", ok)
        self.assertIn("Restart-Computer -Force", ok)
        self.assertNotIn("wbox-setup.done", failed)
        self.assertIn("shutdown.exe /s /t 60", failed)
        self.assertEqual(ud.count("Restart-Computer"), 1)
        body = ud[ud.index("function Invoke-WboxSetup {"):ud.index("try {\n  Set-WboxDeadline")]
        self.assertNotIn("wbox-setup.done", body)
        self.assertIn('instance_initiated_shutdown_behavior = "terminate"', block("aws_launch_template", "member_wbox"))

    def test_the_box_ends_itself_at_the_watchdogs_lifetime(self):
        ud = user_data()
        fn = ud[ud.index("function Set-WboxDeadline {"):ud.index("function Invoke-WboxSetup {")]
        self.assertIn("$boot.AddHours(${local.member_box_max_hours}).ToString('o')", fn)
        self.assertIn("(New-ScheduledTaskTrigger -AtStartup)", fn)
        self.assertIn("-RepetitionInterval (New-TimeSpan -Minutes 15)", fn)
        self.assertIn("if ([DateTime]::UtcNow -ge $deadline.ToUniversalTime()) { shutdown.exe /s /t 0", fn)
        self.assertIn("} catch { $deadline = [DateTime]::MinValue }", fn)
        self.assertLess(ud.index("try {\n  Set-WboxDeadline\n} catch {"), ud.index("if (Test-Path 'C:\\wbox-setup.done')"))

    def test_user_data_fits_ec2s_limit(self):
        self.assertLess(len(("<powershell>\n" + user_data()).encode()), 15000)


# ============================================================ the scripts, against a fake aws
FAKE_AWS = r'''#!PYTHON -I
# A stand-in for the aws CLI. $FAKE_STATE/<Name> holds a running box's instance id (one per line);
# $FAKE_STATE/<vol>.attached the instance a volume is attached to (detached once that box is gone). Every call is
# logged to $FAKE_STATE/calls. FAKE_NO_CAPACITY lists types with no capacity; FAKE_ATTACH_FAILS makes attach-volume
# fail; FAKE_ATTACH_RACE makes it fail because another `up` attached it to the same box first; FAKE_IP_IN_USE=N makes
# the first N run-instances fail with InvalidIPAddress.InUse; FAKE_ATTACH_AT=N attaches a volume to its box just before
# answering the Nth describe-volumes (another `up` on the same box got there in between).
import os, sys
state = os.environ["FAKE_STATE"]
args = sys.argv[1:]
with open(os.path.join(state, "calls"), "a") as f:
    f.write(" ".join(args) + "\n")
op = args[1] if len(args) > 1 else ""

def opt(name):
    return args[args.index(name) + 1] if name in args else ""

def tag_name():
    for a in args:
        if a.startswith("Name=tag:Name,Values="):
            return a.split("=", 2)[2]
    return ""

def box(name):
    p = os.path.join(state, name)
    return open(p).read().strip() if os.path.exists(p) else ""

def boxes():
    return [n for n in os.listdir(state) if n.startswith(("wbox-", "pbox-", "gbox-", "parallel-box", "gpu-box")) and "." not in n]

def vol_of(name):
    return "vol-" + name

if op == "describe-instances":
    name = tag_name()
    iid = box(name) if name else ""
    if not name and opt("--instance-ids"):  # by instance id (pbox waits on the instance it launched, #621)
        want = opt("--instance-ids")
        iid = next((box(n) for n in boxes() if box(n) == want), "")
    q = opt("--query")
    if not iid:
        print("None\tNone\tNone\tNone\tNone" if "[InstanceId,InstanceType" in q else "None")
    elif "[InstanceId,InstanceType" in q:
        print("%s\tg4dn.xlarge\tspot\t10.0.0.7\t2026-10-18T10:00:00+00:00" % iid)
    elif q.endswith(".PrivateIpAddress"):
        print("10.0.0.7")
    elif q.endswith(".PublicIpAddress"):
        print("203.0.113.7")
    elif q.endswith(".InstanceType"):
        print("g4dn.xlarge")
    elif q.endswith(".InstanceLifecycle"):
        print("spot")
    elif "AvailabilityZone" in q:
        print("us-east-2c")
    elif "Fallback" in q or "Ipv6Prefixes" in q:
        print("None")  # no box here was launched at a fallback site or with --open
    else:
        print("\n".join(iid.split()))
elif op == "describe-volumes":
    name = tag_name()
    if not name and opt("--volume-ids").startswith("vol-") and "[State," in opt("--query"):
        # by id: State and attachment. FAKE_VOL_BUSY=N: the first N answers say it is still detaching from a gone box.
        busy = int(box("vol-busy-count") or 0)
        if busy < int(os.environ.get("FAKE_VOL_BUSY", "0")):
            open(os.path.join(state, "vol-busy-count"), "w").write(str(busy + 1))
            print("in-use\ti-old")
        else:
            print("available\tNone")
        sys.exit(0)
    if not name:
        print("us-east-2c" if "AvailabilityZone" in opt("--query") else "None")
        sys.exit(0)
    vol = vol_of(name)
    seen = int(box("describe-volumes-count") or 0) + 1
    open(os.path.join(state, "describe-volumes-count"), "w").write(str(seen))
    if str(seen) == os.environ.get("FAKE_ATTACH_AT"):
        open(os.path.join(state, vol + ".attached"), "w").write(box(name.rsplit("-", 1)[0]).split()[0])
    att = box(vol + ".attached")
    live = att and any(att in box(n).split() for n in boxes())
    q = opt("--query")
    if "Attachments" in q:
        print("%s\t%s\tus-west-1a\t%s" % (vol, "in-use" if live else "available", att if live else "None"))
    elif "Size" in q:
        print("%s\t100\tavailable" % vol)
    else:
        print(vol)
elif op == "describe-spot-price-history":
    print("0.2554")
elif op == "describe-instance-types":
    print("192")
elif op == "describe-launch-template-versions":
    print("spot")
elif op == "run-instances":
    n = int(os.environ.get("FAKE_IP_IN_USE", "0"))
    used = int(box("ip-in-use-count") or 0)
    if used < n:
        open(os.path.join(state, "ip-in-use-count"), "w").write(str(used + 1))
        sys.stderr.write("An error occurred (InvalidIPAddress.InUse) when calling the RunInstances operation: Address 10.0.0.7 is in use.\n")
        sys.exit(254)
    t = opt("--instance-type")
    if t in os.environ.get("FAKE_NO_CAPACITY", "").split():
        sys.stderr.write("An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation\n")
        sys.exit(254)
    name = opt("--launch-template").split(",")[0].split("=", 1)[1]
    open(os.path.join(state, name), "w").write("i-new-%s\n" % name)
    print("i-new-%s" % name)
elif op == "attach-volume":
    if os.environ.get("FAKE_ATTACH_FAILS") or os.environ.get("FAKE_ATTACH_RACE"):
        if os.environ.get("FAKE_ATTACH_RACE"):
            open(os.path.join(state, opt("--volume-id") + ".attached"), "w").write(opt("--instance-id"))
        sys.stderr.write("An error occurred (VolumeInUse)\n")
        sys.exit(254)
    open(os.path.join(state, opt("--volume-id") + ".attached"), "w").write(opt("--instance-id"))
elif op == "terminate-instances":
    gone = [g for g in args[args.index("--instance-ids") + 1:] if g.startswith("i-")]
    for name in boxes():
        if box(name) in gone:
            os.remove(os.path.join(state, name))
elif args[0] == "secretsmanager":
    sid = opt("--secret-id")
    if sid.endswith("/ssh-key"):
        print("-----BEGIN OPENSSH PRIVATE KEY-----\nfake-key-for-%s\n-----END OPENSSH PRIVATE KEY-----" % sid)
    else:
        print("fake-password-for-%s" % sid)
elif args[0] == "ssm":
    print('{"region": "us-east-2", "fallback": {}}')
'''


class ScriptRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home, self.state, bin_dir = root / "home", root / "state", root / "bin"
        for d in (self.home, self.state, bin_dir):
            d.mkdir()
        (bin_dir / "aws").write_text(FAKE_AWS.replace("PYTHON", sys.executable, 1))
        # DCV "answers" at once unless FAKE_DCV_DOWN: the TCP probe runs under timeout, so a fake timeout stands in.
        (bin_dir / "timeout").write_text('#!/bin/sh\n[ -z "$FAKE_DCV_DOWN" ]\n')
        (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "ssh").write_text('#!/bin/sh\necho "ssh $*" >> "$FAKE_STATE/ssh"\nexit 0\n')
        for f in ("aws", "timeout", "sleep", "ssh"):
            (bin_dir / f).chmod(0o755)
        self.env = {"PATH": "%s:/usr/bin:/bin" % bin_dir, "HOME": str(self.home), "FAKE_STATE": str(self.state), "TZ": "UTC"}

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, script, *args, user="colton", **env):
        (self.state / "calls").write_text("")
        (self.state / "describe-volumes-count").write_text("0")
        r = subprocess.run(["bash", str(SCRIPTS / script), *args], env=dict(self.env, MEMBER_BOX_USER=user, **env),
                           capture_output=True, text=True, timeout=60)
        return r, (self.state / "calls").read_text().splitlines()

    # ---------------------------------------------------------------- wbox
    def test_wbox_hands_a_member_to_their_own_box_and_the_owner_to_his(self):
        r, calls = self.run_script("wbox.sh", "up", user="connor")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "launched box=wbox-connor type=g4dn.xlarge ip=10.0.0.7")
        runs = [c for c in calls if " run-instances " in " %s " % c]
        self.assertEqual(len(runs), 1)
        self.assertIn("LaunchTemplateName=wbox-connor,Version=$Latest", runs[0])
        self.assertEqual([c for c in calls if "attach-volume" in c],
                         ["ec2 attach-volume --region us-west-1 --volume-id vol-wbox-connor-data --instance-id i-new-wbox-connor --device xvdf"])
        # the owner's wbox is the stop/start one, found by its own name
        r, calls = self.run_script("wbox.sh", "status", user="ejc3")
        self.assertIn("Name=tag:Name,Values=wbox", calls[0])
        self.assertIn("no Windows box found", r.stderr)
        self.assertFalse([c for c in calls if "wbox-" in c])

    def test_a_member_takes_no_slot_and_cannot_use_the_owners_ssm_commands(self):
        for args in (("up", "2"), ("down", "colin"), ("run", "Get-Date"), ("launch", "x.exe"), ("connect", "--show-password")):
            with self.subTest(args=args):
                r, calls = self.run_script("wbox-member.sh", *args)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(calls, [])
        r, calls = self.run_script("wbox-member.sh", "status", user="ejc3")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(calls, [])

    def test_wbox_password_reads_only_the_persons_secret(self):
        r, calls = self.run_script("wbox-member.sh", "password", user="skevh")
        self.assertEqual(r.stdout.strip(), "fake-password-for-wbox-skevh/administrator-password")
        r, calls = self.run_script("wbox-member.sh", "connect", user="skevh")
        self.assertNotIn("fake-password", r.stdout + r.stderr)

    def test_wbox_up_resumes_an_existing_box_instead_of_trusting_it(self):
        (self.state / "wbox-colton").write_text("i-111\n")
        r, calls = self.run_script("wbox-member.sh", "up")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "running box=wbox-colton ip=10.0.0.7")
        self.assertFalse([c for c in calls if "run-instances" in c])
        self.assertEqual([c for c in calls if "attach-volume" in c],
                         ["ec2 attach-volume --region us-west-1 --volume-id vol-wbox-colton-data --instance-id i-111 --device xvdf"])
        # attached already: nothing to attach, and still waits for DCV (exit 4 when it never answers)
        r, calls = self.run_script("wbox-member.sh", "up", FAKE_DCV_DOWN="1")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertFalse([c for c in calls if "attach-volume" in c or "terminate" in c])

    def test_wbox_up_races_and_failures(self):
        (self.state / "wbox-colin").write_text("i-333\n")
        r, calls = self.run_script("wbox-member.sh", "up", user="colin", FAKE_ATTACH_RACE="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in calls if "terminate-instances" in c])
        (self.state / "wbox-colin.attached").unlink(missing_ok=True)
        (self.state / "vol-wbox-colin-data.attached").unlink()
        r, calls = self.run_script("wbox-member.sh", "up", user="colin", FAKE_ATTACH_FAILS="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ec2 terminate-instances --region us-west-1 --instance-ids i-333", calls)
        (self.state / "wbox-colin").write_text("i-a\ni-b\n")
        r, calls = self.run_script("wbox-member.sh", "up", user="colin")
        self.assertEqual(r.returncode, 1)
        self.assertIn("more than one wbox-colin exists", r.stderr)

    def test_wbox_attach_found_done_by_another_up_meanwhile_is_success(self):
        # the first read says detached; by the second, a concurrent `up` attached it to this same box
        (self.state / "wbox-connor").write_text("i-222\n")
        r, calls = self.run_script("wbox-member.sh", "up", user="connor", FAKE_ATTACH_AT="2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "running box=wbox-connor ip=10.0.0.7")
        self.assertFalse([c for c in calls if "attach-volume" in c or "terminate" in c])
        self.assertNotIn("waiting for it to be free", r.stderr)

    def test_wbox_waits_for_its_fixed_address_then_falls_back_to_the_larger_type(self):
        r, calls = self.run_script("wbox-member.sh", "up", FAKE_IP_IN_USE="2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len([c for c in calls if "run-instances" in c]), 3)
        self.assertIn("still holds its fixed address", r.stderr)
        (self.state / "wbox-colton").unlink()
        r, calls = self.run_script("wbox-member.sh", "up", FAKE_NO_CAPACITY="g4dn.xlarge g4dn.2xlarge")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertFalse([c for c in calls if "attach-volume" in c])

    def test_wbox_status_and_down_touch_only_the_persons_box(self):
        (self.state / "wbox-colton").write_text("i-111\n")
        (self.state / "wbox-connor").write_text("i-222\n")
        r, _ = self.run_script("wbox-member.sh", "status")
        self.assertEqual(r.stdout.splitlines()[0], "wbox-colton: RUNNING at 10.0.0.7 (g4dn.xlarge, spot), DCV answering; "
                                                    "the watchdog ends it after an idle hour or at 16:00 UTC")
        self.assertNotIn("ON-DEMAND", WMEMBER)
        r, calls = self.run_script("wbox-member.sh", "down", user="connor")
        self.assertEqual([c for c in calls if "terminate" in c], ["ec2 terminate-instances --region us-west-1 --instance-ids i-222"])
        self.assertTrue((self.state / "wbox-colton").exists())

    # ---------------------------------------------------------------- gbox
    def test_gbox_member_launches_their_template_without_a_subnet_and_uses_their_key(self):
        r, calls = self.run_script("gpu-box.sh", "up", user="colin")
        self.assertEqual(r.returncode, 0, r.stderr)
        runs = [c for c in calls if "run-instances" in c]
        self.assertEqual(len(runs), 1)
        self.assertIn("LaunchTemplateName=gbox-colin,Version=$Latest", runs[0])
        self.assertNotIn("--subnet-id", runs[0])
        self.assertFalse([c for c in calls if "describe-subnets" in c or "describe-instance-type-offerings" in c])
        key = self.home / ".ssh" / "member-box"
        self.assertIn("fake-key-for-member-box/colin/ssh-key", key.read_text())
        self.assertEqual(stat.S_IMODE(key.stat().st_mode), 0o600)
        ssh = (self.state / "ssh").read_text()
        self.assertIn("-i %s" % key, ssh)
        self.assertIn("ubuntu@10.0.0.7", ssh)  # the private address
        self.assertNotIn("dev_hop", ssh)

    def test_gbox_member_takes_no_slot_and_status_all_is_just_theirs(self):
        r, calls = self.run_script("gpu-box.sh", "up", "2", user="colin")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(calls, [])
        r, calls = self.run_script("gpu-box.sh", "status", "all", user="colin")
        self.assertEqual(r.stdout.strip(), "gbox-colin: down ($0)")
        (self.state / "gbox-colin").write_text("i-9\n")
        r, calls = self.run_script("gpu-box.sh", "down", user="colin")
        self.assertEqual([c for c in calls if "terminate" in c], ["ec2 terminate-instances --region us-west-2 --instance-ids i-9"])

    def test_gbox_owner_is_unchanged(self):
        r, calls = self.run_script("gpu-box.sh", "status", "all", user="ejc3")
        self.assertEqual(r.stdout.splitlines(), ["gpu-box: down ($0)", "gpu-box-2: down ($0)", "gpu-box-3: down ($0)", "gpu-box-4: down ($0)", "gpu-box-metal: down ($0)", "gpu-box-native: down ($0)"])

    # ---------------------------------------------------------------- pbox
    def test_pbox_member_launches_their_box_with_their_volume_and_key(self):
        r, calls = self.run_script("parallel-box.sh", "up", "compute-192", user="skevh", FAKE_IP_IN_USE="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in calls if c.startswith("ssm ")], "a member reads no pointer")
        runs = [c for c in calls if "run-instances" in c]
        self.assertEqual(len(runs), 2)  # one wait for the fixed address, then the launch
        self.assertIn("--region us-east-2", runs[-1])
        self.assertIn("LaunchTemplateName=pbox-skevh,Version=$Latest", runs[-1])
        self.assertIn("--instance-type c8g.48xlarge", runs[-1])
        self.assertIn("ec2 attach-volume --region us-east-2 --volume-id vol-pbox-skevh-work --instance-id i-new-pbox-skevh --device /dev/sdf", calls)
        self.assertEqual(r.stdout.strip().splitlines()[-1],
                         "launched box=pbox-skevh region=us-east-2 az=us-east-2c type=c8g.48xlarge ip=203.0.113.7 id=i-new-pbox-skevh work=persistent")
        self.assertIn("-i %s" % (self.home / ".ssh" / "member-box"), (self.state / "ssh").read_text())

    def test_pbox_member_up_resumes_an_existing_box_instead_of_trusting_it(self):
        # an interrupted launch left a running box without its work volume
        (self.state / "pbox-skevh").write_text("i-7\n")
        r, calls = self.run_script("parallel-box.sh", "up", "compute-192", user="skevh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in calls if "run-instances" in c])
        self.assertEqual([c for c in calls if "attach-volume" in c],
                         ["ec2 attach-volume --region us-east-2 --volume-id vol-pbox-skevh-work --instance-id i-7 --device /dev/sdf"])
        self.assertEqual(r.stdout.strip().splitlines()[-1],
                         "running box=pbox-skevh region=us-east-2 az=us-east-2c ip=203.0.113.7 id=i-7 work=persistent")
        self.assertTrue((self.state / "ssh").exists(), "it waits for SSH as a fresh launch does")
        # now attached: nothing to attach, still a running line
        r, calls = self.run_script("parallel-box.sh", "up", "compute-192", user="skevh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in calls if "attach-volume" in c or "terminate" in c])
        self.assertIn("running box=pbox-skevh", r.stdout)

    def test_pbox_waits_for_a_detaching_volume_before_launching(self):
        """#512: right after `pbox down` the work volume is still detaching; `up` waits before it launches anything."""
        r, calls = self.run_script("parallel-box.sh", "up", "2", "compute-192", user="ejc3", FAKE_VOL_BUSY="3")
        self.assertEqual(r.returncode, 0, r.stderr)
        waits = [i for i, c in enumerate(calls) if c.startswith("ec2 describe-volumes") and "--volume-ids" in c and "[State," in c]
        runs = [i for i, c in enumerate(calls) if "run-instances" in c]
        self.assertEqual(len(waits), 4, "three busy answers, then available")
        self.assertLess(waits[-1], runs[0], "the launch comes only after the volume is free")
        self.assertIn("waiting for it to be free before launching", r.stderr + r.stdout)
        self.assertIn("ec2 attach-volume --region us-east-2 --volume-id vol-parallel-box-2-work --instance-id i-new-parallel-box-2 --device /dev/sdf", calls)

    def test_pbox_owner_up_attaches_the_volume_of_an_existing_box(self):
        # another `up` launched the box but had not attached its volume yet
        (self.state / "parallel-box-2").write_text("i-9\n")
        r, calls = self.run_script("parallel-box.sh", "up", "2", "compute-192", user="ejc3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in calls if "run-instances" in c])
        self.assertEqual([c for c in calls if "attach-volume" in c],
                         ["ec2 attach-volume --region us-east-2 --volume-id vol-parallel-box-2-work --instance-id i-9 --device /dev/sdf"])

    def test_pbox_refuses_to_launch_while_the_volume_stays_attached(self):
        r, calls = self.run_script("parallel-box.sh", "up", "2", "compute-192", user="ejc3", FAKE_VOL_BUSY="1000")
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse([c for c in calls if "run-instances" in c], "nothing is launched, so nothing is paid for")
        self.assertIn("nothing was launched", r.stderr + r.stdout)

    def test_pbox_owner_box_that_cannot_get_its_volume_is_terminated(self):
        r, calls = self.run_script("parallel-box.sh", "up", "2", "compute-192", user="ejc3", FAKE_ATTACH_FAILS="1")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue([c for c in calls if "terminate-instances" in c and "i-new-parallel-box-2" in c],
                        "a 192-core box without /mnt/work is not left running")

    def test_pbox_member_attach_failures_and_races(self):
        (self.state / "pbox-colin").write_text("i-8\n")
        r, calls = self.run_script("parallel-box.sh", "up", "compute-192", user="colin", FAKE_ATTACH_RACE="1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse([c for c in calls if "terminate" in c])
        (self.state / "vol-pbox-colin-work.attached").unlink()
        r, calls = self.run_script("parallel-box.sh", "up", "compute-192", user="colin", FAKE_ATTACH_FAILS="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ec2 terminate-instances --region us-east-2 --instance-ids i-8", calls)
        # a fresh launch whose attach fails is terminated too
        r, calls = self.run_script("parallel-box.sh", "up", "compute-192", user="colin", FAKE_ATTACH_FAILS="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ec2 terminate-instances --region us-east-2 --instance-ids i-new-pbox-colin", calls)
        # held by another instance: not taken, the box is ended rather than left waiting
        (self.state / "pbox-colin").write_text("i-9\n")
        (self.state / "pbox-colin-old").write_text("i-old\n")
        (self.state / "vol-pbox-colin-work.attached").write_text("i-old")
        r, calls = self.run_script("parallel-box.sh", "up", "compute-192", user="colin")
        self.assertEqual(r.returncode, 1)
        self.assertIn("still in-use (attached to i-old)", r.stderr)
        self.assertFalse([c for c in calls if "attach-volume" in c])

    def test_pbox_owner_running_box_is_reported_as_before(self):
        (self.state / "parallel-box").write_text("i-1\n")
        (self.state / "vol-parallel-box-work.attached").write_text("i-1")  # its volume is on it: nothing to attach
        r, calls = self.run_script("parallel-box.sh", "up", "compute-192", user="ejc3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "running box=1 region=us-east-2 az=us-east-2c ip=203.0.113.7 id=i-1 work=persistent")
        self.assertFalse([c for c in calls if "attach-volume" in c])

    def test_pbox_member_refuses_slots_kvm_and_fallback(self):
        for args, rc in ((("up", "2", "compute-48"), 1), (("up", "kvm"), 2), (("up", "kvm-192"), 2), (("up", "kvm-96"), 2),
                         (("up", "compute-48", "--fallback"), 1), (("up", "compute-48", "--regions", "us-east-1"), 1)):
            with self.subTest(args=args):
                r, calls = self.run_script("parallel-box.sh", *args, user="connor")
                self.assertEqual(r.returncode, rc, r.stderr)
                self.assertFalse([c for c in calls if "run-instances" in c])
        r, _ = self.run_script("parallel-box.sh", "up", "kvm-192", user="connor")
        self.assertIn("the kvm flavors are the owner's", r.stderr)

    def test_pbox_member_must_pick_a_flavor_and_sees_no_kvm(self):
        r, calls = self.run_script("parallel-box.sh", "up", user="connor")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(calls, [], "no flavor: not one AWS call, so nothing is launched")
        self.assertIn("needs a FLAVOR", r.stderr)
        self.assertIn("compute-32", r.stderr)
        self.assertNotIn("kvm-", r.stderr)
        self.assertIn("usage: parallel-box.sh up FLAVOR", r.stderr)
        r, calls = self.run_script("parallel-box.sh", "up", "general-96", user="connor", FAKE_NO_CAPACITY=" ".join(
            ["m8g.24xlarge", "m9g.24xlarge", "m8gd.24xlarge", "m9gd.24xlarge"]))
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual([c.split("--instance-type ")[1].split()[0] for c in calls if "run-instances" in c],
                         ["m8g.24xlarge", "m9g.24xlarge", "m8gd.24xlarge", "m9gd.24xlarge"])

    def test_pbox_member_no_capacity_offers_no_fallback(self):
        types = "c8g.48xlarge c9g.48xlarge c8gn.48xlarge c8gb.48xlarge c8gd.48xlarge c9gd.48xlarge"
        r, calls = self.run_script("parallel-box.sh", "up", "compute-192", user="connor", FAKE_NO_CAPACITY=types)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertNotIn("--fallback", r.stderr)

    def test_pbox_status_and_down_for_a_member_and_the_owner(self):
        (self.state / "pbox-colin").write_text("i-7\n")
        r, calls = self.run_script("parallel-box.sh", "status", user="colin")
        self.assertEqual(r.stdout.splitlines()[0], "pbox-colin:")
        self.assertEqual(sum(1 for l in r.stdout.splitlines() if l.endswith(":")), 1)
        r, calls = self.run_script("parallel-box.sh", "down", user="colin")
        self.assertEqual([c for c in calls if "terminate" in c], ["ec2 terminate-instances --region us-east-2 --instance-ids i-7"])
        r, calls = self.run_script("parallel-box.sh", "status", user="ejc3")
        self.assertEqual([l for l in r.stdout.splitlines() if l.endswith(":")],
                         ["parallel-box %d:" % n for n in range(1, 9)])
        self.assertTrue([c for c in calls if c.startswith("ssm ")], "the owner still reads the pointer")


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

#!/usr/bin/env python3
"""The Windows playtest spot boxes (playbox.tf + scripts/playbox.sh): three numbered slots launched by a dev box from
their own templates with a tag-scoped grant, reachable only from the dev fleet, each with a D: disk that outlives every
instance and is formatted only when blank, and ended by their own watchdog after an idle hour or at a hard lifetime.

Offline: reads the Terraform source, runs the real watchdog Lambda body against fake boto3, and runs the real script
against a fake `aws` on PATH. Live proof is the jumpbox's apply, `aws iam simulate-principal-policy` against
nextjs-dev-role, and one `playbox up N` / `playbox down N`."""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "playbox.tf").read_text()
SCRIPT = (ROOT / "scripts" / "playbox.sh").read_text()
WATCHDOG = (ROOT / "parallel-box-watchdog.tf").read_text()
SLOTS = ["playbox-1", "playbox-2", "playbox-3"]


def block(kind, name, text=TF):
    m = re.search(r'^(?:resource|data) "%s" "%s" \{\n.*?^\}' % (kind, name), text, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


def statement(sid):
    m = re.search(r'statement \{\s*sid\s*=\s*"' + re.escape(sid) + r'".*?\n  \}\n', TF, re.S)
    assert m, "statement %s missing" % sid
    return m.group()


def hcl_list(name):
    m = re.search(r"\b" + re.escape(name) + r"\s*=\s*\[(.*?)\]", TF, re.S)
    assert m, name
    return re.findall(r'"([^"]+)"', m.group(1))


def user_data():
    return re.search(r"slot => <<-PS\n(.*?)\nPS\n", TF, re.S).group(1)


class SlotsAndScript(unittest.TestCase):
    def test_three_numbered_slots_and_the_script_agrees(self):
        self.assertIn('playbox_slots = { for n in [1, 2, 3] : tostring(n) => "playbox-${n}" }', TF)
        self.assertIn('SLOTS="1 2 3"', SCRIPT)
        self.assertIn('NAME="playbox-$SLOT"', SCRIPT)
        self.assertIn('VOLTAG="$NAME-data"', SCRIPT)
        self.assertIn('tags = { Name = "${each.value}-data", Purpose = "windows-playtest" }', block("aws_ebs_volume", "playbox_data"))

    def test_types_region_and_lifetime_match_terraform(self):
        self.assertEqual(hcl_list("playbox_types"), ["g4dn.xlarge", "g4dn.2xlarge"])
        self.assertEqual(re.search(r'^TYPES="([^"]+)"', SCRIPT, re.M).group(1).split(), hcl_list("playbox_types"))
        self.assertIn('REGION="us-west-1"', SCRIPT)
        self.assertIn("playbox_region = var.aws_region", TF)
        self.assertRegex((ROOT / "variables.tf").read_text(), r'variable "aws_region" \{[^}]*default\s*=\s*"us-west-1"')
        hours = re.search(r"playbox_max_hours\s*=\s*(\d+)", TF).group(1)
        self.assertIn("MAX_HOURS=%s " % hours, SCRIPT)

    def test_installed_on_every_dev_box_that_holds_the_grant(self):
        self.assertIn('${base64encode(file("${path.module}/scripts/playbox.sh"))}', (ROOT / "dev-selfupdate.tf").read_text())
        self.assertEqual((ROOT / "dev-user-data.tf").read_text().count("${local.playbox_setup}"), 2)  # arm and x86
        self.assertEqual((ROOT / "nextjs-user-data.tf").read_text().count("${local.playbox_setup}"), 1)
        attach = block("aws_iam_role_policy_attachment", "playbox_control")
        self.assertIn("{ dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }", attach)


class Network(unittest.TestCase):
    def test_only_rdp_and_dcv_from_the_dev_fleet(self):
        sg = block("aws_security_group", "playbox")
        ingress = sg.split("egress", 1)[0]
        self.assertIn('for_each = { rdp = ["tcp", 3389], dcv-tcp = ["tcp", 8443], dcv-udp = ["udp", 8443] }', ingress)
        self.assertEqual(ingress.count("cidr_blocks"), 1)
        self.assertIn("cidr_blocks = local.playbox_client_cidr", ingress)
        self.assertIn("playbox_client_cidr = [for s in local.dev_fleet_subnets : s.cidr_block]", TF)
        self.assertNotRegex(ingress, r"0\.0\.0\.0/0|::/0")

    def test_its_subnet_reaches_the_internet_and_nothing_else(self):
        rt = block("aws_route_table", "playbox")
        self.assertEqual(rt.count("route {"), 1)
        self.assertIn("gateway_id = aws_internet_gateway.main.id", rt)
        self.assertNotRegex(rt, r"vpc_peering|transit_gateway|vpn")
        self.assertIn("route_table_id = aws_route_table.playbox.id", block("aws_route_table_association", "playbox"))
        # its own /24 in the main VPC, not one another subnet uses (wbox 68, games 64-67, imagine 70-71)
        self.assertIn("cidrsubnet(data.aws_vpc.selected.cidr_block, 8, 69)", block("aws_subnet", "playbox"))
        others = "\n".join(p.read_text() for p in ROOT.glob("*.tf") if p.name != "playbox.tf")
        self.assertNotRegex(others, r"cidrsubnet\(data\.aws_vpc\.selected\.cidr_block, 8, 69\)|=\s*69\b")

    def test_the_template_launches_into_that_subnet_and_group(self):
        lt = block("aws_launch_template", "playbox")
        self.assertIn("subnet_id                   = aws_subnet.playbox.id", lt)
        self.assertIn("security_groups             = [aws_security_group.playbox.id]", lt)
        self.assertIn("availability_zone       = local.playbox_az", block("aws_subnet", "playbox"))
        self.assertIn("availability_zone = local.playbox_az", block("aws_ebs_volume", "playbox_data"))


class Disk(unittest.TestCase):
    def test_the_data_disks_survive(self):
        vol = block("aws_ebs_volume", "playbox_data")
        self.assertIn("prevent_destroy = true", vol)
        self.assertIn("encrypted         = true", vol)
        self.assertIn("for_each          = local.playbox_slots", vol)
        self.assertNotIn("DevEBS", vol, "a DevEBS tag would put the disk in reach of the dev boxes' delete grant")

    def test_found_by_its_volume_id_and_formatted_only_when_blank(self):
        ud = user_data()
        self.assertIn('$serial = \'${replace(aws_ebs_volume.playbox_data[slot].id, "-", "")}\'', ud)
        self.assertIn('$disk = Get-Disk | Where-Object { $_.SerialNumber -like "$serial*" }', ud)
        self.assertNotIn("Where-Object PartitionStyle -eq 'RAW'", ud)
        self.assertEqual(ud.count("Format-Volume"), 1)
        self.assertLess(ud.index("if ($disk.PartitionStyle -eq 'RAW') {"), ud.index("Format-Volume"))

    def test_roblox_and_the_installers_live_on_d(self):
        ud = user_data()
        self.assertIn("(Join-Path $env:LOCALAPPDATA 'Roblox') = 'D:\\Roblox\\LocalAppData'", ud)
        self.assertIn("'Roblox') = 'D:\\Roblox\\Documents'", ud)
        self.assertIn("New-Item -ItemType Junction", ud)
        self.assertIn("$cache = 'D:\\playbox\\cache'", ud)
        # an existing real folder is set aside, never deleted
        self.assertIn("Rename-Item -LiteralPath $link", ud)
        self.assertNotRegex(ud, r"Remove-Item[^\n]*\$link")
        self.assertIn("New-ScheduledTaskTrigger -AtLogOn -User 'Administrator'", ud)


class Boot(unittest.TestCase):
    def test_setup_retries_until_complete_and_marks_done_last(self):
        ud = user_data()
        self.assertIn("<persist>true</persist>", ud)
        self.assertLess(ud.index("Register-ScheduledTask"), ud.index("New-Item 'C:\\playbox-setup.done'"))
        self.assertLess(ud.index("Install-WindowsFeature"), ud.index("New-Item 'C:\\playbox-setup.done'"))
        self.assertIn("} catch {", ud)
        # the Roblox download is the one non-fatal step
        roblox = ud.split("https://www.roblox.com/download/client", 1)[1].split("New-Item -ItemType Directory -Force 'C:\\playbox'", 1)[0]
        self.assertIn("catch { Write-Output", roblox)

    def test_no_template_sequences_but_the_three_intended(self):
        found = re.findall(r"\$\{[^}]*\}|%\{", user_data())
        self.assertEqual(found, ["${aws_secretsmanager_secret.playbox_admin[slot].name}", "${local.playbox_region}",
                                 '${replace(aws_ebs_volume.playbox_data[slot].id, "-", "")}'])

    def test_the_template_is_one_time_spot_tags_everything_and_needs_imdsv2(self):
        lt = block("aws_launch_template", "playbox")
        self.assertIn('market_type = "spot"', lt)
        self.assertIn('spot_instance_type             = "one-time"', lt)
        self.assertIn('instance_interruption_behavior = "terminate"', lt)
        self.assertIn('instance_initiated_shutdown_behavior = "terminate"', lt)
        self.assertIn('http_tokens   = "required"', lt)
        self.assertIn("delete_on_termination = true", lt)
        self.assertNotIn("key_name", lt)
        for kind, tags in (("instance", "{ Name = each.value, Purpose = \"windows-playtest\" }"),
                           ("volume", "{ Name = each.value, Role = \"root\" }"),
                           ("network-interface", "{ Name = each.value }")):
            self.assertRegex(lt, r'resource_type = "%s"\n\s+tags\s+= %s' % (re.escape(kind), re.escape(tags)))
        self.assertIn("user_data = base64encode(local.playbox_user_data[each.key])", lt)


class Grant(unittest.TestCase):
    RUN_SIDS = ("RunPlayboxInstanceOnly", "RunPlayboxRootVolume", "RunPlayboxEni", "RunPlayboxReferencedResources")

    def test_every_launch_statement_requires_a_slot_template(self):
        for sid in self.RUN_SIDS:
            with self.subTest(sid=sid):
                self.assertRegex(statement(sid), r'"ec2:LaunchTemplate"\s*\n\s*values\s*=\s*local\.playbox_template_arns\n')
        self.assertEqual(TF.count('actions   = ["ec2:RunInstances"]') + TF.count('actions = ["ec2:RunInstances"]'), 4)

    def test_created_resources_carry_a_slot_name_and_only_their_own_tag_keys(self):
        for sid, keys in (("RunPlayboxInstanceOnly", "instance"), ("RunPlayboxRootVolume", "volume"), ("RunPlayboxEni", "eni")):
            with self.subTest(sid=sid):
                s = statement(sid)
                self.assertRegex(s, r'"aws:RequestTag/Name"\s*\n\s*values\s*=\s*local\.playbox_names\n')
                self.assertIn("values   = local.playbox_tag_keys.%s" % keys, s)
        self.assertIn('playbox_names = [for k in sort(keys(local.playbox_slots)) : local.playbox_slots[k]]', TF)

    def test_the_instance_is_one_of_two_types_with_this_profile_only(self):
        s = statement("RunPlayboxInstanceOnly")
        self.assertIn("values   = local.playbox_types", s)
        self.assertIn("values   = [aws_iam_instance_profile.playbox.arn]", s)
        self.assertIn('"ec2:MetadataHttpTokens"', s)
        self.assertIn('values   = ["dedicated", "host"]', s)
        self.assertIn("resources = [local.playbox_instance_arn]", s)

    def test_the_root_volume_is_the_templates_shape(self):
        s = statement("RunPlayboxRootVolume")
        for key in ('"ec2:VolumeType"', '"ec2:VolumeSize"', '"ec2:Encrypted"'):
            self.assertIn(key, s)
        self.assertIn("values   = [tostring(local.playbox_root_size)]", s)

    def test_referenced_resources_are_exact_arns(self):
        s = statement("RunPlayboxReferencedResources")
        self.assertIn("::image/${nonsensitive(data.aws_ssm_parameter.playbox_ami.value)}", s)
        self.assertIn(":subnet/${aws_subnet.playbox.id}", s)
        self.assertIn(":security-group/${aws_security_group.playbox.id}", s)
        self.assertNotIn("*", s)

    def test_passrole_terminate_attach_and_describe_are_scoped(self):
        p = statement("PassPlayboxRole")
        self.assertIn("resources = [aws_iam_role.playbox.arn]", p)
        self.assertIn('values   = ["ec2.amazonaws.com"]', p)
        t = statement("TerminateTaggedPlayboxOnly")
        self.assertIn('actions   = ["ec2:TerminateInstances"]', t)
        self.assertRegex(t, r'"ec2:ResourceTag/Name"\s*\n\s*values\s*=\s*local\.playbox_names\n')
        self.assertIn("resources = local.playbox_data_arns", statement("AttachPlayboxDataVolume"))
        self.assertIn('"ec2:ResourceTag/Name"', statement("AttachToTaggedPlayboxOnly"))
        d = statement("DescribeForPlaybox")
        self.assertIn('"aws:RequestedRegion"', d)
        self.assertNotRegex(d, r'"ec2:(Run|Terminate|Create|Attach|Delete|Modify)')
        self.assertIn("ec2:CreateAction", statement("TagPlayboxAtLaunchOnly"))

    def test_the_grant_holds_nothing_else(self):
        doc = block("aws_iam_policy_document", "playbox_control")
        actions = set(re.findall(r'"((?:ec2|iam|secretsmanager|ssm|s3):\w+)"', doc)) - {
            "ec2:LaunchTemplate", "ec2:InstanceType", "ec2:Tenancy", "ec2:MetadataHttpTokens", "ec2:InstanceProfile",
            "ec2:VolumeType", "ec2:VolumeSize", "ec2:Encrypted", "ec2:CreateAction", "iam:PassedToService"}
        self.assertEqual(actions, {"ec2:RunInstances", "ec2:CreateTags", "ec2:TerminateInstances", "ec2:AttachVolume",
                                   "iam:PassRole", "secretsmanager:GetSecretValue", "ec2:DescribeInstances",
                                   "ec2:DescribeVolumes", "ec2:DescribeSpotPriceHistory"})

    def test_the_passwords_have_exactly_their_readers(self):
        policy = block("aws_secretsmanager_secret_policy", "playbox_admin")
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.playbox_password_readers)', policy)
        self.assertIn("playbox_password_readers = [aws_iam_role.playbox.arn, aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]", TF)
        self.assertIn('name                    = "${each.value}/administrator-password"', block("aws_secretsmanager_secret", "playbox_admin"))


class Watchdog(unittest.TestCase):
    def test_its_own_function_and_role_reach_the_slots_only(self):
        fn = block("aws_lambda_function", "playbox_watchdog")
        self.assertIn('TAG_NAMES       = join(",", local.playbox_names)', fn)
        self.assertIn("IDLE_MINUTES    = tostring(local.playbox_idle_minutes)", fn)
        self.assertIn("IDLE_CPU_PCT    = tostring(local.playbox_idle_cpu_pct)", fn)
        self.assertIn("MAX_AGE_MINUTES = jsonencode({ for n in local.playbox_names : n => local.playbox_max_hours * 60 })", fn)
        self.assertIn("filename         = data.archive_file.parallel_watchdog.output_path", fn)
        self.assertNotIn("provider", fn, "us-west-1, next to the boxes")
        self.assertRegex(TF, r"playbox_idle_minutes = 60\n")
        self.assertRegex(TF, r"playbox_idle_cpu_pct = 15\n")
        role = block("aws_iam_role_policy", "playbox_watchdog")
        self.assertIn('Condition = { StringEquals = { "ec2:ResourceTag/Name" = local.playbox_names } }', role)
        self.assertIn("Resource  = local.playbox_instance_arn", role)
        self.assertIn('schedule_expression = "rate(5 minutes)"', block("aws_cloudwatch_event_rule", "playbox_watchdog"))
        # the parallel boxes' role is not widened to reach these
        self.assertNotIn("playbox", block("aws_iam_role_policy", "parallel_watchdog", WATCHDOG))

    def run_watchdog(self, instances, cpu):
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
                return {"Datapoints": [{"Maximum": p} for p in cpu[kw["Dimensions"][0]["Value"]]]}

        class SNS:
            def publish(self, **kw):
                calls["body"].append(kw["Message"])

        fake = types.ModuleType("boto3")
        fake.client = lambda name, region_name=None: {"ec2": EC2(), "cloudwatch": CW(), "sns": SNS()}[name]
        saved_mod, saved_env = sys.modules.get("boto3"), dict(os.environ)
        sys.modules["boto3"] = fake
        os.environ.update(TAG_NAMES=",".join(SLOTS), IDLE_MINUTES="60", IDLE_CPU_PCT="15",
                          MAX_AGE_MINUTES=json.dumps({n: 360 for n in SLOTS}), SNS_TOPIC_ARN="arn:aws:sns:test")
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

    def test_an_idle_windows_desktop_ends_and_its_notice_names_its_slot(self):
        actions, calls = self.run_watchdog([("i-2", "playbox-2", 90)], {"i-2": self.IDLE_DESKTOP})
        self.assertEqual(actions, {"i-2": "terminated"})
        self.assertIn("playbox up 2\n", calls["body"][0])
        self.assertIn("Its D: disk (Roblox and its caches) is untouched", calls["body"][0])
        self.assertNotIn("pbox up", calls["body"][0])

    def test_a_game_being_played_keeps_it_but_the_lifetime_still_ends_it(self):
        busy = self.IDLE_DESKTOP[:5] + [38.0] + self.IDLE_DESKTOP[6:]
        actions, calls = self.run_watchdog([("i-1", "playbox-1", 120), ("i-3", "playbox-3", 365), ("i-y", "playbox-2", 30)],
                                           {"i-1": busy, "i-3": busy, "i-y": self.IDLE_DESKTOP})
        self.assertEqual(actions, {"i-1": "busy", "i-3": "terminated_lifetime", "i-y": "too_young"})
        self.assertEqual(calls["terminate"], ["i-3"])


FAKE_AWS = r'''#!PYTHON -I
# A stand-in for the aws CLI. $FAKE_STATE/<Name> holds a running box's instance id; every call is logged to
# $FAKE_STATE/calls; FAKE_NO_CAPACITY lists types with no spot capacity; FAKE_ATTACH_FAILS makes attach-volume fail.
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

if op == "describe-instances":
    iid = box(tag_name())
    q = opt("--query")
    if not iid:
        print("None\tNone\tNone\tNone\tNone" if "PrivateIpAddress" in q else "")
    elif "PrivateIpAddress" in q:
        print("%s\tg4dn.xlarge\tspot\t10.0.69.%s\t2026-10-18T10:00:00+00:00" % (iid, tag_name()[-1]))
    else:
        print(iid)
elif op == "describe-volumes":
    print("vol-0%s\tavailable\tus-west-1a" % tag_name().split("-")[1])
elif op == "describe-spot-price-history":
    print("0.2554")
elif op == "run-instances":
    t = opt("--instance-type")
    if t in os.environ.get("FAKE_NO_CAPACITY", "").split():
        sys.stderr.write("An error occurred (InsufficientInstanceCapacity) when calling the RunInstances operation\n")
        sys.exit(254)
    name = opt("--launch-template").split(",")[0].split("=", 1)[1]
    open(os.path.join(state, name), "w").write("i-new-%s\n" % name)
    print("i-new-%s" % name)
elif op == "attach-volume":
    if os.environ.get("FAKE_ATTACH_FAILS"):
        sys.stderr.write("An error occurred (VolumeInUse)\n")
        sys.exit(254)
elif op == "terminate-instances":
    gone = args[args.index("--instance-ids") + 1:]
    gone = [g for g in gone if g.startswith("i-")]
    for name in os.listdir(state):
        if name.startswith("playbox-") and box(name) in gone:
            os.remove(os.path.join(state, name))
elif args[0] == "secretsmanager":
    print("fake-password-for-%s" % opt("--secret-id"))
'''


class ScriptRuns(unittest.TestCase):
    """scripts/playbox.sh itself against a fake aws: each slot's commands touch only that slot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.state, bin_dir = root / "state", root / "bin"
        for d in (self.state, bin_dir):
            d.mkdir()
        (bin_dir / "aws").write_text(FAKE_AWS.replace("PYTHON", sys.executable, 1))
        # DCV "answers" at once: the script's TCP probe runs under timeout, so a fake timeout that succeeds stands in.
        (bin_dir / "timeout").write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n")
        for f in ("aws", "timeout", "sleep"):
            (bin_dir / f).chmod(0o755)
        self.env = {"PATH": "%s:/usr/bin:/bin" % bin_dir, "HOME": str(root), "FAKE_STATE": str(self.state), "TZ": "UTC"}

    def tearDown(self):
        self.tmp.cleanup()

    def playbox(self, *args, **env):
        (self.state / "calls").write_text("")
        r = subprocess.run(["bash", str(ROOT / "scripts" / "playbox.sh"), *args], env=dict(self.env, **env),
                           capture_output=True, text=True, timeout=60)
        return r, (self.state / "calls").read_text().splitlines()

    def test_up_launches_from_its_slots_template_and_attaches_its_disk(self):
        r, calls = self.playbox("up", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        runs = [c for c in calls if " run-instances " in " %s " % c]
        self.assertEqual(len(runs), 1, calls)
        self.assertIn("LaunchTemplateName=playbox-2,Version=$Latest", runs[0])
        self.assertIn("--instance-type g4dn.xlarge", runs[0])
        self.assertNotIn("--tag-specifications", runs[0])
        self.assertNotIn("--subnet-id", runs[0])
        attach = [c for c in calls if "attach-volume" in c]
        self.assertEqual(attach, ["ec2 attach-volume --region us-west-1 --volume-id vol-02 --instance-id i-new-playbox-2 --device xvdf"])
        self.assertEqual(r.stdout.strip().splitlines()[-1], "launched slot=2 type=g4dn.xlarge ip=10.0.69.2")
        self.assertIn("ssh -L 8443:10.0.69.2:8443", r.stderr)
        self.assertNotIn("fake-password", r.stdout + r.stderr)

    def test_up_falls_back_to_the_larger_type_then_exits_3(self):
        r, calls = self.playbox("up", "1", FAKE_NO_CAPACITY="g4dn.xlarge")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("launched slot=1 type=g4dn.2xlarge", r.stdout)
        (self.state / "playbox-1").unlink()
        r, calls = self.playbox("up", "1", FAKE_NO_CAPACITY="g4dn.xlarge g4dn.2xlarge")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(len([c for c in calls if "run-instances" in c]), 2)
        self.assertFalse([c for c in calls if "attach-volume" in c])

    def test_a_box_whose_disk_cannot_be_attached_is_terminated(self):
        r, calls = self.playbox("up", "3", FAKE_ATTACH_FAILS="1")
        self.assertEqual(r.returncode, 1)
        self.assertIn("ec2 terminate-instances --region us-west-1 --instance-ids i-new-playbox-3", calls)
        self.assertFalse((self.state / "playbox-3").exists())

    def test_up_of_a_running_slot_launches_nothing(self):
        (self.state / "playbox-1").write_text("i-111\n")
        r, calls = self.playbox("up", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "running slot=1 ip=10.0.69.1")
        self.assertFalse([c for c in calls if "run-instances" in c])

    def test_down_terminates_only_its_own_slot(self):
        (self.state / "playbox-1").write_text("i-111\n")
        (self.state / "playbox-3").write_text("i-333\n")
        r, calls = self.playbox("down", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([c for c in calls if "terminate-instances" in c],
                         ["ec2 terminate-instances --region us-west-1 --instance-ids i-333"])
        self.assertTrue((self.state / "playbox-1").exists())
        self.assertIn("Its D: disk is kept", r.stderr)

    def test_a_slot_is_required_and_checked_before_any_aws_call(self):
        for args in (("up",), ("down",), ("connect",), ("up", "4"), ("down", "0"), ("up", "1", "2"), ("status", "x")):
            with self.subTest(args=args):
                r, calls = self.playbox(*args)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(calls, [])

    def test_connect_prints_the_password_only_when_asked(self):
        (self.state / "playbox-2").write_text("i-222\n")
        r, calls = self.playbox("connect", "2")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("fake-password", r.stdout + r.stderr)
        self.assertFalse([c for c in calls if c.startswith("secretsmanager")])
        self.assertIn("playbox.sh connect 2 --show-password", r.stderr)
        r, _ = self.playbox("connect", "2", "--show-password")
        self.assertEqual(r.stdout.strip(), "fake-password-for-playbox-2/administrator-password")
        r, calls = self.playbox("status", "2", "--show-password")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(calls, [])

    def test_status_shows_every_slot_and_the_lifetime(self):
        (self.state / "playbox-2").write_text("i-222\n")
        r, _ = self.playbox("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = [l for l in r.stdout.splitlines() if l.startswith("playbox-")]
        self.assertEqual(lines, ["playbox-1: down ($0 compute)",
                                 "playbox-2: RUNNING at 10.0.69.2 (g4dn.xlarge, spot), DCV answering; "
                                 "the watchdog ends it after an idle hour or at 16:00 UTC",
                                 "playbox-3: down ($0 compute)"])
        self.assertEqual(r.stdout.count("D: disk: vol-0"), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)

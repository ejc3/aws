#!/usr/bin/env python3
"""The Windows playtest box (wbox.tf): reachable only from the dev boxes, kept off the fleet's
peers, its game disk never destroyed or reformatted, the dev boxes limited to start/stop, and
stopped after one idle hour. Offline; reads the Terraform source."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "wbox.tf").read_text()
AUTOSTOP = (ROOT / "dev-auto-stop-lambda.tf").read_text()


def block(kind, name, text=TF):
    m = re.search(r'^(?:resource|data) "%s" "%s" \{\n.*?^\}' % (kind, name), text, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


class WboxTests(unittest.TestCase):
    def test_only_rdp_and_dcv_from_the_dev_fleet(self):
        sg = block("aws_security_group", "wbox")
        self.assertIn('for_each = { rdp = ["tcp", 3389], dcv-tcp = ["tcp", 8443], dcv-udp = ["udp", 8443] }', sg)
        self.assertIn("cidr_blocks = local.wbox_client_cidr", sg.split("egress", 1)[0])
        self.assertEqual(sg.split("egress", 1)[0].count("cidr_blocks"), 1, "one ingress source list")
        self.assertIn("wbox_client_cidr = [for s in local.dev_fleet_subnets : s.cidr_block]", TF)
        self.assertNotRegex(sg.split("egress", 1)[0], r"0\.0\.0\.0/0|::/0")

    def test_its_subnet_reaches_the_internet_and_nothing_else(self):
        rt = block("aws_route_table", "wbox")
        self.assertEqual(rt.count("route {"), 1)
        self.assertIn("gateway_id = aws_internet_gateway.main.id", rt)
        self.assertNotRegex(rt, r"vpc_peering|transit_gateway|vpn")
        self.assertIn("route_table_id = aws_route_table.wbox.id", block("aws_route_table_association", "wbox"))

    def test_the_game_disk_survives_and_is_formatted_only_when_blank(self):
        self.assertRegex(block("aws_ebs_volume", "wbox_games"), r"prevent_destroy = true")
        # The game disk is found by its EBS volume id, never "any RAW disk" (the instance store is
        # RAW too, and wiped at every stop), and only a RAW one is ever formatted.
        self.assertIn('wbox_games_serial = replace(aws_ebs_volume.wbox_games.id, "-", "")', TF)
        self.assertIn('$disk = Get-Disk | Where-Object { $_.SerialNumber -like "$serial*" }', TF)
        self.assertNotIn("Get-Disk | Where-Object PartitionStyle -eq 'RAW'", TF)
        self.assertIn("if ($disk.PartitionStyle -eq 'RAW') {", TF)

    def test_setup_retries_until_it_completes_and_waits_for_its_inputs(self):
        self.assertIn("<persist>true</persist>", TF)
        body = TF.split("wbox_user_data = <<-PS", 1)[1]
        self.assertLess(body.index("Install-WindowsFeature"), body.index("New-Item 'C:\\wbox-setup.done'"),
                        "the marker is written only after every step")
        self.assertIn("catch {", body)
        instance = block("aws_instance", "wbox")
        for dep in ("aws_secretsmanager_secret_version.wbox_admin", "aws_iam_role_policy.wbox"):
            self.assertIn(dep, instance)
        self.assertIn("ignore_changes = [ami, user_data]", block("aws_instance", "wbox"))

    def test_the_dev_boxes_may_only_start_stop_reboot_and_read_the_password(self):
        doc = block("aws_iam_policy_document", "wbox_control")
        self.assertIn('actions   = ["ec2:StartInstances", "ec2:StopInstances", "ec2:RebootInstances"]', doc)
        self.assertIn("resources = [aws_instance.wbox[0].arn]", doc)
        self.assertEqual(set(re.findall(r'"(\w+:\w+)"', doc)),
                         {"ec2:StartInstances", "ec2:StopInstances", "ec2:RebootInstances", "secretsmanager:GetSecretValue",
                          "ssm:SendCommand", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "ssm:ListCommands"})
        # SendCommand only to this instance, with only the PowerShell document.
        send = doc.split('sid     = "RunPowerShellOnTheWindowsBox"', 1)[1].split("statement {", 1)[0]
        self.assertEqual(re.findall(r"^\s+(aws_instance\.\S+|\"arn:aws:ssm:[^\"]+\")", send, re.M),
                         ["aws_instance.wbox[0].arn,", '"arn:aws:ssm:${var.aws_region}::document/AWS-RunPowerShellScript"'])
        self.assertNotIn("StartSession", doc)

    def test_the_password_has_exactly_its_readers(self):
        policy = block("aws_secretsmanager_secret_policy", "wbox_admin")
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.wbox_password_readers)', policy)
        self.assertIn("wbox_password_readers = [aws_iam_role.wbox.arn, aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]", TF)

    def test_it_stops_after_one_idle_hour(self):
        fn = block("aws_lambda_function", "wbox_auto_stop")
        self.assertIn('IDLE_HOURS    = "1"', fn)
        self.assertIn("INSTANCE_IDS  = aws_instance.wbox[0].id", fn)
        self.assertIn('schedule_expression = "rate(10 minutes)"', block("aws_cloudwatch_event_rule", "wbox_auto_stop"))
        self.assertRegex(AUTOSTOP, r'"ec2:ResourceTag/Name" = \[[^\]]*"wbox"')


if __name__ == "__main__":
    unittest.main(verbosity=2)

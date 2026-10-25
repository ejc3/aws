#!/usr/bin/env python3
"""dolphin-maps-moderation.tf: the dolphin-maps Workers may call Rekognition DetectModerationLabels (us-west-1) and Comprehend
DetectToxicContent (us-west-2) and nothing else, each pinned to its region; everything is in the Dolphin account; each
Worker has its own user whose key Terraform writes only into its own environment's administration-only container; and an
hourly-volume alarm notifies dolphin-alerts."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-maps-moderation.tf").read_text()
NAMES = ["MODERATION_AWS_ACCESS_KEY_ID", "MODERATION_AWS_REGION", "MODERATION_AWS_SECRET_ACCESS_KEY", "MODERATION_TEXT_REGION"]


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, name):
    m = re.search(r'^%s "%s" \{\n.*?^\}\n' % (re.escape(kind), re.escape(name)), code(TF) + "\n", re.M | re.S)
    assert m, (kind, name)
    return m.group()


class GrantTests(unittest.TestCase):
    def test_two_permissions_each_pinned_to_its_own_region(self):
        doc = block('data "aws_iam_policy_document"', "dolphin_maps_moderation")
        statements = doc.split("statement {")[1:]
        self.assertEqual(len(statements), 2)
        expected = {"rekognition:DetectModerationLabels": "local.dolphin_maps_moderation_region",
                    "comprehend:DetectToxicContent": "local.dolphin_maps_moderation_text_region"}
        for s in statements:
            (action,) = re.findall(r'actions\s+= \["([^"]+)"\]', s)
            self.assertIn('variable = "aws:RequestedRegion"', s)
            self.assertIn('test     = "StringEquals"', s)
            self.assertIn("values   = [%s]" % expected.pop(action), s)
        self.assertEqual(expected, {})
        self.assertNotRegex(code(TF), r'"(rekognition|comprehend):\*"|"\*:\*"')
        self.assertIn('dolphin_maps_moderation_region      = "us-west-1"', TF)
        self.assertIn('dolphin_maps_moderation_text_region = "us-west-2"', TF)  # no Comprehend in us-west-1

    def test_each_worker_has_its_own_user_with_only_that_policy(self):
        self.assertIn('prod  = { user = "dolphin-maps-moderation-worker-prod", worker = "dolphin-maps-prod", container = "workers-prod/dolphin-maps/terraform" }', TF)
        self.assertIn('stage = { user = "dolphin-maps-moderation-worker-stage", worker = "dolphin-maps-stage", container = "workers-stage/dolphin-maps/terraform" }', TF)
        pol = block('resource "aws_iam_user_policy"', "dolphin_maps_moderation_worker")
        self.assertIn("policy   = data.aws_iam_policy_document.dolphin_maps_moderation.json", pol)
        self.assertEqual(code(TF).count('resource "aws_iam_user_policy"'), 1)
        for kind in ("aws_iam_role", "aws_iam_policy_attachment", "aws_iam_user_policy_attachment", "aws_iam_group"):
            self.assertNotIn('resource "%s"' % kind, TF)


class DolphinAccountTests(unittest.TestCase):
    def test_every_aws_resource_is_in_the_dolphin_account(self):
        found = re.findall(r'^resource "(aws_\w+)" "(\w+)" \{\n(.*?)^\}', code(TF), re.M | re.S)
        self.assertGreaterEqual(len(found), 7)
        for kind, name, body in found:
            self.assertRegex(body, r"(?m)^  provider\s+= aws\.dolphin$", "%s.%s" % (kind, name))

    def test_nothing_names_the_dolphin_account_or_trusts_it(self):
        self.assertNotIn("aws_organizations_account.dolphin", TF)
        self.assertNotIn("local.dolphin_account_id", TF)
        self.assertNotRegex(TF, r"\b\d{12}\b")
        self.assertNotRegex(code(TF), r"sts:AssumeRole|Principal = \{")

    def test_both_regions_are_ones_the_dolphin_scp_allows(self):
        ver = block('resource "aws_secretsmanager_secret_version"', "dolphin_maps_moderation_worker")
        self.assertIn("contains(local.dolphin_security_regions, r)", ver)
        regions = (ROOT / "dolphin-security.tf").read_text()
        allowed = re.search(r"dolphin_security_regions = \[(.*?)\]", regions).group(1)
        for region in ("us-west-1", "us-west-2"):
            self.assertIn('"%s"' % region, allowed)


class KeyTests(unittest.TestCase):
    def test_each_key_goes_only_to_its_own_environments_container(self):
        ver = block('resource "aws_secretsmanager_secret_version"', "dolphin_maps_moderation_worker")
        self.assertIn("secret_id = aws_secretsmanager_secret.dolphin_maps_moderation_worker[each.key].id", ver)
        self.assertIn("aws_iam_access_key.dolphin_maps_moderation_worker[each.key].id", ver)
        self.assertIn("aws_iam_access_key.dolphin_maps_moderation_worker[each.key].secret", ver)
        self.assertEqual(sorted(re.findall(r"^\s+(MODERATION_[A-Z_]+)\s+=", ver, re.M)), NAMES)
        self.assertIn('startswith(each.value.container, each.key == "prod" ? "workers-prod/" : "workers-stage/")', ver)
        self.assertNotRegex(ver, r'\["(prod|stage)"\]')  # never one environment's key by a fixed index

    def test_the_containers_are_administration_only(self):
        pol = block('resource "aws_secretsmanager_secret_policy"', "dolphin_maps_moderation_worker")
        self.assertIn('Effect    = "Deny"', pol)
        self.assertIn('Principal = "*"', pol)
        self.assertIn('Action    = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"]', pol)
        self.assertIn('Condition = { ArnNotLike = { "aws:PrincipalArn" = local.dolphin_maps_moderation_admins } }', pol)
        self.assertIn("block_public_policy = true", pol)
        admins = re.search(r"dolphin_maps_moderation_admins = \[(.*?)\n  \]", TF, re.S).group(1)
        self.assertIn(':role/OrganizationAccountAccessRole"', admins)
        self.assertIn("local.dolphin_terraform_role_arn", admins)
        self.assertIn("local.dolphin_sso_admin_role_arns", admins)
        self.assertEqual(len(re.findall(r"^\s+\S", admins, re.M)), 3)

    def test_the_loaders_merge_the_terraform_containers(self):
        prod = (ROOT / "scripts/workers-prod-secrets.sh").read_text()
        case = re.search(r"\n  dolphin-maps\)\n(.*?)\n    ;;", prod, re.S).group(1)
        self.assertIn("MANAGED_CONTAINER=1", case)
        self.assertIn('read_container "workers-prod/$SITE/terraform"', prod)
        stage = (ROOT / "scripts/workers-stage-secrets.sh").read_text()
        self.assertIn("dolphin-maps) SOURCE_ACCOUNT=$DOLPHIN_ACCOUNT_ID MANAGED_CONTAINER=1 ;;", stage)
        self.assertIn('read_or_say "workers-stage/$SITE/terraform"', stage)
        for name in NAMES:
            out_of_band = " ".join(re.findall(r'OUT_OF_BAND_NAMES\+?="([^"]+)"', case))
            self.assertNotIn(name, out_of_band)  # required managed names are never also out of band


class AlarmTests(unittest.TestCase):
    def test_hourly_volume_alarm_notifies_dolphin_alerts(self):
        alarm = block('resource "aws_cloudwatch_metric_alarm"', "dolphin_maps_moderation_volume")
        self.assertIn('alarm_name          = "dolphin-maps-moderation-volume"', alarm)
        self.assertIn("threshold           = local.dolphin_maps_moderation_hourly_alarm", alarm)
        self.assertIn('namespace           = "AWS/Rekognition"', alarm)
        self.assertIn('metric_name         = "SuccessfulRequestCount"', alarm)
        self.assertIn('dimensions          = { Operation = "DetectModerationLabels" }', alarm)
        self.assertIn("period              = 3600", alarm)
        self.assertIn("alarm_actions       = [data.aws_sns_topic.dolphin_alerts.arn]", alarm)
        self.assertIn("var.aws_region == local.dolphin_maps_moderation_region", alarm)
        self.assertIn("dolphin_maps_moderation_hourly_alarm = 500", TF)


class DocsTests(unittest.TestCase):
    def test_agents_md_has_the_section(self):
        agents = (ROOT / "AGENTS.md").read_text()
        section = agents[agents.index("### Dolphin Maps picture and text moderation"):]
        section = section[:section.index("\n### ", 4)]
        for text in ("dolphin-maps-moderation.tf", "us-west-1", "us-west-2", "dolphin-maps-moderation-worker-prod",
                     "dolphin-maps-moderation-worker-stage", "workers-prod/dolphin-maps/terraform",
                     "workers-stage/dolphin-maps/terraform", "dolphin-maps-moderation-volume", "-replace=", *NAMES):
            self.assertIn(text, section)


if __name__ == "__main__":
    unittest.main(buffer=True)

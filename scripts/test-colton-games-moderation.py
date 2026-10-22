#!/usr/bin/env python3
"""colton-games-moderation.tf: colton-games may call Rekognition DetectModerationLabels and nothing else, from Vercel through
OIDC per environment and from the staging Worker through an administration-only key."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "colton-games-moderation.tf").read_text()


def block(kind, name):
    return re.search(r'%s "%s" \{.*?\n\}\n' % (kind, re.escape(name)), TF, re.S).group()


class ModerationTests(unittest.TestCase):
    def test_the_one_permission_is_detect_moderation_labels_in_one_region(self):
        doc = block('data "aws_iam_policy_document"', "moderation")
        self.assertEqual(doc.count("statement {"), 1)
        self.assertIn('moderation_policy_action = "rekognition:DetectModerationLabels"', TF)
        self.assertIn('variable = "aws:RequestedRegion"', doc)
        actions = re.findall(r'"(rekognition:[A-Za-z*]+|[a-z0-9-]+:[A-Za-z*]+)"', TF)
        self.assertEqual({a for a in actions if a.startswith("rekognition:")}, {"rekognition:DetectModerationLabels"})
        self.assertNotIn("rekognition:*", TF)
        for kind, name in (('resource "aws_iam_role_policy"', "colton_games_moderation"),
                           ('resource "aws_iam_user_policy"', "colton_games_moderation_worker"),
                           ('resource "aws_iam_role_policy"', "nextjs_dev_moderation")):
            self.assertRegex(block(kind, name), r"policy\s+= data\.aws_iam_policy_document\.moderation\.json")

    def test_each_vercel_environment_assumes_only_its_own_role(self):
        role = block('resource "aws_iam_role"', "colton_games_moderation")
        self.assertIn("Federated = aws_iam_openid_connect_provider.vercel.arn", role)
        self.assertIn('project:${local.vercel_project_name}:environment:${each.key}"', role)
        self.assertIn("StringEquals", role)
        self.assertNotIn("StringLike", role)
        self.assertIn('production = "colton-games-moderation"', TF)
        self.assertIn('preview    = "colton-games-moderation-preview"', TF)

    def test_the_worker_key_is_administration_only(self):
        pol = block('resource "aws_secretsmanager_secret_policy"', "colton_games_moderation_worker_key")
        self.assertIn('Effect    = "Deny"', pol)
        self.assertIn('ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals }', pol)
        ver = block('resource "aws_secretsmanager_secret_version"', "colton_games_moderation_worker_key")
        for name in ("MODERATION_AWS_ACCESS_KEY_ID", "MODERATION_AWS_SECRET_ACCESS_KEY", "MODERATION_AWS_REGION"):
            self.assertIn(name, ver)

    def test_vercel_gets_the_role_per_target_and_no_secret(self):
        env = block('resource "vercel_project_environment_variable"', "colton_games_moderation")
        self.assertIn('"MODERATION_ROLE_ARN/production" = { targets = ["production"], value = aws_iam_role.colton_games_moderation["production"].arn }', env)
        self.assertIn('"MODERATION_ROLE_ARN/preview"    = { targets = ["preview"], value = aws_iam_role.colton_games_moderation["preview"].arn }', env)
        self.assertIn("sensitive  = false", env)
        self.assertNotIn("secret", env.lower())

    def test_spend_has_an_alarm(self):
        alarm = block('resource "aws_cloudwatch_metric_alarm"', "colton_games_moderation_volume")
        self.assertIn('dimensions          = { Operation = "DetectModerationLabels" }', alarm)
        self.assertIn("alarm_actions       = [aws_sns_topic.cost_alerts.arn]", alarm)



class AiServicesOptOutTests(unittest.TestCase):
    """Pictures sent to Rekognition are kids' photos: every account is opted out of AWS keeping and using them."""

    ORG = (ROOT / "security-organization.tf").read_text()

    def test_the_root_carries_the_opt_out_policy(self):
        self.assertRegex(self.ORG, r'enabled_policy_types\s*=\s*\["AISERVICES_OPT_OUT_POLICY"')
        self.assertIn('services = { default = { opt_out_policy = { "@@assign" = "optOut" } } }', self.ORG)
        self.assertIn("target_id = aws_organizations_organization.security.roots[0].id", self.ORG)

if __name__ == "__main__":
    unittest.main(buffer=True)

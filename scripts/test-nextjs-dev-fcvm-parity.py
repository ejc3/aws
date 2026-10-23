#!/usr/bin/env python3
"""Offline tests for nextjs-dev-fcvm-parity.tf: the owner's nextjs-ct container gets fcvm's abilities through one managed
policy on nextjs-dev-role, each secret's own policy admits the role, and every member container is denied all of it."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "nextjs-dev-fcvm-parity.tf").read_text()
CT = (ROOT / "nextjs-ct-user-data.tf").read_text()


def read(name):
    return (ROOT / name).read_text()


class ParityTests(unittest.TestCase):
    def test_one_managed_policy_attached_to_nextjs_dev_role(self):
        self.assertEqual(TF.count('resource "aws_iam_policy"'), 1)
        self.assertIn("role       = aws_iam_role.nextjs_dev.name", TF)
        # Never on the retired shared box, where every account has sudo and the role's raw credentials (the rollback).
        self.assertIn("count      = local.nextjs_ct_serving ? 1 : 0", TF)
        self.assertNotIn('resource "aws_iam_role_policy" ', TF)  # inline space is near IAM's limit

    def test_every_parity_secret_admits_nextjs_dev_in_its_own_policy(self):
        self.assertIn("turso_api_token_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]", read("dev-ai-services.tf"))
        self.assertIn("claw_master_backup_key_readers = concat([aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]",
                      read("dev-ai-services.tf"))
        self.assertIn("dolphin_relay_signup_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]", read("dolphin-relay.tf"))
        self.assertIn("dolphin_films_nonprod_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]", read("dolphin-films.tf"))
        self.assertIn("[aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn, aws_iam_role.dolphin_maps_loop.arn]",
                      read("dolphin-maps-loop.tf"))
        self.assertIn("[aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]", read("claw-master-dashboard-site.tf"))
        secrets = TF.split("nextjs_dev_parity_secrets = concat(", 1)[1].split("\n  )\n", 1)[0]
        for name in ("turso_api_token", "claw_master_backup_key", "dolphin_relay_signup", "dolphin_maps_pems",
                     "claw_master_dashboard_agent_access", "dolphin_films"):
            self.assertIn(f"aws_secretsmanager_secret.{name}", secrets, name)
        self.assertIn('if startswith(k, "nonprod/")', secrets)  # never production's

    def test_members_are_denied_everything_the_policy_adds(self):
        member = CT.split("nextjs_ct_deny_member = [", 1)[1].split("\n  ]\n", 1)[0]
        self.assertIn("Resource = local.nextjs_dev_parity_secrets", member)
        self.assertIn('"secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"]', member)
        self.assertIn("Resource = [aws_ssm_parameter.constellation.arn, aws_ssm_parameter.astryx.arn]", member)  # the pointers too
        self.assertIn("NotResource = local.nextjs_ct_member_bedrock", member)
        for action in ("bedrock-mantle:*", "aws-marketplace:Subscribe", "ses:*", "codeartifact:*", "sts:GetServiceBearerToken"):
            self.assertIn(f'"{action}"', member, action)
        for res in ("aws_cloudwatch_log_group.claw_master_server.arn", "aws_s3_bucket.dolphin_maps_loop.arn"):
            self.assertIn(res, member, res)
        self.assertIn('Action   = "sts:AssumeRole"', member)  # constellation's apply role, as Dolphin's

    def test_members_keep_exactly_haiku_and_deepseek(self):
        keep = TF.split("nextjs_ct_member_bedrock = [", 1)[1].split("\n  ]\n", 1)[0]
        self.assertIn("${local.bedrock_haiku_model}", keep)
        self.assertIn("${local.bedrock_haiku_profile}", keep)
        self.assertEqual(sorted(re.findall(r"foundation-model/(deepseek[^\"]+)", keep)), ["deepseek.r1-v1:0", "deepseek.v3.2"])
        self.assertNotIn("anthropic.*", keep)

    def test_left_out_on_purpose(self):
        for absent in ("github-runner", "github-pat-ejc3", "browser_manager_tunnel_token", "claw_master_windows_pc_access",
                       "AssignIpv6Addresses", "ssm:SendCommand", "TerminateInstances"):
            self.assertNotIn(absent, TF.split("resource ", 1)[1], absent)

    def test_constellation_trusts_nextjs_dev_and_the_grant_names_only_that_role(self):
        role = read("constellation-account.tf").split('resource "aws_iam_role" "constellation_terraform" {', 1)[1].split("\n}\n", 1)[0]
        self.assertIn("Principal = { AWS = aws_iam_role.nextjs_dev.arn }", role)
        stmt = TF.split('Sid      = "ConstellationApply"', 1)[1].split("},", 1)[0]
        self.assertIn("Resource = aws_iam_role.constellation_terraform.arn", stmt)

    def test_astryx_trusts_nextjs_dev_and_the_grant_names_only_that_role(self):
        role = read("astryx-account.tf").split('resource "aws_iam_role" "astryx_terraform" {', 1)[1].split("\n}\n", 1)[0]
        self.assertIn("Principal = { AWS = aws_iam_role.nextjs_dev.arn }", role)
        stmt = TF.split('Sid      = "AstryxApply"', 1)[1].split("},", 1)[0]
        self.assertIn("Resource = aws_iam_role.astryx_terraform.arn", stmt)
        stmt = TF.split('Sid      = "AstryxPointer"', 1)[1].split("},", 1)[0]
        self.assertIn("Resource = aws_ssm_parameter.astryx.arn", stmt)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

#!/usr/bin/env python3
"""Offline contracts for the standing Browser Run token (browser-run.tf).

Owner, 2026-11-14: "What is this window thing? Kill the window. We shouldn't need back and forth with jumphost".
The standing token holds Browser Run Write only; only administration and credentials issued to fcvm-arm can read it.

Run from the repo root:  python3 -S -B scripts/test-browser-run.py
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
TF = (ROOT / "browser-run.tf").read_text()


def block(kind, name, source=TF):
    match = re.search(r'^%s "%s" \{\n.*?^\}' % (kind, name), source, re.S | re.M)
    assert match, (kind, name)
    return match.group()


class BrowserRunTests(unittest.TestCase):
    def test_the_token_is_standing_with_no_window(self):
        for gone in ("browser_run_window", "browser_run_leases", "plantimestamp", "not_before", "expires_on"):
            self.assertNotIn(gone, TF)
        self.assertNotIn("for_each", TF)

    def test_token_and_payload_only_exist_when_the_primary_arm_host_is_enabled(self):
        for kind, name in (
            ('data "cloudflare_account_api_token_permission_groups_list"', "browser_rendering_write"),
            ('resource "cloudflare_account_token"', "browser_run"),
            ('resource "aws_secretsmanager_secret_version"', "cloudflare_browser_run"),
        ):
            self.assertRegex(block(kind, name), r"count\s*= var\.enable_firecracker_instance \? 1 : 0")

    def test_token_is_pinned_to_the_exact_primary_arm_host_addresses(self):
        token = block('resource "cloudflare_account_token"', "browser_run")
        self.assertIn("request_ip = { in = concat(local.browser_run_arm_ipv4, local.browser_run_arm_ipv6) }", token)
        self.assertIn('for eip in aws_eip.firecracker_dev_next : "${eip.public_ip}/32"', TF)
        self.assertIn('for ip in eni.ipv6_addresses : "${ip}/128"', TF)
        self.assertIn('for eni in data.aws_network_interface.browser_run_arm :', TF)
        eni = block('data "aws_network_interface"', "browser_run_arm")
        self.assertIn("id    = aws_network_interface.firecracker_dev[count.index].id", eni)
        self.assertIn("depends_on = [terraform_data.firecracker_dev_host_ipv6]", eni)
        self.assertIn('length(local.browser_run_arm_ipv4) == 1 && length(local.browser_run_arm_ipv6) == 1', token)
        self.assertNotRegex(TF, r"aws_eip\.(jumpbox|x86_dev)|ipv6_prefixes|local\.dev_fleet_subnets")

    def test_the_token_holds_browser_run_write_on_this_account_only(self):
        lookup = block('data "cloudflare_account_api_token_permission_groups_list"', "browser_rendering_write")
        self.assertIn('name       = "Browser Run Write"', lookup)
        self.assertIn('scope      = "com.cloudflare.api.account"', lookup)
        self.assertIn('if g.name == "Browser Run Write"', TF)
        token = block('resource "cloudflare_account_token"', "browser_run")
        self.assertIn("provider   = cloudflare.token_minter", token)
        self.assertEqual(token.count("effect"), 1)
        self.assertEqual(token.count("permission_groups"), 1)
        self.assertIn("permission_groups = [for id in local.browser_run_groups : { id = id }]", token)
        self.assertIn('resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })',
                      token)
        self.assertIn("length(local.browser_run_groups) == 1", token)
        self.assertNotIn("cloudflare_account_token.workers_deploy", TF)
        self.assertNotIn("kitesurf", (ROOT / "workers-deploy.tf").read_text().lower())

    def test_the_dev_grant_requires_credentials_issued_to_the_managed_primary_arm(self):
        self.assertIn("browser_run_dev_role = aws_iam_role.dev_server.arn", TF)
        self.assertIn("browser_run_arm_arns = aws_instance.firecracker_dev[*].arn", TF)
        policy = block('resource "aws_secretsmanager_secret_policy"', "cloudflare_browser_run")
        self.assertIn("block_public_policy = true", policy)
        allow_sid = 'Sid       = "ReadFromPrimaryArmCredentials"'
        deny_sid = 'Sid       = "DenyDevRoleWithoutPrimaryArmCredentials"'
        self.assertIn(allow_sid, policy)
        self.assertIn(deny_sid, policy)
        allow = policy[policy.index(allow_sid):policy.index(deny_sid)]
        self.assertIn('Effect    = "Allow"', allow)
        self.assertIn("Principal = { AWS = local.browser_run_dev_role }", allow)
        self.assertIn('Action    = "secretsmanager:GetSecretValue"', allow)
        self.assertIn('Condition = { ArnEquals = { "ec2:SourceInstanceARN" = instance_arn } }', allow)
        self.assertEqual(policy.count('for instance_arn in local.browser_run_arm_arns'), 2)
        self.assertNotRegex(TF, r"aws_iam_role\.(nextjs_dev|dev_ebs_only)")

    def test_wrong_or_missing_instance_provenance_cannot_be_allowed_by_an_identity_policy(self):
        policy = block('resource "aws_secretsmanager_secret_policy"', "cloudflare_browser_run")
        sid = 'Sid       = "DenyDevRoleWithoutPrimaryArmCredentials"'
        self.assertIn(sid, policy)
        deny = policy[policy.index(sid):]
        self.assertIn('Effect    = "Deny"', deny)
        self.assertIn('Principal = "*"', deny)
        self.assertIn('Action    = local.browser_run_read_actions', deny)
        self.assertIn('ArnEquals            = { "aws:PrincipalArn" = local.browser_run_dev_role }', deny)
        self.assertIn('ArnNotEqualsIfExists = { "ec2:SourceInstanceARN" = instance_arn }', deny)
        self.assertIn('"secretsmanager:GetSecretValue",\n    "secretsmanager:BatchGetSecretValue",', TF)

    def test_administration_stays_exempt_and_a_missing_arm_fails_closed(self):
        policy = block('resource "aws_secretsmanager_secret_policy"', "cloudflare_browser_run")
        deny_sid = 'Sid       = "DenyOtherPrincipals"'
        allow_sid = 'Sid       = "ReadFromPrimaryArmCredentials"'
        self.assertIn(deny_sid, policy)
        self.assertIn(allow_sid, policy)
        deny = policy[policy.index(deny_sid):policy.index(allow_sid)]
        self.assertIn('Effect    = "Deny"', deny)
        self.assertIn('Principal = "*"', deny)
        self.assertIn('Action    = local.browser_run_read_actions', deny)
        self.assertIn('ArnNotLike = {', deny)
        self.assertIn('"aws:PrincipalArn" = concat(', deny)
        self.assertIn('local.games_mp_admin_principals,', deny)
        self.assertIn('length(local.browser_run_arm_arns) == 1 ? [local.browser_run_dev_role] : []', deny)

    def test_the_payload_is_written_after_the_policy(self):
        payload = block('resource "aws_secretsmanager_secret_version"', "cloudflare_browser_run")
        self.assertIn("api_token  = cloudflare_account_token.browser_run[0].value", payload)
        self.assertIn("account_id = var.cloudflare_account_id", payload)
        self.assertIn("depends_on = [aws_secretsmanager_secret_policy.cloudflare_browser_run]", payload)


if __name__ == "__main__":
    unittest.main(buffer=True)

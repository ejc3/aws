#!/usr/bin/env python3
"""Offline contracts for the standing Browser Run token (browser-run.tf).

Owner, 2026-11-14: "What is this window thing? Kill the window. We shouldn't need back and forth with jumphost".
The token is minted on every apply, holds Browser Run Write and nothing else, and is readable by dev-server-role.

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
        self.assertNotRegex(TF, r"(?m)^\s*(for_each|count)\s*=")
        for kind, name in (
            ('data "cloudflare_account_api_token_permission_groups_list"', "browser_rendering_write"),
            ('resource "cloudflare_account_token"', "browser_run"),
            ('resource "aws_secretsmanager_secret_version"', "cloudflare_browser_run"),
        ):
            block(kind, name)

    def test_the_token_is_not_pinned_to_an_address(self):
        token = block('resource "cloudflare_account_token"', "browser_run")
        self.assertNotRegex(token, r"(?m)^  condition\s*=")
        self.assertNotIn("request_ip", TF)
        self.assertNotIn("aws_eip.jumpbox", TF)

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

    def test_dev_server_role_and_administration_read_it_and_no_one_else(self):
        self.assertIn("browser_run_readers = [aws_iam_role.dev_server.arn]", TF)
        policy = block('resource "aws_secretsmanager_secret_policy"', "cloudflare_browser_run")
        self.assertIn("block_public_policy = true", policy)
        allow = policy[policy.index('Sid       = "MetalBoxesRead"'):policy.index('Sid       = "OnlyAdministrationAndMetalBoxes"')]
        self.assertIn('Effect    = "Allow"', allow)
        self.assertIn("Principal = { AWS = local.browser_run_readers }", allow)
        self.assertIn('Action    = "secretsmanager:GetSecretValue"', allow)
        deny = policy[policy.index('Sid       = "OnlyAdministrationAndMetalBoxes"'):]
        self.assertIn('Effect    = "Deny"', deny)
        self.assertIn('Principal = "*"', deny)
        self.assertIn('Action    = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"]', deny)
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.browser_run_readers)', deny)
        self.assertNotRegex(TF, r"aws_iam_role\.(nextjs_dev|dev_ebs_only)")

    def test_the_payload_is_written_after_the_policy(self):
        payload = block('resource "aws_secretsmanager_secret_version"', "cloudflare_browser_run")
        self.assertIn("api_token  = cloudflare_account_token.browser_run.value", payload)
        self.assertIn("account_id = var.cloudflare_account_id", payload)
        self.assertIn("depends_on = [aws_secretsmanager_secret_policy.cloudflare_browser_run]", payload)


if __name__ == "__main__":
    unittest.main(buffer=True)

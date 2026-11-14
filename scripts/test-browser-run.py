#!/usr/bin/env python3
"""Offline contracts for the opt-in administrator Browser Run credential."""
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
    def test_default_and_expired_windows_do_not_mint_or_store_a_payload(self):
        window = block("variable", "browser_run_window")
        self.assertIn("default = null", window)
        self.assertIn('timeadd(var.browser_run_window.starts_at, "1h")', window)
        self.assertIn("timecmp(var.browser_run_window.starts_at, var.browser_run_window.expires_at) < 0", window)
        self.assertIn("var.browser_run_window == null ? false", TF)
        self.assertIn("timecmp(plantimestamp(), var.browser_run_window.expires_at) < 0", TF)
        self.assertIn('"${var.browser_run_window.starts_at}/${var.browser_run_window.expires_at}"', TF)
        for kind, name in (
            ('data "cloudflare_account_api_token_permission_groups_list"', "browser_rendering_write"),
            ('resource "cloudflare_account_token"', "browser_run"),
            ('resource "aws_secretsmanager_secret_version"', "cloudflare_browser_run"),
        ):
            self.assertRegex(block(kind, name), r"for_each\s*=\s*local\.browser_run_leases")

    def test_only_administrators_may_retrieve_the_secret(self):
        policy = block('resource "aws_secretsmanager_secret_policy"', "cloudflare_browser_run")
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('Principal = "*"', policy)
        self.assertIn(
            'Action    = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"]', policy
        )
        self.assertIn('"aws:PrincipalArn" = local.games_mp_admin_principals', policy)
        self.assertNotRegex(TF, r"aws_iam_role\.(dev_server|nextjs_dev|dev_ebs_only)")
        payload = block('resource "aws_secretsmanager_secret_version"', "cloudflare_browser_run")
        self.assertIn("depends_on = [aws_secretsmanager_secret_policy.cloudflare_browser_run]", payload)

    def test_token_is_time_and_ip_bound_with_exactly_one_permission(self):
        lookup = block('data "cloudflare_account_api_token_permission_groups_list"', "browser_rendering_write")
        token = block('resource "cloudflare_account_token"', "browser_run")
        self.assertIn('name       = "Browser Run Write"', lookup)
        self.assertIn('scope      = "com.cloudflare.api.account"', lookup)
        self.assertIn("provider   = cloudflare.token_minter", token)
        self.assertIn("not_before = each.value.starts_at", token)
        self.assertIn("expires_on = each.value.expires_at", token)
        self.assertIn('request_ip = { in = [for ip in aws_eip.jumpbox : "${ip.public_ip}/32"] }', token)
        self.assertIn("length(aws_eip.jumpbox) == 1", token)
        self.assertIn('if g.name == "Browser Run Write"]) == 1', token)
        self.assertEqual(token.count("permission_groups = ["), 1)
        self.assertNotIn("cloudflare_account_token.workers_deploy", TF)
        self.assertNotIn("kitesurf", (ROOT / "workers-deploy.tf").read_text().lower())


if __name__ == "__main__":
    unittest.main(buffer=True)

#!/usr/bin/env python3
"""The dev boxes' Cloudflare deploy token (cloudflare-dev-deploy.tf): three named permissions on one account, readable
by the administration set and dev-server-role only (never nextjs-dev), and checked against the live group names."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "cloudflare-dev-deploy.tf").read_text()
ALL_TF = "\n".join(p.read_text() for p in ROOT.glob("*.tf"))


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


class DevDeployTokenTests(unittest.TestCase):
    def test_exactly_three_permission_groups_pinned_by_name(self):
        groups = dict(re.findall(r'^\s+"([A-Za-z0-9 ]+)"\s+=\s+"([0-9a-f]{32})"$', code(TF), re.M))
        self.assertEqual(groups, {
            "Workers Scripts Write": "e086da7e2179491d91ee5f35b3ca210a",
            "D1 Write": "09b2857d1c31407795e75e3fed8617a1",
            "Account Settings Read": "c1fde68c7bcc44588cbb6ddbc16d6480",
        })
        self.assertEqual(code(TF).count("permission_groups"), 1)
        self.assertIn('resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })', TF)

    def test_each_pinned_id_is_checked_against_its_name_before_the_token_is_made(self):
        self.assertIn('data "cloudflare_account_permission_group" "dev_deploy"', TF)
        self.assertIn("permission_group_id = each.value", TF)
        self.assertIn("group.name == name", TF)

    def test_minted_through_the_minting_provider_never_by_api_call(self):
        self.assertIn('resource "cloudflare_account_token" "dev_deploy"', TF)
        self.assertIn("provider   = cloudflare.token_minter", TF)
        self.assertNotRegex(code(TF), r"local-exec|provisioner|curl|api\.cloudflare\.com")
        self.assertIn("secret_string = cloudflare_account_token.dev_deploy.value", TF)

    def test_only_administration_and_the_metal_boxes_may_read_it(self):
        self.assertIn('name                    = "cloudflare-dev-deploy-token"', TF)
        self.assertIn("cloudflare_dev_deploy_readers = [aws_iam_role.dev_server.arn]", TF)
        self.assertIn("concat(local.games_mp_admin_principals, local.cloudflare_dev_deploy_readers)", TF)
        self.assertIn('Effect    = "Deny"', TF)
        self.assertNotIn("nextjs_dev", code(TF))
        self.assertIn("role       = aws_iam_role.dev_server.name", TF)

    def test_the_site_repos_token_stays_administration_only(self):
        workers = code((ROOT / "workers-deploy.tf").read_text())
        self.assertNotIn("dev_server", workers)
        self.assertNotIn("nextjs_dev", workers)

    def test_nothing_reads_the_stored_value_back_into_terraform(self):
        self.assertNotRegex(ALL_TF, r'(data|ephemeral)\s+"aws_secretsmanager_secret_version"\s+"[^"]*dev_deploy')


if __name__ == "__main__":
    unittest.main()

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
    def test_exactly_three_permission_groups_named_on_one_account(self):
        self.assertIn('cloudflare_dev_deploy_groups = ["Workers Scripts Write", "D1 Write", "Account Settings Read"]', TF)
        self.assertNotRegex(code(TF), r'"[0-9a-f]{32}"', "no permission group id is pinned or guessed")
        self.assertEqual(code(TF).count("permission_groups ="), 1)
        self.assertIn('resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })', TF)

    def test_ids_come_from_the_token_endpoint_the_minting_token_may_read(self):
        # /accounts/<id>/tokens/permission_groups (API Tokens Read), not /accounts/<id>/iam/permission_groups.
        self.assertIn('data "cloudflare_account_api_token_permission_groups_list" "dev_deploy"', TF)
        self.assertNotIn("cloudflare_account_permission_group", code(TF).replace("cloudflare_account_api_token_permission_groups_list", ""))
        self.assertIn('name       = replace(each.key, " ", "%20")', TF)
        self.assertIn("one(data.cloudflare_account_api_token_permission_groups_list.dev_deploy[name].result).id", TF)

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

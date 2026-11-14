#!/usr/bin/env python3
"""The Browser Run token (browser-run.tf): Browser Rendering Write on this account only, minted through the token
minter from a permission group looked up by exact name, stored with the account id in a container that only
administration and dev-server-role may read."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "browser-run.tf").read_text()
CODE = "\n".join(line for line in TF.splitlines() if not line.lstrip().startswith("#"))


def resource(kind, name):
    m = re.search(r'^(resource|data) "%s" "%s" \{\n.*?^\}' % (kind, name), CODE, re.S | re.M)
    assert m, (kind, name)
    return m.group()


class TokenTests(unittest.TestCase):
    def test_one_permission_group_by_exact_name_on_the_account(self):
        lookup = resource("cloudflare_account_api_token_permission_groups_list", "browser_rendering_write")
        self.assertIn('name       = "Browser Rendering Write"', lookup)
        self.assertIn('scope      = "com.cloudflare.api.account"', lookup)
        self.assertIn("provider   = cloudflare.token_minter", lookup)
        self.assertIn('if g.name == "Browser Rendering Write"', CODE)
        token = resource("cloudflare_account_token", "browser_run")
        self.assertIn("provider   = cloudflare.token_minter", token)
        self.assertIn("length(local.browser_run_permission_group_ids) == 1", token)
        self.assertEqual(token.count("effect            ="), 1, "one policy")
        self.assertIn('jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })', token)
        self.assertNotIn("zone", token)
        self.assertNotIn("expires_on", token, "long-lived, rotated by -replace")

    def test_no_other_permission_group_id_is_named(self):
        self.assertEqual(re.findall(r'"[0-9a-f]{32}"', CODE), [])


class ReaderTests(unittest.TestCase):
    def test_administration_and_the_metal_boxes_read_it_and_no_one_else(self):
        policy = resource("aws_secretsmanager_secret_policy", "cloudflare_browser_run")
        self.assertIn("Principal = { AWS = aws_iam_role.dev_server.arn }", policy)
        self.assertIn(
            '"aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn])', policy)
        self.assertNotIn("nextjs_dev", CODE)
        self.assertNotIn("dev_ebs_only", CODE)

    def test_the_secret_holds_the_account_id_and_the_token(self):
        version = resource("aws_secretsmanager_secret_version", "cloudflare_browser_run")
        self.assertIn("account_id = var.cloudflare_account_id", version)
        self.assertIn("api_token  = cloudflare_account_token.browser_run.value", version)


if __name__ == "__main__":
    unittest.main()

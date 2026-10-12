#!/usr/bin/env python3
"""dolphin-relay.tf: the relay's deploy token reaches only a main-only GitHub environment, never a dev box; the invite
token is generated here, readable by administration and the metal boxes, and handed to the same environment."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-relay.tf").read_text()
D1_WRITE = "09b2857d1c31407795e75e3fed8617a1"


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, name):
    m = re.search(r'^(?:resource|data|ephemeral|provider) "%s"(?: "%s")? \{\n(.*?)^\}' % (kind, name), TF, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class DeployTokenTests(unittest.TestCase):
    def test_token_is_minted_with_exactly_the_three_permissions_on_the_account(self):
        tok = block("cloudflare_account_token", "dolphin_relay_deploy")
        self.assertIn("provider   = cloudflare.token_minter", tok)
        self.assertEqual(re.findall(r"\{ id = local\.([a-z0-9_]+) \}", tok),
                         ["workers_scripts_write", "d1_write", "account_settings_read"])
        self.assertIn('d1_write = "%s"' % D1_WRITE, TF)
        self.assertIn('"com.cloudflare.api.account.${var.cloudflare_account_id}" = "*"', tok)

    def test_no_dev_box_can_read_the_deploy_token(self):
        pol = block("aws_secretsmanager_secret_policy", "dolphin_relay_deploy_token")
        self.assertIn('"aws:PrincipalArn" = local.games_mp_admin_principals }', pol)
        self.assertNotIn("dev_server", pol)
        self.assertNotIn("nextjs_dev", pol)
        grants = [b for b in re.findall(r'resource "aws_iam_role_policy_attachment" "[^"]+" \{(.*?)\n\}', TF, re.S)]
        self.assertTrue(all("signup_read" in g for g in grants), grants)

    def test_environment_admits_main_only_and_holds_the_token(self):
        env = block("github_repository_environment", "dolphin_relay")
        self.assertIn("protected_branches     = false", env)
        self.assertIn("custom_branch_policies = true", env)
        self.assertIn('branch_pattern = "main"', block("github_repository_environment_deployment_policy", "dolphin_relay_main"))
        sec = block("github_actions_environment_secret", "dolphin_relay_cloudflare_token")
        self.assertIn('secret_name     = "CLOUDFLARE_API_TOKEN"', sec)
        self.assertIn("cloudflare_account_token.dolphin_relay_deploy.value", sec)
        self.assertNotRegex(code(TF), r'resource "github_actions_secret"')  # never repository-wide

    def test_github_app_key_never_reaches_state(self):
        self.assertIn('ephemeral "aws_secretsmanager_secret_version" "dolphin_relay_github_app"', TF)
        self.assertNotRegex(code(TF), r'data "aws_secretsmanager_secret_version"')


class SignupTokenTests(unittest.TestCase):
    def test_signup_token_readers_are_administration_and_the_metal_boxes(self):
        self.assertIn("dolphin_relay_signup_readers = [aws_iam_role.dev_server.arn]", TF)
        pol = block("aws_secretsmanager_secret_policy", "dolphin_relay_signup")
        self.assertIn("concat(local.games_mp_admin_principals, local.dolphin_relay_signup_readers)", pol)
        self.assertIn("role       = aws_iam_role.dev_server.name", block("aws_iam_role_policy_attachment", "dolphin_relay_signup_read"))
        doc = block("aws_iam_policy_document", "dolphin_relay_signup_read")
        self.assertEqual(re.findall(r'actions\s*=\s*\[(.*?)\]', doc), ['"secretsmanager:GetSecretValue"'])

    def test_the_worker_gets_the_same_token_from_the_environment(self):
        sec = block("github_actions_environment_secret", "dolphin_relay_signup")
        self.assertIn('secret_name     = "SIGNUP_TOKEN"', sec)
        self.assertIn("random_password.dolphin_relay_signup.result", sec)
        self.assertIn("random_password.dolphin_relay_signup.result", block("aws_secretsmanager_secret_version", "dolphin_relay_signup"))


if __name__ == "__main__":
    unittest.main(verbosity=2)

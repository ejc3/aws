#!/usr/bin/env python3
"""dolphin-family.tf: the deploy token has exactly Workers Scripts Write, D1 Write and Account Settings Read, reaches
only a main-only GitHub environment and administrators, and never a dev box; runner-app.tf serves the repository behind
its own token gate, starting closed."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-family.tf").read_text()
RUNNER_APP = (ROOT / "runner-app.tf").read_text()
RUNNER_REPOS = (ROOT / "runner-repos.tf").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, name, text=TF):
    m = re.search(r'^(?:resource|data|ephemeral|provider|variable) "%s"(?: "%s")? \{\n(.*?)^\}' % (kind, name), text, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class DeployTokenTests(unittest.TestCase):
    def test_token_is_minted_with_exactly_the_three_permissions_on_the_account(self):
        tok = block("cloudflare_account_token", "dolphin_family_deploy")
        self.assertIn("provider   = cloudflare.token_minter", tok)
        self.assertEqual(re.findall(r"\{ id = local\.([a-z0-9_]+) \}", tok), ["workers_scripts_write", "d1_write", "account_settings_read"])
        self.assertIn('"com.cloudflare.api.account.${var.cloudflare_account_id}" = "*"', tok)

    def test_no_dev_box_can_read_the_deploy_token(self):
        pol = block("aws_secretsmanager_secret_policy", "dolphin_family_deploy_token")
        self.assertIn('"aws:PrincipalArn" = local.games_mp_admin_principals }', pol)
        self.assertNotIn("dev_server", code(TF))
        self.assertNotIn("nextjs_dev", code(TF))
        self.assertNotRegex(code(TF), r'resource "aws_iam_')

    def test_environment_admits_main_only_and_holds_the_token(self):
        env = block("github_repository_environment", "dolphin_family")
        self.assertIn("protected_branches     = false", env)
        self.assertIn("custom_branch_policies = true", env)
        self.assertIn('branch_pattern = "main"', block("github_repository_environment_deployment_policy", "dolphin_family_main"))
        sec = block("github_actions_environment_secret", "dolphin_family_cloudflare_token")
        self.assertIn('secret_name     = "CLOUDFLARE_API_TOKEN"', sec)
        self.assertIn("cloudflare_account_token.dolphin_family_deploy.value", sec)
        self.assertNotRegex(code(TF), r'resource "github_actions_secret"')  # never repository-wide


class RunnerTests(unittest.TestCase):
    def test_served_with_the_dolphin_label_and_its_own_cap(self):
        self.assertIn('"dolphin-labs-hq/dolphin-family" = { label = "dolphin", max = 2, alarm = "dolphin-family" }', RUNNER_APP)
        self.assertIn('"dolphin-labs-hq/dolphin-family"', RUNNER_REPOS)

    def test_its_token_gate_starts_closed(self):
        gate = re.search(r'^variable "dolphin_family_token_ready" \{\n(.*?)^\}', RUNNER_APP, re.M | re.S).group(1)
        self.assertIn("default     = false", gate)
        self.assertIn('"dolphin-labs-hq/dolphin-family" = var.dolphin_family_token_ready', RUNNER_APP)
        hook = re.search(r'resource "github_repository_webhook" "runner_app_dolphin_family" \{\n(.*?)^\}', RUNNER_APP, re.M | re.S).group(1)
        self.assertIn("count      = local.dolphin_family_webhook ? 1 : 0", hook)
        self.assertIn('repository = "dolphin-family"', hook)


if __name__ == "__main__":
    unittest.main()

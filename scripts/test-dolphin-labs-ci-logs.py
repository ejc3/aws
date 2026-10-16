#!/usr/bin/env python3
"""dolphin-labs-ci-logs.tf: dolphin-labs' post-deploy check gets a token that can only read Workers Logs, and only a job
on that repository's main branch can read it."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-labs-ci-logs.tf").read_text()
OBSERVABILITY = (ROOT / "cloudflare-observability.tf").read_text()
WORKERS_OBSERVABILITY_READ = "66c1ed49f4ed46098b75696a6d4ee3c9"


def block(kind, name):
    m = re.search(r'^(?:resource|provider) "%s"(?: "%s")? \{\n(.*?)^\}' % (kind, name), TF, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class TokenTests(unittest.TestCase):
    def test_token_reads_workers_logs_and_nothing_else(self):
        tok = block("cloudflare_account_token", "dolphin_labs_ci_logs")
        self.assertIn("provider   = cloudflare.token_minter", tok)
        self.assertEqual(re.findall(r"\{ id = ([^ ]+) \}", tok),
                         ["local.cloudflare_observability_account_groups.workers_observability_read"])
        self.assertIn('workers_observability_read = "%s"' % WORKERS_OBSERVABILITY_READ, OBSERVABILITY)
        self.assertEqual(tok.count("effect"), 1)
        self.assertIn('"com.cloudflare.api.account.${var.cloudflare_account_id}" = "*"', tok)
        self.assertNotIn("zone", tok)

    def test_no_copy_for_a_dev_box(self):
        self.assertNotIn("aws_secretsmanager_secret", TF)
        self.assertNotIn("aws_iam", TF)


class GitHubTests(unittest.TestCase):
    def test_secret_lives_in_a_main_only_environment(self):
        env = block("github_repository_environment", "dolphin_labs_production_check")
        self.assertIn("custom_branch_policies = true", env)
        self.assertIn("protected_branches     = false", env)
        policy = block("github_repository_environment_deployment_policy", "dolphin_labs_production_check_main")
        self.assertIn('branch_pattern = "main"', policy)
        sec = block("github_actions_environment_secret", "dolphin_labs_ci_logs")
        self.assertIn('secret_name     = "CLOUDFLARE_OBSERVABILITY_TOKEN"', sec)
        self.assertIn("environment     = github_repository_environment.dolphin_labs_production_check.environment", sec)
        self.assertIn("cloudflare_account_token.dolphin_labs_ci_logs.value", sec)
        self.assertNotIn("github_actions_secret", re.sub(r"github_actions_environment_secret", "", TF))

    def test_every_github_resource_uses_the_app_provider(self):
        resources = re.findall(r'^resource "github_[a-z_]+" "[^"]+" \{\n(.*?)^\}', TF, re.M | re.S)
        self.assertEqual(len(resources), 3)
        for body in resources:
            self.assertIn("provider", body)
            self.assertIn("github.dolphin_labs_ci", body)
        self.assertIn('repository  = local.dolphin_labs_ci_repository', block("github_repository_environment", "dolphin_labs_production_check"))
        self.assertIn('dolphin_labs_ci_repository  = "dolphin-labs"', TF)
        provider = re.search(r'^provider "github" \{\n(.*?)^\}', TF, re.M | re.S).group(1)
        self.assertIn('alias = "dolphin_labs_ci"', provider)
        self.assertIn('owner = "dolphin-labs-hq"', provider)


if __name__ == "__main__":
    unittest.main()

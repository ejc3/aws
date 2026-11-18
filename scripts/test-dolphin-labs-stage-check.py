#!/usr/bin/env python3
"""dolphin-labs-stage-check.tf: dolphin-labs' staging check gets an Access service token that the staging-Workers
application alone admits, held as secrets of the main-only `cloudflare-deploy` environment, with no copy elsewhere."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-labs-stage-check.tf").read_text()
STAGE = (ROOT / "workers-stage.tf").read_text()
EVERYTHING = "\n".join(path.read_text() for path in sorted(ROOT.glob("*.tf")))


def block(text, kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n(.*?)^\}' % (kind, name), text, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class AccessTests(unittest.TestCase):
    def test_a_service_token_of_its_own_admitted_by_a_non_identity_policy(self):
        block(TF, "cloudflare_zero_trust_access_service_token", "dolphin_labs_stage_check")
        policy = block(TF, "cloudflare_zero_trust_access_policy", "dolphin_labs_stage_check")
        self.assertIn('decision         = "non_identity"', policy)
        self.assertEqual(re.findall(r"token_id = (\S+)", policy),
                         ["cloudflare_zero_trust_access_service_token.dolphin_labs_stage_check.id"])

    def test_only_the_staging_workers_application_admits_it(self):
        policy_id = "cloudflare_zero_trust_access_policy.dolphin_labs_stage_check.id"
        app = block(STAGE, "cloudflare_zero_trust_access_application", "workers_stage")
        self.assertIn(policy_id, app)
        self.assertEqual(EVERYTHING.count(policy_id), 1)
        self.assertEqual(EVERYTHING.count("cloudflare_zero_trust_access_service_token.dolphin_labs_stage_check.id"), 1)
        # Precedences on the application stay distinct.
        precedences = re.findall(r"precedence = (\d+)", app)
        self.assertEqual(len(precedences), len(set(precedences)))


class GitHubTests(unittest.TestCase):
    def test_both_halves_are_secrets_of_the_main_only_deploy_environment(self):
        for name, secret, value in (("dolphin_labs_stage_check_id", "STAGE_ACCESS_CLIENT_ID", "client_id"),
                                    ("dolphin_labs_stage_check_secret", "STAGE_ACCESS_CLIENT_SECRET", "client_secret")):
            sec = block(TF, "github_actions_environment_secret", name)
            self.assertIn("provider        = github.dolphin_labs_ci", sec)
            self.assertIn('secret_name     = "%s"' % secret, sec)
            self.assertIn("environment     = github_repository_environment.dolphin_labs_cloudflare_deploy.environment", sec)
            self.assertIn("cloudflare_zero_trust_access_service_token.dolphin_labs_stage_check.%s" % value, sec)

    def test_no_copy_for_a_dev_box(self):
        self.assertNotIn("aws_secretsmanager_secret", TF)
        self.assertNotIn("aws_iam", TF)
        self.assertNotIn("output", TF)


if __name__ == "__main__":
    unittest.main()

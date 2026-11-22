#!/usr/bin/env python3
"""constellation-stage-smoke.tf: constellation's lights-stage smoke gets an Access service token that only the exact
lights-stage hostname admits, held as secrets of constellation's main-only `cloudflare-deploy` environment, with no
copy elsewhere."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "constellation-stage-smoke.tf").read_text()
STAGE = (ROOT / "workers-stage.tf").read_text()
EVERYTHING = "\n".join(path.read_text() for path in sorted(ROOT.glob("*.tf")))
TOKEN = "cloudflare_zero_trust_access_service_token.lights_stage_smoke"
POLICY = "cloudflare_zero_trust_access_policy.lights_stage_smoke.id"
ENV = 'environment     = github_repository_environment.workers_deploy_ejc3["constellation"].environment'


def block(text, kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n(.*?)^\}' % (kind, name), text, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class AccessTests(unittest.TestCase):
    def test_a_service_token_of_its_own_admitted_by_a_non_identity_policy(self):
        block(TF, "cloudflare_zero_trust_access_service_token", "lights_stage_smoke")
        policy = block(TF, "cloudflare_zero_trust_access_policy", "lights_stage_smoke")
        self.assertIn('decision         = "non_identity"', policy)
        self.assertEqual(re.findall(r"token_id = (\S+)", policy), [TOKEN + ".id"])

    def test_only_the_exact_lights_stage_hostname_admits_the_ci_token(self):
        app = block(TF, "cloudflare_zero_trust_access_application", "lights_stage_smoke")
        self.assertIn("domain           = local.lights_stage_hostname", app)
        self.assertRegex(TF, r'lights_stage_hostname = "lights-stage\.\$\{cloudflare_workers_subdomain\.cc_games\.subdomain\}\.workers\.dev"')
        self.assertNotRegex(app, r"\b(destinations|self_hosted_domains)\s*=")
        self.assertEqual(EVERYTHING.count(POLICY), 1)
        self.assertEqual(EVERYTHING.count(TOKEN + ".id"), 1)
        shared = block(STAGE, "cloudflare_zero_trust_access_application", "workers_stage")
        policy = r"id\s*=\s*(cloudflare_zero_trust_access_policy\.\w+\.id),?\s+precedence\s*=\s*(\d+)"
        self.assertNotIn(POLICY, shared)
        self.assertEqual(re.findall(policy, app), re.findall(policy, shared) + [(POLICY, "4")])
        self.assertIn('"lights-stage"', STAGE)


class GitHubTests(unittest.TestCase):
    def test_both_halves_are_secrets_of_the_main_only_deploy_environment(self):
        for name, secret, value in (("lights_stage_smoke_id", "STAGE_ACCESS_CLIENT_ID", "client_id"),
                                    ("lights_stage_smoke_secret", "STAGE_ACCESS_CLIENT_SECRET", "client_secret")):
            sec = block(TF, "github_actions_environment_secret", name)
            self.assertIn("provider        = github.agents_md", sec)
            self.assertIn('repository      = "constellation"', sec)
            self.assertIn('secret_name     = "%s"' % secret, sec)
            self.assertIn(ENV, sec)
            reference = f"{TOKEN}.{value}"
            self.assertRegex(sec, rf"(?m)^  plaintext_value\s*= {re.escape(reference)}$")
            self.assertEqual(EVERYTHING.count(reference), 1)
            self.assertIn("depends_on = [cloudflare_zero_trust_access_application.lights_stage_smoke]", sec)
        self.assertIn('"constellation"', re.search(r"workers_deploy_environments_ejc3 = toset\(\[[^\]]*\]\)", EVERYTHING).group(0))

    def test_the_url_is_a_variable_of_the_same_environment(self):
        var = block(TF, "github_actions_environment_variable", "lights_stage_smoke_url")
        self.assertIn('variable_name = "LIGHTS_STAGE_URL"', var)
        self.assertIn('value         = "https://${local.lights_stage_hostname}/"', var)

    def test_no_copy_for_a_dev_box(self):
        self.assertNotIn("aws_secretsmanager_secret", TF)
        self.assertNotIn("aws_iam", TF)
        self.assertNotIn("output", TF)


if __name__ == "__main__":
    unittest.main(verbosity=1)

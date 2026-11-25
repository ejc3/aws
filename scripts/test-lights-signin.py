#!/usr/bin/env python3
"""lights-signin.tf: the Lights phone's sign-in skips Access on /api/v1/* of lights-stage only, where the Worker's own
token is the gate; /native/login stays behind Access; the token key goes only into the Lights Worker's staging
container."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "lights-signin.tf").read_text()
VISION = (ROOT / "lights-vision.tf").read_text()
EVERYTHING = "\n".join(path.read_text() for path in sorted(ROOT.glob("*.tf")))


def block(text, kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n(.*?)^\}' % (kind, name), text, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class AccessTests(unittest.TestCase):
    def test_only_api_v1_on_the_lights_host_skips_access(self):
        self.assertRegex(TF, r'lights_stage_bearer_paths = \["\$\{local\.lights_stage_hostname\}/api/v1/\*"\]')
        app = block(TF, "cloudflare_zero_trust_access_application", "lights_stage_bearer_paths")
        self.assertIn("domain       = local.lights_stage_bearer_paths[0]", app)
        self.assertIn('destinations = [for uri in local.lights_stage_bearer_paths : { type = "public", uri = uri }]', app)
        self.assertEqual(re.findall(r"id\s*=\s*(cloudflare_zero_trust_access_policy\.\w+\[0\]\.id)", app),
                         ["cloudflare_zero_trust_access_policy.lights_stage_bearer_paths[0].id"])
        policy = block(TF, "cloudflare_zero_trust_access_policy", "lights_stage_bearer_paths")
        self.assertIn('decision   = "bypass"', policy)
        # The bypass is attached to this application only, and nothing names /native here.
        self.assertEqual(EVERYTHING.count("cloudflare_zero_trust_access_policy.lights_stage_bearer_paths[0].id"), 1)
        self.assertNotIn("native", TF.split("locals {")[1])


class OrderTests(unittest.TestCase):
    def test_the_bypass_waits_for_the_workers_access_check(self):
        # Step 1 of lights-signin.tf's ORDER: nothing skips Access until the Worker verifies it.
        self.assertRegex(TF, r"(?m)^  lights_phone_bypass\s*= false$")
        for kind in ("cloudflare_zero_trust_access_policy", "cloudflare_zero_trust_access_application"):
            self.assertIn("count      = local.lights_phone_bypass ? 1 : 0".replace("      ", " " * (6 if kind.endswith("policy") else 8)),
                          block(TF, kind, "lights_stage_bearer_paths"))


class KeyTests(unittest.TestCase):
    def test_the_token_key_goes_only_into_the_lights_staging_container(self):
        self.assertIn("length = 32", block(TF, "random_bytes", "lights_token_key"))
        self.assertEqual(EVERYTHING.count("random_bytes.lights_token_key.hex"), 1)
        version = block(VISION, "aws_secretsmanager_secret_version", "lights_vision_worker")
        self.assertRegex(version, r"LIGHTS_TOKEN_KEY\s*= random_bytes\.lights_token_key\.hex")
        self.assertRegex(version, r"ACCESS_TEAM_DOMAIN\s*= local\.browser_manager_issuer")
        # The audience of the application in front of /native/login: the lights-stage hostname's.
        self.assertRegex(version, r"ACCESS_AUD\s*= cloudflare_zero_trust_access_application\.lights_stage_smoke\.aud")
        self.assertIn('container = "workers-stage/lights/terraform"', VISION)
        self.assertNotRegex(EVERYTHING, r'output\s+"[^"]*lights[^"]*token')


if __name__ == "__main__":
    unittest.main(buffer=True)

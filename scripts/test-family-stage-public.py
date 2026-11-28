#!/usr/bin/env python3
"""family-stage-public.tf: on Family's custom host, only /privacy*, /support* and the compiled assets bypass Access;
the rest of the host has the staging Workers' own policies; the custom domain waits for both applications."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "family-stage-public.tf").read_text()
STAGE = (ROOT / "workers-stage.tf").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(text, kind, name):
    head = f'resource "{kind}" "{name}" {{'
    body = code(text)[code(text).index(head):]
    depth = 0
    for i, ch in enumerate(body):
        depth += (ch == "{") - (ch == "}")
        if ch == "}" and depth == 0:
            return body[:i + 1]


def policies(app):
    return re.findall(r"id\s*= (cloudflare_zero_trust_access_policy\.\w+)\.id", app)


class FamilyPublicPagesTests(unittest.TestCase):
    def test_only_the_two_pages_and_compiled_assets_bypass(self):
        self.assertIn('family_stage_public_hostname = "family.dolphin-labs.dev"', code(TF))
        paths = re.search(r"family_stage_public_paths\s*= \[([^\]]*)\]", code(TF)).group(1)
        self.assertEqual(re.findall(r'"([^"]+)"', paths), ["privacy*", "support*", "_next/static/*"])
        bypass = block(TF, "cloudflare_zero_trust_access_policy", "family_stage_public_pages")
        self.assertIn('decision   = "bypass"', bypass)
        app = block(TF, "cloudflare_zero_trust_access_application", "family_stage_public_pages")
        self.assertIn('for path in local.family_stage_public_paths : { type = "public", uri = "${local.family_stage_public_hostname}/${path}" }', app)
        self.assertEqual(policies(app), ["cloudflare_zero_trust_access_policy.family_stage_public_pages"])
        # The bypass policy is attached to nothing else.
        self.assertEqual(code(TF).count("cloudflare_zero_trust_access_policy.family_stage_public_pages.id"), 1)
        self.assertEqual(len(re.findall(r'decision\s*= "bypass"', code(TF))), 1)

    def test_the_rest_of_the_host_has_the_staging_workers_policies(self):
        host = block(TF, "cloudflare_zero_trust_access_application", "family_stage_public_host")
        self.assertIn('domain           = local.family_stage_public_hostname', host)
        self.assertIn('{ type = "public", uri = local.family_stage_public_hostname }', host)
        stage = block(STAGE, "cloudflare_zero_trust_access_application", "workers_stage")
        self.assertEqual(policies(host), policies(stage))
        self.assertTrue(policies(host))

    def test_the_custom_domain_is_the_staging_worker_and_waits_for_access(self):
        domain = block(TF, "cloudflare_workers_custom_domain", "family_stage_public")
        self.assertIn('service    = "family-stage"', domain)
        self.assertIn('"family-stage"', STAGE)  # the Worker the Worker-native application already covers
        for app in ("family_stage_public_host", "family_stage_public_pages", "workers_stage"):
            self.assertIn(f"cloudflare_zero_trust_access_application.{app},", domain)


if __name__ == "__main__":
    unittest.main(buffer=True)

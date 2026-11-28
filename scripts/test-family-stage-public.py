#!/usr/bin/env python3
"""Check the Family public hostname's Access policy and isolated two-document Worker."""
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
    def test_only_the_two_page_paths_get_public_destinations(self):
        self.assertIn('family_stage_public_hostname = "family.dolphin-labs.dev"', code(TF))
        pins = code(TF).split("family_stage_public_digests = {", 1)[1].split("\n  }", 1)[0]
        self.assertEqual([line.strip().split(" = ")[0] for line in pins.strip().splitlines()], ["privacy", "support"])
        self.assertEqual(len(re.findall(r'"[0-9a-f]{64}"', pins)), 2)
        self.assertRegex(TF, r'family_stage_public_commit\s*= "[0-9a-f]{40}"')
        self.assertIn('for name, page in data.github_repository_file.family_public_page : "/${name}/" => page.content', TF)
        self.assertIn("provider   = github.dolphin_labs_ci", TF)
        self.assertIn("branch     = local.family_stage_public_commit", TF)
        self.assertIn('file       = "apps/family/web/legal-release/${each.key}.html"', TF)
        self.assertIn("sha256(self.content) == each.value", TF)
        app = block(TF, "cloudflare_zero_trust_access_application", "family_stage_public_pages")
        self.assertIn('domain     = "${local.family_stage_public_hostname}/privacy/"', app)
        self.assertIn('for name in keys(local.family_stage_public_digests) : { type = "public", uri = "${local.family_stage_public_hostname}/${name}/" }', app)
        self.assertNotIn("*", app)
        self.assertEqual(policies(app), ["cloudflare_zero_trust_access_policy.family_stage_public_pages"])
        bypass = block(TF, "cloudflare_zero_trust_access_policy", "family_stage_public_pages")
        self.assertIn('decision   = "bypass"', bypass)
        self.assertIn("everyone = {}", bypass)
        self.assertEqual(code(TF).count("cloudflare_zero_trust_access_policy.family_stage_public_pages.id"), 1)
        self.assertEqual(len(re.findall(r'decision\s*= "bypass"', code(TF))), 1)

    def test_the_rest_of_the_host_has_the_staging_workers_policies(self):
        host = block(TF, "cloudflare_zero_trust_access_application", "family_stage_public_host")
        self.assertIn('domain           = local.family_stage_public_hostname', host)
        self.assertIn('{ type = "public", uri = local.family_stage_public_hostname }', host)
        stage = block(STAGE, "cloudflare_zero_trust_access_application", "workers_stage")
        self.assertEqual(policies(host), policies(stage))
        self.assertTrue(policies(host))

    def test_the_custom_domain_routes_only_to_the_dedicated_worker_after_access(self):
        worker = block(TF, "cloudflare_workers_script", "family_public_pages")
        self.assertIn('script_name        = "family-public-pages"', worker)
        self.assertIn('file("${path.module}/workers/family-public-pages/worker.mjs")', worker)
        self.assertIn("familyPublicPages(${jsonencode(local.family_stage_public_hostname)}, ${jsonencode(local.family_stage_public_html)})", worker)
        self.assertNotIn("bindings", worker)
        self.assertNotIn("assets", worker)
        subdomain = block(TF, "cloudflare_workers_script_subdomain", "family_public_pages")
        self.assertIn("cloudflare_workers_script.family_public_pages.script_name", subdomain)
        self.assertRegex(subdomain, r"\benabled\s*= false")
        self.assertRegex(subdomain, r"previews_enabled\s*= false")
        domain = block(TF, "cloudflare_workers_custom_domain", "family_stage_public")
        self.assertIn("service    = cloudflare_workers_script.family_public_pages.script_name", domain)
        self.assertIn("cloudflare_workers_script_subdomain.family_public_pages,", domain)
        self.assertIn("cloudflare_zero_trust_access_application.family_stage_public_host,", domain)
        self.assertNotIn("cloudflare_zero_trust_access_application.family_stage_public_pages,", domain)
        app = block(TF, "cloudflare_zero_trust_access_application", "family_stage_public_pages")
        self.assertIn("depends_on = [cloudflare_workers_custom_domain.family_stage_public]", app)


if __name__ == "__main__":
    unittest.main(buffer=True)

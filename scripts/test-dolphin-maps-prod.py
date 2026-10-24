#!/usr/bin/env python3
"""Dolphin Maps production on Cloudflare (dolphin-labs-zone.tf, scripts/workers-prod-secrets.sh; ejc3/aws #529):
maps.dolphin-labs.dev is a proxied placeholder record and a Workers route to dolphin-maps-prod; it is public through an
Access application of its own with a bypass-everyone policy, which exists only after the record and the route, while the
*.dolphin-labs.dev wildcard application is unchanged; and the loader reads workers-prod/dolphin-maps in the Dolphin
account, run here for real against stub `aws` and `npx`."""
import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-labs-zone.tf").read_text()
SCRIPT = ROOT / "scripts" / "workers-prod-secrets.sh"
DOLPHIN_ROLE = "arn:aws:iam::484888470752:role/OrganizationAccountAccessRole"
# The names dolphin-maps-prod reads (issue #529 and the arrival page's server-side place lookups).
# APP_ORIGIN is a wrangler.jsonc var, not a secret.
NAMES = ["ACCOUNT_TOKEN_KEY", "APPLE_BUNDLE_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY", "APPLE_SERVICES_ID", "APPLE_TEAM_ID",
         "BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID", "EXA_API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
         "LEDGER_AUTH_TOKEN", "LEDGER_DATABASE_URL", "SCRAPINGDOG_API_KEY", "TURSO_AUTH_TOKEN",
         "TURSO_DATABASE_URL"]


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n(.*?)^\}' % (kind, name), code(TF), re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class ServingTests(unittest.TestCase):
    def test_the_host_and_worker_are_named_once(self):
        self.assertIn('dolphin_maps_host   = "maps.dolphin-labs.dev"', TF)
        self.assertIn('dolphin_maps_worker = "dolphin-maps-prod"', TF)

    def test_the_record_is_a_proxied_placeholder_with_no_origin(self):
        record = block("cloudflare_dns_record", "dolphin_maps")
        self.assertIn("zone_id = var.dolphin_zone_id\n", record)
        self.assertIn("name    = local.dolphin_maps_host\n", record)
        self.assertIn('type    = "A"\n', record)
        self.assertIn('content = "192.0.2.1"\n', record)
        self.assertIn("proxied = true\n", record)
        self.assertIn("ttl     = 1\n", record)
        comment = re.search(r'comment = "(.*)"\n', record).group(1)
        self.assertTrue(comment.startswith("proxied; Worker ${local.dolphin_maps_worker} route serves all"), comment)
        # Cloudflare's Free plan caps a record comment at 100 characters.
        self.assertLessEqual(len(comment.replace("${local.dolphin_maps_worker}", "dolphin-maps-prod")), 100)

    def test_the_route_sends_the_whole_host_to_the_production_worker(self):
        route = block("cloudflare_workers_route", "dolphin_maps")
        self.assertIn("zone_id = var.dolphin_zone_id\n", route)
        self.assertIn('pattern = "${local.dolphin_maps_host}/*"\n', route)
        self.assertIn("script  = local.dolphin_maps_worker\n", route)

    def test_production_does_not_follow_the_previews_switch(self):
        for kind, name in (("cloudflare_dns_record", "dolphin_maps"), ("cloudflare_workers_route", "dolphin_maps"),
                           ("cloudflare_zero_trust_access_policy", "dolphin_maps_public"),
                           ("cloudflare_zero_trust_access_application", "dolphin_maps_public"),
                           ("cloudflare_zone_setting", "dolphin_always_use_https")):
            body = block(kind, name)
            self.assertNotRegex(body, r"(?m)^\s*(count|for_each)\s*=", name)

    def test_http_is_upgraded_on_the_zone(self):
        setting = block("cloudflare_zone_setting", "dolphin_always_use_https")
        self.assertIn("zone_id    = var.dolphin_zone_id", setting)
        self.assertIn('setting_id = "always_use_https"', setting)
        self.assertIn('value      = "on"', setting)


class AccessTests(unittest.TestCase):
    def test_the_bypass_policy_lets_everyone_in_and_does_nothing_else(self):
        policy = block("cloudflare_zero_trust_access_policy", "dolphin_maps_public")
        self.assertIn('decision   = "bypass"', policy)
        self.assertEqual(re.sub(r"\s+", "", re.search(r"include = (\[.*?\])", policy, re.S).group(1)), "[{everyone={}}]")
        self.assertNotRegex(policy, r"\b(exclude|require)\b")

    def test_the_bypass_application_covers_exactly_the_maps_host(self):
        app = block("cloudflare_zero_trust_access_application", "dolphin_maps_public")
        self.assertIn('type       = "self_hosted"', app)
        self.assertIn("domain     = local.dolphin_maps_host\n", app)
        dests = re.search(r"destinations = \[(.*?)\n  \]", app, re.S).group(1)
        self.assertEqual(re.sub(r"\s+", "", dests), '{type="public",uri=local.dolphin_maps_host},')
        policies = re.findall(r"id\s+= (cloudflare_zero_trust_access_policy\.\w+)\.id", app)
        self.assertEqual(policies, ["cloudflare_zero_trust_access_policy.dolphin_maps_public"])
        self.assertNotIn("*", app.replace("/*", ""))

    def test_the_bypass_exists_only_after_the_host_is_the_workers(self):
        # Without the record the name falls to the * wildcard, the previews tunnel; a bypass then would make a preview
        # public. Without the route the record answers nothing.
        app = block("cloudflare_zero_trust_access_application", "dolphin_maps_public")
        self.assertIn("depends_on = [cloudflare_dns_record.dolphin_maps, cloudflare_workers_route.dolphin_maps]", app)

    def test_the_bypass_policy_is_attached_nowhere_else(self):
        self.assertEqual(code(TF).count("cloudflare_zero_trust_access_policy.dolphin_maps_public.id"), 1)
        everywhere = "\n".join(code(p.read_text()) for p in ROOT.glob("*.tf"))
        self.assertEqual(everywhere.count("dolphin_maps_public.id"), 1)
        self.assertEqual(len(re.findall(r'decision\s+= "bypass"', code(TF))), 1)

    def test_the_wildcard_application_is_unchanged(self):
        app = block("cloudflare_zero_trust_access_application", "dolphin_labs")
        self.assertIn("count            = local.dolphin_enabled ? 1 : 0", app)
        self.assertIn('domain           = "*.dolphin-labs.dev"', app)
        self.assertIn("allowed_idps              = [cloudflare_zero_trust_access_identity_provider.github[0].id]", app)
        self.assertIn("auto_redirect_to_identity = true", app)
        self.assertEqual(re.findall(r"id\s+= (cloudflare_zero_trust_access_policy\.[\w\[\]0-9]+)\.id", app),
                         ["cloudflare_zero_trust_access_policy.dolphin_allowed[0]"])
        self.assertNotIn("destinations", app)
        self.assertNotIn("bypass", app)
        policy = block("cloudflare_zero_trust_access_policy", "dolphin_allowed")
        self.assertIn('decision         = "allow"', policy)
        self.assertIn("github_organization = {", policy)
        self.assertNotIn("everyone", policy)

    def test_the_wildcard_record_still_points_at_the_previews_tunnel(self):
        record = block("cloudflare_dns_record", "dolphin_wildcard")
        self.assertIn('name    = "*"', record)
        self.assertIn('content = "${cloudflare_zero_trust_tunnel_cloudflared.dolphin_labs[0].id}.cfargotunnel.com"', record)


FAKE_AWS = r'''#!/usr/bin/env python3
import os, sys
a = " ".join(sys.argv[1:])
open(os.environ["AWS_LOG"], "a").write("ARGV %s KEY=%s PROFILE=%s\n" % (a, os.environ.get("AWS_ACCESS_KEY_ID", "-"), os.environ.get("AWS_PROFILE", "-")))
if a.startswith("sts assume-role"):
    if os.environ.get("FAKE_ASSUME_FAILS"):
        sys.stderr.write("An error occurred (AccessDenied)\n"); sys.exit(254)
    if os.environ.get("FAKE_ASSUME_MALFORMED"):
        print("None\tNone\tNone"); sys.exit(0)
    print("ASIAFAKEDOLPHIN\tfake-secret-key-0123\tfake-session-token-4567")
elif "workers-prod/" in a:
    p = os.environ.get("FAKE_OOB")
    if p is None or os.environ.get("AWS_ACCESS_KEY_ID") != "ASIAFAKEDOLPHIN":
        sys.stderr.write("An error occurred (ResourceNotFoundException)\n"); sys.exit(254)
    print(p)
elif "cloudflare-workers-deploy-token" in a:
    print("cfat_fake-deploy-token-value-for-the-test-0000000000")
else:
    sys.exit(3)
'''
FAKE_NPX = r'''#!/usr/bin/env python3
import os, sys
open(os.environ["NPX_LOG"], "a").write("ARGV " + " ".join(sys.argv[1:]) + "\n")
open(os.environ["NPX_STDIN"], "w").write(sys.stdin.read())
print("Successfully created secret for key: X")
'''


class LoaderTests(unittest.TestCase):
    VALUES = {name: "%s-VALUE-%02d-abcdefghijklmnop" % (name.lower(), i) for i, name in enumerate(NAMES)}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for name, body in (("aws", FAKE_AWS), ("npx", FAKE_NPX)):
            p = self.tmp / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        self.log, self.stdin, self.aws_log = self.tmp / "npx.log", self.tmp / "npx.stdin", self.tmp / "aws.log"

    def run_loader(self, oob, **extra):
        env = dict(os.environ, PATH="%s:%s" % (self.tmp, os.environ["PATH"]), NPX_LOG=str(self.log),
                   NPX_STDIN=str(self.stdin), AWS_LOG=str(self.aws_log), AWS_PROFILE="example-profile", **extra)
        env.pop("AWS_ACCESS_KEY_ID", None)
        if oob is None:
            env.pop("FAKE_OOB", None)
        else:
            env["FAKE_OOB"] = json.dumps(oob)
        r = subprocess.run(["bash", str(SCRIPT), "dolphin-maps"], capture_output=True, text=True, env=env, timeout=60)
        return r, r.stdout + r.stderr

    def test_it_loads_the_dolphin_account_container_into_the_prod_worker(self):
        r, out = self.run_loader(self.VALUES)
        self.assertEqual(r.returncode, 0, out)
        self.assertEqual(json.loads(self.stdin.read_text()), self.VALUES)
        self.assertIn("secret bulk --name dolphin-maps-prod", self.log.read_text())
        self.assertNotIn("-stage", self.log.read_text())
        aws = self.aws_log.read_text()
        self.assertIn("sts assume-role --role-arn %s" % DOLPHIN_ROLE, aws)
        reads = [l for l in aws.splitlines() if "workers-prod/" in l]
        # One read, of the one container, with the Dolphin session and no profile to override it; no Terraform container.
        self.assertEqual(len(reads), 1, reads)
        self.assertIn("--secret-id workers-prod/dolphin-maps ", reads[0])
        self.assertIn("KEY=ASIAFAKEDOLPHIN PROFILE=-", reads[0])
        self.assertNotIn("/terraform", aws)

    def test_it_prints_names_never_values_or_credentials(self):
        r, out = self.run_loader(self.VALUES)
        self.assertIn("APPLE_PRIVATE_KEY", out)
        for secret in list(self.VALUES.values()) + ["cfat_fake-deploy-token", "fake-secret-key-0123", "fake-session-token-4567"]:
            self.assertNotIn(secret, out)
            self.assertNotIn(secret, self.log.read_text())

    def test_no_session_means_no_read_and_no_load(self):
        for extra in ({"FAKE_ASSUME_FAILS": "1"}, {"FAKE_ASSUME_MALFORMED": "1"}):
            if self.aws_log.exists():
                self.aws_log.unlink()
            r, out = self.run_loader(self.VALUES, **extra)
            self.assertNotEqual(r.returncode, 0, extra)
            self.assertIn("no session in account 484888470752", out)
            self.assertNotIn("workers-prod/", self.aws_log.read_text(), "never read with the caller's own credentials")
        self.assertFalse(self.log.exists())

    def test_an_incomplete_or_extra_set_is_refused(self):
        missing = {k: v for k, v in self.VALUES.items() if k != "APPLE_PRIVATE_KEY"}
        for oob in (missing, {**self.VALUES, "APP_ORIGIN": "https://maps.dolphin-labs.dev"}):
            r, out = self.run_loader(oob)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("must hold exactly", out)
        self.assertFalse(self.log.exists())

    def test_placeholders_empties_and_a_missing_container_are_refused(self):
        for oob in ({**self.VALUES, "GOOGLE_CLIENT_SECRET": "[SENSITIVE]"}, {**self.VALUES, "TURSO_AUTH_TOKEN": " "}, None, {}):
            r, out = self.run_loader(oob)
            self.assertNotEqual(r.returncode, 0, oob)
        self.assertFalse(self.log.exists())

    def test_each_place_lookup_credential_is_required_before_loading(self):
        for name in ("BROWSERBASE_API_KEY", "BROWSERBASE_PROJECT_ID", "EXA_API_KEY", "SCRAPINGDOG_API_KEY"):
            with self.subTest(name=name):
                r, out = self.run_loader({k: v for k, v in self.VALUES.items() if k != name})
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("must hold exactly", out)
                self.assertFalse(self.log.exists(), "a missing credential must not reach Wrangler")

    def test_the_loader_names_are_the_workers(self):
        text = SCRIPT.read_text()
        case = re.search(r"\n  dolphin-maps\)\n(.*?)\n    ;;", text, re.S).group(1)
        names = " ".join(re.findall(r'OUT_OF_BAND_NAMES\+?="([^"]+)"', case)).split()
        self.assertEqual(sorted(names), NAMES)
        self.assertIn("SOURCE_ACCOUNT=$DOLPHIN_ACCOUNT_ID", case)
        self.assertIn('MANAGED_CONTAINER=""', case)


class DocsTests(unittest.TestCase):
    def test_agents_md_names_the_host_and_worker(self):
        agents = (ROOT / "AGENTS.md").read_text()
        para = agents[agents.index("**Which production is behind Cloudflare"):]
        para = para[:para.index("\n\n")]
        self.assertIn("maps.dolphin-labs.dev", para)
        self.assertIn("dolphin-maps-prod", para)
        self.assertNotIn("dolphin-maps had no public production", agents)


if __name__ == "__main__":
    unittest.main(buffer=True)

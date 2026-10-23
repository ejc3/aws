#!/usr/bin/env python3
"""Astryx Swift's self-serve TestFlight invitations (astryx-join.tf): astryx-join.dolphin-labs.dev is public through a
bypass application for exactly that host (after its record and route; the *.dolphin-labs.dev gate unchanged); a managed
Turnstile widget for exactly that host gives the Worker its site key and secret; a KV namespace holds the rate limits;
the Worker is deployed by Terraform from scripts/astryx-join.mjs with the dedicated App Manager key as secret bindings,
never the broader key; the whole host exists only once that key's secret does, with workers.dev off.
Offline: reads the sources. The Worker's own logic is tested by scripts/test-astryx-join.mjs (node --test)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "astryx-join.tf").read_text()
WORKER = (ROOT / "scripts" / "astryx-join.mjs").read_text()
CI = (ROOT / ".github" / "workflows" / "lambda-tests.yml").read_text()
ZONE = (ROOT / "dolphin-labs-zone.tf").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, name, text=TF):
    m = re.search(r'^(?:resource|data) "%s" "%s" \{\n(.*?)^\}' % (kind, name), code(text), re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


GATED = [
    ("resource", "aws_secretsmanager_secret_policy", "astryx_join_asc_key"),
    ("data", "aws_secretsmanager_secret_version", "astryx_join_asc_key"),
    ("data", "aws_secretsmanager_secret", "astryx_join_asc_key"),
    ("resource", "cloudflare_dns_record", "astryx_join"),
    ("resource", "cloudflare_workers_kv_namespace", "astryx_join_limits"),
    ("resource", "cloudflare_turnstile_widget", "astryx_join"),
    ("resource", "cloudflare_zero_trust_access_policy", "astryx_join_public"),
    ("resource", "cloudflare_zero_trust_access_application", "astryx_join_public"),
    ("resource", "cloudflare_workers_script", "astryx_join"),
    ("resource", "cloudflare_workers_script_subdomain", "astryx_join"),
    ("resource", "cloudflare_workers_route", "astryx_join"),
]


class TheSwitch(unittest.TestCase):
    def test_every_object_exists_only_once_the_key_secret_does(self):
        for _, kind, name in GATED:
            self.assertRegex(block(kind, name), r"^\s+count\s+= local\.astryx_join_ready \? 1 : 0\n", name)
        # Every resource and data source in the file is either gated or the list that decides the gate.
        declared = re.findall(r'^(resource|data) "(\w+)" "(\w+)"', code(TF), re.M)
        self.assertEqual(sorted(declared), sorted(GATED + [("data", "aws_secretsmanager_secrets", "astryx_join_asc_key")]))

    def test_the_switch_is_the_exact_secret_name_and_a_missing_secret_fails_no_plan(self):
        self.assertIn('astryx_join_asc_secret = "astryx/testflight-asc-key"', TF)
        lst = block("aws_secretsmanager_secrets", "astryx_join_asc_key")
        self.assertIn('name   = "name"', lst)
        self.assertIn("values = [local.astryx_join_asc_secret]", lst)
        # ListSecrets matches a prefix: only the exact name counts.
        self.assertIn("astryx_join_ready = contains(data.aws_secretsmanager_secrets.astryx_join_asc_key.names, local.astryx_join_asc_secret)", TF)

    def test_only_administration_may_read_the_key(self):
        policy = block("aws_secretsmanager_secret_policy", "astryx_join_asc_key")
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('Principal = "*"', policy)
        self.assertIn('Action    = "secretsmanager:GetSecretValue"', policy)
        self.assertIn('Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }', policy)


class PublicHost(unittest.TestCase):
    def test_the_bypass_policy_lets_everyone_in_and_does_nothing_else(self):
        policy = block("cloudflare_zero_trust_access_policy", "astryx_join_public")
        self.assertIn('decision   = "bypass"', policy)
        self.assertEqual(re.sub(r"\s+", "", re.search(r"include = (\[.*?\])", policy, re.S).group(1)), "[{everyone={}}]")
        self.assertNotRegex(policy, r"\b(exclude|require)\b")

    def test_the_bypass_application_covers_exactly_this_host(self):
        app = block("cloudflare_zero_trust_access_application", "astryx_join_public")
        self.assertIn('type       = "self_hosted"', app)
        self.assertIn("domain     = local.astryx_join_hostname\n", app)
        dests = re.search(r"destinations = \[(.*?)\n  \]", app, re.S).group(1)
        self.assertEqual(re.sub(r"\s+", "", dests), '{type="public",uri=local.astryx_join_hostname},')
        self.assertEqual(re.findall(r"id\s+= (cloudflare_zero_trust_access_policy\.[\w\[\]0-9]+)\.id", app),
                         ["cloudflare_zero_trust_access_policy.astryx_join_public[0]"])
        self.assertNotIn("*", app.replace("/*", ""))
        self.assertEqual(code(TF).count("astryx_join_public[0].id"), 1)
        everywhere = "\n".join(code(p.read_text()) for p in ROOT.glob("*.tf"))
        self.assertEqual(everywhere.count("astryx_join_public[0].id"), 1)

    def test_no_sign_in_gate_of_its_own(self):
        self.assertEqual(len(re.findall(r'decision\s+= "(allow|non_identity)"', code(TF))), 0)
        self.assertNotIn("email_domain", code(TF))
        self.assertNotIn("onetimepin", code(TF))

    def test_the_bypass_exists_only_after_the_host_is_the_workers(self):
        # Without the record the name falls to the * wildcard, the previews tunnel, which a bypass would make public.
        app = block("cloudflare_zero_trust_access_application", "astryx_join_public")
        self.assertIn("depends_on = [cloudflare_dns_record.astryx_join, cloudflare_workers_route.astryx_join]", app)

    def test_the_wildcard_gate_is_unchanged(self):
        app = block("cloudflare_zero_trust_access_application", "dolphin_labs", ZONE)
        self.assertIn('domain           = "*.dolphin-labs.dev"', app)
        self.assertEqual(re.findall(r"id\s+= (cloudflare_zero_trust_access_policy\.[\w\[\]0-9]+)\.id", app),
                         ["cloudflare_zero_trust_access_policy.dolphin_allowed[0]"])


class TurnstileAndLimits(unittest.TestCase):
    def test_a_managed_widget_for_exactly_this_host(self):
        w = block("cloudflare_turnstile_widget", "astryx_join")
        self.assertIn("domains    = [local.astryx_join_hostname]", w)
        self.assertIn('mode       = "managed"', w)

    def test_its_site_key_is_plain_and_its_secret_is_a_secret_both_from_the_widget(self):
        w = block("cloudflare_workers_script", "astryx_join")
        self.assertIn('{ name = "TURNSTILE_SITE_KEY", type = "plain_text", text = cloudflare_turnstile_widget.astryx_join[0].sitekey }', w)
        self.assertIn('{ name = "TURNSTILE_SECRET", type = "secret_text", text = cloudflare_turnstile_widget.astryx_join[0].secret }', w)

    def test_the_limits_namespace_is_bound_as_limits(self):
        w = block("cloudflare_workers_script", "astryx_join")
        self.assertIn('{ name = "LIMITS", type = "kv_namespace", namespace_id = cloudflare_workers_kv_namespace.astryx_join_limits[0].id }', w)
        self.assertIn("const PER_IP_PER_HOUR = 5;", WORKER)
        self.assertIn("const DAILY_CAP = 500;", WORKER)


class Serving(unittest.TestCase):
    def test_the_host_and_worker_are_named_once(self):
        self.assertIn('astryx_join_hostname = "astryx-join.dolphin-labs.dev"', TF)
        self.assertIn('astryx_join_worker   = "astryx-join"', TF)

    def test_the_record_is_a_proxied_placeholder_with_no_origin(self):
        record = block("cloudflare_dns_record", "astryx_join")
        self.assertIn("zone_id = var.dolphin_zone_id\n", record)
        self.assertIn("name    = local.astryx_join_hostname\n", record)
        self.assertIn('type    = "A"\n', record)
        self.assertIn('content = "192.0.2.1"\n', record)
        self.assertIn("proxied = true\n", record)
        comment = re.search(r'comment = "(.*)"\n', record).group(1)
        # Cloudflare's Free plan caps a record comment at 100 characters.
        self.assertLessEqual(len(comment.replace("${local.astryx_join_worker}", "astryx-join")), 100)

    def test_the_route_is_the_workers_only_address(self):
        route = block("cloudflare_workers_route", "astryx_join")
        self.assertIn('pattern = "${local.astryx_join_hostname}/*"', route)
        self.assertIn("script  = cloudflare_workers_script.astryx_join[0].script_name", route)
        self.assertIn("depends_on = [cloudflare_workers_script_subdomain.astryx_join]", route)
        sub = block("cloudflare_workers_script_subdomain", "astryx_join")
        self.assertIn("enabled          = false", sub)
        self.assertIn("previews_enabled = false", sub)


class TheWorker(unittest.TestCase):
    def test_terraform_uploads_this_file_and_redeploys_when_it_changes(self):
        w = block("cloudflare_workers_script", "astryx_join")
        self.assertIn('astryx_join_module   = "${path.module}/scripts/astryx-join.mjs"', TF)
        self.assertIn("content_file       = local.astryx_join_module", w)
        self.assertIn("content_sha256     = filesha256(local.astryx_join_module)", w)
        self.assertIn('main_module        = "astryx-join.mjs"', w)
        self.assertIn("script_name        = local.astryx_join_worker", w)
        # One file, no build: it imports nothing.
        self.assertNotRegex(WORKER, r"(?m)^\s*import\s")
        self.assertIn("export default {", WORKER)

    def test_the_bindings_are_exactly_the_ones_the_worker_reads(self):
        w = block("cloudflare_workers_script", "astryx_join")
        bindings = dict(re.findall(r'\{ name = "(\w+)", type = "(\w+)", (?:text|namespace_id) = [^}]+\}', w))
        self.assertEqual(bindings, {
            "TURNSTILE_SITE_KEY": "plain_text", "TURNSTILE_SECRET": "secret_text", "LIMITS": "kv_namespace",
            "ASC_BETA_GROUP_ID": "plain_text", "ASC_ISSUER_ID": "secret_text", "ASC_KEY_ID": "secret_text",
            "ASC_PRIVATE_KEY": "secret_text",
        })
        self.assertEqual(sorted(set(re.findall(r"env\.([A-Z_]+)", WORKER))), sorted(bindings))
        for k in ("ISSUER_ID", "KEY_ID", "PRIVATE_KEY"):
            self.assertIn('type = "secret_text", text = local.astryx_join_asc["%s"]' % k, w)
        self.assertIn("jsondecode(data.aws_secretsmanager_secret_version.astryx_join_asc_key[0].secret_string)", TF)

    def test_the_group_is_configuration_not_code(self):
        group = re.search(r'astryx_join_beta_group_id = "([0-9a-f-]{36})"', TF).group(1)
        self.assertIn('{ name = "ASC_BETA_GROUP_ID", type = "plain_text", text = local.astryx_join_beta_group_id }', TF)
        self.assertNotIn(group, WORKER)

    def test_never_the_broader_admin_key(self):
        self.assertNotIn("games-worth-watching/app-store-connect", code(TF))
        self.assertNotIn("aws_secretsmanager_secret.app_store_connect", code(TF))
        self.assertNotIn("app_store_connect", code(TF))

    def test_a_malformed_key_fails_the_plan_without_showing_it(self):
        w = block("cloudflare_workers_script", "astryx_join")
        self.assertIn("precondition {", w)
        self.assertIn("condition = nonsensitive(alltrue([", w)
        self.assertIn('"-----BEGIN PRIVATE KEY-----"', w)
        msg = re.search(r'error_message = "(.*)"', w).group(1)
        self.assertNotIn("${", msg)

    def test_the_worker_checks_turnstile_then_the_domain_and_logs_no_address(self):
        self.assertIn("const SITEVERIFY = 'https://challenges.cloudflare.com/turnstile/v0/siteverify';", WORKER)
        self.assertIn("const EMAIL_DOMAIN = '@meta.com';", WORKER)
        handler = WORKER[WORKER.index("export function makeHandler"):]
        self.assertLess(handler.index("deps.turnstile("), handler.index("metaEmail(typed)"))
        self.assertLess(handler.index("metaEmail(typed)"), handler.index("deps.limits(ip)"))
        self.assertLess(handler.index("deps.limits(ip)"), handler.index("inviteTester("))
        # The one log call site is the injected log; the handler never passes the address to it.
        self.assertEqual(WORKER.count("console."), 1)
        for call in re.findall(r"log\(\{[^}]*\}\)", WORKER):
            self.assertNotRegex(call, r"\b(email|typed|ip)\b", call)

    def test_the_app_store_connect_token_is_es256_for_twenty_minutes(self):
        self.assertIn("const ASC_TOKEN_SECONDS = 20 * 60;", WORKER)
        self.assertIn("alg: 'ES256', kid: keyId", WORKER)
        self.assertIn("aud: 'appstoreconnect-v1'", WORKER)


class Wiring(unittest.TestCase):
    def test_ci_runs_both_tests(self):
        self.assertIn("node --test scripts/test-astryx-join.mjs", CI)
        self.assertIn("python3 -S -B scripts/test-astryx-join.py", CI)


if __name__ == "__main__":
    unittest.main(buffer=True)

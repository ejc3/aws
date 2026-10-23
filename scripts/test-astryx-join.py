#!/usr/bin/env python3
"""Astryx Swift's self-serve TestFlight invitations (astryx-join.tf): astryx-join.dolphin-labs.dev is gated by an Access
application of its own that admits only @meta.com addresses through the one-time PIN; the Worker is deployed by
Terraform from scripts/astryx-join.mjs with the dedicated App Manager key as secret bindings, never the broader key; the
whole host exists only once that key's secret does, and is built record, Access, Worker, route, with workers.dev off.
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
    ("resource", "cloudflare_zero_trust_access_policy", "astryx_join_meta"),
    ("resource", "cloudflare_zero_trust_access_application", "astryx_join"),
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


class Access(unittest.TestCase):
    def test_the_policy_admits_exactly_the_meta_com_domain_and_nothing_else(self):
        self.assertIn('astryx_join_email_domain = "meta.com"', TF)
        policy = block("cloudflare_zero_trust_access_policy", "astryx_join_meta")
        self.assertIn('decision         = "allow"', policy)
        include = re.search(r"include = (\[.*?\])\n", policy, re.S).group(1)
        self.assertEqual(re.sub(r"\s+", "", include), "[{email_domain={domain=local.astryx_join_email_domain}}]")
        self.assertNotRegex(policy, r"\b(exclude|require|everyone|service_token|non_identity|bypass)\b")

    def test_the_application_covers_exactly_the_host_with_the_one_time_pin_only(self):
        app = block("cloudflare_zero_trust_access_application", "astryx_join")
        self.assertIn("domain           = local.astryx_join_hostname\n", app)
        self.assertIn('destinations     = [{ type = "public", uri = local.astryx_join_hostname }]', app)
        self.assertIn("allowed_idps               = [cloudflare_zero_trust_access_identity_provider.onetimepin.id]", app)
        self.assertIn("auto_redirect_to_identity  = true", app)
        self.assertIn("http_only_cookie_attribute = true", app)
        self.assertEqual(re.findall(r"id\s+= (cloudflare_zero_trust_access_policy\.[\w\[\]0-9]+)\.id", app),
                         ["cloudflare_zero_trust_access_policy.astryx_join_meta[0]"])
        self.assertNotIn("google", app)
        self.assertNotIn("github", app)
        self.assertEqual(code(TF).count("astryx_join_meta[0].id"), 1)

    def test_the_application_exists_only_after_the_record(self):
        # Before the record the name falls to the * wildcard, the previews tunnel: a @meta.com sign-in must never reach it.
        app = block("cloudflare_zero_trust_access_application", "astryx_join")
        self.assertIn("depends_on = [cloudflare_dns_record.astryx_join]", app)

    def test_the_wildcard_gate_is_unchanged(self):
        app = block("cloudflare_zero_trust_access_application", "dolphin_labs", ZONE)
        self.assertIn('domain           = "*.dolphin-labs.dev"', app)
        self.assertEqual(re.findall(r"id\s+= (cloudflare_zero_trust_access_policy\.[\w\[\]0-9]+)\.id", app),
                         ["cloudflare_zero_trust_access_policy.dolphin_allowed[0]"])


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

    def test_the_route_is_the_workers_only_address_and_comes_last(self):
        route = block("cloudflare_workers_route", "astryx_join")
        self.assertIn('pattern = "${local.astryx_join_hostname}/*"', route)
        self.assertIn("script  = cloudflare_workers_script.astryx_join[0].script_name", route)
        self.assertIn("cloudflare_zero_trust_access_application.astryx_join", route.split("depends_on")[1])
        self.assertIn("cloudflare_workers_script_subdomain.astryx_join", route.split("depends_on")[1])
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
        bindings = dict(re.findall(r'\{ name = "(\w+)", type = "(\w+)", text = [^}]+\}', w))
        self.assertEqual(bindings, {
            "ACCESS_TEAM_DOMAIN": "plain_text", "ACCESS_AUD": "plain_text", "ASC_BETA_GROUP_ID": "plain_text",
            "ASC_ISSUER_ID": "secret_text", "ASC_KEY_ID": "secret_text", "ASC_PRIVATE_KEY": "secret_text",
        })
        self.assertEqual(sorted(set(re.findall(r"env\.([A-Z_]+)", WORKER))), sorted(bindings))
        self.assertIn('text = cloudflare_zero_trust_access_application.astryx_join[0].aud', w)
        self.assertIn("text = local.browser_manager_issuer", w)
        self.assertIn('browser_manager_issuer   = "https://ejc3.cloudflareaccess.com"', (ROOT / "browser-manager.tf").read_text())
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

    def test_the_worker_checks_the_token_and_domain_itself_and_logs_no_address(self):
        self.assertIn("claims.iss !== teamDomain", WORKER)
        self.assertIn("auds.includes(aud)", WORKER)
        self.assertIn("const EMAIL_DOMAIN = '@meta.com';", WORKER)
        self.assertIn("request.headers.get('cf-access-jwt-assertion')", WORKER)
        # The one log call site is the injected log; the handler never passes the address to it.
        self.assertEqual(WORKER.count("console."), 1)
        for call in re.findall(r"log\(\{[^}]*\}\)", WORKER):
            self.assertNotRegex(call, r"\bemail\b", call)

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

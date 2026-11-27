#!/usr/bin/env python3
"""Offline checks for dolphin-costume.tf: Costume Party's staging host, whose server runs in the Dolphin account
(dolphin-aws costume.tf). Only the sign-in path is behind Access (and checked again at the connector), the server gets
exactly its own paths, everything else is a 404, and the connector token and the host's values go to the Dolphin
account."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-costume.tf").read_text()


def block(header, text=TF):
    start = text.index(header)
    depth = 0
    for i in range(text.index("{", start), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError(header)


def setting(name):
    m = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', TF, re.M)
    if not m:
        raise AssertionError(name)
    return m.group(1)


class CostumeStageTests(unittest.TestCase):
    def test_only_the_owner_signs_in(self):
        policy = block('resource "cloudflare_zero_trust_access_policy" "costume_stage_owner"')
        self.assertIn('decision         = "allow"', policy)
        self.assertIn("include          = [{ email = { email = local.browser_manager_owner } }]", policy)
        login = block('resource "cloudflare_zero_trust_access_application" "costume_stage_login"')
        self.assertIn("domain           = local.costume_stage_login", login)
        self.assertIn("policies = [{ id = cloudflare_zero_trust_access_policy.costume_stage_owner.id, precedence = 1 }]", login)
        self.assertNotRegex(TF, r"[\w.+-]+@[\w-]+\.\w+")

    def test_the_connector_requires_access_on_the_login_path_then_the_server_then_404(self):
        config = block('resource "cloudflare_zero_trust_tunnel_cloudflared_config" "dolphin_costume"')
        login_rule = config[config.index("# The sign-in"):config.index("path     = local.costume_stage.api_regex")]
        self.assertIn("required  = true", login_rule)
        self.assertIn("aud_tag   = [cloudflare_zero_trust_access_application.costume_stage_login.aud]", login_rule)
        order = [config.index(s) for s in ('path     = "^${local.costume_stage.login_path}"',
                                            "path     = local.costume_stage.api_regex", '{ service = "http_status:404" }')]
        self.assertEqual(order, sorted(order))
        # The server listens on 127.0.0.1 only (not ::1, which "localhost" may mean).
        self.assertEqual(config.count('service  = "http://127.0.0.1:${local.costume_stage.port}"'), 2)
        self.assertIn("port       = 8081", TF)

    def test_the_regexes_route_exactly_the_servers_paths(self):
        self.assertRegex(setting("login_path"), r"^/[A-Za-z0-9/_-]+$")
        login = re.compile("^" + setting("login_path"))
        api = re.compile(setting("api_regex"))
        for path in ("/lattice/native/login", "/lattice/native/login?app=costume"):
            self.assertTrue(login.search(path), path)
        for path in ("/lattice/v1/costume/healthz", "/healthz", "/", "/x/lattice/native/login"):
            self.assertFalse(login.search(path), path)
        # The server's routes the app calls (dolphin-labs apps/costume/server/src/costume/*.ts), and its health.
        for path in ("/lattice/v1/costume", "/lattice/v1/costume/healthz", "/lattice/v1/costume/rooms/ABCD2222222222222222/live",
                     "/lattice/v1/costume/session/renew", "/lattice/v1/costume/search", "/lattice/v1/costume/review", "/healthz"):
            self.assertTrue(api.search(path), path)
        # The sentinel's handover route (task-local), Lattice's own paths, and anything else never reach the server.
        for path in ("/internal/costume/handover", "/lattice/v1/costumex", "/lattice/v1/lattice", "/lattice/live",
                     "/lattice/native/login", "/healthzx", "/", "/x/lattice/v1/costume/rooms"):
            self.assertFalse(api.search(path), path)

    def test_the_rest_of_the_host_bypasses_for_the_servers_own_token(self):
        public = block('resource "cloudflare_zero_trust_access_application" "costume_stage_public"')
        self.assertIn("domain       = local.costume_stage.hostname", public)
        self.assertIn('decision   = "bypass"', block('resource "cloudflare_zero_trust_access_policy" "costume_stage_public"'))

    def test_the_hostname_appears_only_after_access_and_the_rules(self):
        dns = block('resource "cloudflare_dns_record" "costume_stage"')
        for dep in ("cloudflare_zero_trust_access_application.costume_stage_login,",
                    "cloudflare_zero_trust_access_application.costume_stage_public,",
                    "cloudflare_zero_trust_tunnel_cloudflared_config.dolphin_costume,"):
            self.assertIn(dep, dns)
        self.assertIn("proxied = true", dns)

    def test_the_token_and_the_hosts_values_go_to_the_dolphin_account_only(self):
        for header in ('resource "aws_secretsmanager_secret" "dolphin_costume_tunnel"',
                       'resource "aws_secretsmanager_secret_version" "dolphin_costume_tunnel"',
                       'resource "aws_ssm_parameter" "dolphin_costume_stage"'):
            self.assertRegex(block(header), r"provider\s+= aws\.dolphin")
        self.assertEqual(TF.count("cloudflared_token.dolphin_costume.token"), 1)
        pointer = block('resource "aws_ssm_parameter" "dolphin_costume_stage"')
        keys = set(re.findall(r"^\s+(\w+)\s+=", pointer.split("jsonencode(", 1)[1], re.M))
        self.assertEqual(keys, {"hostname", "access_team_domain", "access_aud", "tunnel_secret_name"})
        self.assertNotIn("token", block('output "dolphin_costume"').lower().replace("connector token", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

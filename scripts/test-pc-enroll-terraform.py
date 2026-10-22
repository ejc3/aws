#!/usr/bin/env python3
"""The PC enrollment service's Terraform (pc-enroll.tf): who may reach it, what its Lambda may touch, and how its origin
is reached. Offline: reads the sources. The Lambda's own logic is tested by scripts/test-pc-enroll.mjs (node --test)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "pc-enroll.tf").read_text()
COLIN = (ROOT / "colin-pc-ssh.tf").read_text()
LAMBDA = (ROOT / "scripts" / "pc-enroll.mjs").read_text()
CI = (ROOT / ".github" / "workflows" / "lambda-tests.yml").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(text, header):
    """The body of one top-level block, from its header to its closing brace at column 0."""
    start = text.index(header)
    return text[start:text.index("\n}\n", start) + 2]


class Gate(unittest.TestCase):
    def test_access_admits_the_owner_only_and_no_service_token(self):
        app = block(TF, 'resource "cloudflare_zero_trust_access_application" "pc_enroll"')
        self.assertIn("domain           = local.pc_enroll_hostname", app)
        self.assertEqual(re.findall(r"id\s+= (cloudflare_zero_trust_access_policy\.\w+)\.id", app),
                         ["cloudflare_zero_trust_access_policy.browser_manager_owner"])
        self.assertNotIn("service_token", code(TF))
        self.assertNotIn("non_identity", code(TF))
        self.assertIn("pc_enroll_owners = [local.browser_manager_owner]", TF)

    def test_the_lambda_is_told_this_applications_aud_and_the_exact_team_issuer(self):
        self.assertIn("aud        = cloudflare_zero_trust_access_application.pc_enroll.aud", TF)
        self.assertIn('teamDomain = "https://ejc3.cloudflareaccess.com"', TF)
        self.assertIn("claims.iss !== teamDomain", LAMBDA)

    def test_the_name_answers_only_after_access_and_strict_tls_are_in_place(self):
        dns = block(TF, 'resource "cloudflare_dns_record" "pc_enroll"')
        self.assertIn("proxied = true", dns)
        for dep in ("cloudflare_zero_trust_access_application.pc_enroll", "cloudflare_ruleset.cc_games_dev_config"):
            self.assertIn(dep, dns.split("depends_on")[1])
        rules = block(TF, 'resource "cloudflare_ruleset" "cc_games_dev_config"')
        self.assertIn('phase       = "http_config_settings"', rules)
        self.assertIn('action_parameters = { ssl = "strict" }', rules)
        self.assertIn('(http.host eq \\"${local.pc_enroll_hostname}\\")', rules)
        # ACM must see its literal CNAME.
        self.assertIn("proxied = false", block(TF, 'resource "cloudflare_dns_record" "pc_enroll_acm_validation"'))


class Origin(unittest.TestCase):
    def test_no_default_endpoint_and_throttled(self):
        api = block(TF, 'resource "aws_apigatewayv2_api" "pc_enroll"')
        self.assertIn("disable_execute_api_endpoint = true", api)
        stage = block(TF, 'resource "aws_apigatewayv2_stage" "pc_enroll"')
        self.assertIn("throttling_rate_limit  = 1", stage)
        self.assertIn('security_policy = "TLS_1_2"', TF)

    def test_exactly_the_three_routes_the_lambda_serves(self):
        routes = block(TF, 'resource "aws_apigatewayv2_route" "pc_enroll"')
        self.assertIn('toset(["GET /pcs", "POST /pcs/{pc}/enroll", "POST /pcs/{pc}/host-key/replace"])', routes)
        self.assertIn("method === 'GET' && path === '/pcs'", LAMBDA)
        self.assertIn(r"(enroll|host-key\/replace)$", LAMBDA)
        self.assertIn("route_key = each.value", routes)
        self.assertNotIn("$default\"", re.sub(r'name\s+= "\$default"', "", code(TF)))  # no catch-all route

    def test_only_this_api_invokes_it_and_the_handler_is_the_file(self):
        perm = block(TF, 'resource "aws_lambda_permission" "pc_enroll"')
        self.assertIn('source_arn    = "${aws_apigatewayv2_api.pc_enroll.execution_arn}/*/*"', perm)
        fn = block(TF, 'resource "aws_lambda_function" "pc_enroll"')
        self.assertIn('handler                        = "pc-enroll.handler"', fn)
        self.assertIn('runtime                        = "nodejs22.x"', fn)
        self.assertIn('source_file = "${path.module}/scripts/pc-enroll.mjs"', TF)
        self.assertIn("export const handler", LAMBDA)
        imports = re.findall(r"^import .* from '([^']+)';$", LAMBDA, re.M)
        self.assertEqual(imports, ["node:crypto"])  # nothing the zip would not carry
        self.assertEqual(sorted(re.findall(r"await import\('([^']+)'\)", LAMBDA)),
                         ["@aws-sdk/client-secrets-manager", "@aws-sdk/client-ssm"])  # the runtime's own SDK


class Grants(unittest.TestCase):
    def policy(self):
        return block(TF, 'resource "aws_iam_role_policy" "pc_enroll"')

    def test_exact_resources_no_wildcards(self):
        policy = self.policy()
        self.assertEqual(sorted(re.findall(r'Action\s+= (\S.*)', policy)), sorted([
            '"secretsmanager:GetSecretValue"',
            '"ssm:GetParameter"',
            '"ssm:PutParameter"',
            '["logs:CreateLogStream", "logs:PutLogEvents"]',
        ]))
        self.assertNotRegex(policy, r'Resource\s+= "\*"')
        self.assertNotIn("/pc-ssh/*", TF)
        self.assertNotIn("ssm:*", TF)

    def test_it_writes_only_the_pins_never_a_client_key(self):
        put = self.policy().split('Sid      = "WriteTheirPins"')[1].split("},")[0]
        self.assertIn("for pc, p in local.pc_enroll_host_key_params", put)
        self.assertNotIn("client", put)
        self.assertIn('pc => "/pc-ssh/${pc}/host-key"', TF)

    def test_the_tunnel_secret_admits_the_lambda_and_the_client_secrets_do_not(self):
        tunnel = block(COLIN, 'resource "aws_secretsmanager_secret_policy" "colin_pc_ssh_tunnel"')
        self.assertIn("concat(local.colin_pc_ssh_admins, [aws_iam_role.pc_enroll.arn])", tunnel)
        access = block(COLIN, 'resource "aws_secretsmanager_secret_policy" "colin_pc_ssh_access"')
        self.assertNotIn("pc_enroll", access)
        self.assertIn("secret_arn = aws_secretsmanager_secret.colin_pc_ssh_tunnel.arn", TF)


class Pins(unittest.TestCase):
    def test_pins_are_seeded_from_the_trusted_host_keys_and_left_to_enrollment(self):
        pin = block(TF, 'resource "aws_ssm_parameter" "pc_enroll_host_key"')
        self.assertIn("value       = each.value.host_key", pin)
        self.assertIn("ignore_changes = [value]", pin)
        self.assertIn('host_key   = local.mac_ssh_macs["colin-pc"].host_key', TF)

    def test_colin_pcs_slots_are_its_two_service_token_clients(self):
        self.assertIn('clients = ["fcvm-ubuntu", "nextjs-colin"]', TF)
        clients = COLIN[COLIN.index("colin_pc_ssh_clients = {"):COLIN.index("\n  }\n", COLIN.index("colin_pc_ssh_clients = {"))]
        self.assertEqual(re.findall(r"^\s+([a-z-]+)\s+= \{", clients, re.M), ["fcvm", "nextjs-colin"])


class Ci(unittest.TestCase):
    def test_ci_runs_both_test_files(self):
        self.assertIn("node --test scripts/test-pc-enroll.mjs", CI)
        self.assertIn("scripts/test-pc-enroll-terraform.py", CI)


if __name__ == "__main__":
    unittest.main(buffer=True)

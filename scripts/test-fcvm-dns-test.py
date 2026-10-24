#!/usr/bin/env python3
"""The fcvm.dev DNS propagation test token (fcvm-dns-test.tf): DNS Read and Write on the fcvm.dev zone only, client IPs
from Terraform (the dev fleet's subnets and the metal boxes' EIPs), an expiry date, and a secret only administration and
dev-server-role may read."""
import re
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "fcvm-dns-test.tf").read_text()
CONST = (ROOT / "constellation-account.tf").read_text()
AGENTS = (ROOT / "AGENTS.md").read_text()

DNS_READ = "82e64a83756745bbbb1c9c2701bf816b"
DNS_WRITE = "4755a26eedb94da69e1066d98aa820be"


def block(header, text=TF):
    start = text.index(header)
    return text[start:text.index("\n}\n", start) + 2]


class TokenTests(unittest.TestCase):
    def test_dns_read_and_write_on_the_fcvm_dev_zone_and_nothing_else(self):
        token = block('resource "cloudflare_account_token" "fcvm_dns_test"')
        self.assertIn("provider   = cloudflare.token_minter", token)
        self.assertEqual(token.count("effect"), 1, "one policy")
        self.assertIn("permission_groups = [{ id = local.dns_read }, { id = local.dns_write }]", token)
        self.assertIn('resources         = jsonencode({ "com.cloudflare.api.account.zone.${var.fcvm_dev_zone_id}" = "*" })', token)
        self.assertNotIn("com.cloudflare.api.account.${", token, "no account-scoped permission")
        self.assertNotIn("zone.*", token, "no other zone")
        self.assertIn(f'dns_read                       = "{DNS_READ}"', TF)
        self.assertIn(f'dns_write           = "{DNS_WRITE}"', (ROOT / "fcvm-dev.tf").read_text())
        self.assertEqual(set(re.findall(r'"([0-9a-f]{32})"', TF)), {DNS_READ})

    def test_client_ips_come_from_terraform_not_literals(self):
        token = block('resource "cloudflare_account_token" "fcvm_dns_test"')
        self.assertIn("request_ip = { in = local.fcvm_dns_test_client_cidrs }", token)
        cidrs = TF.split("fcvm_dns_test_client_cidrs = concat(", 1)[1].split("\n  )", 1)[0]
        self.assertIn("[for s in local.dev_fleet_subnets : s.ipv6_cidr_block]", cidrs)
        self.assertIn('[for e in aws_eip.firecracker_dev : "${e.public_ip}/32"]', cidrs)
        self.assertIn('[for e in aws_eip.x86_dev : "${e.public_ip}/32"]', cidrs)
        self.assertNotRegex(TF, r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "no IPv4 literal")
        self.assertNotRegex(TF, r'"[0-9a-f]{1,4}:[0-9a-f:]+(/\d+)?"', "no IPv6 literal")

    def test_it_expires(self):
        token = block('resource "cloudflare_account_token" "fcvm_dns_test"')
        self.assertIn("expires_on = local.fcvm_dns_test_token_expires_on", token)
        m = re.search(r'fcvm_dns_test_token_expires_on = "([^"]+)"', TF)
        self.assertTrue(m)
        datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ")

    def test_the_name_convention_and_the_zone_wide_reach_are_written_down(self):
        for text in (TF, AGENTS):
            self.assertIn("dnstest-*.fcvm.dev", text)
            for name in ("slot-*", "ec2-*", "fleet-api-stage"):
                self.assertIn(name, text)
        self.assertIn("could edit or delete ANY record in fcvm.dev", TF)
        self.assertIn("The token can edit any record in the zone", AGENTS)


class ReaderTests(unittest.TestCase):
    def test_administration_and_dev_server_role_read_it_and_no_one_else(self):
        policy = block('resource "aws_secretsmanager_secret_policy" "fcvm_dns_test_token"')
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn("concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn])", policy)
        self.assertNotIn("nextjs_dev", TF)

    def test_the_grant_is_read_only_and_on_a_policy_only_dev_server_role_holds(self):
        grant = block('resource "aws_iam_policy" "dev_server_constellation_terraform"', CONST)
        stmt = grant.split('Sid      = "ReadTheFcvmDnsTestToken"', 1)[1].split("}", 1)[0]
        self.assertIn('Action   = "secretsmanager:GetSecretValue"', stmt)
        self.assertIn("Resource = aws_secretsmanager_secret.fcvm_dns_test_token.arn", stmt)
        attach = block('resource "aws_iam_role_policy_attachment" "dev_server_constellation_terraform"', CONST)
        self.assertIn("role       = aws_iam_role.dev_server.name", attach)
        self.assertNotIn("for_each", attach)

    def test_the_value_comes_from_the_token(self):
        version = block('resource "aws_secretsmanager_secret_version" "fcvm_dns_test_token"')
        self.assertIn("secret_string = cloudflare_account_token.fcvm_dns_test.value", version)
        self.assertIn('name                    = "fcvm-dns-test-token"', TF)
        self.assertIn("--secret-id fcvm-dns-test-token", AGENTS)


if __name__ == "__main__":
    unittest.main(buffer=True)

#!/usr/bin/env python3
"""Offline tests for fcvm-dev-prod.tf: the production fcvm fleet's Cloudflare side. Everything is off until
var.fcvm_fleet_prod_enabled; with it on, fleet-api.fcvm.dev serves /live and /poster.jpg publicly and /agent only to the
prod agents' token, fleet-ops.fcvm.dev is Access-only, and the Turnstile secret is readable by the prod coordinator only.
The apex, www and a Worker custom domain are the go-live PR's, not this file's."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "fcvm-dev-prod.tf").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


CODE = code(TF) + "\n"


def block(header):
    start = CODE.index(header)
    return CODE[start:CODE.index("\n}\n", start) + 2]


def headers(kind):
    return re.findall(r'^%s "(\w+)" "(\w+)" \{' % kind, CODE, re.M)


class SwitchTests(unittest.TestCase):
    def test_the_switch_defaults_off(self):
        var = block('variable "fcvm_fleet_prod_enabled"')
        self.assertIn("type        = bool", var)
        self.assertIn("default     = false", var)
        self.assertIn("fcvm_fleet_prod             = var.fcvm_fleet_prod_enabled ? 1 : 0", CODE)

    def test_every_resource_and_data_source_follows_the_switch(self):
        found = [("resource", k, n) for k, n in headers("resource")] + [("data", k, n) for k, n in headers("data")]
        self.assertGreater(len(found), 15)
        for prefix, kind, name in found:
            b = block(f'{prefix} "{kind}" "{name}"')
            self.assertRegex(b, r"\n  count\s+= local\.fcvm_fleet_prod\n", f"{kind}.{name}")
        self.assertIn("value = var.fcvm_fleet_prod_enabled ? {", block('output "fcvm_dev_prod"'))


class GoLiveTests(unittest.TestCase):
    def test_no_apex_www_or_custom_domain(self):
        names = [re.search(r"name    = (\S+)", block(f'resource "cloudflare_dns_record" "{n}"')).group(1)
                 for _, n in headers("resource") if _ == "cloudflare_dns_record"]
        self.assertEqual(sorted(names), ["local.fcvm_dev_api_prod_hostname", "local.fcvm_dev_ops_prod_hostname"])
        self.assertIn('fcvm_dev_api_prod_hostname  = "fleet-api.fcvm.dev"', CODE)
        self.assertIn('fcvm_dev_ops_prod_hostname  = "fleet-ops.fcvm.dev"', CODE)
        self.assertNotIn("cloudflare_workers_custom_domain", CODE)
        self.assertNotIn("cloudflare_workers_route", CODE)
        self.assertNotIn("www", CODE)


class AccessTests(unittest.TestCase):
    def test_agents_and_ci_have_prod_only_tokens(self):
        agents = block('resource "cloudflare_zero_trust_access_policy" "fcvm_fleet_prod_agents"')
        self.assertIn('decision         = "non_identity"', agents)
        self.assertIn("cloudflare_zero_trust_access_service_token.fcvm_fleet_prod_agents[0].id", agents)
        ci = block('resource "cloudflare_zero_trust_access_policy" "fcvm_prod_ci"')
        self.assertIn('decision   = "non_identity"', ci)
        self.assertIn("cloudflare_zero_trust_access_service_token.fcvm_prod_ci[0].id", ci)
        # Staging's credentials are never admitted here.
        for staging in ("fcvm_fleet_agents.", "fcvm_stage_ci", "fcvm_fleet_stage_worker", "fcvm_dev_stage_owner"):
            self.assertNotIn(staging, CODE)
        self.assertNotIn("everyone", CODE)
        self.assertNotIn('"bypass"', CODE)

    def test_agent_path_takes_only_the_agents_and_ops_only_owner_and_ci(self):
        agent = block('resource "cloudflare_zero_trust_access_application" "fcvm_dev_agent_prod"')
        self.assertEqual(re.findall(r"\{ id = ([\w.\[\]]+), precedence", agent),
                         ["cloudflare_zero_trust_access_policy.fcvm_fleet_prod_agents[0].id"])
        self.assertIn('fcvm_dev_agent_prod_path    = "${local.fcvm_dev_api_prod_hostname}/agent"', CODE)
        ops = block('resource "cloudflare_zero_trust_access_application" "fcvm_dev_ops_prod"')
        self.assertEqual(re.findall(r"\{ id = ([\w.\[\]]+), precedence", ops),
                         ["cloudflare_zero_trust_access_policy.fcvm_dev_prod_owner[0].id",
                          "cloudflare_zero_trust_access_policy.fcvm_prod_ci[0].id"])
        owner = block('resource "cloudflare_zero_trust_access_policy" "fcvm_dev_prod_owner"')
        self.assertIn("include          = [{ email = { email = local.browser_manager_owner } }]", owner)


class IngressTests(unittest.TestCase):
    def ingress(self):
        cfg = block('resource "cloudflare_zero_trust_tunnel_cloudflared_config" "fcvm_fleet_prod_coordinator"')
        body = cfg[cfg.index("ingress = ["):]
        rules, depth, cur = [], 0, ""
        for ch in body[body.index("[") + 1:]:
            if ch == "{":
                depth += 1
            if depth:
                cur += ch
            if ch == "}":
                depth -= 1
                if depth == 0:
                    rules.append(cur)
                    cur = ""
            if ch == "]" and depth == 0:
                break
        return rules

    def test_rules_in_order(self):
        rules = self.ingress()
        self.assertEqual(len(rules), 6)
        agent, public, api_rest, ops_agent, ops, last = rules
        self.assertIn('path     = "^/agent$"', agent)
        self.assertIn("required  = true", agent)
        self.assertIn("aud_tag   = [cloudflare_zero_trust_access_application.fcvm_dev_agent_prod[0].aud]", agent)
        # The only routes without the connector's Access check: exactly /live and /poster.jpg on the API host.
        self.assertIn('path     = "^/(live|poster\\\\.jpg)$"', public)
        self.assertIn("hostname = local.fcvm_dev_api_prod_hostname", public)
        self.assertNotIn("access", public)
        self.assertIn('service = "http_status:404"', api_rest)
        self.assertIn("local.fcvm_dev_api_prod_hostname", api_rest)
        self.assertIn('path = "^/agent"', ops_agent)
        self.assertIn('service = "http_status:404"', ops_agent)
        self.assertIn("hostname = local.fcvm_dev_ops_prod_hostname", ops)
        self.assertIn("required  = true", ops)
        self.assertIn("aud_tag   = [cloudflare_zero_trust_access_application.fcvm_dev_ops_prod[0].aud]", ops)
        self.assertEqual(last.strip(), '{ service = "http_status:404" }')
        # Every route that reaches the coordinator, other than the public one, requires Access.
        for r in rules:
            if "http://localhost" in r and r is not public:
                self.assertIn("required  = true", r)


class SecretTests(unittest.TestCase):
    def test_names_tags_and_keys(self):
        for name, key in (("coordinator-tunnel-token", "fcvm_fleet_prod_coordinator_tunnel"),
                          ("agent-access-token", "fcvm_fleet_prod_agent_access"),
                          ("ci-access-token", "fcvm_fleet_prod_ci_access"),
                          ("turnstile", "fcvm_fleet_prod_turnstile")):
            s = block(f'resource "aws_secretsmanager_secret" "{key}"')
            self.assertIn(f'name                    = "fcvm-fleet/prod/{name}"', s)
            self.assertIn("provider                = aws.fcvm_fleet", s)
            self.assertIn("local.fcvm_fleet_prod_tags", s)
        self.assertIn('fcvm_fleet_prod_tags        = { Managed = "ejc3/aws", Fleet = "prod" }', CODE)
        tv = block('resource "aws_secretsmanager_secret_version" "fcvm_fleet_prod_turnstile"')
        self.assertIn("FLEET_TURNSTILE_SITE_KEY = cloudflare_turnstile_widget.fcvm_dev_prod[0].sitekey", tv)
        self.assertIn("FLEET_TURNSTILE_SECRET   = cloudflare_turnstile_widget.fcvm_dev_prod[0].secret", tv)

    def test_turnstile_widget_is_managed_for_fcvm_dev(self):
        w = block('resource "cloudflare_turnstile_widget" "fcvm_dev_prod"')
        self.assertIn('domains    = ["fcvm.dev"]', w)
        self.assertIn('mode       = "managed"', w)

    def test_turnstile_secret_readable_by_the_prod_coordinator_and_terraform_only(self):
        pol = block('resource "aws_secretsmanager_secret_policy" "fcvm_fleet_prod_turnstile"')
        self.assertIn('Effect    = "Deny"', pol)
        self.assertIn('Principal = "*"', pol)
        self.assertIn('Action    = "secretsmanager:GetSecretValue"', pol)
        self.assertIn('ArnNotEquals = { "aws:PrincipalArn" = concat(local.fcvm_fleet_prod_turnstile_readers[0], [local.fcvm_fleet_access_role_arn]) }', pol)
        readers = CODE[CODE.index("fcvm_fleet_prod_turnstile_readers = ["):]
        readers = readers[:readers.index("]]") + 2]
        self.assertIn("data.aws_caller_identity.fcvm_fleet_prod[*].account_id", readers)
        self.assertEqual(re.findall(r'role/\$\{local\.fcvm_fleet_prod_coordinator\}(-execution)?"', readers), ["-execution", ""])
        self.assertIn('fcvm_fleet_prod_coordinator = "fcvm-fleet-prod-coordinator"', CODE)
        self.assertIn("provider = aws.fcvm_fleet", block('data "aws_caller_identity" "fcvm_fleet_prod"'))
        # No Allow statement: the readers' own IAM policies grant, so a missing role is never a malformed policy.
        self.assertNotIn('"Allow"', pol)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

#!/usr/bin/env python3
"""Offline tests for fcvm-dev.tf (ejc3/aws#607): the fcvm.dev zone, its DNS-only token, the coordinator tunnel behind Access,
and the secrets written into the fcvm-fleet account. Staging only: no public apex or fleet-api.fcvm.dev yet."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "fcvm-dev.tf").read_text()


def block(header):
    start = TF.index(header)
    return TF[start:TF.index("\n}\n", start) + 2]


class ZoneTests(unittest.TestCase):
    def test_dnssec_on_and_ssl_full_strict(self):
        dnssec = block('resource "cloudflare_zone_dnssec" "fcvm_dev"')
        self.assertIn('status  = "active"', dnssec)
        self.assertNotIn("ignore_changes", dnssec)
        self.assertIn('value      = "strict"', block('resource "cloudflare_zone_setting" "fcvm_dev_ssl"'))
        self.assertIn('value      = "on"', block('resource "cloudflare_zone_setting" "fcvm_dev_always_use_https"'))

    def test_nothing_public_yet(self):
        # The apex and fleet-api.fcvm.dev go live only on the owner's go-ahead.
        names = re.findall(r'name    = "?([^"\n]+)"?\n', "".join(block(m.group(0)[:-2]) for m in re.finditer(r'resource "cloudflare_dns_record" "\w+" \{', TF)))
        self.assertEqual(names, ["local.fcvm_dev_api_stage_hostname"])
        self.assertNotRegex(TF, r'"fleet-api\.fcvm\.dev"')
        self.assertNotIn('"fcvm.dev"', TF.split("variable", 1)[1].replace('"fcvm.dev staging', ""))


class TokenTests(unittest.TestCase):
    def test_the_dns_token_edits_dns_in_this_zone_only(self):
        tok = block('resource "cloudflare_account_token" "fcvm_fleet_dns"')
        self.assertIn("provider   = cloudflare.token_minter", tok)
        self.assertEqual(re.findall(r"\{ id = ([^}]+) \}", tok), ["local.dns_write"])
        self.assertIn('"com.cloudflare.api.account.zone.${var.fcvm_dev_zone_id}" = "*"', tok)
        self.assertNotIn("com.cloudflare.api.account.${", tok)
        self.assertIn('dns_write           = "4755a26eedb94da69e1066d98aa820be"', TF)


class AccessTests(unittest.TestCase):
    def test_both_stage_hosts_are_owner_only_and_agents_get_only_their_path(self):
        owner = block('resource "cloudflare_zero_trust_access_policy" "fcvm_dev_stage_owner"')
        self.assertIn('decision         = "allow"', owner)
        self.assertIn("include          = [{ email = { email = local.browser_manager_owner } }]", owner)
        for app in ("fcvm_dev_api_stage", "fcvm_dev_site_stage"):
            self.assertIn("cloudflare_zero_trust_access_policy.fcvm_dev_stage_owner.id", block(f'resource "cloudflare_zero_trust_access_application" "{app}"'))
        agents = block('resource "cloudflare_zero_trust_access_policy" "fcvm_fleet_agents"')
        self.assertIn('decision         = "non_identity"', agents)
        self.assertIn("service_token = { token_id = cloudflare_zero_trust_access_service_token.fcvm_fleet_agents.id }", agents)
        self.assertNotIn("everyone", TF)
        self.assertNotIn('"bypass"', TF)

    def test_the_connector_requires_an_access_token_on_every_path(self):
        cfg = block('resource "cloudflare_zero_trust_tunnel_cloudflared_config" "fcvm_fleet_coordinator"')
        self.assertEqual(cfg.count("hostname ="), 1)
        self.assertIn("required  = true", cfg)
        self.assertIn("cloudflare_zero_trust_access_application.fcvm_dev_api_stage.aud", cfg)
        self.assertIn("cloudflare_zero_trust_access_application.fcvm_dev_agent.aud", cfg)

    def test_the_record_waits_for_access(self):
        rec = block('resource "cloudflare_dns_record" "fcvm_dev_api_stage"')
        for dep in ("fcvm_dev_api_stage", "fcvm_dev_agent"):
            self.assertIn(f"cloudflare_zero_trust_access_application.{dep}", rec)


class StageWorkerTests(unittest.TestCase):
    """ejc3/aws#627: the fcvm-fleet-stage Worker's envelope. Its only URL is stage.fcvm.dev, behind the owner-only
    application; workers.dev and previews stay off, and the custom domain is created after Access covers the host."""

    def test_workers_dev_and_previews_stay_off(self):
        sub = block('resource "cloudflare_workers_script_subdomain" "fcvm_fleet_stage"')
        self.assertIn("script_name      = local.fcvm_fleet_stage_worker", sub)
        self.assertIn("enabled          = false", sub)
        self.assertIn("previews_enabled = false", sub)
        self.assertIn('fcvm_fleet_stage_worker = "fcvm-fleet-stage"', TF)
        self.assertIn('id = "${var.cloudflare_account_id}/${local.fcvm_fleet_stage_worker}"', TF)

    def test_the_custom_domain_is_the_owner_only_stage_host_and_waits_for_access(self):
        dom = block('resource "cloudflare_workers_custom_domain" "fcvm_fleet_stage"')
        self.assertIn("zone_id    = var.fcvm_dev_zone_id", dom)
        self.assertIn("hostname   = local.fcvm_dev_site_stage_host", dom)
        self.assertIn("service    = local.fcvm_fleet_stage_worker", dom)
        self.assertIn("cloudflare_zero_trust_access_application.fcvm_dev_site_stage,", dom)
        self.assertIn("cloudflare_workers_script_subdomain.fcvm_fleet_stage,", dom)
        self.assertIn('fcvm_dev_site_stage_host    = "stage.fcvm.dev"', TF)
        self.assertIn("domain           = local.fcvm_dev_site_stage_host", block('resource "cloudflare_zero_trust_access_application" "fcvm_dev_site_stage"'))
        self.assertEqual(TF.count('resource "cloudflare_workers_custom_domain"'), 1)


class FleetSecretTests(unittest.TestCase):
    def test_the_three_secrets_live_in_the_fleet_account(self):
        for name, path in (("fcvm_fleet_dns_token", "fcvm-fleet/cloudflare-dns-token"),
                           ("fcvm_fleet_coordinator_tunnel", "fcvm-fleet/coordinator-tunnel-token"),
                           ("fcvm_fleet_agent_access", "fcvm-fleet/agent-access-token")):
            s = block(f'resource "aws_secretsmanager_secret" "{name}"')
            self.assertIn("provider                = aws.fcvm_fleet", s)
            self.assertIn(f'name                    = "{path}"', s)
            self.assertIn("provider", block(f'resource "aws_secretsmanager_secret_version" "{name}"'))
        self.assertNotIn("aws.fcvm_fleet_west2", TF)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

#!/usr/bin/env python3
"""dolphin-site-probe.tf: a read-only Cloudflare token for the Dolphin account's site probe (Workers Logs on the account,
analytics on gamesworthwatching.com alone), written only into the Dolphin account's container, once that container
exists."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-site-probe.tf").read_text()
OBSERVABILITY = (ROOT / "cloudflare-observability.tf").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, name, text=TF):
    m = re.search(r'^(?:resource|data) "%s" "%s" \{\n(.*?)^\}' % (kind, name), text, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class TokenTests(unittest.TestCase):
    def test_token_reads_workers_logs_and_the_sites_zone_analytics_and_nothing_else(self):
        tok = block("cloudflare_account_token", "dolphin_site_probe")
        self.assertIn("provider   = cloudflare.token_minter", tok)
        groups = re.findall(r"\{ id = local\.(cloudflare_observability_\w+)\.(\w+) \}", tok)
        self.assertEqual(groups, [("cloudflare_observability_account_groups", "workers_observability_read"),
                                  ("cloudflare_observability_zone_groups", "analytics_read")])
        # Both groups are read-only ones the observability token already pins.
        self.assertIn('workers_observability_read = "66c1ed49f4ed46098b75696a6d4ee3c9"', OBSERVABILITY)
        self.assertIn('analytics_read          = "9c88f9c5bce24ce7af9a958ba9c504db"', OBSERVABILITY)
        resources = re.findall(r'"(com\.cloudflare\.[^"]+)" = "\*"', tok)
        self.assertEqual(resources, ["com.cloudflare.api.account.${var.cloudflare_account_id}",
                                     "com.cloudflare.api.account.zone.${var.gww_zone_id}"])
        self.assertNotRegex(tok, r"(?i)write|edit")


class SecretTests(unittest.TestCase):
    def test_value_goes_only_to_the_dolphin_accounts_container_once_it_exists(self):
        ver = block("aws_secretsmanager_secret_version", "dolphin_site_probe")
        self.assertIn("provider  = aws.dolphin", ver)
        self.assertIn("count     = local.dolphin_site_probe_secret_ready ? 1 : 0", ver)
        self.assertIn("secret_id = local.dolphin_site_probe_secret", ver)
        self.assertIn('dolphin_site_probe_secret = "dolphin-site-probe/cloudflare"', TF)
        for key in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ZONE_ID"):
            self.assertIn(key, ver)
        # dolphin-aws owns the container and its policy; this file never creates one in either account.
        self.assertNotRegex(code(TF), r'resource "aws_secretsmanager_secret(_policy)?" ')


if __name__ == "__main__":
    unittest.main()

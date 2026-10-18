#!/usr/bin/env python3
"""dolphin-labs.dev email: the privacy alias and the ej@ and steve@ work addresses forward only to people/addresses keys."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-labs-email.tf").read_text()


class DolphinLabsEmail(unittest.TestCase):
    def test_no_address_literal_in_the_file(self):
        self.assertNotRegex(TF, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")

    def test_work_aliases_are_ej_and_steve_from_people_keys(self):
        block = re.search(r"dolphin_work_aliases = \{(.*?)\n  \}", TF, re.S).group(1)
        self.assertIn("ej    = local.dolphin_privacy_enabled ? local.dolphin_privacy_forward", block)
        self.assertIn("steve = local.dolphin_enabled ? local.dolphin_steve_email", block)
        self.assertIn('value = "${each.key}@${local.dolphin_labs_domain}"', TF)
        self.assertIn("actions  = [{ type = \"forward\", value = [each.value] }]", TF)

    def test_steve_gets_his_own_verified_destination_and_rules_wait_for_destinations(self):
        self.assertIn('resource "cloudflare_email_routing_address" "steve"', TF)
        self.assertIn("email      = local.dolphin_steve_email", TF)
        self.assertIn("depends_on = [cloudflare_email_routing_dns.dolphin, cloudflare_email_routing_address.owner, cloudflare_email_routing_address.steve]", TF)

    def test_work_rules_do_not_share_the_privacy_rule_priority(self):
        prios = re.findall(r"priority = (\d+)", TF)
        self.assertEqual(sorted(prios), ["10", "20"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""mapslink-zone.tf: every request to mapslink.app or www.mapslink.app, any path, is a 302 to the Dolphin Maps car page,
answered at the edge with no origin and no Access application."""
import re
import unittest
from pathlib import Path

TF = (Path(__file__).resolve().parent.parent / "mapslink-zone.tf").read_text()
CODE = re.sub(r"#.*", "", TF)


def block(kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n(.*?)^\}' % (kind, name), TF, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class MapslinkTests(unittest.TestCase):
    def test_both_names_are_proxied_with_no_origin(self):
        for name, rtype, content in (("mapslink_apex", "A", "192.0.2.1"), ("mapslink_www", "CNAME", "mapslink.app")):
            record = block("cloudflare_dns_record", name)
            self.assertIn("zone_id = var.mapslink_zone_id", record)
            self.assertIn('type    = "%s"\n' % rtype, record)
            self.assertIn('content = "%s"\n' % content, record)
            self.assertIn("proxied = true\n", record)
            # Cloudflare's Free plan caps a record comment at 100 characters.
            self.assertLessEqual(len(re.search(r'comment = "(.*)"\n', record).group(1)), 100)

    def test_every_request_is_a_302_to_the_car_page(self):
        self.assertIn('mapslink_target = "https://maps.dolphin-labs.dev/car"', TF)
        rules = block("cloudflare_ruleset", "mapslink_redirects")
        self.assertIn("zone_id     = var.mapslink_zone_id", rules)
        self.assertIn('phase       = "http_request_dynamic_redirect"', rules)
        self.assertEqual(rules.count("ref "), 1)
        self.assertIn('expression  = "(http.host in {\\"mapslink.app\\" \\"www.mapslink.app\\"})"', rules)
        self.assertIn("status_code           = 302", rules)
        self.assertIn("target_url            = { value = local.mapslink_target }", rules)

    def test_https_only_and_no_access_gate(self):
        setting = block("cloudflare_zone_setting", "mapslink_always_use_https")
        self.assertIn('setting_id = "always_use_https"', setting)
        self.assertIn('value      = "on"', setting)
        self.assertNotIn("zero_trust_access", CODE)
        self.assertNotIn("workers_route", CODE)


if __name__ == "__main__":
    unittest.main(buffer=True)

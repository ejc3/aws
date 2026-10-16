#!/usr/bin/env python3
"""gamesworthwatching-zone.tf: http and www reach https://gamesworthwatching.com at the edge, before the Worker and its
static assets, with the path and query kept."""
import re
import unittest
from pathlib import Path

TF = (Path(__file__).resolve().parent.parent / "gamesworthwatching-zone.tf").read_text()


def block(kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n(.*?)^\}' % (kind, name), TF, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class RedirectTests(unittest.TestCase):
    def test_http_is_upgraded_on_this_zone(self):
        setting = block("cloudflare_zone_setting", "gww_always_use_https")
        self.assertIn("zone_id    = var.gww_zone_id", setting)
        self.assertIn('setting_id = "always_use_https"', setting)
        self.assertIn('value      = "on"', setting)

    def test_www_goes_to_the_apex_keeping_path_and_query(self):
        rules = block("cloudflare_ruleset", "gww_redirects")
        self.assertIn("zone_id     = var.gww_zone_id", rules)
        self.assertIn('phase       = "http_request_dynamic_redirect"', rules)
        self.assertEqual(rules.count("ref "), 1)
        self.assertIn('expression  = "(http.host eq \\"www.gamesworthwatching.com\\")"', rules)
        self.assertIn("status_code           = 308", rules)
        self.assertIn('target_url            = { expression = "concat(\\"https://gamesworthwatching.com\\", http.request.uri.path)" }', rules)
        self.assertIn("preserve_query_string = true", rules)


if __name__ == "__main__":
    unittest.main()

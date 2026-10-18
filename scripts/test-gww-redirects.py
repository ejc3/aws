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


class ServingTests(unittest.TestCase):
    """The Worker dolphin-labs-prod is the only shape: proxied records and routes, no switch back to Vercel (the
    dolphin-labs Vercel team is being deleted), and the record contents left as they are served."""

    CODE = re.sub(r"#.*", "", TF)

    def test_both_records_are_always_proxied(self):
        for name, content in (("gww_apex", '"76.76.21.21"'), ("gww_www", '"cname.vercel-dns.com"')):
            record = block("cloudflare_dns_record", name)
            self.assertIn("zone_id = var.gww_zone_id", record)
            self.assertIn("proxied = true\n", record)
            self.assertIn("ttl     = 1\n", record)
            self.assertIn('comment = "proxied; ', record)
            # Unchanged from what is served, so this file's history never replaces a serving record.
            self.assertIn("content = %s\n" % content, record)

    def test_the_routes_are_unconditional_and_go_to_the_production_worker(self):
        self.assertIn('gww_worker = "dolphin-labs-prod"', TF)
        route = block("cloudflare_workers_route", "gww")
        self.assertIn('for_each = toset(["gamesworthwatching.com/*", "www.gamesworthwatching.com/*"])', route)
        self.assertIn("script   = local.gww_worker", route)

    def test_there_is_no_switch_or_variable_pointing_at_vercel(self):
        self.assertNotIn("_on_cloudflare", TF)
        self.assertNotIn("gww_vercel", TF)
        self.assertNotIn(" ? ", self.CODE, "no conditional: the Worker path is the only shape")
        self.assertNotRegex(self.CODE, r'(?i)"vercel (apex|www)')


if __name__ == "__main__":
    unittest.main()

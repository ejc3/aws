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


class RateLimitTests(unittest.TestCase):
    """The anonymous per-request routes are limited per address at the edge (dolphin-labs#1609, #1885), and nothing
    else on the site is."""

    def test_two_anonymous_routes_are_limited_per_address(self):
        rules = block("cloudflare_ruleset", "gww_rate_limit")
        self.assertIn("zone_id     = var.gww_zone_id", rules)
        self.assertIn('phase       = "http_ratelimit"', rules)
        self.assertEqual(rules.count("ref "), 1)
        self.assertIn('expression  = "(http.request.uri.path in {\\"/api/media/search\\" \\"/api/app-health\\"})"', rules)
        self.assertIn('action      = "block"', rules)
        # The Free plan's only shape: address and data centre, a 10 s period and a 10 s block.
        self.assertIn('characteristics     = ["ip.src", "cf.colo.id"]', rules)
        self.assertIn("period              = 10\n", rules)
        self.assertIn("mitigation_timeout  = 10\n", rules)
        self.assertIn("requests_per_period = 60\n", rules)

    def test_no_other_rate_limit_ruleset_on_this_zone(self):
        self.assertEqual(TF.count('phase       = "http_ratelimit"'), 1)


class ServingTests(unittest.TestCase):
    """The Worker dolphin-labs-prod is the only shape: proxied records with no origin, routes, and no switch back to
    Vercel (the dolphin-labs Vercel team is being deleted)."""

    CODE = re.sub(r"#.*", "", TF)

    def test_both_records_are_proxied_and_have_no_origin(self):
        # The apex is the Workers-only placeholder; www stays a CNAME (a type change is a delete+create, with a gap) and
        # points at the apex, inside this zone.
        for name, rtype, content in (("gww_apex", "A", "192.0.2.1"), ("gww_www", "CNAME", "gamesworthwatching.com")):
            record = block("cloudflare_dns_record", name)
            self.assertIn("zone_id = var.gww_zone_id", record)
            self.assertIn('type    = "%s"\n' % rtype, record)
            self.assertIn('content = "%s"\n' % content, record)
            self.assertIn("proxied = true\n", record)
            self.assertIn("ttl     = 1\n", record)
            comment = re.search(r'comment = "(.*)"\n', record).group(1)
            self.assertTrue(comment.startswith("proxied; Worker ${local.gww_worker} route serves all"), comment)
            # Cloudflare's Free plan caps a record comment at 100 characters.
            self.assertLessEqual(len(comment.replace("${local.gww_worker}", "dolphin-labs-prod")), 100)

    def test_no_record_names_vercel(self):
        for name in ("gww_apex", "gww_www"):
            record = block("cloudflare_dns_record", name)
            self.assertNotRegex(record, r"(?i)vercel|76\.76\.21\.|cname\.vercel-dns", name)
        self.assertNotRegex(self.CODE, r"(?i)vercel-dns|76\.76\.21\.\d+")

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
    unittest.main(buffer=True)

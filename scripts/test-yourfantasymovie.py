#!/usr/bin/env python3
"""yourfantasymovie.tf: the Fantasy Films hostname. Served by the Worker dolphin-films-prod through proxied records and
Workers routes, one switch (local.yourfantasymovie_on_cloudflare) that also turns the records back to DNS only for Vercel."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "yourfantasymovie.tf").read_text()
FILMS = (ROOT / "dolphin-films.tf").read_text()


class DnsTests(unittest.TestCase):
    def test_every_record_follows_the_switch_and_is_in_the_one_zone(self):
        records = re.findall(r'resource "cloudflare_dns_record" "(\w+)" \{(.*?)\n\}', TF, re.S)
        self.assertEqual(sorted(n for n, _ in records), ["yourfantasymovie_apex", "yourfantasymovie_www"])
        for name, body in records:
            self.assertRegex(body, r"proxied +=  ?local\.yourfantasymovie_on_cloudflare\n", name)
            self.assertRegex(body, r"ttl +=  ?local\.yourfantasymovie_on_cloudflare \? 1 : 300\n", name)
            self.assertIn("var.yourfantasymovie_zone_id", body)

    def test_the_routes_send_apex_and_www_to_the_production_worker_only_when_switched(self):
        self.assertIn('yourfantasymovie_worker        = "dolphin-films-prod"', TF)
        route = re.search(r'resource "cloudflare_workers_route" "yourfantasymovie" \{(.*?)\n\}', TF, re.S).group(1)
        self.assertIn('local.yourfantasymovie_on_cloudflare ? toset(["yourfantasymovie.com/*", "www.yourfantasymovie.com/*"]) : toset([])', route)
        self.assertIn("var.yourfantasymovie_zone_id", route)
        self.assertIn("local.yourfantasymovie_worker", route)

    def test_the_apex_has_vercels_two_addresses_and_www_a_cname(self):
        self.assertRegex(TF, r'yourfantasymovie_apex_ips += \["216\.150\.1\.1", "216\.150\.16\.1"\]')
        self.assertIn('type    = "CNAME"', TF)
        self.assertRegex(TF, r'content = "[a-z0-9]+\.vercel-dns-\d+\.com"')

    def test_the_zone_id_is_this_domains_and_no_other_zone_is_touched(self):
        self.assertIn('default     = "3580bb80ae9321c474048a88bfeec525"', TF)
        for other in ("38302d3b9d8d603a055d42f7a4a86ec9", "7efede9790c3944c5d8b3a1fb3747fdf", "5a7a8d961d72744d2e7fd155dcdeb42b"):
            self.assertNotIn(other, TF)

    def test_no_secret_or_vercel_resource_is_here(self):
        self.assertNotIn("vercel_", re.sub(r"#.*", "", TF).replace("vercel-dns", "").replace('"vercel apex', "").replace('"vercel www', ""))
        self.assertNotRegex(TF, r"eyJ[A-Za-z0-9_-]{20,}")

    def test_dolphin_films_tf_points_at_it(self):
        self.assertIn("yourfantasymovie.com (yourfantasymovie.tf)", FILMS)
        self.assertNotIn("TODO(owner): if it gets a hostname", FILMS)


if __name__ == "__main__":
    unittest.main()

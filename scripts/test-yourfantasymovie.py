#!/usr/bin/env python3
"""yourfantasymovie.tf: the Fantasy Films hostname. DNS only (never proxied: that breaks Vercel certificate issuance), the
records Vercel reports for the project, and nothing else here (the Vercel project is made with its CLI)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "yourfantasymovie.tf").read_text()
FILMS = (ROOT / "dolphin-films.tf").read_text()


class DnsTests(unittest.TestCase):
    def test_every_record_is_dns_only_and_in_the_one_zone(self):
        records = re.findall(r'resource "cloudflare_dns_record" "(\w+)" \{(.*?)\n\}', TF, re.S)
        self.assertEqual(sorted(n for n, _ in records), ["yourfantasymovie_apex", "yourfantasymovie_www"])
        for name, body in records:
            self.assertIn("proxied  = false" if "proxied  =" in body else "proxied = false", body, name)
            self.assertIn("zone_id", body)
            self.assertIn("var.yourfantasymovie_zone_id", body)

    def test_the_apex_has_vercels_two_addresses_and_www_a_cname(self):
        self.assertIn('yourfantasymovie_apex_ips = ["216.150.1.1", "216.150.16.1"]', TF)
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

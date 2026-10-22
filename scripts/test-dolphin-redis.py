#!/usr/bin/env python3
"""dolphin-redis.tf: what this repository still owns of the Dolphin sites' Redis (the store runs on ECS, built by
dolphin-labs-hq/dolphin-aws): the tunnel serving only the two hostnames, its DNS, the tokens and the two secrets in the
Dolphin account, with no credential in the file and no instance left behind."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-redis.tf").read_text()


class DolphinRedisTests(unittest.TestCase):
    def test_every_aws_resource_is_in_the_dolphin_account(self):
        found = re.findall(r'(?:resource|data) "(aws_\w+)" "(\w+)" \{(.*?)\n\}', TF, re.S)
        self.assertTrue(found)
        for kind, name, body in found:
            self.assertIn("provider = aws.dolphin", re.sub(r"\s+", " ", body), f"{kind}.{name}")

    def test_the_retired_instance_left_nothing_behind(self):
        # The store runs on ECS in the Dolphin account (dolphin-aws); none of the instance's pieces may come back here.
        for kind in ("aws_instance", "aws_security_group", "aws_iam_role", "aws_iam_instance_profile", "aws_dlm_lifecycle_policy"):
            self.assertNotIn(f'resource "{kind}"', TF, kind)
        self.assertFalse((ROOT / "scripts/dolphin-redis").exists(), "the patch now lives with the image build in dolphin-aws")

    def test_the_tunnel_serves_only_the_two_hostnames_on_loopback(self):
        self.assertIn('{ service = "http_status:404" }', TF)
        self.assertIn('host = "kv.gamesworthwatching.com"', TF)
        self.assertIn('host = "kv-stage.gamesworthwatching.com"', TF)
        self.assertIn('service = "http://127.0.0.1:${r.port}"', TF)
        self.assertNotIn("dolphin-labs.dev\"", TF)

    def test_no_secret_is_written_here(self):
        self.assertNotRegex(TF, r"(eyJ[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})")
        self.assertIn("random_password.dolphin_redis_token", TF)
        self.assertIn("data.cloudflare_zero_trust_tunnel_cloudflared_token.dolphin_redis.token", TF)


if __name__ == "__main__":
    unittest.main(buffer=True)

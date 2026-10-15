#!/usr/bin/env python3
"""dolphin-redis.tf: the Dolphin sites' Redis in the Dolphin account. No inbound path, every resource in the Dolphin
account except the tunnel and DNS, no credential in the file, and the data volume protected."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-redis.tf").read_text()


def resources(kind):
    return re.findall(r'resource "%s" "(\w+)" \{(.*?)\n\}' % kind, TF, re.S)


class DolphinRedisTests(unittest.TestCase):
    def test_every_aws_resource_is_in_the_dolphin_account(self):
        for kind, name, body in re.findall(r'(?:resource|data) "(aws_\w+)" "(\w+)" \{(.*?)\n\}', TF, re.S):
            self.assertIn("provider = aws.dolphin", re.sub(r"\s+", " ", body), f"{kind}.{name}")

    def test_no_inbound_rule_and_no_public_service_port(self):
        [(_, sg)] = resources("aws_security_group")
        self.assertNotIn("ingress", sg)
        for port in ("8079", "8078"):
            self.assertIn(f"-p 127.0.0.1:${{r.port}}:80", TF)
        self.assertNotRegex(TF, r"-p 0\.0\.0\.0|-p \$\{r\.port\}")

    def test_the_instance_keeps_its_data(self):
        [(_, inst)] = resources("aws_instance")
        self.assertIn("prevent_destroy = true", inst)
        self.assertIn("delete_on_termination = false", inst)
        self.assertIn('http_tokens   = "required"', inst)

    def test_the_tunnel_serves_only_the_two_hostnames(self):
        self.assertIn('{ service = "http_status:404" }', TF)
        self.assertIn('host = "kv.gamesworthwatching.com"', TF)
        self.assertIn('host = "kv-stage.gamesworthwatching.com"', TF)
        self.assertNotIn("dolphin-labs.dev\"", TF)

    def test_images_are_pinned_and_no_secret_is_written_here(self):
        for image in re.findall(r'= "([\w./-]+:[\w.-]+)"', TF):
            self.assertNotRegex(image, r":latest$")
        self.assertNotRegex(TF, r"(eyJ[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16})")
        self.assertIn("random_password.dolphin_redis_token", TF)

    def test_the_rest_server_is_built_from_a_pinned_commit_with_a_raised_body_limit(self):
        self.assertRegex(TF, r'dolphin_redis_srh_commit = "[0-9a-f]{40}"')
        self.assertRegex(TF, r'srh +=  ?"srh-local:0\.0\.10-auth-body64mb"')
        self.assertIn("git -C /opt/srh-src checkout -q ${local.dolphin_redis_srh_commit}", TF)
        self.assertIn('file("${path.module}/scripts/dolphin-redis/srh-auth.patch")', TF)
        self.assertIn("--oom-score-adj -1000", TF)
        patch = (ROOT / "scripts/dolphin-redis/srh-auth.patch").read_text()
        self.assertIn("length: 64_000_000", patch)
        # the token is checked before the body is parsed
        self.assertLess(patch.index("+  plug(:require_token)"), patch.index("length: 64_000_000"))
        self.assertIn("Plug.Crypto.secure_compare", patch)

    def test_the_instance_may_read_only_its_own_secret(self):
        [(_, pol)] = resources("aws_iam_role_policy")
        self.assertIn('Action = "secretsmanager:GetSecretValue"', pol)
        self.assertIn("aws_secretsmanager_secret.dolphin_redis_instance.arn", pol)


if __name__ == "__main__":
    unittest.main()

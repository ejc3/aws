#!/usr/bin/env python3
"""browser-manager.tf: rotating the AWS browser connector keeps the remotely managed tunnel's identity, and the new
token reaches only its existing secret. (The application itself lives in github.com/ejc3/browser-manager.)"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "browser-manager.tf").read_text()
MAC = (ROOT / "browser-manager-mac.tf").read_text()


def block(kind, type_, name):
    match = re.search(rf'^{kind} "{type_}" "{name}" \{{\n.*?^\}}', SOURCE, re.M | re.S)
    assert match, f"missing {kind} {type_}.{name}"
    return match.group(0)


class TunnelRotation(unittest.TestCase):
    def test_rotation_preserves_the_remotely_managed_tunnel_identity(self):
        secret = block("resource", "random_bytes", "browser_manager_tunnel_secret")
        self.assertRegex(secret, r"length\s*=\s*32\b")
        self.assertRegex(secret, r'keepers\s*=\s*\{\s*rotation\s*=\s*"\d{4}-\d{2}-\d{2}"\s*\}')
        tunnel = block("resource", "cloudflare_zero_trust_tunnel_cloudflared", "browser_manager")
        self.assertRegex(tunnel, r'config_src\s*=\s*"cloudflare"')
        self.assertRegex(tunnel, r"tunnel_secret\s*=\s*random_bytes\.browser_manager_tunnel_secret\.base64")
        self.assertRegex(tunnel, r"prevent_destroy\s*=\s*true")
        self.assertNotRegex(tunnel, r"replace_triggered_by|ignore_changes|local-exec|remote-exec")
        self.assertNotIn('resource "random_id" "browser_manager_tunnel_secret"', SOURCE)
        self.assertNotIn("random_bytes.browser_manager_tunnel_secret.", MAC)

    def test_replacement_token_is_read_after_the_patch_and_published_only_to_its_secret(self):
        token = block("data", "cloudflare_zero_trust_tunnel_cloudflared_token", "browser_manager")
        self.assertRegex(token, r"tunnel_id\s*=\s*cloudflare_zero_trust_tunnel_cloudflared\.browser_manager\.id")
        self.assertRegex(token, r"depends_on\s*=\s*\[cloudflare_zero_trust_tunnel_cloudflared\.browser_manager\]")
        version = block("resource", "aws_secretsmanager_secret_version", "browser_manager_tunnel_token")
        self.assertRegex(version, r"secret_id\s*=\s*aws_secretsmanager_secret\.browser_manager_tunnel_token\.id")
        self.assertRegex(version, r"secret_string\s*=\s*data\.cloudflare_zero_trust_tunnel_cloudflared_token\.browser_manager\.token")
        output = re.search(r'^output "browser_manager_tunnel_token" \{\n.*?^\}', SOURCE, re.M | re.S)
        self.assertTrue(output)
        self.assertRegex(output.group(0), r"sensitive\s*=\s*true")


if __name__ == "__main__":
    unittest.main(buffer=True)

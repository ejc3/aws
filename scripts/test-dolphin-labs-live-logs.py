#!/usr/bin/env python3
"""dolphin-labs-live-logs.tf: the read token reaches that one bucket's objects, read only, and its S3 credentials are
written only into the Dolphin account's container, once that container exists."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-labs-live-logs.tf").read_text()
PODCASTS = (ROOT / "dolphin-labs-podcasts.tf").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, name, text=TF):
    m = re.search(r'^(?:resource|data) "%s" "%s" \{\n(.*?)^\}' % (kind, name), text, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class TokenTests(unittest.TestCase):
    def test_token_only_reads_objects_in_this_bucket(self):
        tok = block("cloudflare_account_token", "dolphin_labs_live_logs_read")
        self.assertIn("provider   = cloudflare.token_minter", tok)
        self.assertEqual(re.findall(r"\{ id = local\.([a-z0-9_]+) \}", tok), ["r2_bucket_item_read"])
        self.assertIn('r2_bucket_item_read  = "6a018a9f2fc74eb6b293b0c548f38b39"', PODCASTS)
        self.assertEqual(
            re.findall(r'"(com\.cloudflare\.[^"]+)" = "\*"', tok),
            ["com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.dolphin_labs_live_logs.name}"],
        )
        self.assertNotIn("write", code(tok).lower())


class SecretTests(unittest.TestCase):
    def test_value_goes_only_to_the_dolphin_accounts_container(self):
        ver = block("aws_secretsmanager_secret_version", "dolphin_labs_live_logs_read")
        self.assertIn("provider  = aws.dolphin", ver)
        self.assertIn("secret_id = local.dolphin_labs_live_logs_read_secret", ver)
        self.assertIn('dolphin_labs_live_logs_read_secret = "dolphin-labs/live-logs-r2"', TF)
        # dolphin-aws owns the container and its policy; this file never creates one in either account.
        self.assertNotRegex(code(TF), r'resource "aws_secretsmanager_secret(_policy)?" ')
        self.assertNotRegex(code(TF), r'resource "(aws_iam_|github_)')

    def test_value_is_the_s3_pair_derived_from_the_token(self):
        ver = block("aws_secretsmanager_secret_version", "dolphin_labs_live_logs_read")
        self.assertEqual(
            re.findall(r"^\s+(R2_[A-Z_]+)\s+=", ver, re.M),
            ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_ENDPOINT"],
        )
        self.assertIn("R2_ACCESS_KEY_ID     = cloudflare_account_token.dolphin_labs_live_logs_read.id", ver)
        self.assertIn("R2_SECRET_ACCESS_KEY = sha256(cloudflare_account_token.dolphin_labs_live_logs_read.value)", ver)

    def test_nothing_is_written_until_the_exact_container_exists(self):
        ver = block("aws_secretsmanager_secret_version", "dolphin_labs_live_logs_read")
        self.assertIn("count     = local.dolphin_labs_live_logs_read_secret_ready ? 1 : 0", ver)
        self.assertIn("provider = aws.dolphin", block("aws_secretsmanager_secrets", "dolphin_labs_live_logs_read"))
        self.assertIn(
            "dolphin_labs_live_logs_read_secret_ready = contains(data.aws_secretsmanager_secrets.dolphin_labs_live_logs_read.names, local.dolphin_labs_live_logs_read_secret)",
            TF,
        )


if __name__ == "__main__":
    unittest.main()

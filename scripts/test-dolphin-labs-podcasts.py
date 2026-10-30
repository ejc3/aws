#!/usr/bin/env python3
"""dolphin-labs-podcasts.tf: a private R2 bucket in the Workers' account, a token that reaches that bucket's objects and
nothing else, and its S3 credentials written only into the Dolphin account's container, once that container exists."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-labs-podcasts.tf").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, name, text=TF):
    m = re.search(r'^(?:resource|data) "%s" "%s" \{\n(.*?)^\}' % (kind, name), text, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class BucketTests(unittest.TestCase):
    def test_bucket_is_in_the_workers_account_under_the_name_the_binding_uses(self):
        b = block("cloudflare_r2_bucket", "dolphin_labs_podcasts")
        self.assertIn("account_id = var.cloudflare_account_id", b)
        self.assertIn('dolphin_labs_podcasts_bucket = "dolphin-labs-podcasts"', TF)
        self.assertIn("name       = local.dolphin_labs_podcasts_bucket", b)

    def test_bucket_has_no_public_address(self):
        self.assertIn("enabled     = false", block("cloudflare_r2_managed_domain", "dolphin_labs_podcasts"))
        self.assertNotIn("cloudflare_r2_custom_domain", code(TF))

    def test_objects_outlive_the_pipelines_120_days(self):
        rule = block("cloudflare_r2_bucket_lifecycle", "dolphin_labs_podcasts")
        self.assertIn("max_age = 180 * 24 * 3600", rule)
        self.assertIn('conditions = { prefix = "" }', rule)


class TokenTests(unittest.TestCase):
    def test_token_reads_and_writes_objects_in_this_bucket_only(self):
        tok = block("cloudflare_account_token", "dolphin_labs_podcasts_r2")
        self.assertIn("provider   = cloudflare.token_minter", tok)
        self.assertEqual(re.findall(r"\{ id = local\.([a-z0-9_]+) \}", tok), ["r2_bucket_item_read", "r2_bucket_item_write"])
        self.assertIn('r2_bucket_item_read  = "6a018a9f2fc74eb6b293b0c548f38b39"', TF)
        self.assertIn('r2_bucket_item_write = "2efd5506f9c8494dacb1fa10a3e7d5b6"', TF)
        resources = re.findall(r'"(com\.cloudflare\.[^"]+)" = "\*"', tok)
        self.assertEqual(
            resources,
            ["com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.dolphin_labs_podcasts.name}"],
        )


class SecretTests(unittest.TestCase):
    def test_value_goes_only_to_the_dolphin_accounts_container(self):
        ver = block("aws_secretsmanager_secret_version", "dolphin_labs_podcasts_r2")
        self.assertIn("provider  = aws.dolphin", ver)
        self.assertIn("secret_id = local.dolphin_labs_podcasts_secret", ver)
        self.assertIn('dolphin_labs_podcasts_secret = "dolphin-labs/podcasts-r2"', TF)
        # dolphin-aws owns the container and its policy; this file never creates one in either account.
        self.assertNotRegex(code(TF), r'resource "aws_secretsmanager_secret(_policy)?" ')
        self.assertNotRegex(code(TF), r'resource "(aws_iam_|github_)')

    def test_value_is_the_s3_pair_derived_from_the_token(self):
        ver = block("aws_secretsmanager_secret_version", "dolphin_labs_podcasts_r2")
        self.assertEqual(
            re.findall(r"^\s+(R2_[A-Z_]+)\s+=", ver, re.M),
            ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"],
        )
        self.assertIn("R2_ACCESS_KEY_ID     = cloudflare_account_token.dolphin_labs_podcasts_r2.id", ver)
        self.assertIn("R2_SECRET_ACCESS_KEY = sha256(cloudflare_account_token.dolphin_labs_podcasts_r2.value)", ver)

    def test_nothing_is_written_until_the_exact_container_exists(self):
        ver = block("aws_secretsmanager_secret_version", "dolphin_labs_podcasts_r2")
        self.assertIn("count     = local.dolphin_labs_podcasts_secret_ready ? 1 : 0", ver)
        self.assertIn("provider = aws.dolphin", block("aws_secretsmanager_secrets", "dolphin_labs_podcasts_r2"))
        self.assertIn(
            "dolphin_labs_podcasts_secret_ready = contains(data.aws_secretsmanager_secrets.dolphin_labs_podcasts_r2.names, local.dolphin_labs_podcasts_secret)",
            TF,
        )

    def test_no_credential_in_the_output(self):
        out = re.search(r'^output "dolphin_labs_podcasts" \{\n(.*?)^\}', TF, re.M | re.S).group(1)
        self.assertNotIn(".value", out)
        self.assertNotIn("sha256", out)


if __name__ == "__main__":
    unittest.main(buffer=True)

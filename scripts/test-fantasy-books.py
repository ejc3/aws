#!/usr/bin/env python3
"""Open Fantasy's staging books: one private R2 bucket, a token for its objects only, read by fcvm-arm alone."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "fantasy-books.tf").read_text()
CODE = "\n".join(line for line in TF.splitlines() if not line.lstrip().startswith("#"))


def block(kind, name):
    match = re.search(r'^resource "%s" "%s" \{\n(.*?)^\}' % (kind, name), CODE, re.M | re.S)
    assert match, (kind, name)
    return match.group(1)


class BucketTests(unittest.TestCase):
    def test_fixed_private_bucket(self):
        bucket = block("cloudflare_r2_bucket", "fantasy_books")
        self.assertIn('fantasy_books_bucket = "fantasy-stage-books"', CODE)
        self.assertIn("name          = local.fantasy_books_bucket", bucket)
        domain = block("cloudflare_r2_managed_domain", "fantasy_books")
        self.assertIn("enabled     = false", domain)
        self.assertNotRegex(CODE, r'"cloudflare_r2_(custom_domain|bucket_cors)"')

    def test_bucket_and_one_time_credential_cannot_be_replaced_implicitly(self):
        for kind, name in (("cloudflare_r2_bucket", "fantasy_books"), ("cloudflare_account_token", "fantasy_books_r2")):
            with self.subTest(kind=kind):
                self.assertRegex(block(kind, name), r"lifecycle\s*\{\s*prevent_destroy = true\s*\}")


class CredentialTests(unittest.TestCase):
    def test_token_reads_and_writes_this_bucket_only(self):
        token = block("cloudflare_account_token", "fantasy_books_r2")
        self.assertIn("provider   = cloudflare.token_minter", token)
        self.assertEqual(re.findall(r"\{ id = local\.([a-z0-9_]+) \}", token), ["r2_bucket_item_read", "r2_bucket_item_write"])
        self.assertEqual(re.findall(r'"(com\.cloudflare\.[^"]+)"\s*=\s*"\*"', token),
                         ["com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.fantasy_books.name}"])

    def test_s3_pair_comes_from_the_narrow_token_and_goes_only_to_the_secret(self):
        self.assertIn("R2_ACCESS_KEY_ID     = cloudflare_account_token.fantasy_books_r2.id", CODE)
        self.assertIn("R2_SECRET_ACCESS_KEY = sha256(cloudflare_account_token.fantasy_books_r2.value)", CODE)
        version = block("aws_secretsmanager_secret_version", "fantasy_books_upload")
        self.assertIn("depends_on    = [aws_secretsmanager_secret_policy.fantasy_books_upload]", version)
        self.assertNotRegex(CODE, re.compile(r'^output\s', re.M))
        self.assertNotIn("github_", CODE)


class ReaderTests(unittest.TestCase):
    def test_administration_and_fcvm_arm_only(self):
        self.assertIn('"fantasy/books-upload"', block("aws_secretsmanager_secret", "fantasy_books_upload"))
        policy = block("aws_secretsmanager_secret_policy", "fantasy_books_upload")
        self.assertEqual(re.findall(r'Effect\s*= "(.*?)"', policy), ["Allow", "Deny", "Deny"])
        allow, others, x86 = policy.split("Sid       = ")[1:]
        self.assertIn("Principal = { AWS = aws_iam_role.dev_server.arn }", allow)
        self.assertIn('ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn]) }', others)
        self.assertIn('StringNotEqualsIfExists = { "aws:userid" = try(local.browser_run_arm_userids[0], "no-fcvm-arm-instance") }', x86)
        for statement in (allow, others, x86):
            self.assertIn('Action    = "secretsmanager:GetSecretValue"', statement)

    def test_resource_set(self):
        self.assertEqual(re.findall(r'^resource "([^"]+)" "([^"]+)"', CODE, re.M), [
            ("cloudflare_r2_bucket", "fantasy_books"),
            ("cloudflare_r2_managed_domain", "fantasy_books"),
            ("cloudflare_account_token", "fantasy_books_r2"),
            ("aws_secretsmanager_secret", "fantasy_books_upload"),
            ("aws_secretsmanager_secret_version", "fantasy_books_upload"),
            ("aws_secretsmanager_secret_policy", "fantasy_books_upload"),
        ])

    def test_ci_runs_this_suite(self):
        self.assertIn("python3 -S -B scripts/test-fantasy-books.py", (ROOT / ".github/workflows/lambda-tests.yml").read_text())


if __name__ == "__main__":
    unittest.main(buffer=True)

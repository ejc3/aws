#!/usr/bin/env python3
"""SDK-free exact-scope guards; fresh plan/live readback remain required."""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
FLAGS = ["block_public_acls", "ignore_public_acls", "block_public_policy", "restrict_public_buckets"]


class S3AccountBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "security-s3-account.tf").read_text()
        self.resources = re.findall(r'(?ms)^resource "([^"]+)" "([^"]+)" \{\n(.*?)^\}', self.source)

    def test_exactly_two_account_controls_and_no_other_resources(self):
        self.assertEqual([(kind, name) for kind, name, _ in self.resources], [
            ("aws_s3_account_public_access_block", "main"),
            ("aws_s3_account_public_access_block", "staging"),
        ])
        self.assertEqual(re.findall(r'^([a-z_]+) ', self.source, re.M), ["resource", "resource"])

    def test_every_public_access_flag_is_unconditionally_true(self):
        for _, name, body in self.resources:
            for flag in FLAGS:
                with self.subTest(account=name, flag=flag):
                    self.assertRegex(body, rf'(?m)^\s*{flag}\s*=\s*true\s*$')
        self.assertNotRegex(self.source, r'\b(count|for_each|lifecycle)\s*[={]')

    def test_exact_existing_account_provider_identity_mapping(self):
        bodies = {name: body for _, name, body in self.resources}
        self.assertRegex(bodies["main"], r'account_id\s*=\s*data\.aws_caller_identity\.current\.account_id')
        self.assertNotRegex(bodies["main"], r'\bprovider\s*=')
        self.assertRegex(bodies["staging"], r'provider\s*=\s*aws\.staging\s')
        self.assertRegex(bodies["staging"], r'account_id\s*=\s*data\.aws_caller_identity\.staging\.account_id')

    def test_no_hidden_permissions_or_bucket_changes(self):
        for _, name, body in self.resources:
            expected = ["account_id", *FLAGS] if name == "main" else ["provider", "account_id", *FLAGS]
            self.assertEqual(re.findall(r'^\s*([a-z_]+)\s*=', body, re.M), expected)
        for forbidden in ("aws_organizations", "aws_iam", "aws_s3_bucket", "aws_s3_object",
                          "provisioner", "local-exec", "ignore_changes"):
            self.assertNotIn(forbidden, self.source)

    def test_credential_free_workflow_preserves_existing_security_tests(self):
        workflow = (ROOT / ".github/workflows/lambda-tests.yml").read_text()
        for script in ("test-s3-account-public-access.py", "test-security-defaults.py",
                       "test-security-external-access.py", "test-backup-recovery.py"):
            self.assertIn("python3 -S -B scripts/" + script, workflow)
        self.assertNotIn("id-token: write", workflow)
        self.assertNotIn("configure-aws-credentials", workflow)

    def test_documentation_distinguishes_existing_access_and_propagation(self):
        readme = (ROOT / "README.md").read_text()
        for phrase in ("two global account controls", "can also block existing public access",
                       "No objects were enumerated or read", "exactly two account-setting creates",
                       "not necessarily simultaneously", "s3-public-metadata.json"):
            self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main(buffer=True)

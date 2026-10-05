#!/usr/bin/env python3
"""people.tf: addresses live in one Secrets Manager secret read only by administration, not in git. The container is created empty
(Terraform holds no value) and no tracked file holds a personal address."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "people.tf").read_text()


def block(header):
    m = re.search(re.escape(header) + r" \{\n.*?\n\}\n", TF, re.S)
    assert m, header
    return m.group(0)


class PeopleTests(unittest.TestCase):
    def test_it_is_a_container_with_no_value_in_terraform(self):
        secret = block('resource "aws_secretsmanager_secret" "people"')
        self.assertIn('name                    = "people/addresses"', secret)
        self.assertNotIn("aws_secretsmanager_secret_version\" \"people", TF)
        self.assertNotIn("secret_string", TF.replace("secret-string", ""))

    def test_only_administration_may_read_it(self):
        policy = block('resource "aws_secretsmanager_secret_policy" "people"')
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('Action    = "secretsmanager:GetSecretValue"', policy)
        self.assertIn("local.games_mp_admin_principals", policy)
        for role in ("dev_server", "nextjs_dev"):
            self.assertNotIn(role, policy)


if __name__ == "__main__":
    unittest.main()

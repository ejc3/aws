#!/usr/bin/env python3
"""people.tf: addresses live in one SecureString parameter, not in git. The container is created with a placeholder and its value
is ignored after creation; and no tracked file holds a personal address."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "people.tf").read_text()


class PeopleTests(unittest.TestCase):
    def test_the_parameter_is_a_securestring_with_a_placeholder_whose_value_is_ignored(self):
        res = re.search(r'resource "aws_ssm_parameter" "people" \{.*?\n\}', TF, re.S).group()
        self.assertIn('name        = "/infra/people"', res)
        self.assertIn('type        = "SecureString"', res)
        self.assertIn("jsonencode({ placeholder = true })", res)
        self.assertIn("ignore_changes = [value]", res)


if __name__ == "__main__":
    unittest.main()

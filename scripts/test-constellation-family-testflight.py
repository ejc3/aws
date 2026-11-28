#!/usr/bin/env python3
"""constellation-family-testflight.tf: only testflight-family.yml on main, in its own environment, can assume the
role; the role reads the App Store Connect key and nothing else; and the key's own policy admits it."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "constellation-family-testflight.tf").read_text()
KEY = (ROOT / "dev-ai-services.tf").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


class FamilyTestflightRoleTests(unittest.TestCase):
    def test_trust_is_the_one_workflow_on_main_in_its_environment(self):
        c = code(TF)
        self.assertIn('"token.actions.githubusercontent.com:sub"              = "repo:ejc3@1694850/constellation@1427792887:environment:testflight-family"', c)
        self.assertIn('"token.actions.githubusercontent.com:ref"              = "refs/heads/main"', c)
        self.assertIn('= "ejc3/constellation/.github/workflows/testflight-family.yml@refs/heads/main"', c)
        self.assertEqual(c.count("StringEquals"), 1)
        self.assertNotIn("StringLike", c)
        self.assertNotIn('"*"', c)

    def test_role_only_reads_the_key(self):
        c = code(TF)
        self.assertEqual(re.findall(r"policy_arn\s*=\s*(\S+)", c), ["aws_iam_policy.app_store_connect_read.arn"])
        self.assertNotIn("aws_iam_role_policy ", c)

    def test_the_keys_policy_admits_the_role(self):
        self.assertIn("aws_iam_role.constellation_family_testflight.arn", code(KEY))


if __name__ == "__main__":
    unittest.main()

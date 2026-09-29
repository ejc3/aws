#!/usr/bin/env python3
"""The ElevenLabs API key (dev-ai-services.tf): readable only by the administration set and the
two dev roles, and the dev roles may read that one secret and nothing else. Offline."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dev-ai-services.tf").read_text()


def block(kind, name):
    m = re.search(r'^(?:resource|data) "%s" "%s" \{\n.*?^\}' % (kind, name), TF, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


class ElevenLabsSecretTests(unittest.TestCase):
    def test_the_value_is_never_in_terraform(self):
        self.assertNotIn("aws_secretsmanager_secret_version", TF)
        self.assertNotRegex(TF, r"sk_[0-9a-f]{20,}")

    def test_the_resource_policy_denies_everyone_but_admins_and_the_two_dev_roles(self):
        policy = block("aws_secretsmanager_secret_policy", "elevenlabs_api_key")
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.elevenlabs_readers)', policy)
        self.assertIn("elevenlabs_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]", TF)

    def test_the_dev_roles_read_this_secret_only(self):
        doc = block("aws_iam_policy_document", "elevenlabs_read")
        self.assertIn('actions   = ["secretsmanager:GetSecretValue"]', doc)
        self.assertIn("resources = [aws_secretsmanager_secret.elevenlabs_api_key.arn]", doc)
        attach = block("aws_iam_role_policy_attachment", "elevenlabs_read")
        self.assertIn("{ dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }", attach)

    def test_no_bedrock_here(self):
        # DeepSeek stays on the metal boxes (dev-instance-common.tf); the kids' box does not get it.
        self.assertNotIn("bedrock:", TF)


if __name__ == "__main__":
    unittest.main(verbosity=2)

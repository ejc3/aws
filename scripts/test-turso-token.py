#!/usr/bin/env python3
"""turso/api-token (dev-ai-services.tf): a container only, readable by the administration set and the metal boxes, never
by nextjs-dev (every account there has sudo), and no value in Terraform."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AI = (ROOT / "dev-ai-services.tf").read_text()
AGENTS = (ROOT / "AGENTS.md").read_text()


def block(header):
    m = re.search(re.escape(header) + r" \{\n.*?\n\}\n", AI, re.S)
    assert m, header
    return m.group(0)


class TursoTokenTests(unittest.TestCase):
    def test_it_is_a_container_with_no_value_in_terraform(self):
        secret = block('resource "aws_secretsmanager_secret" "turso_api_token"')
        self.assertIn('name                    = "turso/api-token"', secret)
        self.assertNotIn("secret_string", AI.split('"turso_api_token"')[1])
        self.assertNotIn("aws_secretsmanager_secret_version\" \"turso", AI)
        self.assertNotRegex(AI, r"eyJ[A-Za-z0-9_-]{20,}")  # no JWT-shaped value anywhere

    def test_only_admins_and_the_metal_role_may_read_it(self):
        self.assertIn("turso_api_token_readers = [aws_iam_role.dev_server.arn]", AI)
        policy = block('resource "aws_secretsmanager_secret_policy" "turso_api_token"')
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn("concat(local.games_mp_admin_principals, local.turso_api_token_readers)", policy)
        self.assertNotIn("nextjs_dev", policy)

    def test_the_identity_grant_is_one_managed_policy_on_the_metal_role_only(self):
        read = block('data "aws_iam_policy_document" "turso_api_token_read"')
        self.assertIn('actions   = ["secretsmanager:GetSecretValue"]', read)
        self.assertIn("aws_secretsmanager_secret.turso_api_token.arn", read)
        att = block('resource "aws_iam_role_policy_attachment" "turso_api_token_read"')
        self.assertIn("aws_iam_role.dev_server.name", att)
        self.assertNotIn("nextjs", att)

    def test_agents_md_says_where_it_lives_and_who_cannot_read_it(self):
        self.assertIn("`turso/api-token`", AGENTS)
        self.assertRegex(AGENTS, r"Turso API token\*\*, metal boxes only")


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""dev-ai-services.tf: the ElevenLabs key readable only by the administration set and the
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
        # It reaches Vercel through state (a data source), never through the configuration.
        self.assertNotIn('resource "aws_secretsmanager_secret_version"', TF)
        self.assertNotRegex(TF, r"sk_[0-9a-f]{20,}")

    def test_the_games_get_it_server_side(self):
        env = block("vercel_project_environment_variable", "elevenlabs_api_key")
        self.assertIn('key        = "ELEVENLABS_API_KEY"', env)
        self.assertIn("value      = data.aws_secretsmanager_secret_version.elevenlabs_api_key.secret_string", env)
        self.assertIn("sensitive  = true", env)
        self.assertIn('target     = ["production", "preview"]', env)
        self.assertNotIn("NEXT_PUBLIC", env)

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

    def test_browserbase_is_for_the_two_dev_roles_the_news_pass_and_the_maps_loop(self):
        self.assertNotIn('resource "aws_secretsmanager_secret_version" "browserbase"', TF)
        self.assertNotRegex(TF, r"bb_live_[A-Za-z0-9]{6,}")
        policy = block("aws_secretsmanager_secret_policy", "browserbase")
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.browserbase_readers)', policy)
        self.assertIn("browserbase_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn, aws_iam_role.dolphin_labs_news_pass.arn, aws_iam_role.dolphin_maps_loop.arn]", TF)
        # dev-ebs-only is the I/O box's role too (io-box.tf): never a reader.
        self.assertNotIn("dev_ebs_only", TF[TF.index("browserbase_readers"):TF.index("browserbase_readers") + 300])
        doc = block("aws_iam_policy_document", "browserbase_read")
        self.assertIn('actions   = ["secretsmanager:GetSecretValue"]', doc)
        self.assertIn("resources = [aws_secretsmanager_secret.browserbase.arn]", doc)
        attach = block("aws_iam_role_policy_attachment", "browserbase_read")
        self.assertIn("{ dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }", attach)

    def test_deepseek_is_for_the_two_dev_roles_and_the_scrapers(self):
        self.assertNotIn('resource "aws_secretsmanager_secret_version" "deepseek"', TF)
        self.assertNotRegex(TF, r"sk-[0-9a-f]{24,}")
        policy = block("aws_secretsmanager_secret_policy", "deepseek")
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.deepseek_readers)', policy)
        self.assertIn("deepseek_readers = local.browserbase_readers", TF)
        doc = block("aws_iam_policy_document", "deepseek_read")
        self.assertIn("resources = [aws_secretsmanager_secret.deepseek.arn]", doc)
        attach = block("aws_iam_role_policy_attachment", "deepseek_read")
        self.assertIn("{ dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }", attach)
        for f in ("dolphin-labs-news.tf", "dolphin-maps-loop.tf"):
            src = (ROOT / f).read_text()
            self.assertIn("aws_secretsmanager_secret.browserbase.arn, aws_secretsmanager_secret.deepseek.arn, aws_secretsmanager_secret.exa.arn]", src)
            self.assertIn("DEEPSEEK_SECRET_ID    = aws_secretsmanager_secret.deepseek.name", src)

    def test_exa_is_for_the_two_dev_roles_and_the_scrapers(self):
        self.assertNotIn('resource "aws_secretsmanager_secret_version" "exa"', TF)
        self.assertNotRegex(TF, r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
        policy = block("aws_secretsmanager_secret_policy", "exa")
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.exa_readers)', policy)
        self.assertIn("exa_readers = local.browserbase_readers", TF)
        self.assertIn("resources = [aws_secretsmanager_secret.exa.arn]", block("aws_iam_policy_document", "exa_read"))
        self.assertIn("{ dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }",
                      block("aws_iam_role_policy_attachment", "exa_read"))
        for f in ("dolphin-labs-news.tf", "dolphin-maps-loop.tf"):
            self.assertIn("EXA_SECRET_ID         = aws_secretsmanager_secret.exa.name", (ROOT / f).read_text())

    def test_claw_master_backup_key_is_for_the_metal_role_only(self):
        self.assertNotIn('resource "aws_secretsmanager_secret_version" "claw_master_backup_key"', TF)
        self.assertNotRegex(TF, r"sk-ant-[A-Za-z0-9_-]{6,}")
        policy = block("aws_secretsmanager_secret_policy", "claw_master_backup_key")
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('concat(local.games_mp_admin_principals, local.claw_master_backup_key_readers)', policy)
        self.assertIn(
            "claw_master_backup_key_readers = concat([aws_iam_role.dev_server.arn], aws_iam_role.claw_master_server[*].arn)", TF)
        doc = block("aws_iam_policy_document", "claw_master_backup_key_read")
        self.assertIn("resources = [aws_secretsmanager_secret.claw_master_backup_key.arn]", doc)
        attach = block("aws_iam_role_policy_attachment", "claw_master_backup_key_read")
        self.assertIn("aws_iam_role.dev_server.name", attach)
        self.assertNotIn("nextjs_dev", attach)

    def test_pricing_is_public_price_lists_only(self):
        doc = block("aws_iam_policy_document", "dev_pricing_read")
        actions = set(re.findall(r'"(pricing:[A-Za-z]+)"', doc))
        self.assertEqual(actions, {"pricing:DescribeServices", "pricing:GetAttributeValues", "pricing:GetProducts",
                                   "pricing:ListPriceLists", "pricing:GetPriceListFileUrl"})
        self.assertNotRegex(TF, r'"(ce|cur|billing|budgets|aws-portal|account):', "no account spend or billing")

    def test_no_bedrock_here(self):
        # DeepSeek stays on the metal boxes (dev-instance-common.tf); the kids' box does not get it.
        self.assertNotIn("bedrock:", TF)


if __name__ == "__main__":
    unittest.main(verbosity=2)

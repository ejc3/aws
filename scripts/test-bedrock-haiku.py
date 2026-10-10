#!/usr/bin/env python3
"""bedrock-haiku.tf: Claude Haiku 5.5 and no other model, for nextjs-dev-role and for one workflow on
dolphin-labs' main through GitHub OIDC. Offline: reads the Terraform source, calls nothing."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "bedrock-haiku.tf").read_text()


def block(kind, name):
    head = 'output "%s"' % name if kind == "output" else 'resource "%s" "%s"' % (kind, name)
    m = re.search(r"^%s \{\n.*?^\}" % re.escape(head), TF, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


def statements():
    m = re.search(r"^  bedrock_haiku_statements = \[\n(.*?)^  \]", TF, re.S | re.M)
    assert m, "local.bedrock_haiku_statements missing"
    return m.group(1)


class GrantTests(unittest.TestCase):
    def test_invoke_is_haiku_5_5_only(self):
        self.assertIn('bedrock_haiku_model   = "anthropic.claude-haiku-5-5"', TF)
        self.assertIn('bedrock_haiku_profile = "us.${local.bedrock_haiku_model}"', TF)
        resources = re.findall(r'"(arn:aws:bedrock:[^"]+)"', statements())
        self.assertEqual(resources, [
            "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/${local.bedrock_haiku_profile}",
            "arn:aws:bedrock:*::foundation-model/${local.bedrock_haiku_model}",
        ])
        action_fields = re.findall(r'Action\s*=\s*(\[.*?\]|"[^"]+")', statements(), re.S)
        actions = set(re.findall(r'"([^"]+)"', " ".join(action_fields)))
        self.assertEqual(actions, {"bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream",
                                   "aws-marketplace:Subscribe", "aws-marketplace:ViewSubscriptions"})

    def test_first_use_subscribe_is_the_haiku_product_only(self):
        subscribe = re.search(r'Action    = "aws-marketplace:Subscribe".*?\n    \}', statements(), re.S)
        self.assertIsNotNone(subscribe)
        self.assertIn('"aws-marketplace:ProductId" = ["prod-6cyn7tgqazjhu"]', subscribe.group())

    def test_both_roles_get_exactly_the_shared_statements(self):
        for name in ("nextjs_dev_bedrock_haiku", "dolphin_labs_news_bedrock"):
            policy = block("aws_iam_role_policy", name)
            self.assertIn("Statement = local.bedrock_haiku_statements", policy)
        self.assertIn("role   = aws_iam_role.nextjs_dev.id", block("aws_iam_role_policy", "nextjs_dev_bedrock_haiku"))
        # nextjs-dev's own policy stays without Bedrock; the grant lives here.
        self.assertNotIn("bedrock:", (ROOT / "nextjs-dev.tf").read_text())


class TrustTests(unittest.TestCase):
    def test_trust_is_one_workflow_on_main_by_immutable_subject(self):
        trust = block("aws_iam_role", "dolphin_labs_news_bedrock")
        self.assertIn("Federated = aws_iam_openid_connect_provider.github.arn", trust)
        self.assertIn('"sts:AssumeRoleWithWebIdentity"', trust)
        self.assertNotIn("StringLike", trust)
        conditions = dict(re.findall(r'"token\.actions\.githubusercontent\.com:(\w+)"\s*=\s*"([^"]+)"', trust))
        self.assertEqual(conditions, {
            "aud": "sts.amazonaws.com",
            "sub": "repo:dolphin-labs-hq@316189183/dolphin-labs@1330391842:ref:refs/heads/main",
            "job_workflow_ref": "dolphin-labs-hq/dolphin-labs/.github/workflows/refresh-news.yml@refs/heads/main",
        })

    def test_output_names_what_the_workflow_needs(self):
        out = block("output", "dolphin_labs_news_bedrock")
        self.assertIn("AWS_BEDROCK_ROLE_ARN = aws_iam_role.dolphin_labs_news_bedrock.arn", out)
        self.assertIn("BEDROCK_MODEL_ID     = local.bedrock_haiku_profile", out)
        self.assertIn('bedrock_haiku_region  = "us-west-2"', TF)


if __name__ == "__main__":
    unittest.main(verbosity=2)

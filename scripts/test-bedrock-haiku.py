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

    def test_every_role_gets_exactly_the_shared_statements(self):
        for name in ("box_bedrock_haiku", "mac_bedrock_haiku"):
            policy = block("aws_iam_role_policy", name)
            self.assertIn("Statement = local.bedrock_haiku_statements", policy)
        self.assertIn("for_each = local.bedrock_haiku_box_roles", block("aws_iam_role_policy", "box_bedrock_haiku"))
        mac = block("aws_iam_role_policy", "mac_bedrock_haiku")
        self.assertIn("role     = aws_iam_role.mac_instance[0].id", mac)
        self.assertIn("count    = var.enable_mac_dev ? 1 : 0", mac)
        # The roles' own files stay without Bedrock; the grant lives here.
        for tf in ("nextjs-dev.tf", "dev-ebs.tf", "wbox.tf", "member-wbox.tf", "claw-master-server.tf", "mac-dev-secrets.tf"):
            self.assertNotIn("bedrock:", (ROOT / tf).read_text(), tf)

    def test_every_box_role_without_bedrock_is_attached(self):
        roles = re.search(r"^  bedrock_haiku_box_roles = merge\(\n(.*?)^  \)", TF, re.S | re.M)
        self.assertIsNotNone(roles)
        # a per-instance role map (member wboxes) joins by a for expression over the resource
        attached = set(re.findall(r"aws_iam_role\.(\w+)(?:\[0\])?(?:\.id\b| :)", roles.group(1)))
        self.assertEqual(attached, {"nextjs_dev", "dev_ebs_only", "wbox", "member_wbox", "claw_master_server"})
        # Every instance profile's role is attached here, already has every anthropic.* model (dev_server),
        # is an administrator (jumpbox_admin), is a CI machine, is the Mac (its own resource above), or is in the
        # Dolphin account (dolphin_redis, which calls no model).
        not_here = {"dev_server", "jumpbox_admin", "runner", "runner_app_instance", "ami_builder", "runner_app_image_builder",
                    "mac_instance", "dolphin_redis"}
        profile_roles = set()
        for tf in ROOT.glob("*.tf"):
            for m in re.finditer(r'^resource "aws_iam_instance_profile" "\w+" \{\n.*?^\}', tf.read_text(), re.S | re.M):
                profile_roles.update(re.findall(r"role\s*=\s*aws_iam_role\.(\w+)", m.group()))
        self.assertTrue(profile_roles, "no instance profiles found")
        self.assertEqual(profile_roles - not_here, attached)


class RetiredTests(unittest.TestCase):
    def test_no_github_role_for_the_news_pass_here(self):
        # The news pass runs in the Dolphin account (dolphin-aws news.tf); refresh-news.yml's role here was never used.
        self.assertNotIn("dolphin_labs_news_bedrock", TF)
        self.assertIn('dolphin_labs_main_sub = "repo:dolphin-labs-hq@316189183/dolphin-labs@1330391842:ref:refs/heads/main"', TF)
        self.assertIn('bedrock_haiku_region  = "us-west-2"', TF)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

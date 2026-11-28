#!/usr/bin/env python3
"""dolphin-labs-family-testflight.tf: only dolphin-labs' testflight-family.yml on main, in its own environment, can
assume the role; the role reads the App Store Connect key and nothing else; the key's own policy admits it; the
environment is main-only with Family's variables (not GWW's AWS_TESTFLIGHT_ROLE_ARN); and the review contact
secret is readable by administration only."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-labs-family-testflight.tf").read_text()
KEY = (ROOT / "dev-ai-services.tf").read_text()
ACCOUNT = (ROOT / "family-account.tf").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, name):
    return code(TF).split(f'resource "{kind}" "{name}" {{')[1].split("\n}")[0]


class FamilyTestflightRoleTests(unittest.TestCase):
    def test_trust_is_the_one_workflow_on_main_in_its_environment(self):
        trust = block("aws_iam_role", "dolphin_labs_family_testflight")
        conditions = dict(re.findall(r'"token\.actions\.githubusercontent\.com:(\w+)"\s*=\s*"([^"]+)"', trust))
        self.assertEqual(conditions, {
            "aud": "sts.amazonaws.com",
            "sub": "repo:dolphin-labs-hq@316189183/dolphin-labs@1330391842:environment:testflight-family",
            "ref": "refs/heads/main",
            "job_workflow_ref": "dolphin-labs-hq/dolphin-labs/.github/workflows/testflight-family.yml@refs/heads/main",
        })
        self.assertEqual(trust.count("StringEquals"), 1)
        self.assertEqual(trust.count("sts:AssumeRoleWithWebIdentity"), 1)
        self.assertNotIn("StringLike", code(TF))
        self.assertNotIn("constellation", code(TF))

    def test_role_only_reads_the_key(self):
        c = code(TF)
        self.assertEqual(re.findall(r"policy_arn\s*=\s*(\S+)", c), ["aws_iam_policy.app_store_connect_read.arn"])
        self.assertNotIn("aws_iam_role_policy ", c)

    def test_the_keys_policy_admits_the_role(self):
        self.assertIn("aws_iam_role.dolphin_labs_family_testflight.arn", code(KEY))
        self.assertNotIn("constellation_family_testflight", code(KEY))

    def test_environment_is_main_only_in_dolphin_labs(self):
        env = block("github_repository_environment", "dolphin_labs_testflight_family")
        self.assertRegex(env, r"provider\s*= github\.dolphin_labs_ci\n")
        self.assertRegex(env, r"repository\s*= local\.dolphin_labs_ci_repository\n")
        self.assertRegex(env, r'environment\s*= "testflight-family"')
        self.assertRegex(env, r"protected_branches\s*= false")
        self.assertRegex(env, r"custom_branch_policies\s*= true")
        policy = block("github_repository_environment_deployment_policy", "dolphin_labs_testflight_family_main")
        self.assertRegex(policy, r'branch_pattern\s*= "main"')

    def test_variables_are_familys_own_names_and_point_at_the_staging_host(self):
        variables = code(TF).split("family_testflight_variables = {")[1].split("}")[0]
        names = re.findall(r"^\s*(\w+)\s*=", variables, re.M)
        self.assertEqual(names, ["FAMILY_TESTFLIGHT_ROLE_ARN", "FAMILY_BUNDLE_ID", "FAMILY_API", "FAMILY_ZERO_URL"])
        self.assertNotIn("AWS_TESTFLIGHT_ROLE_ARN", code(TF))
        self.assertRegex(variables, r"FAMILY_TESTFLIGHT_ROLE_ARN\s*= aws_iam_role\.dolphin_labs_family_testflight\.arn")
        host = re.search(r'hostname\s*= "([^"]+)"', ACCOUNT).group(1)
        self.assertIn(f'family_stage_url = "https://{host}"', code(TF))
        for name in ("FAMILY_API", "FAMILY_ZERO_URL"):
            self.assertRegex(variables, rf"{name}\s*= local\.family_stage_url")
        var = block("github_actions_environment_variable", "dolphin_labs_testflight_family")
        self.assertIn("for_each      = local.family_testflight_variables", var)
        self.assertIn("github_repository_environment.dolphin_labs_testflight_family.environment", var)

    def test_review_contact_is_administration_read_only_and_copied_into_the_environment(self):
        policy = block("aws_secretsmanager_secret_policy", "family_testflight_review_contact")
        allows = [s for s in policy.split("Sid") if '"Allow"' in s]
        self.assertEqual(len(allows), 1)
        self.assertIn('Action    = "secretsmanager:PutSecretValue"', allows[0])
        self.assertNotIn("GetSecretValue", allows[0])
        read_deny = policy.split('"OnlyAdministrationCanRead"')[1].split("Sid")[0]
        self.assertIn('"secretsmanager:GetSecretValue"', read_deny)
        self.assertIn('ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals }', read_deny)
        self.assertIn('name                    = "family/testflight-review-contact"', code(TF))
        secret = block("github_actions_environment_secret", "dolphin_labs_testflight_family_review_contact")
        self.assertIn("github_repository_environment.dolphin_labs_testflight_family.environment", secret)
        self.assertIn("for_each        = toset(local.family_review_contact_keys)", secret)
        keys = re.search(r"family_review_contact_keys = \[([^\]]+)\]", code(TF)).group(1)
        self.assertEqual(re.findall(r'"(\w+)"', keys), [
            "REVIEW_CONTACT_FIRST_NAME", "REVIEW_CONTACT_LAST_NAME", "REVIEW_CONTACT_PHONE", "REVIEW_CONTACT_EMAIL"])
        seed = block("aws_secretsmanager_secret_version", "family_testflight_review_contact_empty")
        self.assertIn("ignore_changes = [secret_string]", seed)


if __name__ == "__main__":
    unittest.main(buffer=True)

#!/usr/bin/env python3
"""The main-only CI environment and the ARM uploader reach one private road-tile bucket."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-maps-tiles.tf").read_text()
CODE = "\n".join(line for line in TF.splitlines() if not line.lstrip().startswith("#"))


def block(kind, name):
    match = re.search(r'^resource "%s" "%s" \{\n(.*?)^\}' % (kind, name), CODE, re.M | re.S)
    assert match, (kind, name)
    return match.group(1)


class BucketTests(unittest.TestCase):
    def test_fixed_bucket_belongs_to_the_workers_account(self):
        bucket = block("cloudflare_r2_bucket", "dolphin_maps_tiles")
        self.assertIn('dolphin_maps_tiles_bucket      = "dolphin-maps-tiles"', CODE)
        self.assertIn("account_id    = var.cloudflare_account_id", bucket)
        self.assertIn("name          = local.dolphin_maps_tiles_bucket", bucket)
        self.assertIn('storage_class = "Standard"', bucket)

    def test_bucket_and_one_time_credential_cannot_be_replaced_implicitly(self):
        for kind, name in (("cloudflare_r2_bucket", "dolphin_maps_tiles"),
                           ("cloudflare_account_token", "dolphin_maps_tiles_r2")):
            with self.subTest(kind=kind):
                self.assertRegex(block(kind, name), r"lifecycle\s*\{\s*prevent_destroy = true\s*\}")

    def test_no_public_domain_or_expiry_rule(self):
        domain = block("cloudflare_r2_managed_domain", "dolphin_maps_tiles")
        self.assertIn("bucket_name = cloudflare_r2_bucket.dolphin_maps_tiles.name", domain)
        self.assertIn("enabled     = false", domain)
        self.assertNotRegex(CODE, r'"cloudflare_r2_(custom_domain|bucket_lifecycle|bucket_cors)"')


class CredentialTests(unittest.TestCase):
    def test_only_existing_bucket_item_read_and_write_groups(self):
        token = block("cloudflare_account_token", "dolphin_maps_tiles_r2")
        self.assertEqual(re.findall(r"\{ id = local\.([a-z0-9_]+) \}", token),
                         ["r2_bucket_item_read", "r2_bucket_item_write"])
        self.assertEqual(re.findall(r'^\s*effect\s*=\s*"([^"]+)"', token, re.M), ["allow"])

    def test_scope_is_exactly_this_bucket_not_the_account_or_other_buckets(self):
        token = block("cloudflare_account_token", "dolphin_maps_tiles_r2")
        self.assertEqual(re.findall(r'"(com\.cloudflare\.[^"]+)"\s*=\s*"\*"', token),
                         ["com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.dolphin_maps_tiles.name}"])
        self.assertNotIn('"com.cloudflare.api.account.', token)

    def test_s3_pair_is_derived_from_only_the_narrow_token(self):
        self.assertIn("R2_ACCESS_KEY_ID     = cloudflare_account_token.dolphin_maps_tiles_r2.id", CODE)
        self.assertIn("R2_SECRET_ACCESS_KEY = sha256(cloudflare_account_token.dolphin_maps_tiles_r2.value)", CODE)
        self.assertIn("R2_ACCOUNT_ID        = var.cloudflare_account_id", CODE)
        self.assertIn("R2_BUCKET            = cloudflare_r2_bucket.dolphin_maps_tiles.name", CODE)

    def test_admin_minter_remains_ephemeral_and_only_the_narrow_credentials_are_copied(self):
        self.assertIn("provider   = cloudflare.token_minter", block("cloudflare_account_token", "dolphin_maps_tiles_r2"))
        provider = (ROOT / "workers-deploy.tf").read_text()
        self.assertIn("api_token = trimspace(ephemeral.aws_secretsmanager_secret_version.cloudflare_account_token.secret_string)", provider)
        version = block("aws_secretsmanager_secret_version", "dolphin_maps_tiles_upload")
        self.assertRegex(version, r"secret_string\s*= jsonencode\(local\.dolphin_maps_tiles_credentials\)")
        self.assertRegex(version, r"depends_on\s*= \[aws_secretsmanager_secret_policy\.dolphin_maps_tiles_upload\]")
        self.assertNotRegex(CODE, re.compile(r'^(?:data|ephemeral) "aws_', re.M))
        self.assertNotRegex(CODE, re.compile(r'^output\s', re.M))


class DevBoundaryTests(unittest.TestCase):
    def test_only_administration_and_the_designated_metal_instance_can_read(self):
        name = "dolphin_maps_tiles_upload"
        self.assertIn('"dolphin-maps/tiles-upload"', block("aws_secretsmanager_secret", name))
        policy = block("aws_secretsmanager_secret_policy", name)
        self.assertEqual(re.findall(r'Effect\s*= "(.*?)"', policy), ["Deny", "Deny"])
        statements = policy.split('Sid       = ')[1:]
        for statement in statements:
            self.assertIn('Principal = "*"', statement)
            self.assertIn('Action    = "secretsmanager:GetSecretValue"', statement)
            self.assertIn('Resource  = aws_secretsmanager_secret.dolphin_maps_tiles_upload.arn', statement)
        self.assertIn('ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn]) }', statements[0])
        self.assertRegex(statements[1], r'ArnEquals\s*= \{ "aws:PrincipalArn" = aws_iam_role\.dev_server\.arn \}')
        self.assertIn('StringNotEqualsIfExists = { "aws:userid" = try(local.browser_run_arm_userids[0], "no-fcvm-arm-instance") }',
                      statements[1])
        self.assertNotIn("ec2:SourceInstanceARN", policy)  # Secrets Manager rejects this EC2-specific key.
        userids = (ROOT / "browser-run.tf").read_text()
        self.assertRegex(userids, r'browser_run_arm_userids\s*= \[for instance in aws_instance\.firecracker_dev : '
                                  r'"\$\{aws_iam_role\.dev_server\.unique_id\}:\$\{instance\.id\}"\]')


class GitHubBoundaryTests(unittest.TestCase):
    def test_dedicated_environment_and_repository_are_fixed(self):
        self.assertIn('dolphin_maps_tiles_repository  = "dolphin-maps"', CODE)
        self.assertIn('dolphin_maps_tiles_environment = "osm-tiles"', CODE)
        environment = block("github_repository_environment", "dolphin_maps_tiles")
        self.assertIn("provider          = github.dolphin_labs_ci", environment)
        self.assertIn("repository        = local.dolphin_maps_tiles_repository", environment)
        self.assertIn("environment       = local.dolphin_maps_tiles_environment", environment)
        self.assertNotIn('"data-push"', CODE)

    def test_branch_policy_admits_only_main_and_no_tags(self):
        environment = block("github_repository_environment", "dolphin_maps_tiles")
        self.assertIn("protected_branches     = false", environment)
        self.assertIn("custom_branch_policies = true", environment)
        policy = block("github_repository_environment_deployment_policy", "dolphin_maps_tiles_main")
        self.assertEqual(re.findall(r'(?:branch|tag)_pattern\s*=\s*"([^"]+)"', policy), ["main"])
        self.assertIn("environment    = github_repository_environment.dolphin_maps_tiles.environment", policy)
        self.assertIn("provider       = github.dolphin_labs_ci", policy)

    def test_only_four_s3_fields_are_environment_secrets_after_main_policy_exists(self):
        secret = block("github_actions_environment_secret", "dolphin_maps_tiles")
        names = re.search(r"for_each\s*=\s*toset\(\[(.*?)\]\)", secret).group(1)
        self.assertEqual(re.findall(r'"(R2_[A-Z_]+)"', names),
                         ["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"])
        self.assertIn("repository      = local.dolphin_maps_tiles_repository", secret)
        self.assertIn("environment     = github_repository_environment.dolphin_maps_tiles.environment", secret)
        self.assertIn("plaintext_value = local.dolphin_maps_tiles_credentials[each.key]", secret)
        self.assertIn("depends_on = [github_repository_environment_deployment_policy.dolphin_maps_tiles_main]", secret)

    def test_resource_set_is_limited_to_the_bucket_token_and_two_credential_destinations(self):
        self.assertEqual(re.findall(r'^resource "([^"]+)" "([^"]+)"', CODE, re.M), [
            ("cloudflare_r2_bucket", "dolphin_maps_tiles"),
            ("cloudflare_r2_managed_domain", "dolphin_maps_tiles"),
            ("cloudflare_account_token", "dolphin_maps_tiles_r2"),
            ("github_repository_environment", "dolphin_maps_tiles"),
            ("github_repository_environment_deployment_policy", "dolphin_maps_tiles_main"),
            ("github_actions_environment_secret", "dolphin_maps_tiles"),
            ("aws_secretsmanager_secret", "dolphin_maps_tiles_upload"),
            ("aws_secretsmanager_secret_version", "dolphin_maps_tiles_upload"),
            ("aws_secretsmanager_secret_policy", "dolphin_maps_tiles_upload"),
        ])

    def test_ci_runs_this_credential_free_suite(self):
        workflow = (ROOT / ".github/workflows/lambda-tests.yml").read_text()
        self.assertIn("python3 -S -B scripts/test-dolphin-maps-tiles.py", workflow)


if __name__ == "__main__":
    unittest.main(buffer=True)

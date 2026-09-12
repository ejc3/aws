#!/usr/bin/env python3
"""SDK-free regression checks for the deliberately small regional-defaults split."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REGIONS = {
    "ap-south-1", "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-southeast-1", "ap-southeast-2", "ca-central-1", "eu-central-1",
    "eu-north-1", "eu-west-1", "eu-west-2", "eu-west-3", "sa-east-1",
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
}
EXISTING = {
    "main": {"us-west-1": "aws", "us-west-2": "aws.west2", "us-east-1": "aws.dr"},
    "staging": {"us-west-1": "aws.staging", "us-east-1": "aws.staging_dr"},
}


def source(path):
    return (ROOT / path).read_text()


def blocks(text, kind):
    """Only the simple top-level blocks in these three opinionated files."""
    return re.findall(r'(?ms)^' + kind + r' ([^\n]+) \{\n(.*?)^\}', text)


class SecurityDefaults(unittest.TestCase):
    def setUp(self):
        self.defaults = source("security-defaults.tf")
        self.providers = source("security-regions.tf")
        self.module = source("modules/security-defaults/main.tf")

    def test_exact_four_resource_types_and_count(self):
        self.assertEqual({name for name, _ in blocks(self.module, "resource")}, {
            '"aws_ebs_encryption_by_default" "security"',
            '"aws_ebs_snapshot_block_public_access" "security"',
            '"aws_ec2_instance_metadata_defaults" "security"',
            '"aws_ssm_service_setting" "block_public_document_sharing"',
        })
        self.assertEqual(len(blocks(self.module, "resource")), 4)
        self.assertEqual(len(blocks(self.defaults, "module")) * 4, 136)

    def test_only_reviewed_default_values(self):
        resources = dict(blocks(self.module, "resource"))
        sharing = resources.pop('"aws_ssm_service_setting" "block_public_document_sharing"')
        self.assertEqual(resources, {
            '"aws_ebs_encryption_by_default" "security"': '  enabled = true\n',
            '"aws_ebs_snapshot_block_public_access" "security"': '  state = "block-all-sharing"\n',
            '"aws_ec2_instance_metadata_defaults" "security"': '  http_tokens = "required"\n',
        })
        code = "\n".join(line for line in sharing.splitlines() if not line.lstrip().startswith("#"))
        self.assertEqual(re.sub(r"\s+", "", code),
            'setting_id="arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:servicesetting/ssm/documents/console/public-sharing-permission"'
            'setting_value="Disable"lifecycle{prevent_destroy=true}')

    def test_exact_both_account_region_sets(self):
        names = [name.strip('"') for name, _ in blocks(self.defaults, "module")]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(set(names), {
            "security_defaults_" + account + "_" + region.replace("-", "_")
            for account in EXISTING for region in REGIONS
        })

    def test_every_module_has_exact_provider_and_no_extra_input(self):
        modules = dict(blocks(self.defaults, "module"))
        for account in EXISTING:
            for region in REGIONS:
                suffix = account + "_" + region.replace("-", "_")
                provider = EXISTING[account].get(region, "aws.security_" + suffix)
                body = modules['"security_defaults_' + suffix + '"']
                self.assertEqual(re.sub(r"\s+", "", body),
                    'source="./modules/security-defaults"providers={aws=' + provider + '}')

    def test_all_new_provider_aliases_resolve_once(self):
        providers = blocks(self.providers, "provider")
        aliases = re.findall(r'alias\s*=\s*"([^"]+)"', self.providers)
        wanted = {
            "security_" + account + "_" + region.replace("-", "_")
            for account in EXISTING for region in REGIONS if region not in EXISTING[account]
        }
        self.assertEqual(len(providers), 29)
        self.assertEqual(len(aliases), len(set(aliases)))
        self.assertEqual(set(aliases), wanted)
        self.assertTrue(all(name == '"aws"' for name, _ in providers))

    def test_provider_regions_match_aliases(self):
        for _, body in blocks(self.providers, "provider"):
            alias = re.search(r'alias\s*=\s*"([^"]+)"', body).group(1)
            region = re.search(r'region\s*=\s*"([^"]+)"', body).group(1)
            self.assertIn(region, REGIONS)
            self.assertTrue(alias.endswith("_" + region.replace("-", "_")))

    def test_only_recovery_providers_assume_existing_role(self):
        for _, body in blocks(self.providers, "provider"):
            staging = '"security_staging_' in body
            self.assertEqual("assume_role" in body, staging)
            if staging:
                self.assertIn('role_arn = "arn:aws:iam::${aws_organizations_account.dev_staging[0].id}:role/OrganizationAccountAccessRole"', body)
        self.assertNotIn("access_key", self.providers)
        self.assertNotIn("secret_key", self.providers)

    def test_existing_regional_aliases_not_redefined(self):
        for aliases in EXISTING.values():
            for provider in aliases.values():
                if "." in provider:
                    self.assertNotRegex(self.providers, r'alias\s*=\s*"' + provider.split(".")[1] + r'"')

    def test_no_other_resource_or_mutation_hooks(self):
        self.assertFalse(blocks(self.defaults + self.providers, "resource"))
        code = "\n".join(line for line in (self.defaults + self.providers + self.module).splitlines()
                         if not line.lstrip().startswith("#"))
        self.assertNotRegex(code, r'\b(provisioner|local-exec|remote-exec|external|command|import|removed|moved)\b')
        self.assertNotRegex(code, r'aws_(instance|ebs_volume|kms_key|backup_|guardduty|cloudcontrolapi|cloudwatch|s3_|sns_|ssm_(?!service_setting)|security_group)')

    def test_ci_is_sdk_and_credential_free(self):
        workflow = source(".github/workflows/lambda-tests.yml")
        self.assertIn("python3 -S -B scripts/test-security-defaults.py", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("id-token: write", workflow)
        self.assertNotIn("configure-aws-credentials", workflow)

    def test_readme_preserves_scope_and_exception_warnings(self):
        readme = source("README.md")
        for phrase in ["136 account settings", "34 account/region combinations", "Private sharing",
                       "explicit launch options can override", "not re-encrypted", "no recurring service subscription"]:
            self.assertIn(phrase, readme)
        self.assertIn("does not revoke existing public shares", readme)


if __name__ == "__main__":
    unittest.main()

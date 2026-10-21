#!/usr/bin/env python3
"""SDK-free scope checks for free, external-only IAM Access Analyzer ownership."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class ExternalAccess(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "security-external-access.tf").read_text()
        self.resources = re.findall(r'(?ms)^resource "([^"]+)" "([^"]+)" \{\n(.*?)^\}', self.source)
        self.analyzers = [(kind, name, body) for kind, name, body in self.resources if kind == "aws_accessanalyzer_analyzer"]

    def test_only_external_analyzer_resources_are_created(self):
        self.assertEqual(len(self.resources), 38)
        self.assertEqual(len(self.analyzers), 34)
        self.assertEqual({kind for kind, _, _ in self.resources},
                         {"aws_accessanalyzer_analyzer", "aws_iam_service_linked_role", "aws_accessanalyzer_archive_rule"})
        self.assertEqual(len({name for _, name, _ in self.analyzers}), 34)

    def test_exact_existing_account_region_routes_are_reused(self):
        defaults = (ROOT / "security-defaults.tf").read_text()
        expected = {"external_" + suffix: provider for suffix, provider in re.findall(
            r'module "security_defaults_([^"]+)" \{\n\s*source\s*=\s*"[^"\n]+"\n\s*providers\s*=\s*\{ aws = ([^} ]+) \}', defaults)}
        self.assertEqual(len(expected), 34)
        self.assertEqual({name: re.search(r'provider\s*=\s*([^\s]+)', body).group(1)
                          for _, name, body in self.analyzers}, expected)
        self.assertNotRegex(self.source, re.compile(r'^provider ', re.M))

    def test_each_analyzer_has_only_fixed_external_account_configuration(self):
        for _, name, body in self.analyzers:
            with self.subTest(name=name):
                fields = re.findall(r'^\s*([a-z_]+)\s*=', body, re.M)
                self.assertEqual(fields, ["provider", "analyzer_name", "type", "tags", "depends_on"])
                self.assertRegex(body, r'analyzer_name\s*=\s*"security-external-access"')
                self.assertRegex(body, r'type\s*=\s*"ACCOUNT"')
                self.assertRegex(body, r'tags\s*=\s*\{ Managed = "terraform" \}')

    def test_no_paid_configuration_suppression_or_workload_mutation(self):
        code = "\n".join(line for line in self.source.splitlines() if not line.lstrip().startswith("#"))
        for forbidden in ["UNUSED_ACCESS", "INTERNAL_ACCESS", "ORGANIZATION", "configuration",
                          "aws_iam_role", "aws_iam_policy", "aws_cloudwatch_", "aws_sns_", "provisioner", "command"]:
            self.assertNotIn(forbidden, code)

    def test_the_only_archive_rules_are_aws_backups_snapshot_copy_share(self):
        # #577: AWS Backup's own temporary share of an EBS snapshot with its service account while it copies into the
        # air-gapped vault. Both filters must hold: an EBS snapshot AND that one principal, in the two copy regions only.
        rules = re.findall(r'resource "aws_accessanalyzer_archive_rule" "(\w+)" \{(.*?)\n\}', self.source, re.S)
        self.assertEqual(sorted(n for n, _ in rules), ["backup_lag_copy_us_east_1", "backup_lag_copy_us_west_1"])
        for name, body in rules:
            self.assertEqual(body.count("filter {"), 2, name)
            self.assertIn('criteria = "resourceType"\n    eq       = ["AWS::EC2::Snapshot"]', body)
            self.assertIn('criteria = "principal.AWS"\n    eq       = [local.access_analyzer_backup_lag_principal]', body)
            self.assertNotIn("contains", body)
            self.assertNotIn("exists", body)
        self.assertRegex(self.source, r'access_analyzer_backup_lag_principal = "\d{12}"')

    def test_no_extra_or_hidden_configuration_blocks(self):
        headers = re.findall(r'^([a-z_]+) ', self.source, re.M)
        self.assertEqual(headers, ["resource"] * 36 + ["locals"] + ["resource"] * 2)
        self.assertNotRegex(self.source, r'\bcount\s*=')

    def test_only_two_service_owned_read_roles_and_account_local_dependencies(self):
        roles = {name: body for kind, name, body in self.resources if kind == "aws_iam_service_linked_role"}
        self.assertEqual(set(roles), {"access_analyzer_main", "access_analyzer_staging"})
        self.assertEqual(re.sub(r'\s+', '', roles["access_analyzer_main"]),
                         'aws_service_name="access-analyzer.amazonaws.com"')
        self.assertEqual(re.sub(r'\s+', '', roles["access_analyzer_staging"]),
                         'provider=aws.stagingaws_service_name="access-analyzer.amazonaws.com"')
        for _, name, body in self.analyzers:
            account = "main" if name.startswith("external_main_") else "staging"
            self.assertRegex(body, r'depends_on\s*=\s*\[aws_iam_service_linked_role\.access_analyzer_' + account + r'\]')

    def test_workflow_is_credential_free(self):
        workflow = (ROOT / ".github/workflows/lambda-tests.yml").read_text()
        self.assertIn("python3 -S -B scripts/test-security-external-access.py", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertNotIn("id-token: write", workflow)
        self.assertNotIn("configure-aws-credentials", workflow)

    def test_documentation_does_not_claim_remediation_or_delivery(self):
        readme = (ROOT / "README.md").read_text()
        for phrase in ["34 external-access analyzers", "no additional charge", "AWSServiceRoleForAccessAnalyzer",
                       "not an access-control enforcement", "No other finding is archived", "does not add email delivery"]:
            self.assertIn(phrase, readme)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Offline regression checks for credential-free CI and retired shared roles.

These inspect the actual Terraform/workflow sources; they are not a substitute
for AWS IAM simulation, a fresh full plan, or an in-flight runner preflight.
Run on a dev host or credential-free GitHub-hosted runner, never fetch secrets.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def source(name):
    return (ROOT / name).read_text()


def resource(filename, kind, name):
    text = source(filename)
    match = re.search(r'^resource "' + re.escape(kind) + r'" "' + re.escape(name)
                      + r'" \{\n.*?^\}', text, re.M | re.S)
    if match is None:
        raise AssertionError(f"Missing {kind}.{name} in {filename}")
    return match.group()


class CISecurityTests(unittest.TestCase):
    def test_main_shared_identity_is_explicitly_retired(self):
        policy = resource('github-actions.tf', 'aws_iam_role_policy', 'github_actions_terraform')
        self.assertRegex(policy, r'Effect\s*=\s*"Deny"')
        self.assertRegex(policy, r'Action\s*=\s*"\*"')
        self.assertRegex(policy, r'Resource\s*=\s*"\*"')
        self.assertNotIn('"Allow"', policy)

    def test_staging_shared_identity_cannot_administer_recovery(self):
        policy = resource('dev-staging-bootstrap.tf', 'aws_iam_role_policy', 'github_actions_staging_retired')
        self.assertRegex(policy, r'Effect\s*=\s*"Deny"')
        self.assertRegex(policy, r'Action\s*=\s*"\*"')
        self.assertRegex(policy, r'Resource\s*=\s*"\*"')
        self.assertNotIn('"Allow"', policy)
        self.assertNotIn('AdministratorAccess', source('dev-staging-bootstrap.tf'))

    def test_no_credential_bearing_drift_reader_is_reintroduced(self):
        self.assertNotIn('drift_secret_names', source('github-actions.tf'))
        self.assertNotIn('drift_metadata_actions', source('github-actions.tf'))
        self.assertNotIn('terraform-drift-read', source('github-actions.tf'))
        self.assertNotIn('terraform.tfstate', source('github-actions.tf'))

    def test_validation_has_no_aws_or_backend_credentials(self):
        workflow = source('.github/workflows/drift.yml')
        self.assertIn('terraform init -backend=false -lockfile=readonly', workflow)
        self.assertIn('terraform validate', workflow)
        self.assertIn('persist-credentials: false', workflow)
        self.assertRegex(workflow, r'(?m)^          fetch-depth: 0$')
        for forbidden in ['id-token:', 'configure-aws-credentials', 'role-to-assume',
                          'secrets.', 'terraform plan', 'terraform apply',
                          'terraform output', 'terraform state']:
            self.assertNotIn(forbidden, workflow)
        self.assertRegex(workflow, r'uses: hashicorp/setup-terraform@[a-f0-9]{40}')

    def test_builder_trust_requires_owner_approved_environment(self):
        trust = resource('github-ami-builder.tf', 'aws_iam_role', 'github_actions_ami_builder')
        self.assertIn('repo:ejc3/fcvm:environment:runner-ami-publish', trust)
        self.assertNotIn('StringLike', trust)
        self.assertNotIn('repo:ejc3/fcvm:ref:', trust)
        self.assertNotIn('repo:ejc3/fcvm:*', trust)
        self.assertNotIn(':pull_request', trust)

    def test_builder_cannot_pass_admin_or_read_state(self):
        policy = resource('github-ami-builder.tf', 'aws_iam_role_policy', 'github_actions_ami_builder')
        self.assertIn('NeverPassAnotherRole', policy)
        self.assertRegex(policy, r'NotResource\s*=\s*aws_iam_role.ami_builder\[0\].arn')
        self.assertIn('iam:PassedToService', policy)
        self.assertIn('NeverChangeIdentityOrReadReusableCredentials', policy)
        for denied in ['iam:Create*', 'iam:Put*', 'iam:Attach*', 'sts:AssumeRole*',
                       'secretsmanager:GetSecretValue', 'ssm:GetParameter*', 's3:GetObject*']:
            self.assertIn('"' + denied + '"', policy)
        self.assertNotIn('jumpbox-admin', policy)
        self.assertNotIn('"ssm:SendCommand"', policy)
        self.assertIn('"ssm:GetCommandInvocation"', policy.split('NeverChangeIdentityOrReadReusableCredentials')[1])
        self.assertIn('"ec2:InstanceType"', policy)
        self.assertIn('"c7gd.8xlarge"', policy)
        self.assertIn('"ec2:Encrypted"', policy)
        self.assertIn('"ec2:MetadataHttpTokens"', policy)
        self.assertIn('"ec2:MetadataHttpPutResponseHopLimit"', policy)
        self.assertIn('"ec2:VolumeSize"', policy)
        self.assertIn('"aws:TagKeys"', policy)

    def test_codeartifact_cannot_restore_retired_ci_write_access(self):
        self.assertNotIn('GitHubActionsPublish', source('codeartifact.tf'))
        self.assertNotIn('aws_iam_role.github_actions_terraform.arn', source('codeartifact.tf'))

    def test_every_workflow_step_name_is_valid_yaml(self):
        # A plain (unquoted) name containing ": " is a YAML mapping error, and GitHub then runs
        # none of that workflow: every offline test in lambda-tests.yml stops, with only a
        # failed run named after the file to show it. This workflow is the one that still runs.
        for workflow in sorted((ROOT / '.github' / 'workflows').glob('*.yml')):
            for number, line in enumerate(workflow.read_text().splitlines(), 1):
                match = re.match(r'\s*(?:- )?name: (.*)$', line)
                if match is None or match.group(1)[:1] in ('"', "'"):
                    continue
                value = match.group(1).split(' #', 1)[0].rstrip()
                self.assertNotRegex(value, r': |:$', f'{workflow.name} line {number}: quote this name')


if __name__ == '__main__':
    unittest.main(verbosity=2)

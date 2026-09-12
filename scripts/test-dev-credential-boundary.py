#!/usr/bin/env python3
"""Offline source guards; live IAM simulation and SSM acceptance are also required."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent


def source(name):
    return (ROOT / name).read_text()


def block(filename, kind, name, resource_type='[^\"]+'):
    match = re.search(
        r'^' + re.escape(kind) + r' "' + resource_type + r'" "' + re.escape(name)
        + r'" \{\n.*?^\}', source(filename), re.M | re.S,
    )
    if match is None:
        raise AssertionError(f'Missing {kind} {name} in {filename}')
    return match.group()


class DevCredentialBoundaryTests(unittest.TestCase):
    def test_dev_attachments_preserve_connectivity_during_cutover(self):
        for filename, name in [
            ('dev-instance-common.tf', 'dev_server_ssm'),
            ('nextjs-dev.tf', 'nextjs_dev_ssm'),
            ('dev-ebs.tf', 'dev_ebs_only_ssm'),
        ]:
            with self.subTest(role=name):
                attachment = block(filename, 'resource', name)
                self.assertIn('aws_iam_policy.ssm_managed_instance.arn', attachment)
                self.assertRegex(attachment, r'create_before_destroy\s*=\s*true')
                self.assertNotIn('AmazonSSMManagedInstanceCore', attachment)

    def test_shared_ssm_policy_retains_management_not_parameter_payloads(self):
        policy = source('ssm-managed-instance.tf')
        for action in ['ssm:UpdateInstanceInformation', 'ssm:GetDocument',
                       'ssmmessages:OpenControlChannel', 'ssmmessages:OpenDataChannel',
                       'ec2messages:GetMessages']:
            self.assertIn('"' + action + '"', policy)
        self.assertNotRegex(policy, r'"ssm:GetParameter[^\"]*"')

    def test_dev_cannot_send_commands_to_ami_builders(self):
        policy = block('dev-instance-common.tf', 'resource', 'dev_server', 'aws_iam_role_policy')
        self.assertNotIn('SSMSendCommandToAMIBuilders', policy)
        self.assertNotIn('ami-builder-temp', policy)
        self.assertIn('SSMSendCommandToRunners', policy)
        self.assertRegex(policy, r'"ssm:resourceTag/Role"\s*=\s*"github-runner"')
        self.assertIn('aws_ssm_parameter.dev_ssh_private_key.arn', policy)

    def test_dev_denies_unscopable_command_input_and_output_reads(self):
        policy = block('dev-instance-common.tf', 'resource', 'dev_server', 'aws_iam_role_policy')
        statement = re.search(r'\{\s*(?:#[^\n]*\n\s*)*'
                              r'Sid\s*=\s*"NeverReadAccountWideCommandOutput".*?\n\s*\}',
                              policy, re.S).group()
        self.assertRegex(statement, r'Effect\s*=\s*"Deny"')
        self.assertRegex(statement, r'Resource\s*=\s*"\*"')
        for action in ['ssm:GetCommandInvocation', 'ssm:ListCommandInvocations',
                       'ssm:ListCommands']:
            self.assertEqual(policy.count('"' + action + '"'), 1)
            self.assertIn('"' + action + '"', statement)
        self.assertNotIn('SSMGetCommandResults', policy)
        self.assertIn('SSMSendCommandToRunners', policy)

    def test_dev_ipv6_assignment_uses_exact_managed_enis_and_optional_gates(self):
        config = source('dev-instance-common.tf')
        policy = block('dev-instance-common.tf', 'resource', 'dev_server', 'aws_iam_role_policy')
        self.assertRegex(config, r'dev_server_ipv6_network_interface_arns\s*=\s*concat\(\s*'
                         r'aws_network_interface\.firecracker_dev\[\*\]\.arn,\s*'
                         r'aws_network_interface\.x86_dev\[\*\]\.arn,\s*\)')
        self.assertIn('length(local.dev_server_ipv6_network_interface_arns) > 0 ? [{', policy)
        self.assertRegex(policy, r'Resource\s*=\s*local.dev_server_ipv6_network_interface_arns')
        self.assertNotIn('network-interface/*', policy)
        self.assertIn('"ec2:AssignIpv6Addresses"', policy)
        self.assertIn('"ec2:UnassignIpv6Addresses"', policy)

    def test_retirement_is_bound_to_only_the_unused_legacy_role_and_main_account(self):
        policy = block('dev-instance-common.tf', 'resource', 'retired_legacy_dev_server',
                       'aws_iam_role_policy')
        self.assertRegex(policy, r'role\s*=\s*"aws-infrastructure-dev-instance-role"')
        self.assertIn('data.aws_caller_identity.current.account_id == "928413605543"', policy)
        self.assertNotIn('aws_iam_role.dev_server', policy)
        self.assertRegex(policy, r'prevent_destroy\s*=\s*true')
        for field, value in [('Effect', 'Deny'), ('Action', '*'), ('Resource', '*')]:
            self.assertRegex(policy, field + r'\s*=\s*"' + re.escape(value) + '"')

    def test_iam_password_policies_share_explicit_strength_without_forced_expiry(self):
        for name in ['main', 'staging']:
            with self.subTest(account=name):
                policy = block('security-iam-passwords.tf', 'resource', name,
                               'aws_iam_account_password_policy')
                for field, value in [
                    ('minimum_password_length', '14'),
                    ('password_reuse_prevention', '24'),
                    ('require_uppercase_characters', 'true'),
                    ('require_lowercase_characters', 'true'),
                    ('require_numbers', 'true'), ('require_symbols', 'true'),
                    ('max_password_age', '0'), ('hard_expiry', 'false'),
                    ('allow_users_to_change_password', 'false'),
                    ('prevent_destroy', 'true'),
                ]:
                    self.assertRegex(policy, r'(?m)^\s*' + field + r'\s*=\s*' + value + r'\s*$')

    def test_password_policy_account_routing_and_existing_singleton_adoption(self):
        config = source('security-iam-passwords.tf')
        policies = re.findall(r'^resource "([^\"]+)" "([^\"]+)"', config, re.M)
        self.assertEqual(policies, [('aws_iam_account_password_policy', 'main'),
                                    ('aws_iam_account_password_policy', 'staging')])
        main = block('security-iam-passwords.tf', 'resource', 'main',
                     'aws_iam_account_password_policy')
        staging = block('security-iam-passwords.tf', 'resource', 'staging',
                        'aws_iam_account_password_policy')
        self.assertNotRegex(main, r'(?m)^\s*provider\s*=')
        self.assertRegex(staging, r'provider\s*=\s*aws\.staging')
        self.assertIn('data.aws_caller_identity.current.account_id == "928413605543"', main)
        self.assertIn('data.aws_caller_identity.staging.account_id == "249042068453"', staging)
        imports = re.findall(r'^import \{\n.*?^\}', config, re.M | re.S)
        self.assertEqual(len(imports), 1)
        self.assertRegex(imports[0], r'to\s*=\s*aws_iam_account_password_policy\.main')
        self.assertRegex(imports[0], r'id\s*=\s*"iam-account-password-policy"')
        self.assertNotRegex(config, r'local-exec|remote-exec|ignore_changes|count\s*=')

    def test_nextjs_connector_metadata_is_exact_and_contains_no_payload(self):
        metadata = block('nextjs-dev.tf', 'data', 'nextjs_connector')
        self.assertIn('data "aws_secretsmanager_secret"', metadata)
        self.assertEqual(re.findall(r'"(cloudflare-[^\"]+)"', metadata), [
            'cloudflare-tunnel-credentials', 'cloudflare-dolphin-tunnel-credentials',
        ])
        self.assertNotIn('secret_version', metadata)
        self.assertNotIn('secret_string', metadata)

    def test_nextjs_policy_uses_only_connector_arns_and_existing_hop_key(self):
        policy = block('nextjs-dev.tf', 'resource', 'nextjs_dev', 'aws_iam_role_policy')
        self.assertEqual(policy.count('data.aws_secretsmanager_secret.nextjs_connector['), 2)
        for name in ['cloudflare-tunnel-credentials', 'cloudflare-dolphin-tunnel-credentials']:
            self.assertIn(f'data.aws_secretsmanager_secret.nextjs_connector["{name}"].arn', policy)
        self.assertIn('aws_secretsmanager_secret.dev_hop.arn', policy)
        self.assertNotIn('secret:cloudflare-', policy)
        self.assertNotIn('cloudflare-tunnel-token', policy)

    def test_optional_dolphin_secret_and_grant_use_existing_zone_gate(self):
        metadata = block('nextjs-dev.tf', 'data', 'nextjs_connector')
        self.assertIn('local.dolphin_enabled ? ["cloudflare-dolphin-tunnel-credentials"] : []', metadata)
        policy = block('nextjs-dev.tf', 'resource', 'nextjs_dev', 'aws_iam_role_policy')
        self.assertRegex(policy, r'local\.dolphin_enabled\s*\?\s*\[\s*'
                         r'data\.aws_secretsmanager_secret\.nextjs_connector'
                         r'\["cloudflare-dolphin-tunnel-credentials"\]\.arn,\s*\]\s*:\s*\[\]')

    def test_required_public_ssh_and_access_service_token_remain(self):
        sg = block('nextjs-dev.tf', 'resource', 'nextjs_dev', 'aws_security_group')
        self.assertRegex(sg, r'from_port\s*=\s*22\b')
        self.assertRegex(sg, r'from_port\s*=\s*2022\b')
        self.assertIn('0.0.0.0/0', sg)
        cf = source('cloudflare.tf')
        self.assertIn('resource "cloudflare_zero_trust_access_service_token" "cc_games_automation"', cf)
        self.assertIn('token_id = cloudflare_zero_trust_access_service_token.cc_games_automation.id', cf)

if __name__ == '__main__':
    unittest.main(verbosity=2)

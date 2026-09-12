#!/usr/bin/env python3
"""Offline IAM source guards. Rendered-policy simulation/live canaries remain gates."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent


def source(name):
    return (ROOT / name).read_text()


def block(filename, kind, name):
    match = re.search(r'^resource "' + re.escape(kind) + r'" "' + re.escape(name)
                      + r'" \{\n.*?^\}', source(filename), re.M | re.S)
    if not match:
        raise AssertionError(f'Missing {kind}.{name}')
    return match.group()


def statements(policy):
    return re.split(r'\n      \},\n      \{\n', policy)


def statement(policy, sid):
    return next(s for s in statements(policy) if re.search(r'Sid\s*=\s*"' + sid + '"', s))


def actions(item):
    field = re.search(r'\bAction\s*=\s*(\[.*?\]|"[^"]+")', item, re.S).group(1)
    return set(re.findall(r'"([^"]+)"', field))


class RunnerIAMBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.runner = block('runner-vpc.tf', 'aws_iam_role_policy', 'runner')
        self.controller = block('runner-autoscale.tf', 'aws_iam_role_policy', 'runner_lambda')

    def test_ssm_agent_attachment_swaps_after_deny_without_connectivity_gap(self):
        attachment = block('runner-vpc.tf', 'aws_iam_role_policy_attachment', 'runner_ssm')
        self.assertIn('aws_iam_policy.ssm_managed_instance.arn', attachment)
        self.assertRegex(attachment, r'create_before_destroy\s*=\s*true')
        self.assertIn('depends_on = [aws_iam_role_policy.runner]', attachment)
        self.assertNotIn('AmazonSSMManagedInstanceCore', source('runner-vpc.tf'))
        self.assertNotIn('aws_ssm_parameter.github_runner_pat', self.runner)

    def test_reusable_parameters_explicitly_denied_even_under_broad_allow(self):
        denied = statement(self.runner, 'DenyReusableParameterPayloads')
        self.assertRegex(denied, r'Effect\s*=\s*"Deny"')
        self.assertEqual(actions(denied), {'ssm:GetParameter', 'ssm:GetParameters',
                                         'ssm:GetParameterHistory', 'ssm:GetParametersByPath'})
        self.assertRegex(denied, r'NotResource\s*=\s*"arn:aws:ssm:us-west-1:.*:parameter/github-runner/bootstrap/\*"')
        self.assertNotIn('Condition', denied)

    def test_ancestor_batch_and_history_paths_are_unconditionally_denied(self):
        denied = statement(self.runner, 'DenyBulkAndHistoricalParameterPayloads')
        self.assertRegex(denied, r'Effect\s*=\s*"Deny"')
        self.assertRegex(denied, r'Resource\s*=\s*"\*"')
        self.assertEqual(actions(denied), {'ssm:GetParameters', 'ssm:GetParameterHistory',
                                         'ssm:GetParametersByPath'})
        self.assertNotIn('Condition', denied)

    def test_own_bootstrap_and_null_guarded_ddb_claim_stay_in_producer(self):
        producer = block('runner-bootstrap.tf', 'aws_iam_role_policy', 'runner_bootstrap')
        for text in ['ssm:GetParameter', 'ssm:DeleteParameter', 'ssm:resourceTag/InstanceArn',
                     'dynamodb:LeadingKeys', '$${ec2:SourceInstanceARN}']:
            self.assertIn(text, producer)
        self.assertRegex(producer, r'Null\s*=\s*\{ "ec2:SourceInstanceARN" = "false" \}')
        self.assertNotIn('dynamodb:', self.runner)

    def test_ipv6_assignment_requires_existing_runner_eni_in_runner_subnet(self):
        ipv6 = statement(self.runner, 'AssignRunnerIpv6')
        self.assertEqual(actions(ipv6), {'ec2:AssignIpv6Addresses'})
        self.assertIn(':network-interface/*', ipv6)
        self.assertIn('"aws:ResourceTag/Role" = "github-runner"', ipv6)
        self.assertIn('"ec2:Subnet" = aws_subnet.runner[0].arn', ipv6)

    def test_launch_pins_ami_owner_purpose_and_exact_network_inputs(self):
        image = statement(self.controller, 'LaunchApprovedRunnerImages')
        self.assertIn('arn:aws:ec2:us-west-1::image/*', image)
        self.assertRegex(image, r'"ec2:Owner"\s*=\s*data.aws_caller_identity.current.account_id')
        self.assertIn('"aws:ResourceTag/Purpose" = "github-runner"', image)
        network = statement(self.controller, 'LaunchExactRunnerNetwork')
        for reference in ['aws_subnet.runner[0].arn', 'aws_security_group.runner[0].arn',
                          ':key-pair/fcvm-ec2']:
            self.assertIn(reference, network)
        self.assertNotIn(':subnet/*', network)
        self.assertNotIn(':security-group/*', network)

    def test_all_created_resources_require_tags_profile_and_encryption(self):
        for sid in ['LaunchTaggedRunnerInstance', 'LaunchTaggedRunnerENI', 'LaunchEncryptedRunnerVolume']:
            item = statement(self.controller, sid)
            self.assertEqual(actions(item), {'ec2:RunInstances'})
            self.assertIn('"aws:RequestTag/Role" = "github-runner"', item)
        instance = statement(self.controller, 'LaunchTaggedRunnerInstance')
        self.assertIn('"ec2:InstanceProfile" = aws_iam_instance_profile.runner[0].arn', instance)
        self.assertIn('"ec2:MetadataHttpTokens" = "required"', instance)
        self.assertIn('"ec2:Encrypted" = "true"', statement(self.controller, 'LaunchEncryptedRunnerVolume'))

    def test_launch_tagging_cannot_be_used_to_adopt_existing_fleet(self):
        launch = statement(self.controller, 'TagOnlyDuringRunnerLaunch')
        self.assertEqual(actions(launch), {'ec2:CreateTags'})
        self.assertIn('"ec2:CreateAction" = "RunInstances"', launch)
        self.assertIn('"aws:RequestTag/Role" = "github-runner"', launch)
        denial = statement(self.controller, 'DenyNonLeaseTagChangesOutsideLaunch')
        self.assertRegex(denial, r'Effect\s*=\s*"Deny"')
        self.assertIn('StringNotEqualsIfExists', denial)
        self.assertIn('"ForAnyValue:StringNotEquals"', denial)
        self.assertIn('"aws:TagKeys" = ["LeaseExpires", "RunnerSeenAt", "CapacityFailedAt"]', denial)
        lease = statement(self.controller, 'UpdateRunnerLeaseOnly')
        self.assertIn('"aws:ResourceTag/Role" = "github-runner"', lease)
        self.assertIn('"aws:TagKeys" = ["LeaseExpires", "RunnerSeenAt", "CapacityFailedAt"]', lease)

    def test_termination_only_covers_runners_and_existing_temporary_builders(self):
        terminating = [s for s in statements(self.controller) if 'ec2:TerminateInstances' in s]
        self.assertEqual(len(terminating), 2)
        for item in terminating:
            self.assertIn(':instance/*', item)
            self.assertIn('Condition', item)
        self.assertIn('"aws:ResourceTag/Role" = "github-runner"', statement(self.controller, 'TerminateRunnerOnly'))
        self.assertIn('"aws:ResourceTag/Name" = "ami-builder-temp"', statement(self.controller, 'ReapExistingTemporaryAMIBuilder'))
        self.assertNotIn('ec2:StopInstances', self.controller)

    def test_controller_wildcard_allow_contains_no_mutating_ec2_actions(self):
        for item in statements(self.controller):
            if re.search(r'Effect\s*=\s*"Allow"', item) and re.search(r'Resource\s*=\s*"\*"', item):
                self.assertEqual(actions(item), {'ec2:DescribeImages', 'ec2:DescribeInstances'})
        self.assertIn('["pat", "user-data"]', self.controller)
        self.assertNotIn('parameter/github-runner/*', self.controller)
        self.assertNotIn('arn:aws:logs:*:*:*', self.controller)


if __name__ == '__main__':
    unittest.main(verbosity=2)


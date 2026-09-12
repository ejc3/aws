#!/usr/bin/env python3
"""The live canary must distinguish an actual denial from a broken test path."""

import importlib.util
from pathlib import Path
import re
from subprocess import CompletedProcess
import unittest

path = Path(__file__).with_name("check-runner-iam-canary.py")
spec = importlib.util.spec_from_file_location("canary", path)
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)


class CanaryResultTests(unittest.TestCase):
    name = canary.PREFIX + "first"

    def result(self, code, stdout="", stderr=""):
        return CompletedProcess([], code, stdout, stderr)

    def test_allow_requires_exact_parameter_name(self):
        self.assertTrue(canary.accepted_parameter_result(self.result(0, self.name + "\n"), self.name, True))
        self.assertFalse(canary.accepted_parameter_result(self.result(0, "other"), self.name, True))
        self.assertFalse(canary.accepted_parameter_result(self.result(1, self.name), self.name, True))

    def test_explicit_api_denial_is_accepted(self):
        result = self.result(254, stderr="An error occurred (AccessDeniedException) when calling the GetParameter operation: denied")
        self.assertTrue(canary.accepted_parameter_result(result, self.name, False))

    def test_transport_missing_parameter_and_success_are_not_denial(self):
        for result in (
            self.result(255, stderr="Permission denied (publickey)"),
            self.result(254, stderr="An error occurred (ParameterNotFound) when calling the GetParameter operation"),
            self.result(254, stderr="Could not connect to the endpoint URL"),
            self.result(127, stderr="aws: command not found"),
            self.result(0, self.name),
        ):
            self.assertFalse(canary.accepted_parameter_result(result, self.name, False))

    def test_broad_error_text_does_not_count_as_iam_denial(self):
        self.assertFalse(canary.accepted_parameter_result(
            self.result(1, stderr="AccessDeniedException"), self.name, False))

    def test_batch_denial_requires_the_batch_operation(self):
        result = self.result(254, stderr="An error occurred (AccessDeniedException) when calling the GetParameters operation: denied")
        self.assertTrue(canary.accepted_parameter_result(result, self.name, False, "GetParameters"))
        self.assertFalse(canary.accepted_parameter_result(result, self.name, False, "GetParameter"))


class CanaryFixtureTests(unittest.TestCase):
    def setUp(self):
        self.source = path.parent.parent.joinpath("runner-bootstrap-canary.tf").read_text()

    def test_only_two_test_hosts_and_two_bound_parameters(self):
        self.assertEqual(re.findall(r'^resource "([^"]+)" "([^"]+)"', self.source, re.M),
                         [("aws_instance", "runner_iam_canary"), ("aws_ssm_parameter", "runner_iam_canary")])
        self.assertIn('for_each = toset(var.enable_github_runner ? ["first", "second"] : [])', self.source)
        self.assertIn('for_each = aws_instance.runner_iam_canary', self.source)
        self.assertIn('InstanceArn = each.value.arn', self.source)
        self.assertIn('name  = "' + canary.PREFIX + '${each.key}"', self.source)
        self.assertIn('value = "security-canary-not-a-credential"', self.source)

    def test_real_role_without_registration_or_new_permissions(self):
        self.assertIn('iam_instance_profile        = aws_iam_instance_profile.runner[0].name', self.source)
        self.assertIn('Role        = "runner-iam-canary"', self.source)
        self.assertIn('Purpose     = "runner-bootstrap-iam-canary"', self.source)
        for forbidden in ('user_data', 'provisioner ', 'local-exec', 'remote-exec',
                          'github_runner_pat', 'secretsmanager', 'dynamodb'):
            self.assertNotIn(forbidden, self.source)

    def test_small_encrypted_disposable_imdsv2_hosts(self):
        for field, value in {'instance_type': '"t4g.micro"', 'volume_type': '"gp3"',
                             'volume_size': '8', 'encrypted': 'true',
                             'delete_on_termination': 'true', 'http_tokens': '"required"',
                             'http_put_response_hop_limit': '1', 'cpu_credits': '"standard"'}.items():
            self.assertRegex(self.source, r'\b' + field + r'\s*=\s*' + re.escape(value) + r'\s*\n')
        self.assertIn('subnet_id                   = aws_subnet.runner[0].id', self.source)
        self.assertIn('vpc_security_group_ids      = [aws_security_group.runner[0].id]', self.source)

    def test_reviewed_ami_and_explicit_removal_reminder(self):
        self.assertIn('owners = ["amazon"]', self.source)
        self.assertIn('al2023-ami-2023.12.20260831.0-kernel-6.18-arm64', self.source)
        self.assertEqual(self.source.count('RemoveAfter = "2026-09-13"'), 2)
        self.assertIn('NOT an automatic expiry policy', self.source)


if __name__ == "__main__":
    unittest.main()

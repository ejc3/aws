#!/usr/bin/env python3
"""The live canary must distinguish an actual denial from a broken test path."""

import importlib.util
from pathlib import Path
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

    def test_registration_allow_requires_success_and_no_item(self):
        self.assertTrue(canary.accepted_registration_result(self.result(0, "null\n"), True))
        for result in (self.result(1, "null"), self.result(0, "{}"),
                       self.result(0, '{"InstanceArn":{"S":"existing"}}'),
                       self.result(0, ""), self.result(0, "None")):
            self.assertFalse(canary.accepted_registration_result(result, True))

    def test_registration_deny_requires_actual_get_item_access_denial(self):
        denial = "An error occurred (AccessDeniedException) when calling the GetItem operation"
        self.assertTrue(canary.accepted_registration_result(self.result(254, stderr=denial), False))
        for result in (self.result(0, "null"), self.result(255, stderr=denial),
                       self.result(254, stderr="An error occurred (ResourceNotFoundException) when calling the GetItem operation"),
                       self.result(254, stderr=denial.replace("GetItem", "PutItem")),
                       self.result(254, stderr="AccessDeniedException")):
            self.assertFalse(canary.accepted_registration_result(result, False))

    def test_registration_key_is_one_exact_current_instance_arn(self):
        self.assertEqual(canary.registration_key("i-0123456789abcdef0"), {
            "InstanceArn": {"S": "arn:aws:ec2:us-west-1:928413605543:instance/i-0123456789abcdef0"}
        })
        for value in ("i-*", "runner-iam-canary-first", "i-01234567", "../other"):
            with self.assertRaises(RuntimeError):
                canary.registration_key(value)

    def test_registration_probe_never_writes_or_scans(self):
        text = path.read_text()
        for operation in ('"put-item"', '"delete-item"', '"scan"', '"query"',
                          'dynamodb.put_item(', 'dynamodb.delete_item(', 'dynamodb.scan(', 'dynamodb.query('):
            self.assertNotIn(operation, text)
        self.assertIn('ProjectionExpression="InstanceArn", ConsistentRead=True', text)
        self.assertIn('"--projection-expression", "InstanceArn", "--consistent-read"', text)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Offline tests of the dedicated benchmark worker and its operator boundary."""

import datetime
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
TF = (ROOT / "benchmark-worker.tf").read_text()
BOOT_RACE_TF = (ROOT / "boot-race.tf").read_text()


def block(header, source=TF):
    start = source.index(header)
    return source[start:source.index("\n}\n", start)]


class Worker(unittest.TestCase):
    def setUp(self):
        self.ec2 = Mock()
        self.env = self.enterContext(patch.dict(os.environ, {
            "ENI": "eni-123", "TEMPLATE": "lt-123", "VERSION": "7", "PUBLIC_IPV4": "192.0.2.1",
        }))
        spec = importlib.util.spec_from_file_location("control", ROOT / "benchmark-worker/control.py")
        self.control = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"boto3": SimpleNamespace(client=lambda _name: self.ec2)}):
            spec.loader.exec_module(self.control)
        self.ec2.describe_network_interfaces.return_value = {"NetworkInterfaces": [{}]}

    def attached(self, hours=2, project="benchmark-worker", index=0):
        inst = {
            "InstanceId": "i-example", "State": {"Name": "running"},
            "Tags": [{"Key": "Name", "Value": "benchmark-worker"}, {"Key": "Project", "Value": project}],
            "LaunchTime": datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours),
        }
        self.ec2.describe_network_interfaces.return_value = {
            "NetworkInterfaces": [{"Attachment": {"InstanceId": inst["InstanceId"], "DeviceIndex": index}}],
        }
        self.ec2.describe_instances.return_value = {"Reservations": [{"Instances": [inst]}]}
        return inst

    def invoke(self, **event):
        return self.control.lambda_handler(event, None)

    def test_launch_is_one_pinned_instance_without_overrides(self):
        self.ec2.run_instances.return_value = {"Instances": [{"InstanceId": "i-test", "State": {"Name": "pending"}}]}
        self.assertEqual(self.invoke(action="up"), {
            "instance": "i-test", "state": "pending", "public_ipv4": "192.0.2.1", "launched": True,
        })
        self.ec2.run_instances.assert_called_once_with(
            LaunchTemplate={"LaunchTemplateId": "lt-123", "Version": "7"}, MinCount=1, MaxCount=1,
        )
        with self.assertRaisesRegex(ValueError, "exactly one action"):
            self.invoke(action="up", InstanceType="m8g.metal")
        self.ec2.run_instances.assert_called_once()

    def test_up_reuses_eni_owner_status_and_never_launches_a_second(self):
        self.attached()
        expected = {"instance": "i-example", "state": "running", "public_ipv4": "192.0.2.1"}
        self.assertEqual(self.invoke(action="up"), expected)
        self.assertEqual(self.invoke(action="status"), expected)
        self.ec2.run_instances.assert_not_called()

    def test_down_is_bound_to_the_instance_observed_by_the_caller(self):
        self.attached()
        with self.assertRaisesRegex(ValueError, "refusing"):
            self.invoke(action="down", instance="i-old")
        self.ec2.terminate_instances.assert_not_called()
        self.assertEqual(self.invoke(action="down", instance="i-example")["state"], "shutting-down")
        self.ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-example"])

    def test_expiry_is_external_to_the_worker(self):
        self.attached(hours=5)
        self.assertEqual(self.invoke(action="reap")["state"], "running")
        self.ec2.terminate_instances.assert_not_called()
        self.attached(hours=6.1)
        self.assertEqual(self.invoke(action="reap")["state"], "shutting-down")
        self.ec2.terminate_instances.assert_called_once_with(InstanceIds=["i-example"])

    def test_periodic_reap_of_an_empty_worker_never_launches_one(self):
        self.assertEqual(self.invoke(action="reap"), {"state": "absent"})
        self.ec2.run_instances.assert_not_called()
        self.ec2.terminate_instances.assert_not_called()
        event = block('resource "aws_cloudwatch_event_target" "benchmark_worker_reaper"')
        self.assertIn('jsonencode({ action = "reap" })', event)
        permission = block('resource "aws_lambda_permission" "benchmark_worker_reaper"')
        self.assertIn('source_arn    = aws_cloudwatch_event_rule.benchmark_worker_reaper[0].arn', permission)

    def test_unexpected_attachment_is_never_touched(self):
        for invalid in ({"project": "ordinary"}, {"index": 1}):
            self.attached(**invalid)
            with self.assertRaises(RuntimeError):
                self.invoke(action="reap")
        self.ec2.run_instances.assert_not_called()
        self.ec2.terminate_instances.assert_not_called()


class Boundaries(unittest.TestCase):
    def test_workload_ingress_is_from_the_dedicated_worker_only(self):
        group = block('resource "aws_security_group" "boot_race"', BOOT_RACE_TF)
        self.assertNotRegex(group, r"(?m)^  ingress\s*\{")
        self.assertRegex(group, r"for_each = local\.benchmark_worker_enabled \? toset\(\[22, 8080\]\)")
        self.assertIn("security_groups = [aws_security_group.benchmark_worker[0].id]", group)
        self.assertEqual(group.count('dynamic "ingress"'), 1)

    def test_only_the_controller_has_launch_and_passrole(self):
        operator = block('resource "aws_iam_role_policy" "benchmark_worker_operator"')
        operator_trust = block('resource "aws_iam_role" "benchmark_worker_operator"')
        control = block('resource "aws_iam_role_policy" "benchmark_worker_control"')
        self.assertNotIn('"ec2:RunInstances"', operator)
        self.assertNotIn('"iam:PassRole"', operator)
        self.assertIn('aws_instance.firecracker_dev[0].arn', operator_trust)
        self.assertIn('Resource  = aws_iam_role.benchmark_worker[0].arn', control)
        self.assertIn('"iam:PassedToService" = "ec2.amazonaws.com"', control)
        self.assertIn('aws_network_interface.benchmark_worker[0].arn', control)
        self.assertIn('depends_on = [aws_cloudwatch_event_target.benchmark_worker_reaper]', operator)

    def test_worker_boots_without_a_shared_login_or_persistent_storage(self):
        template = block('resource "aws_launch_template" "benchmark_worker"')
        self.assertNotIn('key_name', template)
        self.assertNotIn('dev_hop', template)
        self.assertIn('instance_type = "c8gd.metal-24xl"', template)
        self.assertIn('market_type = "spot"', template)
        self.assertRegex(template, r'max_price\s*= "1\.50"')
        self.assertNotIn("instance_initiated_shutdown_behavior", template)
        self.assertIn('http_tokens                 = "required"', template)
        self.assertIn('local.pbox_local_nvme_setup', template)
        self.assertIn('test -c /dev/kvm', template)
        self.assertIn('delete_on_termination = true', template)
        self.assertIn('delete_on_termination = false', template)


if __name__ == "__main__":
    unittest.main(buffer=True)

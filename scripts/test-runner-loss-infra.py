#!/usr/bin/env python3
"""Offline checks on the metal-runner evidence boundary."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "runner-loss.tf").read_text()
DEV = (ROOT / "dev-instance-common.tf").read_text()


def block(source, resource, name):
    match = re.search(rf'^resource "{resource}" "{name}" {{\n.*?^}}', source, re.M | re.S)
    if not match:
        raise AssertionError(f"missing {resource}.{name}")
    return match.group()


class RunnerLossInfraTests(unittest.TestCase):
    def test_dev_can_read_only_the_projected_group(self):
        policy = block(DEV, "aws_iam_policy", "dev_server_service_quotas_read")
        statement = re.search(r'Sid\s*= "ReadMetalRunnerLosses"\n(.*?)\n\s*},', policy, re.S).group(1)
        actions = re.findall(r'"(logs:[^"]+)"', statement)
        self.assertCountEqual(actions, ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams"])
        self.assertRegex(statement, r'Resource\s*= "\$\{aws_cloudwatch_log_group.runner_loss.arn\}:\*"\s*$')
        group = block(TF, "aws_cloudwatch_log_group", "runner_loss")
        self.assertIn('"/aws/lambda/github-runner-loss"', group)
        self.assertGreaterEqual(int(re.search(r'retention_in_days\s*= (\d+)', group)[1]), 30)

    def test_collector_only_reads_ec2_in_runner_regions_and_writes_its_own_streams(self):
        policy = block(TF, "aws_iam_role_policy", "runner_loss")
        actions = re.findall(r'"((?:ec2|logs|ssm|iam|cloudtrail|lambda):[^"]+)"', policy)
        self.assertCountEqual(actions, ["ec2:DescribeInstances", "ec2:DescribeSpotInstanceRequests",
                                        "logs:CreateLogStream", "logs:PutLogEvents"])
        self.assertIn('"aws:RequestedRegion" = local.runner_regions', policy)
        self.assertIn('"${aws_cloudwatch_log_group.runner_loss.arn}:log-stream:*"', policy)

    def test_eventbridge_can_invoke_from_only_the_exact_rule(self):
        permission = block(TF, "aws_lambda_permission", "runner_loss_scan")
        self.assertIn('"events.amazonaws.com"', permission)
        self.assertRegex(permission, r'source_arn\s*= aws_cloudwatch_event_rule.runner_loss\[0\].arn')
        # EventBridge rules do not send SourceAccount to Lambda.
        self.assertNotIn("source_account", permission)

    def test_only_accepted_events_from_our_controller_log_groups_invoke_collector(self):
        sources = re.search(r'runner_loss_controllers = .*?toset\(\[(.*?)\]\)', TF)[1]
        self.assertCountEqual(re.findall(r'"([^"]+)"', sources),
                              ["github-runner-cleanup", "github-runner-webhook", "github-runner-reuse"])
        permission = block(TF, "aws_lambda_permission", "runner_loss_controller")
        self.assertIn('source_account = data.aws_caller_identity.current.account_id', permission)
        self.assertIn('"${aws_cloudwatch_log_group.runner_loss_source[each.key].arn}:*"', permission)
        self.assertIn('"logs.amazonaws.com"', permission)
        subscription = block(TF, "aws_cloudwatch_log_subscription_filter", "runner_loss_controller")
        self.assertIn(r'"{ $.event = \"runner_termination_accepted\" }"', subscription)
        self.assertIn("aws_lambda_permission.runner_loss_controller", subscription)


if __name__ == "__main__":
    unittest.main(buffer=True)

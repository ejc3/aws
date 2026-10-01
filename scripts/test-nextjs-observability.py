#!/usr/bin/env python3
"""Memory and swap alarms must be able to see data and must not read "no data" as healthy.

2026-10-01: nextjs-dev ran out of memory (two Node processes of ~11.6 GB) with `nextjs-dev-memory-pressure`
green. The CloudWatch agent on the box could never publish, because its role had no cloudwatch:PutMetricData:
the CWAgent namespace was empty for the box's whole life, and with treat_missing_data = notBreaching an alarm
with no data is green forever. Offline: reads the Terraform source."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEXTJS = (ROOT / "nextjs-dev.tf").read_text()
ALARMS = (ROOT / "nextjs-alarms.tf").read_text()
CM = (ROOT / "claude-master-server.tf").read_text()
USER_DATA = (ROOT / "nextjs-user-data.tf").read_text()


def block(text, kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n.*?^\}' % (kind, name), text, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


class MetricsCanBePublished(unittest.TestCase):
    def test_the_role_may_publish_to_the_agents_namespace_and_nothing_else(self):
        policy = block(NEXTJS, "aws_iam_role_policy", "nextjs_dev_metrics")
        self.assertIn('"cloudwatch:PutMetricData"', policy)
        self.assertIn('"cloudwatch:namespace" = "CWAgent"', policy)
        self.assertEqual(policy.count("Action"), 1, "one action")
        self.assertNotRegex(policy, r"logs:|cloudwatch:\*|\"\*\"\s*$")
        self.assertIn("aws_iam_role.nextjs_dev.id", policy)

    def test_the_namespace_granted_is_the_one_the_agent_and_the_alarms_use(self):
        self.assertIn('"namespace": "CWAgent"', USER_DATA)
        for name in ("nextjs_memory", "nextjs_swap"):
            self.assertIn('namespace           = "CWAgent"', block(ALARMS, "aws_cloudwatch_metric_alarm", name))


class NoDataIsNotHealthy(unittest.TestCase):
    def alarms(self):
        return [(ALARMS, "nextjs_memory"), (ALARMS, "nextjs_swap"),
                (CM, "claude_master_server_memory"), (CM, "claude_master_server_swap")]

    def test_host_metric_alarms_report_no_data_instead_of_staying_green(self):
        for text, name in self.alarms():
            alarm = block(text, "aws_cloudwatch_metric_alarm", name)
            self.assertRegex(alarm, r'treat_missing_data\s+=\s+"missing"', name)
            self.assertNotRegex(alarm, r'treat_missing_data\s+=\s+"notBreaching"', name)
            self.assertIn("insufficient_data_actions = [aws_sns_topic.cost_alerts.arn]", re.sub(r"\s+=\s+", " = ", alarm), name)

    def test_there_are_four_so_none_was_missed(self):
        self.assertEqual(len(self.alarms()), 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)

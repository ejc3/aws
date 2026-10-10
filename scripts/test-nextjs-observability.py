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


class RunawayIsKilledNotPaged(unittest.TestCase):
    """2026-10-10: the box paged itself unreachable with the kernel never OOM-killing anything."""

    def args(self):
        return re.search(r'^EARLYOOM_ARGS="(.*)"$', USER_DATA, re.M).group(1)

    def test_earlyoom_is_installed_enabled_and_started(self):
        for line in ("apt-get install -y earlyoom", "systemctl enable earlyoom.service", "systemctl restart earlyoom.service"):
            self.assertIn(line, USER_DATA)

    def test_memory_alone_decides_because_this_box_has_swap(self):
        # earlyoom acts only when memory AND swap are both under their minimums; with a 4 GB swapfile the box would page
        # long before swap ran out. -s 100 puts swap always "under", so memory decides.
        self.assertIn("swapon /swapfile", USER_DATA)
        self.assertIn("-m 4,2 -s 100,100", self.args())

    def test_a_next_dev_server_is_preferred_and_the_access_path_is_never_a_candidate(self):
        args = self.args()
        prefer = re.search(r"--prefer '([^']*)'", args).group(1)
        avoid = re.search(r"--avoid '([^']*)'", args).group(1)
        # Next sets its title to "next-server (vX.Y.Z)"; the kernel keeps 15 characters of it.
        self.assertRegex("next-server (v1", prefer)
        self.assertNotRegex("claude", prefer)
        for name in ("sshd", "cloudflared", "amazon-ssm-agent", "tmux", "systemd-journald"):
            self.assertRegex(name, avoid)
        self.assertNotRegex("next-server (v1", avoid)
        self.assertNotRegex("node", avoid)


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

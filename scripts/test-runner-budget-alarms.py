#!/usr/bin/env python3
"""Evaluate the actual budget alarm selections with Terraform and no provider or credentials."""
import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path


SOURCE = Path(__file__).resolve().parents[1] / "runner-app.tf"
ALARMS = ("runner_budget_throttled", "runner_budget_weekly")


class RunnerBudgetAlarms(unittest.TestCase):
    def test_each_alarm_selects_only_served_groups_when_the_runner_is_enabled(self):
        source = SOURCE.read_text()
        selected = re.search(r"^  runner_budget_served = .+$", source, re.M).group()
        # Dolphin's multi-repo tuple and dated increase differ from the other groups' policies.
        fixture = """
variable "enable_github_runner" { type = bool }
locals {
  runner_budget = {
    dolphin = { repos = ["labs", "films"], daily_usd = 120, launch_week = { daily_usd = 192 } }
    imagine = { repos = ["imagine"], daily_usd = 50, launch_week = null }
    constellation = { repos = ["constellation"], daily_usd = 50, launch_week = null }
  }
  runner_app_config = { labs = {}, imagine = {} }
"""
        fixture += selected + "\n"
        for name in ALARMS:
            resource = re.search(rf'^resource "aws_cloudwatch_metric_alarm" "{name}" \{{.*?^\}}', source, re.M | re.S).group()
            gate = re.search(r"^  for_each\s+= (.+)$", resource, re.M)[1]
            fixture += f"  {name} = {gate}\n"
        fixture += "}\n"
        expression = "jsonencode({" + ", ".join(f"{name}=keys(local.{name})" for name in ALARMS) + "})"
        with tempfile.TemporaryDirectory(prefix="runner-budget-alarms-") as tmp:
            Path(tmp, "main.tf").write_text(fixture)
            for enabled, expected in (("true", ["dolphin", "imagine"]), ("false", [])):
                with self.subTest(enabled=enabled):
                    result = subprocess.run(
                        ["terraform", "console", "-no-color", f"-var=enable_github_runner={enabled}"],
                        input=expression + "\n", cwd=tmp, text=True, capture_output=True, timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(result.stderr.strip(), result.stderr)
                    self.assertEqual(json.loads(json.loads(result.stdout)), {name: expected for name in ALARMS})


if __name__ == "__main__":
    unittest.main()

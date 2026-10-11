#!/usr/bin/env python3
"""dolphin-maps-loop.tf: dolphin-maps' scheduled loops in Lambda. The schedules start off and run in Los
Angeles time; the GitHub App's secret is readable by the function and the jumpbox role only; the deploy role
trusts one workflow on main; Terraform never takes the code back from the deploy; nobody but administration
deletes the journal. Offline."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-maps-loop.tf").read_text()
AI = (ROOT / "dev-ai-services.tf").read_text()


def block(kind, name):
    m = re.search(r'^(?:resource|data|output|variable) (?:"%s" )?"%s" \{\n.*?^\}' % (kind, name), TF, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


class ScheduleTests(unittest.TestCase):
    def test_both_schedules_start_off_behind_one_committed_gate(self):
        self.assertRegex(block("", "dolphin_maps_loop_enabled"), r"default\s+= false")
        schedule = block("aws_scheduler_schedule", "dolphin_maps_loop")
        self.assertIn('state      = var.dolphin_maps_loop_enabled ? "ENABLED" : "DISABLED"', schedule)

    def test_the_schedules_are_in_los_angeles_time_and_name_their_mode(self):
        schedule = block("aws_scheduler_schedule", "dolphin_maps_loop")
        self.assertIn('schedule_expression_timezone = "America/Los_Angeles"', schedule)
        self.assertIn("input    = jsonencode({ mode = each.key })", schedule)
        self.assertIn('live  = "cron(*/5 6-8 ? * MON-FRI *)"', TF)
        self.assertIn('sweep = "cron(0 23 ? * SUN *)"', TF)

    def test_a_failed_pass_is_not_retried(self):
        self.assertIn("maximum_retry_attempts       = 0", block("aws_lambda_function_event_invoke_config", "dolphin_maps_loop"))
        self.assertIn("maximum_retry_attempts       = 0", block("aws_scheduler_schedule", "dolphin_maps_loop"))


class AccessTests(unittest.TestCase):
    def test_the_github_app_secret_is_the_function_and_the_jumpbox_role_only(self):
        policy = block("aws_secretsmanager_secret_policy", "dolphin_maps_loop_github_app")
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn("concat([aws_iam_role.dolphin_maps_loop.arn], local.github_admin_token_readers)", policy)
        self.assertNotIn("games_mp_admin_principals", policy)
        self.assertNotIn("secret_string", TF)

    def test_the_function_reads_its_two_secrets_and_writes_only_the_journal(self):
        policy = block("aws_iam_role_policy", "dolphin_maps_loop")
        self.assertIn("[aws_secretsmanager_secret.dolphin_maps_loop_github_app.arn, aws_secretsmanager_secret.browserbase.arn]", policy)
        self.assertIn('Resource = "${aws_s3_bucket.dolphin_maps_loop.arn}/journal/*"', policy)
        self.assertNotIn("code/", policy)
        self.assertIn("aws_iam_role.dolphin_maps_loop.arn", re.search(r"browserbase_readers = \[[^\]]*\]", AI).group())

    def test_the_deploy_role_trusts_deploy_loop_yml_on_main_and_nothing_else(self):
        role = block("aws_iam_role", "dolphin_maps_loop_deploy")
        self.assertIn('"token.actions.githubusercontent.com:sub"              = local.dolphin_maps_loop_sub', role)
        self.assertIn('"dolphin-labs-hq/dolphin-maps/.github/workflows/deploy-loop.yml@refs/heads/main"', role)
        self.assertIn('dolphin_maps_loop_sub    = "repo:dolphin-labs-hq@316189183/dolphin-maps@1409029367:ref:refs/heads/main"', TF)
        self.assertNotIn("StringLike", role)
        deploy = block("aws_iam_role_policy", "dolphin_maps_loop_deploy")
        self.assertIn('"${aws_s3_bucket.dolphin_maps_loop.arn}/code/*"', deploy)
        self.assertNotRegex(deploy, r"iam:|PassRole|AddPermission")

    def test_terraform_leaves_the_deployed_code_and_handler_alone(self):
        fn = block("aws_lambda_function", "dolphin_maps_loop")
        self.assertIn("ignore_changes = [filename, source_code_hash, s3_bucket, s3_key, s3_object_version, handler]", fn)
        self.assertIn("reserved_concurrent_executions = 1", fn)

    def test_only_administration_deletes_the_journal(self):
        policy = block("aws_s3_bucket_policy", "dolphin_maps_loop")
        statement = re.search(r'Sid       = "JournalIsNotDeletedByHand".*?\}\s*\}', policy, re.S).group()
        self.assertIn('"${aws_s3_bucket.dolphin_maps_loop.arn}/journal/*"', statement)
        self.assertIn("local.games_mp_admin_principals", statement)


class AlarmTests(unittest.TestCase):
    def test_a_failed_sweep_alarms_on_the_line_the_function_logs(self):
        # dolphin-maps pipeline/src/aws/lambda.ts logs "loop <mode> failed: <why>" before it throws.
        self.assertIn('pattern        = "\\"loop sweep failed\\""', block("aws_cloudwatch_log_metric_filter", "dolphin_maps_loop_sweep_failed"))
        self.assertIn("evaluation_periods  = 1", block("aws_cloudwatch_metric_alarm", "dolphin_maps_loop_sweep_failed"))
        live = block("aws_cloudwatch_metric_alarm", "dolphin_maps_loop_errors")
        self.assertIn("evaluation_periods  = 3", live)
        self.assertIn("datapoints_to_alarm = 3", live)

    def test_the_placeholder_only_throws(self):
        text = (ROOT / "dolphin-maps-loop" / "placeholder.mjs").read_text()
        self.assertIn("throw new Error", text)
        json.dumps(text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

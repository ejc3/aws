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
        self.assertIn("schedule_expression          = each.value.schedule", schedule)
        self.assertIn("arn      = aws_lambda_function.dolphin_maps_loop[each.key].arn", schedule)
        self.assertIn('live  = { schedule = "cron(*/5 6-8 ? * MON-FRI *)", timeout = 300, memory = 1024 }', TF)
        self.assertIn('sweep = { schedule = "cron(0 23 ? * SUN *)", timeout = 900, memory = 2048 }', TF)

    def test_a_failed_pass_is_not_retried(self):
        self.assertIn("maximum_retry_attempts       = 0", block("aws_lambda_function_event_invoke_config", "dolphin_maps_loop"))
        self.assertIn("for_each                     = aws_lambda_function.dolphin_maps_loop", block("aws_lambda_function_event_invoke_config", "dolphin_maps_loop"))
        self.assertIn("maximum_retry_attempts       = 0", block("aws_scheduler_schedule", "dolphin_maps_loop"))


class AccessTests(unittest.TestCase):
    def test_the_github_app_secret_is_the_function_and_the_jumpbox_role_only(self):
        policy = block("aws_secretsmanager_secret_policy", "dolphin_maps_loop_github_app")
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn("concat([aws_iam_role.dolphin_maps_loop.arn], local.github_admin_token_readers)", policy)
        self.assertNotIn("games_mp_admin_principals", policy)
        self.assertNotIn("secret_string", TF)

    def test_the_function_reads_its_secrets_and_writes_only_the_journal(self):
        policy = block("aws_iam_role_policy", "dolphin_maps_loop")
        self.assertIn("concat([aws_secretsmanager_secret.dolphin_maps_loop_github_app.arn, aws_secretsmanager_secret.browserbase.arn], [for s in aws_secretsmanager_secret.scraper_api_key : s.arn])", policy)
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
        self.assertIn("Resource = [for f in aws_lambda_function.dolphin_maps_loop : f.arn]", deploy)
        self.assertIn('"${aws_s3_bucket.dolphin_maps_loop.arn}/code/*"', deploy)
        self.assertNotRegex(deploy, r"iam:|PassRole|AddPermission")

    def test_terraform_leaves_the_deployed_code_alone_but_owns_the_handler(self):
        fn = block("aws_lambda_function", "dolphin_maps_loop")
        self.assertIn("ignore_changes = [filename, source_code_hash, s3_bucket, s3_key, s3_object_version]", fn)
        self.assertRegex(fn, r'handler\s+= "index.handler"')
        self.assertIn("reserved_concurrent_executions = 1", fn)
        self.assertIn('source_file = "${path.module}/dolphin-maps-loop/index.mjs"', block("archive_file", "dolphin_maps_loop_placeholder"))

    def test_the_deploy_role_cannot_change_the_functions_configuration(self):
        deploy = block("aws_iam_role_policy", "dolphin_maps_loop_deploy")
        actions = re.search(r"Action\s+= \[([^\]]*lambda:[^\]]*)\]", deploy).group(1)
        self.assertNotIn("UpdateFunctionConfiguration", actions)
        self.assertIn('"lambda:UpdateFunctionCode"', actions)

    def test_the_journal_is_versioned_so_an_overwrite_keeps_the_record(self):
        self.assertIn('status = "Enabled"', block("aws_s3_bucket_versioning", "dolphin_maps_loop"))
        lifecycle = block("aws_s3_bucket_lifecycle_configuration", "dolphin_maps_loop")
        self.assertIn("noncurrent_version_expiration", lifecycle)
        self.assertIn("depends_on = [aws_s3_bucket_versioning.dolphin_maps_loop]", lifecycle)

    def test_only_administration_deletes_the_journal(self):
        policy = block("aws_s3_bucket_policy", "dolphin_maps_loop")
        statement = re.search(r'Sid       = "JournalIsNotDeletedByHand".*?\}\s*\}', policy, re.S).group()
        self.assertIn('"${aws_s3_bucket.dolphin_maps_loop.arn}/journal/*"', statement)
        self.assertIn("local.games_mp_admin_principals", statement)


class ReplayTests(unittest.TestCase):
    def test_the_metal_boxes_can_read_noncurrent_journal_versions(self):
        grant = block("aws_iam_role_policy", "dev_server_dolphin_maps_loop_journal")
        self.assertIn('["s3:GetObject", "s3:GetObjectVersion"]', grant)
        self.assertIn('["s3:ListBucket", "s3:ListBucketVersions"]', grant)
        self.assertNotRegex(grant, r"s3:Put|s3:Delete")


class AlarmTests(unittest.TestCase):
    def test_each_loop_is_its_own_function_so_lambda_counts_every_sweep_failure(self):
        # A timeout, running out of memory or a failed start logs nothing of the function's own: only Lambda's
        # Errors metric for the sweep's own function sees every failure, and one failed sweep alarms.
        fn = block("aws_lambda_function", "dolphin_maps_loop")
        self.assertIn("for_each      = local.dolphin_maps_loop_modes", fn)
        self.assertIn('function_name = "${local.dolphin_maps_loop_name}-${each.key}"', fn)
        sweep = block("aws_cloudwatch_metric_alarm", "dolphin_maps_loop_sweep_failed")
        self.assertIn('metric_name         = "Errors"', sweep)
        self.assertIn('dimensions          = { FunctionName = aws_lambda_function.dolphin_maps_loop["sweep"].function_name }', sweep)
        self.assertIn("evaluation_periods  = 1", sweep)
        self.assertNotIn("aws_cloudwatch_log_metric_filter", TF)
        live = block("aws_cloudwatch_metric_alarm", "dolphin_maps_loop_errors")
        self.assertIn('aws_lambda_function.dolphin_maps_loop["live"].function_name', live)
        self.assertIn("evaluation_periods  = 6", live)
        self.assertIn("datapoints_to_alarm = 3", live)

    def test_dropped_passes_alarm_for_each_loop(self):
        dropped = block("aws_cloudwatch_metric_alarm", "dolphin_maps_loop_dropped")
        self.assertIn("for_each            = local.dolphin_maps_loop_modes", dropped)
        self.assertIn('metric_name         = "AsyncEventsDropped"', dropped)

    def test_a_loop_that_stops_running_alarms_while_its_schedule_is_on(self):
        idle = block("aws_cloudwatch_metric_alarm", "dolphin_maps_loop_not_running")
        self.assertIn("var.dolphin_maps_loop_enabled ? { live = 4, sweep = 7 } : {}", idle)
        self.assertIn('metric_name         = "Invocations"', idle)
        self.assertIn('treat_missing_data  = "breaching"', idle)
        self.assertIn("datapoints_to_alarm = each.value", idle)

    def test_the_placeholder_only_throws(self):
        text = (ROOT / "dolphin-maps-loop" / "index.mjs").read_text()
        self.assertIn("export const handler", text)
        self.assertIn("throw new Error", text)
        json.dumps(text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

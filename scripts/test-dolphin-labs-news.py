#!/usr/bin/env python3
"""dolphin-labs-news.tf: the news pass's journal cannot be erased, its deploy role reaches code only, and it is watched."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-labs-news.tf").read_text()


def block(kind, name):
    if kind in ("variable", "output"):
        m = re.search(r'^%s "%s" \{\n(.*?)^\}' % (kind, name), TF, re.M | re.S)
    else:
        m = re.search(r'^(?:resource|data) "%s" "%s" \{\n(.*?)^\}' % (kind, name), TF, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class JournalTests(unittest.TestCase):
    def test_bucket_is_versioned_and_old_versions_are_kept_400_days(self):
        self.assertIn('status = "Enabled"', block("aws_s3_bucket_versioning", "dolphin_labs_news"))
        life = block("aws_s3_bucket_lifecycle_configuration", "dolphin_labs_news")
        journal = life[life.index('id     = "journal"'):life.index('id     = "local-journals"')]
        self.assertRegex(journal, r"noncurrent_version_expiration \{\s*noncurrent_days = 400\s*\}")
        self.assertNotIn("STANDARD_IA", life)  # objects under 128 KB never transition; it did nothing
        self.assertIn("depends_on = [aws_s3_bucket_versioning.dolphin_labs_news]", life)

    def test_only_the_administration_set_may_delete_journal_versions(self):
        pol = block("aws_s3_bucket_policy", "dolphin_labs_news")
        self.assertIn('Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]', pol)
        self.assertIn('"aws:PrincipalArn" = local.games_mp_admin_principals', pol)


class AccessTests(unittest.TestCase):
    def test_deploy_role_updates_code_only(self):
        dep = block("aws_iam_role_policy", "dolphin_labs_news_deploy")
        self.assertIn('"lambda:UpdateFunctionCode"', dep)
        self.assertNotIn("UpdateFunctionConfiguration", re.sub(r"#.*", "", dep))
        trust = block("aws_iam_role", "dolphin_labs_news_deploy")
        self.assertIn("= local.dolphin_labs_main_sub", trust)

    def test_terraform_owns_the_handler_the_stand_in_provides(self):
        fn = block("aws_lambda_function", "dolphin_labs_news_pass")
        self.assertIn('handler       = "index.handler"', fn)
        ignored = re.search(r"ignore_changes = \[(.*?)\]", fn).group(1)
        self.assertNotIn("handler", ignored)
        self.assertTrue((ROOT / "dolphin-labs-news/index.mjs").is_file())
        self.assertIn("export const handler", (ROOT / "dolphin-labs-news/index.mjs").read_text())

    def test_boxes_are_nextjs_dev_only_never_the_io_boxes_role(self):
        boxes = block("aws_iam_role_policy", "box_dolphin_labs_news_journal")
        self.assertIn("for_each = { nextjs-dev = aws_iam_role.nextjs_dev.id }", boxes)
        # the versions an overwrite kept are readable for replays, still only under journal/ and local/
        self.assertIn('Action   = ["s3:GetObject", "s3:GetObjectVersion"]', boxes)
        self.assertIn('Action    = ["s3:ListBucket", "s3:ListBucketVersions"]', boxes)
        self.assertIn('"s3:prefix" = ["journal/*", "local/*"]', boxes)
        readme = (ROOT / "README.md").read_text()
        self.assertIn("## dolphin-labs news pass", readme)
        self.assertIn("Turn off `refresh-news.yml`'s schedule", readme)
        self.assertNotIn("dev_ebs_only", TF)


class WatchTests(unittest.TestCase):
    def test_not_running_alarm_once_enabled_treats_silence_as_breaching(self):
        a = block("aws_cloudwatch_metric_alarm", "dolphin_labs_news_pass_not_running")
        self.assertIn("count               = var.dolphin_labs_news_enabled ? 1 : 0", a)
        self.assertIn('metric_name         = "Invocations"', a)
        self.assertIn('treat_missing_data  = "breaching"', a)
        self.assertIn('comparison_operator = "LessThanThreshold"', a)
        self.assertIn("period              = local.dolphin_labs_news_minutes * 60", a)

    def test_one_number_drives_schedule_function_and_alarms(self):
        self.assertNotIn('variable "dolphin_labs_news_schedule"', TF)
        self.assertIn("schedule_expression          = local.dolphin_labs_news_schedule", TF)
        self.assertIn('NEWS_PASS_MINUTES     = tostring(local.dolphin_labs_news_minutes)', TF)
        self.assertRegex(TF, r'dolphin_labs_news_schedule = local\.dolphin_labs_news_minutes == 60 \?')

    def test_is_off_here_since_the_move_to_the_dolphin_account(self):
        var = block("variable", "dolphin_labs_news_enabled")
        self.assertRegex(var, r"default     = false\b")
        self.assertIn("dolphin-aws news.tf", var)
        self.assertIn("refresh-news.yml", var)
        self.assertIn("two minutes after the deadline", TF)



class XStepTests(unittest.TestCase):
    def test_x_function_is_its_own_from_the_same_bundle(self):
        fn = block("aws_lambda_function", "dolphin_labs_news_x")
        self.assertIn('handler       = "index.xHandler"', fn)
        self.assertIn("role          = aws_iam_role.dolphin_labs_news_x.arn", fn)
        self.assertIn("filename    = data.archive_file.dolphin_labs_news_placeholder.output_path", fn)
        self.assertIn("reserved_concurrent_executions = 1", fn)
        self.assertIn("timeout     = 240", fn)
        ignored = re.search(r"ignore_changes = \[(.*?)\]", fn).group(1)
        self.assertNotIn("handler", ignored)
        self.assertIn("source_code_hash", ignored)
        self.assertIn("export const xHandler", (ROOT / "dolphin-labs-news/index.mjs").read_text())
        self.assertIn("maximum_retry_attempts       = 0", block("aws_lambda_function_event_invoke_config", "dolphin_labs_news_x"))

    def test_x_role_reaches_haiku_the_redis_and_monid_secrets_and_its_logs_only(self):
        pol = block("aws_iam_role_policy", "dolphin_labs_news_x")
        self.assertIn("Statement = concat(local.bedrock_haiku_statements, [", pol)
        self.assertIn("Resource = [aws_secretsmanager_secret.dolphin_labs_news.arn, aws_secretsmanager_secret.dolphin_labs_monid.arn]", pol)
        self.assertEqual(sorted(re.findall(r'"(s3|secretsmanager|logs|lambda):(\w+)"', pol)),
                         [("logs", "CreateLogStream"), ("logs", "PutLogEvents"), ("s3", "PutObject"), ("secretsmanager", "GetSecretValue")])
        self.assertNotIn("browserbase", pol)
        fn = block("aws_lambda_function", "dolphin_labs_news_x")
        for line in ("NEWS_SECRET_ID      = aws_secretsmanager_secret.dolphin_labs_news.name",
                     "MONID_SECRET_ID     = aws_secretsmanager_secret.dolphin_labs_monid.name",
                     "NEWS_BEDROCK_REGION = local.bedrock_haiku_region",
                     "NEWS_BEDROCK_MODEL  = local.bedrock_haiku_profile",
                     'NEWS_ENVIRONMENT    = "production"'):
            self.assertIn(line, fn)

    def test_x_journal_goes_to_the_news_bucket_under_x_journal_put_only(self):
        fn = block("aws_lambda_function", "dolphin_labs_news_x")
        self.assertIn("NEWS_JOURNAL_BUCKET = aws_s3_bucket.dolphin_labs_news.bucket", fn)
        self.assertNotIn("NEWS_JOURNAL_PREFIX", fn)  # the step's own default, x-journal/
        pol = block("aws_iam_role_policy", "dolphin_labs_news_x")
        s3 = [st for st in re.split(r"\bSid\s+=", pol) if '"s3:' in st]
        self.assertEqual(len(s3), 1, s3)
        self.assertIn('Action   = "s3:PutObject"', s3[0])
        self.assertIn('Resource = "${aws_s3_bucket.dolphin_labs_news.arn}/x-journal/*"', s3[0])
        self.assertEqual(re.findall(r"aws_s3_bucket\.dolphin_labs_news\.arn[^\"\]]*", pol),
                         ["aws_s3_bucket.dolphin_labs_news.arn}/x-journal/*"])

    def test_the_pass_no_longer_reaches_monid(self):
        self.assertNotIn("dolphin_labs_monid", block("aws_iam_role_policy", "dolphin_labs_news_pass"))
        self.assertNotIn("MONID", block("aws_lambda_function", "dolphin_labs_news_pass"))
        self.assertIn("[aws_iam_role.dolphin_labs_news_x.arn]", block("aws_secretsmanager_secret_policy", "dolphin_labs_monid"))
        self.assertIn("aws_iam_role.dolphin_labs_news_x.arn", block("aws_secretsmanager_secret_policy", "dolphin_labs_news"))

    def test_x_schedule_is_off_here_and_targets_the_x_function(self):
        self.assertRegex(block("variable", "news_x_schedule_enabled"), r"default     = false\b")
        sch = block("aws_scheduler_schedule", "dolphin_labs_news_x")
        self.assertIn('state      = var.news_x_schedule_enabled ? "ENABLED" : "DISABLED"', sch)
        self.assertIn('schedule_expression          = "rate(5 minutes)"', sch)
        self.assertIn("arn      = aws_lambda_function.dolphin_labs_news_x.arn", sch)
        self.assertIn("role_arn = aws_iam_role.dolphin_labs_news_scheduler.arn", sch)
        self.assertIn("Resource = [aws_lambda_function.dolphin_labs_news_pass.arn, aws_lambda_function.dolphin_labs_news_x.arn]",
                      block("aws_iam_role_policy", "dolphin_labs_news_scheduler"))

    def test_x_has_its_own_errors_alarm_and_the_pass_alarms_watch_the_pass(self):
        a = block("aws_cloudwatch_metric_alarm", "dolphin_labs_news_x_errors")
        self.assertIn("dimensions          = { FunctionName = aws_lambda_function.dolphin_labs_news_x.function_name }", a)
        self.assertIn("alarm_actions       = [aws_sns_topic.cost_alerts.arn]", a)
        for name in ("dolphin_labs_news_pass_errors", "dolphin_labs_news_pass_not_running"):
            self.assertIn("FunctionName = aws_lambda_function.dolphin_labs_news_pass.function_name", block("aws_cloudwatch_metric_alarm", name))

    def test_x_not_running_alarm_exists_only_while_its_schedule_is_on(self):
        a = block("aws_cloudwatch_metric_alarm", "dolphin_labs_news_x_not_running")
        self.assertIn("count               = var.news_x_schedule_enabled ? 1 : 0", a)
        self.assertIn('metric_name         = "Invocations"', a)
        self.assertIn("dimensions          = { FunctionName = aws_lambda_function.dolphin_labs_news_x.function_name }", a)
        self.assertIn('treat_missing_data  = "breaching"', a)
        self.assertIn('comparison_operator = "LessThanThreshold"', a)

    def test_readme_runbook_covers_the_x_step(self):
        readme = (ROOT / "README.md").read_text()
        section = readme[readme.index("## dolphin-labs news pass"):readme.index("## Private browser manager")]
        for needle in ("dolphin-labs/monid", "news_x_schedule_enabled", "NEWS_X_LAMBDA_FUNCTION", "dolphin-labs-news-x-not-running"):
            self.assertIn(needle, section)

    def test_the_dev_box_can_see_the_x_step_but_not_its_secret(self):
        op = block("aws_iam_role_policy", "box_dolphin_labs_news_operate")
        self.assertIn("aws_lambda_function.dolphin_labs_news_x.arn", op)
        self.assertIn("aws_cloudwatch_log_group.dolphin_labs_news_x.arn", op)
        self.assertIn("aws_scheduler_schedule.dolphin_labs_news_x.arn", op)
        self.assertNotIn("dolphin_labs_monid", op)

    def test_deploy_role_updates_both_functions_code_and_output_names_the_x_function(self):
        dep = block("aws_iam_role_policy", "dolphin_labs_news_deploy")
        self.assertIn("Resource = [aws_lambda_function.dolphin_labs_news_pass.arn, aws_lambda_function.dolphin_labs_news_x.arn]", dep)
        out = block("output", "dolphin_labs_news")
        self.assertIn("NEWS_X_LAMBDA_FUNCTION    = aws_lambda_function.dolphin_labs_news_x.function_name", out)

    def test_monid_secret_value_is_set_out_of_band(self):
        self.assertIn('name                    = "dolphin-labs/monid"', block("aws_secretsmanager_secret", "dolphin_labs_monid"))
        self.assertNotIn("aws_secretsmanager_secret_version", TF)

    def test_dev_box_may_only_put_the_monid_key(self):
        put = block("aws_iam_role_policy", "box_dolphin_labs_monid_put")
        self.assertIn("for_each = { nextjs-dev = aws_iam_role.nextjs_dev.id }", put)
        self.assertEqual(re.findall(r'"secretsmanager:\w+"', put), ['"secretsmanager:PutSecretValue"'])
        self.assertIn("Resource = aws_secretsmanager_secret.dolphin_labs_monid.arn", put)


if __name__ == "__main__":
    unittest.main(verbosity=2)

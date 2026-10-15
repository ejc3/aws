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

    def test_is_on_and_says_to_retire_the_github_job_first(self):
        var = block("variable", "dolphin_labs_news_enabled")
        self.assertRegex(var, r"default     = true\b")
        self.assertIn("refresh-news.yml", var)
        self.assertIn("two minutes after the deadline", TF)


if __name__ == "__main__":
    unittest.main(verbosity=2)

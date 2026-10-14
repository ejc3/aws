# dolphin-labs' team-news pass, run in AWS: a Lambda on a schedule, with Claude Haiku 5.5 on Bedrock,
# a journal of every pass in S3 for backtesting, and its two secrets in Secrets Manager.
# It replaces the GitHub Actions job (dolphin-labs-hq/dolphin-labs refresh-news.yml); the GitHub OIDC
# role dolphin-labs-news-bedrock (bedrock-haiku.tf) stays until that job is retired.
#
#   dolphin-labs-news-pass        the function. Terraform creates it with a stand-in (dolphin-labs-news/
#                                 index.mjs) and owns its configuration, the handler `index.handler`
#                                 included; deploy-news.yml (main) of dolphin-labs-hq/dolphin-labs uploads a
#                                 bundle to code/ and calls UpdateFunctionCode through
#                                 dolphin-labs-news-deploy, and may change nothing else.
#   dolphin-labs-news-<account>   the bucket:
#                                   journal/  one compressed record per pass (its parsed inputs, the
#                                             schedule, each Haiku prompt without page text and its
#                                             answer, score components, outputs, the commit). Kept
#                                             400 days, so a season and the one before can be replayed
#                                             against changed code. The bucket is versioned: a PUT over
#                                             an existing record keeps the old one as a noncurrent
#                                             version for 400 days, and only the administration set may
#                                             delete a version, so a bad deploy cannot erase history.
#                                   local/    journals of passes run on a dev box. 30 days.
#                                   code/     deployed bundles. Lambda copies the code at update time,
#                                             so a bundle is kept 30 days only for a redeploy.
#   dolphin-labs/news             JSON secret: KV_REST_API_URL and KV_REST_API_TOKEN, the Upstash Redis
#                                 the site reads the lists from. Value set out of band.
#   browserbase/credentials       existing (dev-ai-services.tf); the function joins its readers.
#
# The schedule starts DISABLED. Switch-over, in order: the first deploy of the bundle; the secret's value;
# turn OFF refresh-news.yml's schedule in dolphin-labs (both would run every hour, bill Haiku and
# Browserbase twice and journal two passes per hour); then set dolphin_labs_news_enabled = true here. A run
# before the deploy fails on the stand-in, which the errors alarm reports; once enabled, the 'has not run'
# alarm watches that the schedule actually fires.
#
# Who reaches what:
#   the function            Bedrock Haiku 5.5; journal/ (write); the two secrets (read); its own logs
#   deploy-news.yml (main)  code/ (write); UpdateFunctionCode, GetFunction(Configuration) on this function only
#   refresh-news.yml (main) journal/ (write), through dolphin-labs-news-bedrock, until it is retired
#   nextjs-dev (the dolphin box, where replays run)
#                           journal/ and local/ (read); local/ (write). Not dev-ebs-only: that role is the
#                           I/O box's too (io-box.tf), and a persistent NFS server has no business here.

variable "dolphin_labs_news_enabled" {
  description = "Run dolphin-labs' news pass on its schedule. false keeps the function and its data and stops the runs. Turn refresh-news.yml's schedule off in dolphin-labs first (see the file header)."
  type        = bool
  default     = false
}

locals {
  dolphin_labs_news_name    = "dolphin-labs-news-pass"
  dolphin_labs_news_bucket  = "dolphin-labs-news-${data.aws_caller_identity.current.account_id}"
  dolphin_labs_news_minutes = 60
  # One number drives the schedule, the function's NEWS_PASS_MINUTES and the alarms' period. Hourly runs
  # at :37 (off the hour, as the GitHub job did); anything else is a plain rate.
  dolphin_labs_news_schedule = local.dolphin_labs_news_minutes == 60 ? "cron(37 * * * ? *)" : "rate(${local.dolphin_labs_news_minutes} minutes)"
}

# -------------------------------------------------------------------------------------
# Bucket
# -------------------------------------------------------------------------------------

resource "aws_s3_bucket" "dolphin_labs_news" {
  bucket = local.dolphin_labs_news_bucket
  tags   = { Name = "dolphin-labs-news", Project = "dolphin-labs" }
}

resource "aws_s3_bucket_public_access_block" "dolphin_labs_news" {
  bucket                  = aws_s3_bucket.dolphin_labs_news.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "dolphin_labs_news" {
  bucket = aws_s3_bucket.dolphin_labs_news.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Versioned so that a PUT over an existing journal record (a replay, a buggy deploy) keeps the original as a
# noncurrent version; deleting a version is refused outside the administration set (bucket policy).
resource "aws_s3_bucket_versioning" "dolphin_labs_news" {
  bucket = aws_s3_bucket.dolphin_labs_news.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "dolphin_labs_news" {
  bucket = aws_s3_bucket.dolphin_labs_news.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# A journal record is written once under a key naming its pass. No principal but the administration set may
# delete from journal/, and with versioning an overwrite only adds a version, so a bad deploy cannot erase
# the history it would be judged against.
resource "aws_s3_bucket_policy" "dolphin_labs_news" {
  bucket = aws_s3_bucket.dolphin_labs_news.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "TlsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.dolphin_labs_news.arn, "${aws_s3_bucket.dolphin_labs_news.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "JournalIsNotDeletedByHand"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = "${aws_s3_bucket.dolphin_labs_news.arn}/journal/*"
        Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.dolphin_labs_news]
}

resource "aws_s3_bucket_lifecycle_configuration" "dolphin_labs_news" {
  bucket = aws_s3_bucket.dolphin_labs_news.id
  rule {
    id     = "journal"
    status = "Enabled"
    filter {
      prefix = "journal/"
    }
    expiration {
      days = 400
    }
    noncurrent_version_expiration {
      noncurrent_days = 400
    }
  }
  rule {
    id     = "local-journals"
    status = "Enabled"
    filter {
      prefix = "local/"
    }
    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }
  rule {
    id     = "code"
    status = "Enabled"
    filter {
      prefix = "code/"
    }
    expiration {
      days = 30
    }
    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
    expiration {
      expired_object_delete_marker = true
    }
  }
  depends_on = [aws_s3_bucket_versioning.dolphin_labs_news]
}

# -------------------------------------------------------------------------------------
# Secret (Upstash Redis)
# -------------------------------------------------------------------------------------

# Set the value out of band, from a 0600 file, never on a command line:
#   aws secretsmanager put-secret-value --region us-west-1 --secret-id dolphin-labs/news \
#     --secret-string file://news-secret.json      # {"KV_REST_API_URL": "...", "KV_REST_API_TOKEN": "..."}
resource "aws_secretsmanager_secret" "dolphin_labs_news" {
  name                    = "dolphin-labs/news"
  description             = "dolphin-labs news pass: the Upstash Redis it writes (JSON: KV_REST_API_URL, KV_REST_API_TOKEN). Value set out of band; see dolphin-labs-news.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "dolphin-labs/news", Project = "dolphin-labs" }
}

resource "aws_secretsmanager_secret_policy" "dolphin_labs_news" {
  secret_arn = aws_secretsmanager_secret.dolphin_labs_news.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheNewsPassCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.dolphin_labs_news.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.dolphin_labs_news_pass.arn]) } }
    }]
  })
}

# -------------------------------------------------------------------------------------
# Function
# -------------------------------------------------------------------------------------

data "archive_file" "dolphin_labs_news_placeholder" {
  type        = "zip"
  source_file = "${path.module}/dolphin-labs-news/index.mjs"
  output_path = "${path.module}/.terraform/dolphin-labs-news-placeholder.zip"
}

resource "aws_cloudwatch_log_group" "dolphin_labs_news_pass" {
  name              = "/aws/lambda/${local.dolphin_labs_news_name}"
  retention_in_days = 30
}

resource "aws_iam_role" "dolphin_labs_news_pass" {
  name = local.dolphin_labs_news_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = local.dolphin_labs_news_name, Project = "dolphin-labs" }
}

resource "aws_iam_role_policy" "dolphin_labs_news_pass" {
  name = "news-pass"
  role = aws_iam_role.dolphin_labs_news_pass.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(local.bedrock_haiku_statements, [
      {
        Sid      = "WriteTheJournal"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = "${aws_s3_bucket.dolphin_labs_news.arn}/journal/*"
      },
      {
        Sid      = "ReadItsSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = [aws_secretsmanager_secret.dolphin_labs_news.arn, aws_secretsmanager_secret.browserbase.arn]
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.dolphin_labs_news_pass.arn}:*"
      },
    ])
  })
}

resource "aws_lambda_function" "dolphin_labs_news_pass" {
  function_name = local.dolphin_labs_news_name
  description   = "dolphin-labs team news: one pass over every source, Haiku stages, lists to Redis, journal to S3"
  role          = aws_iam_role.dolphin_labs_news_pass.arn
  handler       = "index.handler"
  runtime       = "nodejs22.x"
  architectures = ["arm64"]
  # The pass sets its own deadline from NEWS_PASS_DEADLINE_MS (780 s, 13 minutes) and stops starting model
  # calls 20 s before it; the 900 s limit leaves two minutes after the deadline for the writes.
  timeout     = 900
  memory_size = 2048
  filename    = data.archive_file.dolphin_labs_news_placeholder.output_path
  # One pass at a time. The pass also takes a lock in Redis, so a manual run cannot overlap either.
  reserved_concurrent_executions = 1

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.dolphin_labs_news_pass.name
  }

  environment {
    variables = {
      NEWS_SECRET_ID        = aws_secretsmanager_secret.dolphin_labs_news.name
      BROWSERBASE_SECRET_ID = aws_secretsmanager_secret.browserbase.name
      NEWS_BEDROCK_REGION   = local.bedrock_haiku_region
      NEWS_BEDROCK_MODEL    = local.bedrock_haiku_profile
      NEWS_JOURNAL_BUCKET   = aws_s3_bucket.dolphin_labs_news.bucket
      NEWS_JOURNAL_PREFIX   = "journal/"
      NEWS_PASS_MINUTES     = tostring(local.dolphin_labs_news_minutes)
      NEWS_PASS_DEADLINE_MS = "780000"
      NEWS_ENVIRONMENT      = "production"
      NODE_OPTIONS          = "--enable-source-maps"
    }
  }

  tags = { Name = local.dolphin_labs_news_name, Project = "dolphin-labs" }

  # deploy-news.yml owns the code; Terraform owns everything else, the handler included (the bundle's
  # entry point is index.mjs exporting handler, as the stand-in's is).
  lifecycle {
    ignore_changes = [filename, source_code_hash, s3_bucket, s3_key, s3_object_version]
  }
}

# A failed pass is not retried by Lambda: the next scheduled pass is the retry, and it keeps the last
# good lists in the meantime.
resource "aws_lambda_function_event_invoke_config" "dolphin_labs_news_pass" {
  function_name                = aws_lambda_function.dolphin_labs_news_pass.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

resource "aws_iam_role" "dolphin_labs_news_scheduler" {
  name = "dolphin-labs-news-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
  tags = { Name = "dolphin-labs-news-scheduler", Project = "dolphin-labs" }
}

resource "aws_iam_role_policy" "dolphin_labs_news_scheduler" {
  name = "invoke-dolphin-labs-news-pass"
  role = aws_iam_role.dolphin_labs_news_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.dolphin_labs_news_pass.arn
    }]
  })
}

resource "aws_scheduler_schedule" "dolphin_labs_news_pass" {
  name       = local.dolphin_labs_news_name
  group_name = "default"
  state      = var.dolphin_labs_news_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window { mode = "OFF" }

  schedule_expression          = local.dolphin_labs_news_schedule
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.dolphin_labs_news_pass.arn
    role_arn = aws_iam_role.dolphin_labs_news_scheduler.arn

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 300
    }
  }
}

# Three failed passes in a row is three hours of stale lists: a broken deploy, a missing secret value or
# Bedrock refusing. One failure is a bad hour and the next pass covers it.
resource "aws_cloudwatch_metric_alarm" "dolphin_labs_news_pass_errors" {
  alarm_name          = "dolphin-labs-news-pass-errors"
  alarm_description   = "dolphin-labs-news-pass failed three passes running: team news is not refreshing (the site keeps its last lists). See /aws/lambda/dolphin-labs-news-pass."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.dolphin_labs_news_pass.function_name }
  statistic           = "Sum"
  period              = local.dolphin_labs_news_minutes * 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# Errors only see passes that ran. Once the schedule is on, a schedule or scheduler role that stops firing,
# or passes dropped by the one-at-a-time limit, show as no invocations at all: two periods without one alarm.
resource "aws_cloudwatch_metric_alarm" "dolphin_labs_news_pass_not_running" {
  count               = var.dolphin_labs_news_enabled ? 1 : 0
  alarm_name          = "dolphin-labs-news-pass-not-running"
  alarm_description   = "dolphin-labs-news-pass has not been invoked for two scheduled periods: the schedule, its role or the target is broken. Team news is not refreshing."
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.dolphin_labs_news_pass.function_name }
  statistic           = "Sum"
  period              = local.dolphin_labs_news_minutes * 60
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# -------------------------------------------------------------------------------------
# Deploy (GitHub OIDC, deploy-news.yml on main)
# -------------------------------------------------------------------------------------

# Trusted as dolphin-labs-news-bedrock is (bedrock-haiku.tf): GitHub's immutable sub for main of
# dolphin-labs-hq/dolphin-labs, narrowed by job_workflow_ref to the one workflow file.
resource "aws_iam_role" "dolphin_labs_news_deploy" {
  name                 = "dolphin-labs-news-deploy"
  description          = "Assumed by dolphin-labs-hq/dolphin-labs deploy-news.yml (main) via GitHub OIDC: upload the news-pass bundle and update the function's code"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud"              = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub"              = local.dolphin_labs_main_sub
          "token.actions.githubusercontent.com:job_workflow_ref" = "dolphin-labs-hq/dolphin-labs/.github/workflows/deploy-news.yml@refs/heads/main"
        }
      }
    }]
  })
  tags = { Name = "dolphin-labs-news-deploy", Project = "dolphin-labs" }
}

resource "aws_iam_role_policy" "dolphin_labs_news_deploy" {
  name = "deploy-news-pass"
  role = aws_iam_role.dolphin_labs_news_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "UploadBundles"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
        Resource = "${aws_s3_bucket.dolphin_labs_news.arn}/code/*"
      },
      {
        Sid    = "UpdateTheFunctionsCode"
        Effect = "Allow"
        # Code only, and the reads to wait for the update to finish. No UpdateFunctionConfiguration: IAM
        # cannot narrow it to the handler, and it would also reach the environment, layers, runtime and
        # timeout. Terraform sets the handler (index.handler) that the bundle provides.
        Action   = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:GetFunctionConfiguration"]
        Resource = aws_lambda_function.dolphin_labs_news_pass.arn
      },
    ]
  })
}

# -------------------------------------------------------------------------------------
# Boxes: replay past passes, keep their own local runs
# -------------------------------------------------------------------------------------

resource "aws_iam_role_policy" "box_dolphin_labs_news_journal" {
  for_each = { nextjs-dev = aws_iam_role.nextjs_dev.id }
  name     = "dolphin-labs-news-journal"
  role     = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # GetObjectVersion too: the versions an overwrite kept are part of the history a replay may need.
        Sid      = "ReadJournals"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = ["${aws_s3_bucket.dolphin_labs_news.arn}/journal/*", "${aws_s3_bucket.dolphin_labs_news.arn}/local/*"]
      },
      {
        Sid       = "ListJournals"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"]
        Resource  = aws_s3_bucket.dolphin_labs_news.arn
        Condition = { StringLike = { "s3:prefix" = ["journal/*", "local/*"] } }
      },
      {
        Sid      = "WriteLocalJournals"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = "${aws_s3_bucket.dolphin_labs_news.arn}/local/*"
      },
    ]
  })
}

# Boxes: see the deployed pass and test it by hand before production moves to it. Invoking runs
# one real pass, the same work the hourly job does. The secret's value stays out of reach:
# metadata only, enough to see that a value has been set.
resource "aws_iam_role_policy" "box_dolphin_labs_news_operate" {
  for_each = { nextjs-dev = aws_iam_role.nextjs_dev.id }
  name     = "dolphin-labs-news-operate"
  role     = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InspectAndInvokeThePass"
        Effect   = "Allow"
        Action   = ["lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:InvokeFunction"]
        Resource = aws_lambda_function.dolphin_labs_news_pass.arn
      },
      {
        Sid      = "ReadItsLogs"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogStreams", "logs:GetLogEvents", "logs:FilterLogEvents"]
        Resource = [aws_cloudwatch_log_group.dolphin_labs_news_pass.arn, "${aws_cloudwatch_log_group.dolphin_labs_news_pass.arn}:*"]
      },
      {
        Sid      = "SeeItsSchedule"
        Effect   = "Allow"
        Action   = ["scheduler:GetSchedule"]
        Resource = aws_scheduler_schedule.dolphin_labs_news_pass.arn
      },
      {
        Sid      = "SeeThatTheSecretIsSet"
        Effect   = "Allow"
        Action   = ["secretsmanager:DescribeSecret", "secretsmanager:ListSecretVersionIds"]
        Resource = aws_secretsmanager_secret.dolphin_labs_news.arn
      },
    ]
  })
}

# The hourly GitHub job (refresh-news.yml, role dolphin-labs-news-bedrock in bedrock-haiku.tf) journals
# its passes too, until the Lambda replaces it: write only, under journal/.
resource "aws_iam_role_policy" "dolphin_labs_news_bedrock_journal" {
  name = "write-news-journal"
  role = aws_iam_role.dolphin_labs_news_bedrock.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "WriteTheJournal"
      Effect   = "Allow"
      Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
      Resource = "${aws_s3_bucket.dolphin_labs_news.arn}/journal/*"
    }]
  })
}

output "dolphin_labs_news" {
  description = "For dolphin-labs-hq/dolphin-labs: the deploy workflow's repository variables, and where the journal is"
  value = {
    AWS_REGION                = var.aws_region
    AWS_NEWS_DEPLOY_ROLE_ARN  = aws_iam_role.dolphin_labs_news_deploy.arn
    NEWS_LAMBDA_FUNCTION      = aws_lambda_function.dolphin_labs_news_pass.function_name
    NEWS_BUCKET               = aws_s3_bucket.dolphin_labs_news.bucket
    NEWS_JOURNAL_PREFIX       = "journal/"
    NEWS_LOCAL_JOURNAL_PREFIX = "local/"
    NEWS_SECRET_ID            = aws_secretsmanager_secret.dolphin_labs_news.name
  }
}

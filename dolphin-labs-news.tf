# dolphin-labs' team-news pass, run in AWS: a Lambda on a schedule, with Claude Haiku 5.5 on Bedrock,
# a journal of every pass in S3 for backtesting, and its two secrets in Secrets Manager.
# It replaces the GitHub Actions job (dolphin-labs-hq/dolphin-labs refresh-news.yml); the GitHub OIDC
# role dolphin-labs-news-bedrock (bedrock-haiku.tf) stays until that job is retired.
#
#   dolphin-labs-news-pass        the function. Terraform creates it with a stand-in handler
#                                 (dolphin-labs-news/placeholder.mjs) and then leaves its code alone:
#                                 dolphin-labs-hq/dolphin-labs deploy-news.yml (main) uploads a bundle to
#                                 code/ and calls UpdateFunctionCode through dolphin-labs-news-deploy.
#   dolphin-labs-news-<account>   the bucket:
#                                   journal/  one compressed record per pass (its parsed inputs, the
#                                             schedule, each Haiku prompt without page text and its
#                                             answer, score components, outputs, the commit). Kept
#                                             400 days, Infrequent Access after 30, so a season and the
#                                             one before can be replayed against changed code.
#                                   local/    journals of passes run on a dev box. 30 days.
#                                   code/     deployed bundles. Lambda copies the code at update time,
#                                             so a bundle is kept 30 days only for a redeploy.
#   dolphin-labs/news             JSON secret: KV_REST_API_URL and KV_REST_API_TOKEN, the Upstash Redis
#                                 the site reads the lists from. Value set out of band.
#   browserbase/credentials       existing (dev-ai-services.tf); the function joins its readers.
#
# The schedule starts DISABLED. Turn on dolphin_labs_news_enabled after the first deploy and after the
# secret has a value; a run before that fails on the stand-in, which the errors alarm reports.
#
# Who reaches what:
#   the function            Bedrock Haiku 5.5; journal/ (write); the two secrets (read); its own logs
#   deploy-news.yml (main)  code/ (write); UpdateFunctionCode and GetFunction on this function only
#   nextjs-dev, dev-ebs-only (the dolphin box and the parallel boxes, where replays run)
#                           journal/ and local/ (read); local/ (write)

variable "dolphin_labs_news_enabled" {
  description = "Run dolphin-labs' news pass on its schedule. false keeps the function and its data and stops the runs."
  type        = bool
  default     = false
}

variable "dolphin_labs_news_schedule" {
  description = "When the news pass runs (EventBridge Scheduler expression, UTC). The function gets NEWS_PASS_MINUTES from local.dolphin_labs_news_minutes; change the two together."
  type        = string
  default     = "cron(37 * * * ? *)"
}

locals {
  dolphin_labs_news_name    = "dolphin-labs-news-pass"
  dolphin_labs_news_bucket  = "dolphin-labs-news-${data.aws_caller_identity.current.account_id}"
  dolphin_labs_news_minutes = 60
  dolphin_labs_news_sub     = "repo:dolphin-labs-hq@316189183/dolphin-labs@1330391842:ref:refs/heads/main"
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

resource "aws_s3_bucket_server_side_encryption_configuration" "dolphin_labs_news" {
  bucket = aws_s3_bucket.dolphin_labs_news.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# A journal record is written once under a key naming its pass; nothing rewrites one. No principal but
# the administration set may delete from journal/, so a bad deploy cannot erase the history it would be
# judged against.
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
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }
    expiration {
      days = 400
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
  }
  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
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
  source_file = "${path.module}/dolphin-labs-news/placeholder.mjs"
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
  handler       = "placeholder.handler"
  runtime       = "nodejs22.x"
  architectures = ["arm64"]
  # The pass sets its own deadline from NEWS_PASS_DEADLINE_MS and stops starting model calls 20 s before
  # it; the function's limit leaves a minute after that for the writes.
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

  # deploy-news.yml owns the code and the handler it names; Terraform owns everything else.
  lifecycle {
    ignore_changes = [filename, source_code_hash, s3_bucket, s3_key, s3_object_version, handler]
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

  schedule_expression          = var.dolphin_labs_news_schedule
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
          "token.actions.githubusercontent.com:sub"              = local.dolphin_labs_news_sub
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
        # GetFunction to wait for the update to finish; UpdateFunctionConfiguration only to name the
        # bundle's handler. Not its role, environment or permissions: those are Terraform's.
        Action   = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionConfiguration"]
        Resource = aws_lambda_function.dolphin_labs_news_pass.arn
      },
    ]
  })
}

# -------------------------------------------------------------------------------------
# Boxes: replay past passes, keep their own local runs
# -------------------------------------------------------------------------------------

resource "aws_iam_role_policy" "box_dolphin_labs_news_journal" {
  for_each = { nextjs-dev = aws_iam_role.nextjs_dev.id, dev-ebs-only = aws_iam_role.dev_ebs_only.id }
  name     = "dolphin-labs-news-journal"
  role     = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadJournals"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = ["${aws_s3_bucket.dolphin_labs_news.arn}/journal/*", "${aws_s3_bucket.dolphin_labs_news.arn}/local/*"]
      },
      {
        Sid       = "ListJournals"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
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

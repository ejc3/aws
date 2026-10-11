# dolphin-maps' scheduled loops, run in AWS: one Lambda on two EventBridge schedules, a journal of every pass
# in S3, and a GitHub App for its commits. It replaces the GitHub Actions loops in dolphin-labs-hq/dolphin-maps
# (scrape-live.yml, scrape-weekly.yml and the dev box's crontab trigger), which stand down when that repository's
# variable LOOPS_RUN_IN_AWS is true.
#
#   dolphin-maps-loop             the function. Terraform creates it with a stand-in handler
#                                 (dolphin-maps-loop/placeholder.mjs) and then leaves its code alone:
#                                 dolphin-maps deploy-loop.yml (main) uploads a bundle to code/ and calls
#                                 UpdateFunctionCode through dolphin-maps-loop-deploy.
#   dolphin-maps-loop (live)      every 5 minutes, 06:00-08:55 America/Los_Angeles, weekdays: {"mode": "live"}.
#                                 The function samples only the routes inside their own morning window; the
#                                 rest of the invocations return at once without reading a secret.
#   dolphin-maps-loop (sweep)     Sunday 23:00 America/Los_Angeles: {"mode": "sweep"}, the coming school week's
#                                 "typically" ranges, then each area's calibration refit.
#   dolphin-maps-loop-<account>   the bucket:
#                                   journal/  one gzipped record per pass: each page's text and the rows read
#                                             from it, and the bundle's sha, so a page Google changed can be
#                                             replayed through fixed code. Kept 400 days, Infrequent Access
#                                             after 30.
#                                   code/     deployed bundles, 30 days (Lambda copies the code at update time).
#   github-app/dolphin-maps-loop  JSON: app_id, slug, private_key, installations {owner: id}. The loop's GitHub
#                                 App: contents read and write, installed on dolphin-labs-hq for
#                                 dolphin-maps only. Each pass mints an installation token narrowed to that
#                                 repository and contents:write, which expires within the hour. Value stored
#                                 from GitHub's app-manifest conversion, never printed.
#   browserbase/credentials       existing (dev-ai-services.tf); the function joins its readers.
#
# Both schedules start DISABLED. Turn on dolphin_maps_loop_enabled after the first deploy and after the app's
# secret has a value; a run before that fails on the stand-in, which the alarms report.
#
# Who reaches what:
#   the function               journal/ (write); the two secrets (read); its own logs
#   deploy-loop.yml (main)     code/ (write); UpdateFunctionCode and GetFunction on this function only
#   dev-server-role            journal/ (read, list): replays on the metal boxes

variable "dolphin_maps_loop_enabled" {
  description = "Run dolphin-maps' scheduled loops (live samples, the weekly sweep). false keeps the function and its data and stops the runs."
  type        = bool
  default     = false
}

locals {
  dolphin_maps_loop_name   = "dolphin-maps-loop"
  dolphin_maps_loop_bucket = "dolphin-maps-loop-${data.aws_caller_identity.current.account_id}"
  dolphin_maps_loop_sub    = "repo:dolphin-labs-hq@316189183/dolphin-maps@1409029367:ref:refs/heads/main"
  dolphin_maps_loop_schedules = {
    live  = "cron(*/5 6-8 ? * MON-FRI *)"
    sweep = "cron(0 23 ? * SUN *)"
  }
}

# -------------------------------------------------------------------------------------
# Bucket
# -------------------------------------------------------------------------------------

resource "aws_s3_bucket" "dolphin_maps_loop" {
  bucket = local.dolphin_maps_loop_bucket
  tags   = { Name = "dolphin-maps-loop", Project = "dolphin-maps" }
}

resource "aws_s3_bucket_public_access_block" "dolphin_maps_loop" {
  bucket                  = aws_s3_bucket.dolphin_maps_loop.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "dolphin_maps_loop" {
  bucket = aws_s3_bucket.dolphin_maps_loop.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "dolphin_maps_loop" {
  bucket = aws_s3_bucket.dolphin_maps_loop.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# A journal record is written once under a key naming its pass; nothing rewrites one. No principal but the
# administration set may delete from journal/, so a bad deploy cannot erase the record of what was read.
resource "aws_s3_bucket_policy" "dolphin_maps_loop" {
  bucket = aws_s3_bucket.dolphin_maps_loop.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "TlsOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.dolphin_maps_loop.arn, "${aws_s3_bucket.dolphin_maps_loop.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "JournalIsNotDeletedByHand"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = "${aws_s3_bucket.dolphin_maps_loop.arn}/journal/*"
        Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.dolphin_maps_loop]
}

resource "aws_s3_bucket_lifecycle_configuration" "dolphin_maps_loop" {
  bucket = aws_s3_bucket.dolphin_maps_loop.id
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
# The loop's GitHub App
# -------------------------------------------------------------------------------------

# Created by the administration agent from a manifest (README.md, the app-manifest flow): name
# dolphin-maps-loop, no webhook, repository permissions contents: write (metadata: read comes with it),
# installed on dolphin-labs-hq with only dolphin-maps selected. Store the conversion's JSON plus
# installations {"dolphin-labs-hq": <id>} from stdin or a 0600 file, never on a command line.
resource "aws_secretsmanager_secret" "dolphin_maps_loop_github_app" {
  name                    = "github-app/dolphin-maps-loop"
  description             = "dolphin-maps' loop GitHub App (JSON: app id, private key, installations): commits the loops' data. Function and jumpbox role only. See dolphin-maps-loop.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "github-app/dolphin-maps-loop", Project = "dolphin-maps" }
}

resource "aws_secretsmanager_secret_policy" "dolphin_maps_loop_github_app" {
  secret_arn = aws_secretsmanager_secret.dolphin_maps_loop_github_app.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTheLoopAndTheJumpboxRoleCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.dolphin_maps_loop_github_app.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat([aws_iam_role.dolphin_maps_loop.arn], local.github_admin_token_readers) } }
    }]
  })
}

# -------------------------------------------------------------------------------------
# Function
# -------------------------------------------------------------------------------------

data "archive_file" "dolphin_maps_loop_placeholder" {
  type        = "zip"
  source_file = "${path.module}/dolphin-maps-loop/placeholder.mjs"
  output_path = "${path.module}/.terraform/dolphin-maps-loop-placeholder.zip"
}

resource "aws_cloudwatch_log_group" "dolphin_maps_loop" {
  name              = "/aws/lambda/${local.dolphin_maps_loop_name}"
  retention_in_days = 30
}

resource "aws_iam_role" "dolphin_maps_loop" {
  name = local.dolphin_maps_loop_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = local.dolphin_maps_loop_name, Project = "dolphin-maps" }
}

resource "aws_iam_role_policy" "dolphin_maps_loop" {
  name = "loop"
  role = aws_iam_role.dolphin_maps_loop.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "WriteTheJournal"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = "${aws_s3_bucket.dolphin_maps_loop.arn}/journal/*"
      },
      {
        Sid      = "ReadItsSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = [aws_secretsmanager_secret.dolphin_maps_loop_github_app.arn, aws_secretsmanager_secret.browserbase.arn]
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.dolphin_maps_loop.arn}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "dolphin_maps_loop" {
  function_name = local.dolphin_maps_loop_name
  description   = "dolphin-maps loops: live Google Maps samples on school mornings, the weekly sweep and refit; commits to the repository, journal to S3"
  role          = aws_iam_role.dolphin_maps_loop.arn
  handler       = "placeholder.handler"
  runtime       = "nodejs22.x"
  architectures = ["arm64"]
  # A sweep (about two minutes of pages) and the refit (about two minutes of CPU on a dev box) fit well
  # inside the limit; memory is set for the refit's CPU share, not its footprint (about 250 MB).
  timeout     = 900
  memory_size = 2048
  filename    = data.archive_file.dolphin_maps_loop_placeholder.output_path
  # One pass at a time: two passes' commits would only retry against each other, but a sweep must never
  # run beside itself.
  reserved_concurrent_executions = 1

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.dolphin_maps_loop.name
  }

  environment {
    variables = {
      BROWSERBASE_SECRET_ID = aws_secretsmanager_secret.browserbase.name
      GITHUB_APP_SECRET_ID  = aws_secretsmanager_secret.dolphin_maps_loop_github_app.name
      LOOP_REPO             = "dolphin-labs-hq/dolphin-maps"
      LOOP_BRANCH           = "main"
      LOOP_JOURNAL_BUCKET   = aws_s3_bucket.dolphin_maps_loop.bucket
      LOOP_JOURNAL_PREFIX   = "journal/"
    }
  }

  tags = { Name = local.dolphin_maps_loop_name, Project = "dolphin-maps" }

  # deploy-loop.yml owns the code and the handler it names; Terraform owns everything else.
  lifecycle {
    ignore_changes = [filename, source_code_hash, s3_bucket, s3_key, s3_object_version, handler]
  }
}

# A failed pass is not retried by Lambda: the next live pass is five minutes away, and a failed sweep is
# better rerun by hand after a look at its journal.
resource "aws_lambda_function_event_invoke_config" "dolphin_maps_loop" {
  function_name                = aws_lambda_function.dolphin_maps_loop.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

resource "aws_iam_role" "dolphin_maps_loop_scheduler" {
  name = "dolphin-maps-loop-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
  tags = { Name = "dolphin-maps-loop-scheduler", Project = "dolphin-maps" }
}

resource "aws_iam_role_policy" "dolphin_maps_loop_scheduler" {
  name = "invoke-dolphin-maps-loop"
  role = aws_iam_role.dolphin_maps_loop_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.dolphin_maps_loop.arn
    }]
  })
}

# In Los Angeles time, as the routes' arrive-by times are: no daylight-saving arithmetic anywhere.
resource "aws_scheduler_schedule" "dolphin_maps_loop" {
  for_each   = local.dolphin_maps_loop_schedules
  name       = "${local.dolphin_maps_loop_name}-${each.key}"
  group_name = "default"
  state      = var.dolphin_maps_loop_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window { mode = "OFF" }

  schedule_expression          = each.value
  schedule_expression_timezone = "America/Los_Angeles"

  target {
    arn      = aws_lambda_function.dolphin_maps_loop.arn
    role_arn = aws_iam_role.dolphin_maps_loop_scheduler.arn
    input    = jsonencode({ mode = each.key })

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 300
    }
  }
}

# Live: three failed passes running (15 minutes of a school morning with no samples) is a broken deploy, a
# missing secret or a changed page; one failure is a bad page load and the next pass covers it.
resource "aws_cloudwatch_metric_alarm" "dolphin_maps_loop_errors" {
  alarm_name          = "dolphin-maps-loop-errors"
  alarm_description   = "dolphin-maps-loop failed three passes running: the morning's live samples are not being kept. See /aws/lambda/dolphin-maps-loop and the journal in the dolphin-maps-loop bucket."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.dolphin_maps_loop.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# Sweep: it runs once a week, so one failure is a week without new ranges or a refit. The function logs
# "loop sweep failed: <why>" before it throws.
resource "aws_cloudwatch_log_metric_filter" "dolphin_maps_loop_sweep_failed" {
  name           = "dolphin-maps-loop-sweep-failed"
  log_group_name = aws_cloudwatch_log_group.dolphin_maps_loop.name
  pattern        = "\"loop sweep failed\""
  metric_transformation {
    name      = "SweepFailures"
    namespace = "DolphinMapsLoop"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "dolphin_maps_loop_sweep_failed" {
  alarm_name          = "dolphin-maps-loop-sweep-failed"
  alarm_description   = "dolphin-maps' weekly sweep failed: no new typical ranges or refit this week. See /aws/lambda/dolphin-maps-loop and journal/sweep/ in the dolphin-maps-loop bucket; rerun with {\"mode\": \"sweep\"}."
  namespace           = "DolphinMapsLoop"
  metric_name         = "SweepFailures"
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

# -------------------------------------------------------------------------------------
# Deploy (GitHub OIDC, deploy-loop.yml on main)
# -------------------------------------------------------------------------------------

# GitHub's immutable sub for main of dolphin-labs-hq/dolphin-maps, narrowed by job_workflow_ref to the one
# workflow file, as dolphin-labs-news-deploy is.
resource "aws_iam_role" "dolphin_maps_loop_deploy" {
  name                 = "dolphin-maps-loop-deploy"
  description          = "Assumed by dolphin-labs-hq/dolphin-maps deploy-loop.yml (main) via GitHub OIDC: upload the loop bundle and update the function's code"
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
          "token.actions.githubusercontent.com:sub"              = local.dolphin_maps_loop_sub
          "token.actions.githubusercontent.com:job_workflow_ref" = "dolphin-labs-hq/dolphin-maps/.github/workflows/deploy-loop.yml@refs/heads/main"
        }
      }
    }]
  })
  tags = { Name = "dolphin-maps-loop-deploy", Project = "dolphin-maps" }
}

resource "aws_iam_role_policy" "dolphin_maps_loop_deploy" {
  name = "deploy-loop"
  role = aws_iam_role.dolphin_maps_loop_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "UploadBundles"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject", "s3:AbortMultipartUpload"]
        Resource = "${aws_s3_bucket.dolphin_maps_loop.arn}/code/*"
      },
      {
        Sid    = "UpdateTheFunctionsCode"
        Effect = "Allow"
        # GetFunction to wait for the update to finish; UpdateFunctionConfiguration only to name the bundle's
        # handler. Not its role, environment or permissions: those are Terraform's.
        Action   = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionConfiguration"]
        Resource = aws_lambda_function.dolphin_maps_loop.arn
      },
    ]
  })
}

# -------------------------------------------------------------------------------------
# The metal boxes: read the journal to replay a pass
# -------------------------------------------------------------------------------------

resource "aws_iam_role_policy" "dev_server_dolphin_maps_loop_journal" {
  name = "dolphin-maps-loop-journal"
  role = aws_iam_role.dev_server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadJournals"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.dolphin_maps_loop.arn}/journal/*"
      },
      {
        Sid       = "ListJournals"
        Effect    = "Allow"
        Action    = "s3:ListBucket"
        Resource  = aws_s3_bucket.dolphin_maps_loop.arn
        Condition = { StringLike = { "s3:prefix" = ["journal/*"] } }
      },
    ]
  })
}

output "dolphin_maps_loop" {
  description = "For dolphin-labs-hq/dolphin-maps: the deploy workflow's repository variables, and where the journal and the app's secret are"
  value = {
    AWS_REGION               = var.aws_region
    AWS_LOOP_DEPLOY_ROLE_ARN = aws_iam_role.dolphin_maps_loop_deploy.arn
    LOOP_LAMBDA_FUNCTION     = aws_lambda_function.dolphin_maps_loop.function_name
    LOOP_BUCKET              = aws_s3_bucket.dolphin_maps_loop.bucket
    LOOP_JOURNAL_PREFIX      = "journal/"
    LOOP_GITHUB_APP_SECRET   = aws_secretsmanager_secret.dolphin_maps_loop_github_app.name
  }
}

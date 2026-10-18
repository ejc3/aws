# dolphin-maps' scheduled loops, run in AWS: two Lambdas from one bundle on two EventBridge schedules, a journal of every pass
# in S3, and a GitHub App for its commits. It replaces the GitHub Actions loops in dolphin-labs-hq/dolphin-maps
# (scrape-live.yml, scrape-weekly.yml and the dev box's crontab trigger), which stand down when that repository's
# variable LOOPS_RUN_IN_AWS is true.
#
#   dolphin-maps-loop-live        every 5 minutes, 06:00-08:55 America/Los_Angeles, weekdays: {"mode": "live"}.
#                                 It samples only the routes inside their own morning window; the rest of the
#                                 invocations return at once without reading a secret.
#   dolphin-maps-loop-sweep       Sunday 23:00 America/Los_Angeles: {"mode": "sweep"}, the coming school week's
#                                 "typically" ranges, then each area's calibration refit.
#                                 One bundle, two functions: each loop has its own limits, and Lambda's own Errors
#                                 metric (timeouts, running out of memory and failed starts included) is that
#                                 loop's alone, so a single failed sweep alarms. Terraform creates both with a
#                                 stand-in handler (dolphin-maps-loop/index.mjs, index.handler like the bundle) and then leaves their code
#                                 alone: dolphin-maps deploy-loop.yml (main) uploads the bundle to code/ and calls
#                                 UpdateFunctionCode on each through dolphin-maps-loop-deploy.
#   dolphin-maps-loop-<account>   the bucket:
#                                   journal/  one gzipped record per pass: each page's text and the rows read
#                                             from it, and the bundle's sha, so a page Google changed can be
#                                             replayed through fixed code. Kept 400 days. The bucket is
#                                             versioned, so an overwrite keeps the earlier record, and only
#                                             administration may delete a record or a version.
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
#   the two functions          journal/ (write); the two secrets (read); their own logs
#   deploy-loop.yml (main)     code/ (write); UpdateFunctionCode, GetFunction and GetFunctionConfiguration on these two
#                              functions only (no UpdateFunctionConfiguration: the handler is Terraform's)
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
  # A live pass is a page or two (minutes at most); a sweep is about six minutes of pages and two of refit,
  # whose memory is set for its CPU share, not its footprint (about 250 MB).
  dolphin_maps_loop_modes = {
    live  = { schedule = "cron(*/5 6-8 ? * MON-FRI *)", timeout = 300, memory = 1024 }
    sweep = { schedule = "cron(0 23 ? * SUN *)", timeout = 900, memory = 2048 }
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

resource "aws_s3_bucket_versioning" "dolphin_maps_loop" {
  bucket = aws_s3_bucket.dolphin_maps_loop.id
  versioning_configuration {
    status = "Enabled"
  }
}

# A journal record is written once under a key naming its pass. The bucket is versioned, so even a bad deploy that
# rewrote a key would leave the earlier record as a noncurrent version, and no principal but the administration set
# may delete from journal/ (a key or a version): the record of what was read cannot be erased from the loop's side.
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
    expiration {
      days = 400
    }
    noncurrent_version_expiration {
      noncurrent_days = 400
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
  }
  depends_on = [aws_s3_bucket_versioning.dolphin_maps_loop]
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
  source_file = "${path.module}/dolphin-maps-loop/index.mjs"
  output_path = "${path.module}/.terraform/dolphin-maps-loop-placeholder.zip"
}

resource "aws_cloudwatch_log_group" "dolphin_maps_loop" {
  for_each          = local.dolphin_maps_loop_modes
  name              = "/aws/lambda/${local.dolphin_maps_loop_name}-${each.key}"
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
        Resource = [aws_secretsmanager_secret.dolphin_maps_loop_github_app.arn, aws_secretsmanager_secret.browserbase.arn, aws_secretsmanager_secret.deepseek.arn, aws_secretsmanager_secret.exa.arn]
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [for g in aws_cloudwatch_log_group.dolphin_maps_loop : "${g.arn}:*"]
      },
    ]
  })
}

resource "aws_lambda_function" "dolphin_maps_loop" {
  for_each      = local.dolphin_maps_loop_modes
  function_name = "${local.dolphin_maps_loop_name}-${each.key}"
  description   = each.key == "live" ? "dolphin-maps live loop: Google Maps samples on school mornings, committed to the repository, journal to S3" : "dolphin-maps weekly loop: the school week's typical ranges and the calibration refit, committed to the repository, journal to S3"
  role          = aws_iam_role.dolphin_maps_loop.arn
  # The bundle deploy-loop.yml uploads exports index.handler too, so the handler never changes and the deploy
  # role needs no UpdateFunctionConfiguration (which IAM cannot narrow to the handler).
  handler       = "index.handler"
  runtime       = "nodejs22.x"
  architectures = ["arm64"]
  timeout       = each.value.timeout
  memory_size   = each.value.memory
  filename      = data.archive_file.dolphin_maps_loop_placeholder.output_path
  # One pass of each loop at a time, so a sweep never runs beside itself. A live pass and a sweep (a sweep rerun by
  # hand on a school morning) can overlap: both commit to main, and the later commit retries against the earlier.
  reserved_concurrent_executions = 1

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.dolphin_maps_loop[each.key].name
  }

  environment {
    variables = {
      BROWSERBASE_SECRET_ID = aws_secretsmanager_secret.browserbase.name
      DEEPSEEK_SECRET_ID    = aws_secretsmanager_secret.deepseek.name
      EXA_SECRET_ID         = aws_secretsmanager_secret.exa.name
      GITHUB_APP_SECRET_ID  = aws_secretsmanager_secret.dolphin_maps_loop_github_app.name
      LOOP_REPO             = "dolphin-labs-hq/dolphin-maps"
      LOOP_BRANCH           = "main"
      LOOP_JOURNAL_BUCKET   = aws_s3_bucket.dolphin_maps_loop.bucket
      LOOP_JOURNAL_PREFIX   = "journal/"
    }
  }

  tags = { Name = "${local.dolphin_maps_loop_name}-${each.key}", Project = "dolphin-maps" }

  # deploy-loop.yml owns the code; Terraform owns everything else, the handler included.
  lifecycle {
    ignore_changes = [filename, source_code_hash, s3_bucket, s3_key, s3_object_version]
  }
}

# A failed pass is not retried by Lambda: the next live pass is five minutes away, and a failed sweep is
# better rerun by hand after a look at its journal.
resource "aws_lambda_function_event_invoke_config" "dolphin_maps_loop" {
  for_each                     = aws_lambda_function.dolphin_maps_loop
  function_name                = each.value.function_name
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
      Resource = [for f in aws_lambda_function.dolphin_maps_loop : f.arn]
    }]
  })
}

# In Los Angeles time, as the routes' arrive-by times are: no daylight-saving arithmetic anywhere.
resource "aws_scheduler_schedule" "dolphin_maps_loop" {
  for_each   = local.dolphin_maps_loop_modes
  name       = "${local.dolphin_maps_loop_name}-${each.key}"
  group_name = "default"
  state      = var.dolphin_maps_loop_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window { mode = "OFF" }

  schedule_expression          = each.value.schedule
  schedule_expression_timezone = "America/Los_Angeles"

  target {
    arn      = aws_lambda_function.dolphin_maps_loop[each.key].arn
    role_arn = aws_iam_role.dolphin_maps_loop_scheduler.arn
    input    = jsonencode({ mode = each.key })

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 300
    }
  }
}

# Live: three failed passes within half an hour of a school morning is a broken deploy, a missing secret or a
# changed page; one failure is a bad page load and the next pass covers it. Three of six periods, not three in a
# row, so a scheduler-jittered pass landing in a neighbouring period cannot reset the count.
resource "aws_cloudwatch_metric_alarm" "dolphin_maps_loop_errors" {
  alarm_name          = "dolphin-maps-loop-errors"
  alarm_description   = "dolphin-maps-loop-live failed three passes within half an hour: the morning's live samples are not being kept. See /aws/lambda/dolphin-maps-loop-live and journal/live/ in the dolphin-maps-loop bucket."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.dolphin_maps_loop["live"].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# Sweep: it runs once a week, so one failure is a week without new ranges or a refit. Lambda's Errors metric for
# the sweep's own function counts every failure: a thrown error, a timeout, running out of memory, a failed start.
resource "aws_cloudwatch_metric_alarm" "dolphin_maps_loop_sweep_failed" {
  alarm_name          = "dolphin-maps-loop-sweep-failed"
  alarm_description   = "dolphin-maps' weekly sweep failed: no new typical ranges or refit this week. See /aws/lambda/dolphin-maps-loop-sweep and journal/sweep/ in the dolphin-maps-loop bucket; rerun it with {\"mode\": \"sweep\"}."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.dolphin_maps_loop["sweep"].function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

# A pass that never ran is not an error: with one run at a time, a live pass that takes past the next five
# minutes throttles the one after it, and a throttled event older than 300 s is dropped without running. Lambda
# counts those in AsyncEventsDropped, not Errors, so each function gets its own alarm on them.
resource "aws_cloudwatch_metric_alarm" "dolphin_maps_loop_dropped" {
  for_each            = local.dolphin_maps_loop_modes
  alarm_name          = "dolphin-maps-loop-${each.key}-dropped"
  alarm_description   = "dolphin-maps-loop-${each.key}: scheduled passes were dropped without running (a pass still running when the next was due, past the 300 s event age). See /aws/lambda/dolphin-maps-loop-${each.key}."
  namespace           = "AWS/Lambda"
  metric_name         = "AsyncEventsDropped"
  dimensions          = { FunctionName = aws_lambda_function.dolphin_maps_loop[each.key].function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

# And a loop that stops running at all (a schedule disabled or broken, the scheduler role changed) produces no
# Errors and no drops. Only while the schedules are on: the live loop is invoked every weekday morning, so four
# days without an invocation spans any weekend; the sweep runs once a week, so seven daily periods always hold
# one run. Missing data is a breach: no invocations publish no datapoint.
resource "aws_cloudwatch_metric_alarm" "dolphin_maps_loop_not_running" {
  for_each            = var.dolphin_maps_loop_enabled ? { live = 4, sweep = 7 } : {}
  alarm_name          = "dolphin-maps-loop-${each.key}-not-running"
  alarm_description   = "dolphin-maps-loop-${each.key} has not been invoked for ${each.value} days although its schedule is on. Check the scheduler schedule dolphin-maps-loop-${each.key} and the dolphin-maps-loop-scheduler role."
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.dolphin_maps_loop[each.key].function_name }
  statistic           = "Sum"
  period              = 86400
  evaluation_periods  = each.value
  datapoints_to_alarm = each.value
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# -------------------------------------------------------------------------------------
# Deploy (GitHub OIDC, deploy-loop.yml on main)
# -------------------------------------------------------------------------------------

# GitHub's immutable sub for main of dolphin-labs-hq/dolphin-maps, narrowed by job_workflow_ref to the one
# workflow file.
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
        # GetFunction and GetFunctionConfiguration to wait for the update and check the handler. No
        # UpdateFunctionConfiguration: it cannot be narrowed to the handler (it would also reach the environment,
        # layers and runtime), and the handler is Terraform's (index.handler, the same in the stand-in and the bundle).
        Action   = ["lambda:UpdateFunctionCode", "lambda:GetFunction", "lambda:GetFunctionConfiguration"]
        Resource = [for f in aws_lambda_function.dolphin_maps_loop : f.arn]
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
        Sid    = "ReadJournals"
        Effect = "Allow"
        # Versions too: an overwritten record survives only as a noncurrent version.
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${aws_s3_bucket.dolphin_maps_loop.arn}/journal/*"
      },
      {
        Sid       = "ListJournals"
        Effect    = "Allow"
        Action    = ["s3:ListBucket", "s3:ListBucketVersions"]
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
    LOOP_LAMBDA_FUNCTIONS    = join(" ", [for f in aws_lambda_function.dolphin_maps_loop : f.function_name])
    LOOP_BUCKET              = aws_s3_bucket.dolphin_maps_loop.bucket
    LOOP_JOURNAL_PREFIX      = "journal/"
    LOOP_GITHUB_APP_SECRET   = aws_secretsmanager_secret.dolphin_maps_loop_github_app.name
  }
}

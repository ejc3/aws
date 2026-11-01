# dolphin-maps' scheduled loops, run in AWS: four Lambdas from one bundle on their EventBridge schedules, a journal of every pass
# in S3, and a GitHub App for its commits. It replaces the GitHub Actions loops in dolphin-labs-hq/dolphin-maps
# (scrape-live.yml, scrape-weekly.yml, refresh-games.yml and the dev box's crontab trigger), which stand down when that repository's
# variable LOOPS_RUN_IN_AWS is true.
#
#   dolphin-maps-loop-live        every 5 minutes, 06:00-08:55 America/Los_Angeles, weekdays: {"mode": "live"}.
#                                 It samples only the routes inside their own morning window; the rest of the
#                                 invocations return at once without reading a secret.
#   dolphin-maps-loop-sweep       Sunday 23:00 America/Los_Angeles: {"mode": "sweep"}, the coming school week's
#                                 "typically" ranges.
#   dolphin-maps-loop-refit       Monday 00:00 America/Los_Angeles, an hour after the sweep: {"mode": "refit"}, each
#                                 area's calibration refit on the ranges main holds. No browser. Its own function
#                                 because the refit's routing guard runs gyms for most of the 15 minutes (it starts
#                                 none after the 12th), which the sweep's pages leave no room for (dolphin-maps #198).
#                                 A schedule, not an invocation by the sweep: the sweep's role needs no
#                                 lambda:InvokeFunction, and a refit can be rerun by hand.
#   dolphin-maps-loop-games       daily 05:15 America/Los_Angeles: {"mode": "games"}, each Bay Area arena's home games
#                                 for the next 30 days from Ball Don't Lie (one call per league the key's plan covers,
#                                 four, 20 s apart), committed as data/events/games.csv. No browser. Its key is
#                                 balldontlie/api-key (dolphin-maps-balldontlie.tf), named by BALLDONTLIE_SECRET_ID.
#                                 The GitHub Actions copy (refresh-games.yml) runs at 12:15 UTC; this is the same hour
#                                 in winter and an hour earlier in summer, before anyone plans the day's drive.
#                                 One bundle, one function a loop: each loop has its own limits, and Lambda's own Errors
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
#   balldontlie/api-key           existing (dolphin-maps-balldontlie.tf), JSON: BALLDONTLIE_API_KEY; the function joins
#                                 its readers for the games pass.
#   dolphin-maps/pems             JSON: PEMS_USERNAME, PEMS_PASSWORD. The PeMS login (Caltrans' traffic data
#                                 service) for the scheduled PeMS refresh. Value set out of band, never in git. The
#                                 function and the administration set read it; dev-server-role reads and puts it.
#
# Eight more schedules invoke the sweep function with the week and weekend modes (dolphin-maps
# docs/design/weekday-traffic.md): they read the days of the week the sweep does not, into observations/gmaps_week.csv,
# which the fit does not read. Seven monthly ones, {"mode": "week", "weekday": "mon"..."sun"}, one per day of the week
# (one day is 18 jobs, one wave of Browserbase's 25 browsers; the whole week would be seven waves, past a Lambda's 15
# minutes), on the first Saturday of the month from 20:00, 20 minutes apart; and {"mode": "weekend"} (the trips'
# Saturday and Sunday) on Thursday 23:00. They all run in the sweep function (its role, its one-at-a-time
# concurrency and its failure alarm), none in a weekday morning (the live loop's browsers), and none overlaps another
# or the Sunday-night sweep: a pass takes minutes against the 20 minutes between them.
#
# All schedules start DISABLED. Turn on dolphin_maps_loop_enabled after the first deploy and after the app's
# secret has a value; a run before that fails on the stand-in, which the alarms report.
#
# Who reaches what:
#   the four functions         journal/ (write); the secrets above (read); their own logs
#   deploy-loop.yml (main)     code/ (write); UpdateFunctionCode, GetFunction and GetFunctionConfiguration on these three
#                              functions only (no UpdateFunctionConfiguration: the handler is Terraform's)
#
# A new function changes the output's LOOP_LAMBDA_FUNCTIONS: set dolphin-maps' repository variable of that name to it
# after the apply, so deploy-loop.yml ships the bundle to the games function too.
#   dev-server-role            journal/ (read, list): replays on the metal boxes; dolphin-maps/pems (read, put)

variable "dolphin_maps_loop_enabled" {
  description = "Run dolphin-maps' scheduled loops (live samples, the weekly sweep and refit, the monthly week passes, the weekly weekend pass and the daily games). false keeps the function and its data and stops the runs."
  type        = bool
  default     = false
}

locals {
  dolphin_maps_loop_name   = "dolphin-maps-loop"
  dolphin_maps_loop_bucket = "dolphin-maps-loop-${data.aws_caller_identity.current.account_id}"
  dolphin_maps_loop_sub    = "repo:dolphin-labs-hq@316189183/dolphin-maps@1409029367:ref:refs/heads/main"
  # A live pass is a page or two (minutes at most); a sweep is about six minutes of pages; a refit is the routing
  # guard's gyms (8:43 on a dev box, not yet measured here): it starts no gym after the 12th minute and keeps the
  # committed calibration for a stage it has not checked. Memory is set for the CPU share, not the footprint
  # (about 250 MB). A games pass is four provider calls 20 s apart, a minute or two (more when the shared key's
  # window is spent and it waits for the reset, about a minute each time).
  dolphin_maps_loop_modes = {
    live  = { schedule = "cron(*/5 6-8 ? * MON-FRI *)", timeout = 300, memory = 1024 }
    sweep = { schedule = "cron(0 23 ? * SUN *)", timeout = 900, memory = 2048 }
    refit = { schedule = "cron(0 0 ? * MON *)", timeout = 900, memory = 2048 }
    games = { schedule = "cron(15 5 * * ? *)", timeout = 600, memory = 512 }
  }
  # The week and weekend passes: the sweep function, another event. Names are the schedules' suffixes. Week passes
  # are 20 minutes apart from Saturday 20:00 (a pass is a few minutes, 15 at the most); `SAT#1` is the month's first
  # Saturday. The weekend pass is on Thursday night, a day of no other loop.
  dolphin_maps_loop_week_days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
  dolphin_maps_loop_extra_schedules = merge(
    {
      for i, day in local.dolphin_maps_loop_week_days : "week-${day}" => {
        schedule = "cron(${(i * 20) % 60} ${20 + floor(i * 20 / 60)} ? * SAT#1 *)"
        input    = { mode = "week", weekday = day }
      }
    },
    { weekend = { schedule = "cron(0 23 ? * THU *)", input = { mode = "weekend" } } },
  )
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
# The PeMS login
# -------------------------------------------------------------------------------------

# Caltrans' PeMS (traffic data) login for dolphin-maps' scheduled PeMS refresh: a weekly GitHub Actions job now, a
# loop Lambda later. Terraform owns the container and who may read it; the value (JSON: PEMS_USERNAME, PEMS_PASSWORD)
# is set out of band and never enters git. The metal boxes (dev-server-role) read it and put it: the login starts on a
# dev box, which stores it here and rotates it later.
resource "aws_secretsmanager_secret" "dolphin_maps_pems" {
  name                    = "dolphin-maps/pems"
  description             = "dolphin-maps' PeMS (Caltrans traffic data) login (JSON: PEMS_USERNAME, PEMS_PASSWORD). Value set out of band; see dolphin-maps-loop.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "dolphin-maps/pems", Project = "dolphin-maps" }
}

resource "aws_secretsmanager_secret_policy" "dolphin_maps_pems" {
  secret_arn = aws_secretsmanager_secret.dolphin_maps_pems.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationTheMetalBoxesAndTheLoopCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.dolphin_maps_pems.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn, aws_iam_role.dolphin_maps_loop.arn]) } }
    }]
  })
}

# The metal boxes read it and store it (the login starts on a dev box, which puts it here and rotates it later):
# GetSecretValue, PutSecretValue and DescribeSecret (a value is set, without reading it back) on this one secret. One
# managed policy, not an inline one: dev-server-role's inline policies are at IAM's 10,240-character total (#574).
data "aws_iam_policy_document" "dolphin_maps_pems_dev" {
  statement {
    sid       = "ReadThePemsLogin"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.dolphin_maps_pems.arn]
  }
  statement {
    sid       = "PutThePemsLogin"
    actions   = ["secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.dolphin_maps_pems.arn]
  }
  # Ball Don't Lie's key (dolphin-maps-balldontlie.tf): read and put, here for the role's 20-policy cap.
  statement {
    sid       = "ReadAndPutTheBallDontLieKey"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.balldontlie.arn]
  }
  # dolphin-maps' MusicKit key (dolphin-maps-musickit.tf): put and described, never read, here for the role's 20-policy cap.
  statement {
    sid       = "PutTheMusicKitKey"
    actions   = ["secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.dolphin_maps_musickit.arn]
  }
}

resource "aws_iam_policy" "dolphin_maps_pems_dev" {
  name        = "dev-dolphin-maps-pems"
  description = "Metal dev boxes: read and put the dolphin-maps PeMS login (dolphin-maps/pems) and the Ball Don't Lie key (balldontlie/api-key); put the MusicKit key"
  policy      = data.aws_iam_policy_document.dolphin_maps_pems_dev.json
}

resource "aws_iam_role_policy_attachment" "dolphin_maps_pems_dev" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.dolphin_maps_pems_dev.arn
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
        Resource = concat([aws_secretsmanager_secret.dolphin_maps_loop_github_app.arn, aws_secretsmanager_secret.browserbase.arn, aws_secretsmanager_secret.dolphin_maps_pems.arn, aws_secretsmanager_secret.balldontlie.arn], [for s in aws_secretsmanager_secret.search_fetch_api_key : s.arn])
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
  description = {
    live  = "dolphin-maps live loop: Google Maps samples on school mornings, committed to the repository, journal to S3"
    sweep = "dolphin-maps weekly loop: the school week's typical ranges, committed to the repository, journal to S3"
    refit = "dolphin-maps weekly refit: each area's calibration on the ranges in the repository, committed to it, journal to S3"
    games = "dolphin-maps games loop: each arena's home games for the next 30 days from Ball Don't Lie, committed to the repository, journal to S3"
  }[each.key]
  role = aws_iam_role.dolphin_maps_loop.arn
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
      DEEPSEEK_SECRET_ID    = aws_secretsmanager_secret.search_fetch_api_key["deepseek"].name
      EXA_SECRET_ID         = aws_secretsmanager_secret.search_fetch_api_key["exa"].name
      SCRAPINGDOG_SECRET_ID = aws_secretsmanager_secret.search_fetch_api_key["scrapingdog"].name
      PEMS_SECRET_ID        = aws_secretsmanager_secret.dolphin_maps_pems.name
      BALLDONTLIE_SECRET_ID = aws_secretsmanager_secret.balldontlie.name
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

# The week and weekend passes: the sweep function, so its permissions and alarms (a failed pass of either counts as a failed
# sweep). In Los Angeles time, like the others.
resource "aws_scheduler_schedule" "dolphin_maps_loop_extra" {
  for_each   = local.dolphin_maps_loop_extra_schedules
  name       = "${local.dolphin_maps_loop_name}-${each.key}"
  group_name = "default"
  state      = var.dolphin_maps_loop_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window { mode = "OFF" }

  schedule_expression          = each.value.schedule
  schedule_expression_timezone = "America/Los_Angeles"

  target {
    arn      = aws_lambda_function.dolphin_maps_loop["sweep"].arn
    role_arn = aws_iam_role.dolphin_maps_loop_scheduler.arn
    input    = jsonencode(each.value.input)

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

# Sweep: it runs once a week, so one failure is a week without new ranges. Lambda's Errors metric for
# the sweep's own function counts every failure: a thrown error, a timeout, running out of memory, a failed start.
resource "aws_cloudwatch_metric_alarm" "dolphin_maps_loop_sweep_failed" {
  alarm_name          = "dolphin-maps-loop-sweep-failed"
  alarm_description   = "dolphin-maps' weekly sweep failed: no new typical ranges this week. See /aws/lambda/dolphin-maps-loop-sweep and journal/sweep/ in the dolphin-maps-loop bucket; rerun it with {\"mode\": \"sweep\"}."
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

# Refit: the same, for the week's calibration. A refit that keeps every committed calibration (the guard refused them
# all, or ran out of time) is a pass that did its job; the journal's log says which.
resource "aws_cloudwatch_metric_alarm" "dolphin_maps_loop_refit_failed" {
  alarm_name          = "dolphin-maps-loop-refit-failed"
  alarm_description   = "dolphin-maps' weekly refit failed: the calibrations were not refit or committed this week. See /aws/lambda/dolphin-maps-loop-refit and journal/refit/ (the guard's verdicts) in the dolphin-maps-loop bucket; rerun it with {\"mode\": \"refit\"}."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.dolphin_maps_loop["refit"].function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

# Games: daily, so one failure is a day's schedule not refreshed; the committed games stay and the next morning's pass
# reads them again. A league refused (an empty read while games are committed ahead) fails the pass after the other
# leagues' games are committed.
resource "aws_cloudwatch_metric_alarm" "dolphin_maps_loop_games_failed" {
  alarm_name          = "dolphin-maps-loop-games-failed"
  alarm_description   = "dolphin-maps' games loop failed: a league's schedule was refused or not read, or the commit failed. See /aws/lambda/dolphin-maps-loop-games and journal/games/ (each provider read) in the dolphin-maps-loop bucket; rerun it with {\"mode\": \"games\"}."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.dolphin_maps_loop["games"].function_name }
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
# one run; the games loop runs every day, so two days without one is a broken schedule. Missing data is a breach: no
# invocations publish no datapoint.
resource "aws_cloudwatch_metric_alarm" "dolphin_maps_loop_not_running" {
  for_each            = var.dolphin_maps_loop_enabled ? { live = 4, sweep = 7, refit = 7, games = 2 } : {}
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

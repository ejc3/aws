# run-cloud-reaper.tf -- delete run.cloud simulators and sandboxes that were left running.
#
# WHY (owner, 2026-11-24: "Add a lambda to catch idle simulators and shut them down"): run.cloud (cloud iOS
# simulators, Android emulators and microVM sandboxes; key in dev-ai-services.tf) bills per active minute against a
# small prepaid balance. Agents are told to give every session --inactivity-timeout and --hard-timeout and to delete
# it when done. This catches the ones that do not: no timeout, a long timeout, or a session simply forgotten.
#
# WHAT: scripts/run-cloud-reaper.py, every five minutes. It lists the account's active iOS and Android sessions, its
# sandboxes and its boxes, and deletes an active session or a running, non-persistent sandbox when ANY of:
#   1. it has no inactivity timeout and no hard timeout, or one over its cap (inactivity over IDLE_MINUTES, hard
#      over MAX_AGE_MINUTES; for a sandbox its idle pause and its lifetime), once it is GRACE_MINUTES old;
#   2. it has been idle for IDLE_MINUTES. Only sandboxes report last activity (last_active_at). Simulators report
#      none, so for them the reaper uses age instead: run.cloud's own inactivity timer (rule 1 makes it at most
#      IDLE_MINUTES) closes an idle one, and rule 3 ends one kept busy or forgotten;
#   3. it is older than MAX_AGE_MINUTES (a sandbox: its current run), whatever its timeouts.
# Defaults: 10 minutes idle, 60 minutes age, 5 minutes grace (the variables below).
#
# WHAT IT NEVER DELETES: anything tagged keep=true; a persistent sandbox (idle pause 0 or no lifetime limit, the
# CLI's --persistent and --expose) or a sandbox behind a box (those, and the boxes, are logged on every run); a
# sandbox that is not running (paused or stopped: compute billing has stopped, and run.cloud destroys it after 48
# hours); a session the API does not describe well enough to judge (that fails the run instead, so the Errors alarm
# says the API changed).
#
# Each run logs one JSON line per session (id, kind, age, idle, decision, reason) and publishes SessionsReaped and
# ActiveSessions in namespace RunCloud. A failed delete is retried by the next run; the run fails only after every
# session has been handled.
#
# The role can read the one secret, write its own log group and publish to its own namespace: nothing else in AWS.
# Its power is in the run.cloud key, which can delete any session in the account. The secret's resource policy
# (dev-ai-services.tf) names this role as a reader of that secret alone.
#
# `aws lambda invoke --function-name run-cloud-reaper --payload '{"dry_run": true}' --cli-binary-format
# raw-in-base64-out /dev/stdout` shows the decisions without deleting anything.

variable "run_cloud_reaper_idle_minutes" {
  description = "run.cloud reaper: delete a sandbox idle this long; also the cap on a session's inactivity timeout and a sandbox's idle pause."
  type        = number
  default     = 10
  validation {
    condition     = var.run_cloud_reaper_idle_minutes >= 1 && floor(var.run_cloud_reaper_idle_minutes) == var.run_cloud_reaper_idle_minutes
    error_message = "A whole number of minutes, at least 1."
  }
}

variable "run_cloud_reaper_max_age_minutes" {
  description = "run.cloud reaper: delete a session (or a sandbox's current run) older than this; also the cap on a hard timeout or sandbox lifetime."
  type        = number
  default     = 60
  validation {
    condition     = var.run_cloud_reaper_max_age_minutes >= 1 && floor(var.run_cloud_reaper_max_age_minutes) == var.run_cloud_reaper_max_age_minutes
    error_message = "A whole number of minutes, at least 1."
  }
}

variable "run_cloud_reaper_grace_minutes" {
  description = "run.cloud reaper: a session with no timeouts or timeouts over the caps is left alone this long, so the agent that made it can finish."
  type        = number
  default     = 5
  validation {
    condition     = var.run_cloud_reaper_grace_minutes >= 1 && floor(var.run_cloud_reaper_grace_minutes) == var.run_cloud_reaper_grace_minutes
    error_message = "A whole number of minutes, at least 1."
  }
}

locals {
  run_cloud_reaper_namespace = "RunCloud"
}

data "archive_file" "run_cloud_reaper" {
  type        = "zip"
  output_path = "${path.module}/.terraform/run-cloud-reaper.zip"
  source {
    filename = "index.py"
    content  = file("${path.module}/scripts/run-cloud-reaper.py")
  }
}

resource "aws_iam_role" "run_cloud_reaper" {
  name = "run-cloud-reaper"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# No AWSLambdaBasicExecutionRole: it would let the role create log groups and write to every one. This is the
# whole grant.
resource "aws_iam_role_policy" "run_cloud_reaper" {
  name = "run-cloud-reaper"
  role = aws_iam_role.run_cloud_reaper.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadTheRunCloudKey"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.run_cloud_api_key.arn
      },
      {
        Sid      = "WriteItsOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.run_cloud_reaper.arn}:*"
      },
      {
        # PutMetricData cannot be resource scoped; the namespace condition is the scope.
        Sid       = "PublishOnlyItsNamespace"
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = local.run_cloud_reaper_namespace } }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "run_cloud_reaper" {
  name              = "/aws/lambda/run-cloud-reaper"
  retention_in_days = 30
}

resource "aws_lambda_function" "run_cloud_reaper" {
  function_name    = "run-cloud-reaper"
  role             = aws_iam_role.run_cloud_reaper.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 240
  filename         = data.archive_file.run_cloud_reaper.output_path
  source_code_hash = data.archive_file.run_cloud_reaper.output_base64sha256

  environment {
    variables = {
      SECRET_ID        = aws_secretsmanager_secret.run_cloud_api_key.name
      API_URL          = "https://api.run.cloud"
      METRIC_NAMESPACE = local.run_cloud_reaper_namespace
      IDLE_MINUTES     = tostring(var.run_cloud_reaper_idle_minutes)
      MAX_AGE_MINUTES  = tostring(var.run_cloud_reaper_max_age_minutes)
      GRACE_MINUTES    = tostring(var.run_cloud_reaper_grace_minutes)
    }
  }

  tags = { Name = "run-cloud-reaper" }

  # The log group exists before the first run (the role could not create it), and the first run can already read
  # the key: the secret's resource policy names this role.
  depends_on = [
    aws_cloudwatch_log_group.run_cloud_reaper,
    aws_iam_role_policy.run_cloud_reaper,
    aws_secretsmanager_secret_policy.run_cloud_api_key,
  ]
}

resource "aws_cloudwatch_event_rule" "run_cloud_reaper" {
  name                = "run-cloud-reaper-every-5-minutes"
  description         = "Delete run.cloud simulators and sandboxes left running"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "run_cloud_reaper" {
  rule = aws_cloudwatch_event_rule.run_cloud_reaper.name
  arn  = aws_lambda_function.run_cloud_reaper.arn
}

# No automatic retry: the next run, five minutes later, retries a failed delete anyway.
resource "aws_lambda_function_event_invoke_config" "run_cloud_reaper" {
  function_name          = aws_lambda_function.run_cloud_reaper.function_name
  maximum_retry_attempts = 0
}

resource "aws_lambda_permission" "run_cloud_reaper" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.run_cloud_reaper.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.run_cloud_reaper.arn
}

# Two failed runs in 15 minutes, not one: a single run.cloud API blip is retried five minutes later and is not worth
# an email. A delete that keeps failing, or an API the reaper no longer understands, fails every run and fires this.
resource "aws_cloudwatch_metric_alarm" "run_cloud_reaper_errors" {
  alarm_name          = "run-cloud-reaper-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 2
  alarm_description   = "The run-cloud-reaper Lambda failed twice in 15 minutes: run.cloud sessions left running may not be deleted (its log says which session or call failed)."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.run_cloud_reaper.function_name }
}

resource "aws_cloudwatch_metric_alarm" "run_cloud_reaper_not_running" {
  alarm_name          = "run-cloud-reaper-not-running"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "The run-cloud-reaper Lambda has not run for 15 minutes (it runs every 5): nothing is deleting run.cloud sessions left running."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "breaching"
  dimensions          = { FunctionName = aws_lambda_function.run_cloud_reaper.function_name }
}

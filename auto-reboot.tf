# auto-reboot.tf -- reboot an ON-DEMAND box that has wedged.
#
# WHY: on 2026-10-01 nextjs-dev ran out of memory and sat dead for 20 hours (network out exactly 0, both tunnels down,
# status check in ALARM) until a person noticed and rebooted it. 2026-07-25 (jumpbox) and 2026-08-16 (nextjs-dev) were
# the same story with the instance status check still "ok". 2026-10-10 (nextjs-dev) paged until SSH and both tunnels were
# dead while the status check read ok and NetworkOut fell to 27 KB per five minutes but never zero, so neither rule saw
# it; the third rule below is that signature. Nothing here prevents a wedge; it ends one in minutes instead of hours.
#
# WHICH BOXES: the on-demand ones only (jumpbox, jumpbox-2, nextjs-dev, claude-master-server). The owner does not want
# automatic restarts of the SPOT boxes (fcvm-metal-arm, fcvm-metal-x86, io-box): they are interruptible and stop/start on
# their own terms, and a person decides about them.
#
# WHAT: scripts/auto-reboot.py, every five minutes. For each RUNNING instance on the list below it asks CloudWatch
# whether the instance status check has failed for 15 minutes in a row, or NetworkOut has been under a 20 KiB floor for 20,
# or the box has been paging for 20 minutes (EBS reads of 30 GiB or more per five minutes, about the gp3 cap, with writes
# under a tenth of that; a 14-day backtest on these boxes found it only in the 2026-10-01 and 2026-10-10 wedges).
# A floor, not exactly zero: an RCU stall (the jumpbox, 2026-10-08) starves userspace while the kernel keeps answering
# TCP, so NetworkOut falls to a trickle -- 11,468 bytes per five minutes -- and never reaches zero. The paging rule
# covers a wedge whose trickle stays above the floor (2026-10-10 nextjs-dev bottomed out at 27,394 bytes).
# Any one is "wedged". It then snapshots the console (the existing redacting capture Lambda, dev-diagnostics.tf),
# reboots the OS, and tells the alert topic.
#
# WHAT IT NEVER DOES:
#   * stop/start (a reboot keeps the console buffer and the instance-store disks),
#   * touch a box that is not running (a deliberate stop stays a stop; the auto-stop Lambdas own that),
#   * touch a spot box (metal, io-box) or an ephemeral one (parallel, GPU, wbox, runners, mac): not on the list,
#   * reboot one box more than once in 3 hours or 3 times in 24 (then it only alerts: a box that wedges again at
#     once needs a person, not a loop),
#   * act on a HOST problem (system status check): a reboot does not fix it; it alerts.
# The role's only EC2 write is ec2:RebootInstances, and only on instances whose Name tag is on the list.
#
# `aws lambda invoke --payload '{"dry_run": true}'` shows what it would do without doing it.

locals {
  # On-demand boxes only. Matched by Name tag, so an instance replaced by Terraform is covered the moment it exists,
  # with no id to keep in sync. Add a SPOT box here only on the owner's say-so.
  auto_reboot_names   = ["jumpbox", "jumpbox-2", "nextjs-dev", "claude-master-server"]
  auto_reboot_regions = ["us-west-1"]
}

# One item per instance: the times of its automatic reboots, and when it was last mentioned. This is what makes the
# brakes work; without it a box that wedges every ten minutes would be rebooted every ten minutes.
resource "aws_dynamodb_table" "auto_reboot" {
  name         = "auto-reboot-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "instance_id"

  attribute {
    name = "instance_id"
    type = "S"
  }

  tags = { Name = "auto-reboot-state" }
}

data "archive_file" "auto_reboot" {
  type        = "zip"
  output_path = "${path.module}/.terraform/auto-reboot.zip"
  source {
    filename = "index.py"
    content  = file("${path.module}/scripts/auto-reboot.py")
  }
}

resource "aws_iam_role" "auto_reboot" {
  name = "auto-reboot"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "auto_reboot_basic" {
  role       = aws_iam_role.auto_reboot.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "auto_reboot" {
  name = "auto-reboot"
  role = aws_iam_role.auto_reboot.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Read-only, and Describe*/GetMetric* cannot be resource scoped.
        Sid      = "LookAtInstancesAndTheirMetrics"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances", "cloudwatch:GetMetricStatistics"]
        Resource = "*"
      },
      {
        # The ONLY thing this role may do to an instance, and only to the persistent boxes by Name tag. No stop,
        # no terminate, no modify, no start.
        Sid      = "RebootOnlyThePersistentBoxes"
        Effect   = "Allow"
        Action   = "ec2:RebootInstances"
        Resource = "arn:aws:ec2:*:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = { "ec2:ResourceTag/Name" = local.auto_reboot_names }
        }
      },
      {
        Sid      = "SnapshotTheConsoleFirst"
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.console_capture.arn
      },
      {
        Sid      = "ItsOwnBrakeState"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
        Resource = aws_dynamodb_table.auto_reboot.arn
      },
      {
        Sid      = "Tell"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      },
    ]
  })
}

resource "aws_lambda_function" "auto_reboot" {
  function_name    = "auto-reboot"
  role             = aws_iam_role.auto_reboot.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 240
  filename         = data.archive_file.auto_reboot.output_path
  source_code_hash = data.archive_file.auto_reboot.output_base64sha256

  environment {
    variables = {
      TARGET_NAMES             = join(",", local.auto_reboot_names)
      REGIONS                  = join(",", local.auto_reboot_regions)
      STATE_TABLE              = aws_dynamodb_table.auto_reboot.name
      SNS_TOPIC_ARN            = aws_sns_topic.cost_alerts.arn
      CONSOLE_CAPTURE_FUNCTION = aws_lambda_function.console_capture.function_name
    }
  }

  tags = { Name = "auto-reboot" }
}

resource "aws_cloudwatch_event_rule" "auto_reboot" {
  name                = "auto-reboot-every-5-minutes"
  description         = "Look for wedged persistent boxes and reboot them"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "auto_reboot" {
  rule = aws_cloudwatch_event_rule.auto_reboot.name
  arn  = aws_lambda_function.auto_reboot.arn
}

# No automatic retry of a failed run: it runs again in five minutes anyway, and a retry of a half-finished run is
# the thing the reservation in the state table exists to make harmless.
resource "aws_lambda_function_event_invoke_config" "auto_reboot" {
  function_name          = aws_lambda_function.auto_reboot.function_name
  maximum_retry_attempts = 0
}

resource "aws_lambda_permission" "auto_reboot" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.auto_reboot.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.auto_reboot.arn
}

# The watcher must not be the thing that fails silently. It errors, or it stops being invoked at all (a missing
# datapoint is breaching here, on purpose).
resource "aws_cloudwatch_metric_alarm" "auto_reboot_errors" {
  alarm_name          = "auto-reboot-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "The auto-reboot Lambda raised an error: a wedged box may not be rebooted."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.auto_reboot.function_name }
}

resource "aws_cloudwatch_metric_alarm" "auto_reboot_not_running" {
  alarm_name          = "auto-reboot-not-running"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "The auto-reboot Lambda has not run for 15 minutes (it runs every 5): nothing is watching for wedged boxes."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "breaching"
  dimensions          = { FunctionName = aws_lambda_function.auto_reboot.function_name }
}

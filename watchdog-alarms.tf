# watchdog-alarms.tf
#
# ALARMS ON THE PARALLEL-BOX WATCHDOGS, which run where their boxes are: parallel-box-watchdog in
# us-west-2 (the owner's GPU boxes and the members' gboxes) and its copy in us-east-2 (the owner's
# parallel boxes and the members' pboxes). Since the members' boxes depend on them for their idle
# reaping, their hard lifetimes and the one-instance-per-name rule, a watchdog that errors or stops
# being invoked must not fail silently. Each watchdog has the same two alarms as member-wbox.tf's:
# Errors >= 1 in 15 minutes, and Invocations < 1 in 15 minutes with missing data breaching (it runs
# every 5).
#
# WHY A RELAY. An alarm's SNS action must be a topic in the alarm's own region, and the alert topic
# (cost-alerts, cost-alerts.tf) and its subscriptions live in us-west-1. So the alarms have no action
# of their own: an EventBridge rule in each region sends the alarm's change to ALARM to us-west-1's
# default event bus (a cross-region event-bus target, with a role that may put events there and
# nowhere else), and one rule there publishes it to cost-alerts. The message is the alarm event
# itself (alarm name, region, reason).

locals {
  watchdog_alarm_bus_arn = "arn:aws:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:event-bus/default"

  # The ALARM transition of exactly these alarms.
  watchdog_alarm_pattern = {
    source        = ["aws.cloudwatch"]
    "detail-type" = ["CloudWatch Alarm State Change"]
    detail        = { state = { value = ["ALARM"] } }
  }
}

# ============================================================ us-west-2
resource "aws_cloudwatch_metric_alarm" "parallel_watchdog_errors" {
  provider            = aws.west2
  alarm_name          = "parallel-box-watchdog-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "The us-west-2 parallel-box watchdog raised an error: an idle, expired or duplicate GPU box (the owner's or a member's) may not be terminated (watchdog-alarms.tf)."
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.parallel_watchdog.function_name }
}

resource "aws_cloudwatch_metric_alarm" "parallel_watchdog_not_running" {
  provider            = aws.west2
  alarm_name          = "parallel-box-watchdog-not-running"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "The us-west-2 parallel-box watchdog has not run for 15 minutes (it runs every 5): nothing reaps the GPU boxes but their own timers (watchdog-alarms.tf)."
  treat_missing_data  = "breaching"
  dimensions          = { FunctionName = aws_lambda_function.parallel_watchdog.function_name }
}

resource "aws_cloudwatch_event_rule" "watchdog_alarm_relay_west2" {
  provider    = aws.west2
  name        = "watchdog-alarm-relay"
  description = "Send the parallel-box watchdog's alarms to us-west-1, where the alert topic is"
  event_pattern = jsonencode(merge(local.watchdog_alarm_pattern, {
    resources = [aws_cloudwatch_metric_alarm.parallel_watchdog_errors.arn, aws_cloudwatch_metric_alarm.parallel_watchdog_not_running.arn]
  }))
}

resource "aws_cloudwatch_event_target" "watchdog_alarm_relay_west2" {
  provider  = aws.west2
  rule      = aws_cloudwatch_event_rule.watchdog_alarm_relay_west2.name
  target_id = "us-west-1-default-bus"
  arn       = local.watchdog_alarm_bus_arn
  role_arn  = aws_iam_role.watchdog_alarm_relay.arn
}

# ============================================================ us-east-2
resource "aws_cloudwatch_metric_alarm" "parallel_watchdog_ohio_errors" {
  provider            = aws.ohio
  alarm_name          = "parallel-box-watchdog-ohio-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "The Ohio parallel-box watchdog raised an error: an idle, expired or duplicate parallel box (the owner's or a member's) may not be terminated (watchdog-alarms.tf)."
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.parallel_watchdog_ohio.function_name }
}

resource "aws_cloudwatch_metric_alarm" "parallel_watchdog_ohio_not_running" {
  provider            = aws.ohio
  alarm_name          = "parallel-box-watchdog-ohio-not-running"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "The Ohio parallel-box watchdog has not run for 15 minutes (it runs every 5): nothing reaps the parallel boxes, and the members' pboxes end only by their own timers (watchdog-alarms.tf)."
  treat_missing_data  = "breaching"
  dimensions          = { FunctionName = aws_lambda_function.parallel_watchdog_ohio.function_name }
}

resource "aws_cloudwatch_event_rule" "watchdog_alarm_relay_ohio" {
  provider    = aws.ohio
  name        = "watchdog-alarm-relay"
  description = "Send the Ohio parallel-box watchdog's alarms to us-west-1, where the alert topic is"
  event_pattern = jsonencode(merge(local.watchdog_alarm_pattern, {
    resources = [aws_cloudwatch_metric_alarm.parallel_watchdog_ohio_errors.arn, aws_cloudwatch_metric_alarm.parallel_watchdog_ohio_not_running.arn]
  }))
}

resource "aws_cloudwatch_event_target" "watchdog_alarm_relay_ohio" {
  provider  = aws.ohio
  rule      = aws_cloudwatch_event_rule.watchdog_alarm_relay_ohio.name
  target_id = "us-west-1-default-bus"
  arn       = local.watchdog_alarm_bus_arn
  role_arn  = aws_iam_role.watchdog_alarm_relay.arn
}

# ============================================================ the relay's role: put events on one bus
resource "aws_iam_role" "watchdog_alarm_relay" {
  name = "watchdog-alarm-relay"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
  tags = { Name = "watchdog-alarm-relay" }
}

resource "aws_iam_role_policy" "watchdog_alarm_relay" {
  name = "put-on-us-west-1-default-bus"
  role = aws_iam_role.watchdog_alarm_relay.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "events:PutEvents", Resource = local.watchdog_alarm_bus_arn }]
  })
}

# ============================================================ us-west-1: to the alert topic
resource "aws_cloudwatch_event_rule" "watchdog_alarms" {
  name        = "watchdog-alarms"
  description = "Publish the relayed parallel-box watchdog alarms to cost-alerts"
  event_pattern = jsonencode(merge(local.watchdog_alarm_pattern, {
    resources = [
      aws_cloudwatch_metric_alarm.parallel_watchdog_errors.arn,
      aws_cloudwatch_metric_alarm.parallel_watchdog_not_running.arn,
      aws_cloudwatch_metric_alarm.parallel_watchdog_ohio_errors.arn,
      aws_cloudwatch_metric_alarm.parallel_watchdog_ohio_not_running.arn,
    ]
  }))
}

resource "aws_cloudwatch_event_target" "watchdog_alarms" {
  rule      = aws_cloudwatch_event_rule.watchdog_alarms.name
  target_id = "cost-alerts"
  arn       = aws_sns_topic.cost_alerts.arn
}

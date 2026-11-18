# The metal runner controller lives on the west coast and can launch into other regions.
# Its private logs stay private. Only selected fields from termination records reach the dev role.
locals {
  runner_loss_controllers = var.enable_github_runner ? toset(["github-runner-cleanup", "github-runner-webhook", "github-runner-reuse"]) : toset([])
}

# Keep the archive and its read path available through a pause in runner launches.
resource "aws_cloudwatch_log_group" "runner_loss" {
  name              = "/aws/lambda/github-runner-loss"
  retention_in_days = 30
}

# Adopt the controllers' auto-created log groups in place. Creating the same groups
# on a new account lets the subscriptions attach before the first Lambda invocation.
data "aws_cloudwatch_log_groups" "runner_loss_sources" {
  log_group_name_prefix = "/aws/lambda/github-runner-"
}

resource "aws_cloudwatch_log_group" "runner_loss_source" {
  for_each     = local.runner_loss_controllers
  name         = "/aws/lambda/${each.key}"
  skip_destroy = true # disabling the runner must preserve existing controller logs
}

import {
  for_each = { for controller in local.runner_loss_controllers : controller => "/aws/lambda/${controller}"
    if contains(data.aws_cloudwatch_log_groups.runner_loss_sources.log_group_names, "/aws/lambda/${controller}")
  }
  to = aws_cloudwatch_log_group.runner_loss_source[each.key]
  id = each.value
}

data "archive_file" "runner_loss" {
  count       = var.enable_github_runner ? 1 : 0
  type        = "zip"
  source_file = "${path.module}/runner-loss/collector.py"
  output_path = "${path.module}/.terraform/runner-loss.zip"
}

resource "aws_iam_role" "runner_loss" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-runner-loss"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "runner_loss" {
  count = var.enable_github_runner ? 1 : 0
  name  = "observe-metal-runner-ends"
  role  = aws_iam_role.runner_loss[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = ["ec2:DescribeInstances", "ec2:DescribeSpotInstanceRequests"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.runner_regions } }
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.runner_loss.arn}:log-stream:*"
      },
    ]
  })
}

resource "aws_lambda_function" "runner_loss" {
  count            = var.enable_github_runner ? 1 : 0
  function_name    = "github-runner-loss"
  description      = "Record metal CI runner state reasons and accepted controller terminations"
  runtime          = "python3.12"
  handler          = "collector.handler"
  filename         = data.archive_file.runner_loss[0].output_path
  source_code_hash = data.archive_file.runner_loss[0].output_base64sha256
  role             = aws_iam_role.runner_loss[0].arn
  timeout          = 60
  environment {
    variables = { RUNNER_REGIONS = jsonencode(local.runner_regions) }
  }
  depends_on = [aws_iam_role_policy.runner_loss]
}

resource "aws_cloudwatch_event_rule" "runner_loss" {
  count               = var.enable_github_runner ? 1 : 0
  name                = "github-runner-loss"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_lambda_permission" "runner_loss_scan" {
  count         = var.enable_github_runner ? 1 : 0
  statement_id  = "AllowScheduledScan"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.runner_loss[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.runner_loss[0].arn
}

resource "aws_cloudwatch_event_target" "runner_loss" {
  count      = var.enable_github_runner ? 1 : 0
  rule       = aws_cloudwatch_event_rule.runner_loss[0].name
  arn        = aws_lambda_function.runner_loss[0].arn
  depends_on = [aws_lambda_permission.runner_loss_scan]
}

resource "aws_lambda_permission" "runner_loss_controller" {
  for_each       = local.runner_loss_controllers
  statement_id   = "AllowLogs-${each.key}"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.runner_loss[0].function_name
  principal      = "logs.amazonaws.com"
  source_arn     = "${aws_cloudwatch_log_group.runner_loss_source[each.key].arn}:*"
  source_account = data.aws_caller_identity.current.account_id
}

resource "aws_cloudwatch_log_subscription_filter" "runner_loss_controller" {
  for_each        = local.runner_loss_controllers
  name            = "accepted-metal-runner-terminations"
  log_group_name  = aws_cloudwatch_log_group.runner_loss_source[each.key].name
  filter_pattern  = "{ $.event = \"runner_termination_accepted\" }"
  destination_arn = aws_lambda_function.runner_loss[0].arn
  depends_on      = [aws_lambda_permission.runner_loss_controller]
}

# EC2's terminal-state evidence expires after about an hour. Alert in time to
# recover it if the scheduled scans stop or a regional describe starts failing.
resource "aws_cloudwatch_log_metric_filter" "runner_loss_scan_complete" {
  count          = var.enable_github_runner ? 1 : 0
  name           = "metal-runner-loss-scan-complete"
  log_group_name = aws_cloudwatch_log_group.runner_loss.name
  pattern        = "{ $.event = \"runner_loss_scan_complete\" }"
  metric_transformation {
    name      = "LossScansCompleted"
    namespace = "GitHubRunners"
    value     = "1"
  }
}

resource "aws_cloudwatch_metric_alarm" "runner_loss_scan_silent" {
  count               = var.enable_github_runner ? 1 : 0
  alarm_name          = "runner-loss-scan-silent"
  alarm_description   = "Metal CI runner termination evidence has not been scanned for 15 minutes. Check github-runner-loss before EC2's terminated instance records expire."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  namespace           = aws_cloudwatch_log_metric_filter.runner_loss_scan_complete[0].metric_transformation[0].namespace
  metric_name         = aws_cloudwatch_log_metric_filter.runner_loss_scan_complete[0].metric_transformation[0].name
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  threshold           = 1
  period              = 300
  statistic           = "Sum"
  treat_missing_data  = "breaching"
}

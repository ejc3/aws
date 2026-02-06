# Cost Monitoring
# 1. Daily email with yesterday's spend (8am UTC / midnight PST)
# 2. Alert if daily spend exceeds $200

# ============================================
# Secrets in SSM Parameter Store
# ============================================

resource "aws_ssm_parameter" "alert_email" {
  name        = "/alerts/email"
  type        = "String"
  value       = "PLACEHOLDER"
  description = "Email for cost alerts"

  lifecycle {
    ignore_changes = [value]
  }
}

# ============================================
# Daily Email Report via Lambda + SES
# ============================================

data "archive_file" "cost_report" {
  type        = "zip"
  output_path = "${path.module}/.terraform/cost-report.zip"

  source {
    content  = <<-PYTHON
import boto3
import json
import os
from datetime import datetime, timedelta

def handler(event, context):
    ce = boto3.client('ce', region_name='us-east-1')
    ses = boto3.client('ses', region_name='us-west-1')
    sns = boto3.client('sns', region_name='us-west-1')
    ssm = boto3.client('ssm', region_name='us-west-1')

    email = ssm.get_parameter(Name='/alerts/email')['Parameter']['Value']
    sns_topic = os.environ.get('SNS_TOPIC_ARN', '')

    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)

    response = ce.get_cost_and_usage(
        TimePeriod={
            'Start': yesterday.isoformat(),
            'End': today.isoformat()
        },
        Granularity='DAILY',
        Metrics=['UnblendedCost'],
        GroupBy=[{'Type': 'DIMENSION', 'Key': 'SERVICE'}]
    )

    total = 0.0
    top_services = []
    lines = ["AWS Cost Report for " + yesterday.isoformat(), "=" * 40, ""]

    for group in response['ResultsByTime'][0]['Groups']:
        service = group['Keys'][0]
        amount = float(group['Metrics']['UnblendedCost']['Amount'])
        if amount > 0.01:
            lines.append(service + ": $" + format(amount, '.2f'))
            total += amount
            if amount > 1.0:
                short_name = service.replace('Amazon ', '').replace('AWS ', '')[:12]
                top_services.append(short_name + ":$" + format(amount, '.0f'))

    lines.extend(["", "-" * 40, "TOTAL: $" + format(total, '.2f')])
    body = "\n".join(lines)

    ses.send_email(
        Source=email,
        Destination={'ToAddresses': [email]},
        Message={
            'Subject': {'Data': 'AWS Daily Cost: $' + format(total, '.2f') + ' (' + yesterday.isoformat() + ')'},
            'Body': {'Text': {'Data': body}}
        }
    )

    # Send SMS via SNS (short message)
    if sns_topic:
        sms_msg = "AWS " + yesterday.isoformat() + ": $" + format(total, '.2f')
        if top_services:
            sms_msg += " (" + ", ".join(top_services[:3]) + ")"
        sns.publish(TopicArn=sns_topic, Message=sms_msg)

    return {'statusCode': 200, 'body': json.dumps({'total': total})}
PYTHON
    filename = "lambda_function.py"
  }
}

resource "aws_lambda_function" "cost_report" {
  function_name    = "daily-cost-report"
  role             = aws_iam_role.cost_report.arn
  handler          = "lambda_function.handler"
  runtime          = "python3.12"
  timeout          = 30
  filename         = data.archive_file.cost_report.output_path
  source_code_hash = data.archive_file.cost_report.output_base64sha256

  environment {
    variables = {
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
    }
  }
}

resource "aws_iam_role" "cost_report" {
  name = "daily-cost-report-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy" "cost_report" {
  name = "cost-report-policy"
  role = aws_iam_role.cost_report.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ce:GetCostAndUsage"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ses:SendEmail"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = [aws_sns_topic.cost_alerts.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = [aws_ssm_parameter.alert_email.arn]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

resource "aws_cloudwatch_event_rule" "cost_report" {
  name                = "daily-cost-report"
  schedule_expression = "cron(0 8 * * ? *)" # 8am UTC = midnight PST
}

resource "aws_cloudwatch_event_target" "cost_report" {
  rule      = aws_cloudwatch_event_rule.cost_report.name
  target_id = "cost-report-lambda"
  arn       = aws_lambda_function.cost_report.arn
}

resource "aws_lambda_permission" "cost_report" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cost_report.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.cost_report.arn
}

# ============================================
# Budget Alert (>$200/day)
# ============================================

data "aws_ssm_parameter" "alert_email" {
  name       = aws_ssm_parameter.alert_email.name
  depends_on = [aws_ssm_parameter.alert_email]
}

resource "aws_sns_topic" "cost_alerts" {
  name = "cost-alerts"
}

resource "aws_sns_topic_policy" "cost_alerts" {
  arn = aws_sns_topic.cost_alerts.arn

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DefaultPolicy"
        Effect    = "Allow"
        Principal = { AWS = "*" }
        Action = [
          "SNS:GetTopicAttributes",
          "SNS:SetTopicAttributes",
          "SNS:AddPermission",
          "SNS:RemovePermission",
          "SNS:DeleteTopic",
          "SNS:Subscribe",
          "SNS:ListSubscriptionsByTopic",
          "SNS:Publish"
        ]
        Resource  = aws_sns_topic.cost_alerts.arn
        Condition = {
          StringEquals = {
            "AWS:SourceOwner" = data.aws_caller_identity.current.account_id
          }
        }
      },
      {
        Sid       = "AllowBudgetsPublish"
        Effect    = "Allow"
        Principal = { Service = "budgets.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.cost_alerts.arn
      }
    ]
  })
}

resource "aws_sns_topic_subscription" "cost_alerts_email" {
  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "email"
  endpoint  = data.aws_ssm_parameter.alert_email.value
}

resource "aws_budgets_budget" "daily_cost" {
  name         = "daily-cost-alert"
  budget_type  = "COST"
  limit_amount = "200"
  limit_unit   = "USD"
  time_unit    = "DAILY"

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_alerts.arn]
  }
}

# ============================================
# CloudWatch Alarms
# ============================================

# Alert if more than 4 runners are running for 30+ minutes
resource "aws_cloudwatch_metric_alarm" "too_many_runners" {
  alarm_name          = "too-many-runners"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 6 # 30 minutes (6 x 5min periods)
  threshold           = 4
  alarm_description   = "More than 4 runners running for 30+ minutes - check for stuck jobs"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "runner_count"
    expression  = "SELECT COUNT(InstanceId) FROM SCHEMA(\"AWS/EC2\", InstanceId) WHERE Role = 'github-runner'"
    label       = "Running Runners"
    period      = 300
    return_data = true
  }
}

# Alert if any runner is running for more than 2 hours
resource "aws_cloudwatch_metric_alarm" "runner_long_running" {
  alarm_name          = "runner-long-running"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 24 # 2 hours (24 x 5min periods)
  threshold           = 0
  alarm_description   = "Runner(s) running for 2+ hours - possible stuck job"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "runner_count"
    expression  = "SELECT COUNT(InstanceId) FROM SCHEMA(\"AWS/EC2\", InstanceId) WHERE Role = 'github-runner'"
    label       = "Running Runners"
    period      = 300
    return_data = true
  }
}

# Alert on high EC2 spend (estimated from running hours)
resource "aws_cloudwatch_metric_alarm" "high_ec2_spend" {
  alarm_name          = "high-ec2-daily-spend"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "EstimatedCharges"
  namespace           = "AWS/Billing"
  period              = 21600 # 6 hours
  statistic           = "Maximum"
  threshold           = 100
  alarm_description   = "EC2 estimated charges exceed $100"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    ServiceName = "AmazonEC2"
    Currency    = "USD"
  }
}

# Alert if jumpbox goes down
resource "aws_cloudwatch_metric_alarm" "jumpbox_status" {
  alarm_name          = "jumpbox-status-check"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "StatusCheckFailed"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Jumpbox instance status check failed"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.jumpbox[0].id
  }
}

# Alert if dev server goes down while running
resource "aws_cloudwatch_metric_alarm" "dev_server_status" {
  count               = var.enable_firecracker_instance ? 1 : 0
  alarm_name          = "dev-server-status-check"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "StatusCheckFailed"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Dev server instance status check failed"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.firecracker_dev[0].id
  }
}

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
        Resource = aws_sns_topic.cost_alerts.arn
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
      },
      {
        Sid       = "AllowSecurityDeliveryHealthAlarms"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = "SNS:Publish"
        Resource  = aws_sns_topic.cost_alerts.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
          ArnLike      = { "aws:SourceArn" = "arn:aws:cloudwatch:us-west-1:${data.aws_caller_identity.current.account_id}:alarm:security-delivery-*" }
        }
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

# fcvm's runner fleet, from GitHubRunners/LiveRunners, which the cleanup Lambda publishes every
# 5 minutes (runner-autoscale.tf). The EC2 Metrics Insights "count by tag" query these used
# before cannot return data: Role is a tag, not a dimension of any AWS/EC2 metric.
resource "aws_cloudwatch_metric_alarm" "too_many_runners" {
  alarm_name          = "too-many-runners"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 6 # 30 minutes (6 x 5min periods)
  threshold           = 4
  alarm_description   = "More than 4 runners running for 30+ minutes - check for stuck jobs"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  namespace           = "GitHubRunners"
  metric_name         = "LiveRunners"
  statistic           = "Maximum"
  period              = 300
}

# The cleanup Lambda enforces every runner's lease and age ceiling. No LiveRunners for 15
# minutes means it is not running, and nothing is ending stuck or idle metal.
resource "aws_cloudwatch_metric_alarm" "runner_cleanup_silent" {
  alarm_name          = "runner-cleanup-silent"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 0
  alarm_description   = "github-runner-cleanup published no runner count for 15 minutes: leases and age ceilings are not being enforced"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "breaching"
  namespace           = "GitHubRunners"
  metric_name         = "LiveRunners"
  statistic           = "SampleCount"
  period              = 300
}

# Alert if any runner is running for more than 2 hours. Warm reuse (up to 12 hours,
# runner-autoscale.tf) can keep a host busy that long on a heavy day, so read it as "look",
# not "broken".
resource "aws_cloudwatch_metric_alarm" "runner_long_running" {
  alarm_name          = "runner-long-running"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 120
  alarm_description   = "Runner(s) running for 2+ hours - possible stuck job"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  namespace           = "GitHubRunners"
  metric_name         = "OldestRunnerAgeMinutes"
  statistic           = "Maximum"
  period              = 300
}

# Alert when the autoscaler refuses to launch while fewer runners than the cap
# can take work. The webhook Lambda emits GitHubRunners/ScaleUpStarved from
# runner-autoscale.tf on every scale-up decision; it is 1 only when the
# per-architecture instance ceiling blocks a launch that healthy-runner
# accounting would have allowed - capacity held by instances that cannot serve a
# job. That state cannot occur in normal operation. On 2026-08-07 the equivalent
# condition ran unnoticed for 3.5 hours because the only evidence was an
# unstructured Lambda log line.
resource "aws_cloudwatch_metric_alarm" "runner_scale_up_starved" {
  alarm_name          = "runner-scale-up-starved"
  comparison_operator = "GreaterThanThreshold"
  # Twenty-four consecutive 5-minute polls (2 hours), not two.
  #
  # ScaleUpStarved now means "work was queued and we refused to add capacity", which
  # ordinary saturation also produces. The previous `counted < max_runners` gate excluded
  # saturation, but at the cost of reading 0 through the entire 3.5-hour incident it was
  # built for, because the wedged runners were GitHub-online the whole time.
  #
  # An interim version used 12 periods and justified it against the longest SINGLE job
  # (43.5 min). That reasoning was wrong: consecutive waves of ordinary 30-40 minute jobs
  # keep the queue non-empty for well over an hour, so one job duration does not bound
  # queue-drain time. Nothing visible to a single poll separates a wedged pool from a deep
  # one either -- a host that wedges mid-job keeps reporting busy=true, exactly like a
  # healthy runner.
  #
  # So this alarm deliberately does not claim to tell them apart. Two hours of a queue
  # that will not drain is worth a look whichever it is.
  evaluation_periods = 24
  threshold          = 0
  alarm_description  = "Queued work went unserved for 2 hours - the pool is wedged, or genuinely that far behind"
  alarm_actions      = [aws_sns_topic.cost_alerts.arn]
  ok_actions         = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data = "notBreaching"

  metric_query {
    id          = "starved"
    expression  = "SELECT MAX(ScaleUpStarved) FROM SCHEMA(\"GitHubRunners\", Architecture)"
    label       = "Scale-up starved"
    period      = 300
    return_data = true
  }
}

# Queued work with nothing online to take it. Distinct from scale-up-starved: that one
# says "we refused to grow", this one says "there is nothing to grow FROM" -- every
# runner is booting, wedged, or gone. It fires fast (two polls) because unlike
# saturation there is no benign version of it.
resource "aws_cloudwatch_metric_alarm" "runner_zero_online" {
  alarm_name          = "runner-zero-online"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2 # two consecutive 5-minute polls
  threshold           = 0
  alarm_description   = "Jobs are queued and no runner is online to take them - the pool is empty, not merely busy"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "zero_online"
    expression  = "SELECT MAX(ZeroOnlineRunners) FROM SCHEMA(\"GitHubRunners\", Architecture)"
    label       = "Zero online runners"
    period      = 300
    return_data = true
  }
}

# EC2 daily spend. This replaces high-ec2-daily-spend, which read AWS/Billing EstimatedCharges:
# billing alerts are off in this account, so that metric does not exist and the alarm could
# never fire. Daily EC2 (compute + EC2-Other) ran $15-148 over 2026-09-13..26, median ~$75, so
# $150 flags a real outlier without paging on a normal heavy day. daily-cost-alert ($200, all
# services) still covers the account.
resource "aws_budgets_budget" "ec2_daily" {
  name         = "ec2-daily"
  budget_type  = "COST"
  limit_amount = "150"
  limit_unit   = "USD"
  time_unit    = "DAILY"

  cost_filter {
    name   = "Service"
    values = ["Amazon Elastic Compute Cloud - Compute", "EC2 - Other"]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_alerts.arn]
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

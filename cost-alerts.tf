# Cost Monitoring
# 1. Daily email with yesterday's spend (8am UTC / midnight PST)
# 2. Alert if daily spend exceeds $200
# 3. SMS alert for both

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

resource "aws_ssm_parameter" "alert_phone" {
  name        = "/alerts/phone"
  type        = "String"
  value       = "PLACEHOLDER"
  description = "Phone for SMS alerts (E.164 format)"

  lifecycle {
    ignore_changes = [value]
  }
}

# ============================================
# Daily Email Report via Lambda + SES + SNS
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

    # Get secrets from SSM
    email = ssm.get_parameter(Name='/alerts/email')['Parameter']['Value']
    phone = ssm.get_parameter(Name='/alerts/phone')['Parameter']['Value']

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
    lines = ["AWS Cost Report for " + yesterday.isoformat(), "=" * 40, ""]

    for group in response['ResultsByTime'][0]['Groups']:
        service = group['Keys'][0]
        amount = float(group['Metrics']['UnblendedCost']['Amount'])
        if amount > 0.01:
            lines.append(service + ": $" + format(amount, '.2f'))
            total += amount

    lines.extend(["", "-" * 40, "TOTAL: $" + format(total, '.2f')])
    body = "\n".join(lines)

    # Send email
    ses.send_email(
        Source=email,
        Destination={'ToAddresses': [email]},
        Message={
            'Subject': {'Data': 'AWS Daily Cost: $' + format(total, '.2f') + ' (' + yesterday.isoformat() + ')'},
            'Body': {'Text': {'Data': body}}
        }
    )

    # Send SMS
    sns.publish(
        PhoneNumber=phone,
        Message='AWS ' + yesterday.isoformat() + ': $' + format(total, '.2f')
    )

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
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter"]
        Resource = [
          aws_ssm_parameter.alert_email.arn,
          aws_ssm_parameter.alert_phone.arn
        ]
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
# Budget Alert (>$200/day) - Email + SMS
# ============================================

# Note: AWS Budgets doesn't support SSM parameters directly,
# so we use SNS with subscriptions that read from SSM at creation time
data "aws_ssm_parameter" "alert_email" {
  name       = aws_ssm_parameter.alert_email.name
  depends_on = [aws_ssm_parameter.alert_email]
}

data "aws_ssm_parameter" "alert_phone" {
  name       = aws_ssm_parameter.alert_phone.name
  depends_on = [aws_ssm_parameter.alert_phone]
}

resource "aws_sns_topic" "cost_alerts" {
  name = "cost-alerts"
}

resource "aws_sns_topic_subscription" "cost_alerts_email" {
  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "email"
  endpoint  = data.aws_ssm_parameter.alert_email.value
}

resource "aws_sns_topic_subscription" "cost_alerts_sms" {
  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "sms"
  endpoint  = data.aws_ssm_parameter.alert_phone.value
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

# alert-telegram.tf -- the alert topic, as short readable Telegram messages on the owner's phone.
#
# WHY (owner, 2026-11-25, after claw-master refused every request for half an hour while its alarm sat in an
# inbox: "is there a free app that can do better than email / cloudwatch alarms. super hard to read aws's" ...
# "telegram please"): CloudWatch's emails are long and hard to read on a phone, and cost-alerts carries about 500
# messages a day, most of them two flapping alarms (runner-scale-up-starved, github-app-runner-errors).
#
# WHAT: scripts/alert-telegram.py, subscribed to cost-alerts beside the email (which is unchanged). An alarm entering
# ALARM is one line: its name and the first sentence of its description. OK is a "recovered after N min" line, only
# for an ALARM it sent. An alarm that fires more than 3 times in 6 hours sends one "flapping" line and is muted for 6
# hours. Other messages (budgets, the security digest, auto-reboot) are their subject and the start of the body.
# Plain text, no formatting interpreted. State is one item per alarm in alert-telegram-state.
#
# SETUP (once, by the owner): make a bot with @BotFather (/newbot), send it any message, and read the chat id from
# https://api.telegram.org/bot<token>/getUpdates (message.chat.id). Then, from a 0600 file holding
# {"bot_token": "...", "chat_id": "..."}:
#   aws secretsmanager put-secret-value --region us-west-1 --secret-id alerts/telegram --secret-string file://<file>
# Until the secret has a value, the function logs "not configured" and does nothing. No Terraform change is needed.
#
# The role reads that one secret and the one table, and writes its own log group: nothing else. The secret's resource
# policy admits administration and this role alone.

resource "aws_secretsmanager_secret" "alert_telegram" {
  name                    = "alerts/telegram"
  description             = "Telegram bot token and chat id for alert-telegram (JSON bot_token, chat_id); value supplied out of band."
  recovery_window_in_days = 7
  tags                    = { Name = "alerts/telegram" }
}

resource "aws_secretsmanager_secret_policy" "alert_telegram" {
  secret_arn = aws_secretsmanager_secret.alert_telegram.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheAlertFunctionCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.alert_telegram.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.alert_telegram.arn]) } }
    }]
  })
}

resource "aws_dynamodb_table" "alert_telegram" {
  name         = "alert-telegram-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "alarm"

  attribute {
    name = "alarm"
    type = "S"
  }

  ttl {
    attribute_name = "expires"
    enabled        = true
  }

  tags = { Name = "alert-telegram-state" }
}

data "archive_file" "alert_telegram" {
  type        = "zip"
  output_path = "${path.module}/.terraform/alert-telegram.zip"
  source {
    filename = "index.py"
    content  = file("${path.module}/scripts/alert-telegram.py")
  }
}

resource "aws_iam_role" "alert_telegram" {
  name = "alert-telegram"
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
resource "aws_iam_role_policy" "alert_telegram" {
  name = "alert-telegram"
  role = aws_iam_role.alert_telegram.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadTheBotSecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.alert_telegram.arn
      },
      {
        Sid      = "KeepItsOwnState"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.alert_telegram.arn
      },
      {
        Sid      = "WriteItsOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.alert_telegram.arn}:*"
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "alert_telegram" {
  name              = "/aws/lambda/alert-telegram"
  retention_in_days = 30
}

resource "aws_lambda_function" "alert_telegram" {
  function_name    = "alert-telegram"
  role             = aws_iam_role.alert_telegram.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 60
  memory_size      = 128
  filename         = data.archive_file.alert_telegram.output_path
  source_code_hash = data.archive_file.alert_telegram.output_base64sha256
  # One at a time: each alarm's flap and mute state is read and written per message. SNS retries a throttled
  # delivery, so a burst queues rather than racing.
  reserved_concurrent_executions = 1

  environment {
    variables = {
      SECRET_ID    = aws_secretsmanager_secret.alert_telegram.name
      STATE_TABLE  = aws_dynamodb_table.alert_telegram.name
      MAIN_ACCOUNT = data.aws_caller_identity.current.account_id
      HOME_REGION  = var.aws_region
      ACCOUNT_NAMES = jsonencode({
        (data.aws_caller_identity.current.account_id) = "main"
        (aws_organizations_account.dev_staging[0].id) = "dev-staging"
      })
    }
  }

  tags = { Name = "alert-telegram" }

  depends_on = [
    aws_cloudwatch_log_group.alert_telegram,
    aws_iam_role_policy.alert_telegram,
    aws_secretsmanager_secret_policy.alert_telegram,
  ]
}

resource "aws_sns_topic_subscription" "alert_telegram" {
  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.alert_telegram.arn
}

resource "aws_lambda_permission" "alert_telegram" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.alert_telegram.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.cost_alerts.arn
}

# Its own failures still reach the email: a send that keeps failing (a revoked token, Telegram down) fails every
# invocation after Lambda's retries.
resource "aws_cloudwatch_metric_alarm" "alert_telegram_errors" {
  alarm_name          = "alert-telegram-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 900
  statistic           = "Sum"
  threshold           = 3
  alarm_description   = "The alert-telegram Lambda failed 3 times in 15 minutes: alerts are not reaching Telegram (the email still gets them; its log says which call failed)."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.alert_telegram.function_name }
}

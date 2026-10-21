# security-alert-digest.tf -- security alerts reach the owner as a few readable emails, not one per finding update.
#
# WHY (2026-10-19): rule security-alerts-notify (security-monitoring.tf) published every event on the central security
# bus straight to the cost-alerts topic, one email each. Security Hub re-imports and Inspector rescans re-send every
# open finding, so 24 hours sent 3,608 emails: 2,604 when Inspector first scanned nextjs-ct, about 500 from EC2.9, and
# 1,995 in five minutes from one Inspector rescan of nextjs-ct's open CVEs. AWS then disabled email on cost-alerts for
# abuse (AWS_SNS_SES_ABUSE_DETECTION; the same happened on 2026-09-13), so for a while the owner received no alert
# email at all. The owner approved a digest Lambda between the alert rules and the email.
#
# THE PATH: security-alerts-notify (its rule and event pattern unchanged) -> SQS queue security-alert-digest-inbox
# (14-day retention; EventBridge retries and the central DLQ in front of it) -> Lambda security-alert-digest (batches,
# partial-batch failures; a record that fails five times goes to security-alert-digest-dlq, which alarms) -> DynamoDB
# security-alert-digest (seen markers and pending alerts) -> SNS cost-alerts. A one-minute schedule runs the same
# Lambda to send what is due. scripts/security-alert-digest.py has the rules:
#   * dedupe: one email per finding identity when first seen, again only after 7 days without updates;
#   * at once: GuardDuty findings, CloudTrail audit events (root use, failed console sign-in, the tamper and IAM list),
#     Access Analyzer findings;
#   * digest, at most one every 10 minutes: Security Hub control findings, Inspector CVEs and anything else;
#   * never more than one message a minute, whatever comes in; a flood becomes one message saying "N more".
# The security delivery observer (security-monitoring.tf) checks this pipeline as part of the alert topology: the
# rule's target is this queue, the queue feeds this function, the function sends to cost-alerts, the flush schedule
# exists, and this DLQ is empty.
#
# Everything else that publishes to cost-alerts (CloudWatch alarms, budgets, other Lambdas, access-audit-notify) is
# unchanged. The alarms below go to cost-alerts too: they are single alarm messages, not the flood.

locals {
  security_alert_digest_timeout = 120
}

resource "aws_dynamodb_table" "security_alert_digest" {
  name         = "security-alert-digest"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk"
  range_key    = "sk"

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
  # Seen markers only; pending alerts carry no expiry and stay until they are mailed.
  ttl {
    attribute_name = "expires"
    enabled        = true
  }
  tags = { Name = "security-alert-digest", Purpose = "security-delivery" }
}

resource "aws_sqs_queue" "security_alert_digest_dlq" {
  name                      = "security-alert-digest-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
  tags                      = { Managed = "terraform", Purpose = "security-delivery" }
}

resource "aws_sqs_queue" "security_alert_digest_inbox" {
  name                      = "security-alert-digest-inbox"
  message_retention_seconds = 1209600
  # At least six times the function timeout, as AWS advises for an SQS event source.
  visibility_timeout_seconds = 6 * local.security_alert_digest_timeout + 180
  sqs_managed_sse_enabled    = true
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.security_alert_digest_dlq.arn
    maxReceiveCount     = 5
  })
  tags = { Managed = "terraform", Purpose = "security-delivery" }
}

resource "aws_sqs_queue_redrive_allow_policy" "security_alert_digest_dlq" {
  queue_url = aws_sqs_queue.security_alert_digest_dlq.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.security_alert_digest_inbox.arn]
  })
}

resource "aws_sqs_queue_policy" "security_alert_digest_inbox" {
  queue_url = aws_sqs_queue.security_alert_digest_inbox.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Only the central notification rule may write here.
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.security_alert_digest_inbox.arn
        Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.security_notify.arn } }
      },
      {
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = aws_sqs_queue.security_alert_digest_inbox.arn
        Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
      }
    ]
  })
}

data "archive_file" "security_alert_digest" {
  type        = "zip"
  output_path = "${path.module}/.terraform/security-alert-digest.zip"
  source {
    filename = "index.py"
    content  = file("${path.module}/scripts/security-alert-digest.py")
  }
}

resource "aws_cloudwatch_log_group" "security_alert_digest" {
  name              = "/aws/lambda/security-alert-digest"
  retention_in_days = 30
}

resource "aws_iam_role" "security_alert_digest" {
  name = "security-alert-digest"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "security_alert_digest" {
  name = "digest-security-alerts"
  role = aws_iam_role.security_alert_digest.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # TransactWriteItems is authorized as its UpdateItem and PutItem; BatchWriteItem deletes mailed items.
        Sid      = "ItsOwnState"
        Effect   = "Allow"
        Action   = ["dynamodb:UpdateItem", "dynamodb:PutItem", "dynamodb:Query", "dynamodb:BatchWriteItem"]
        Resource = aws_dynamodb_table.security_alert_digest.arn
      },
      {
        Sid      = "ItsInbox"
        Effect   = "Allow"
        Action   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:ChangeMessageVisibility", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.security_alert_digest_inbox.arn
      },
      {
        Sid      = "Tell"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      },
      {
        # #577: to tell AWS Backup's own air-gapped-copy snapshot share from any other, a held Access Analyzer snapshot
        # finding is checked against the snapshot and CloudTrail. Read-only; these calls take no resource-level scope.
        Sid      = "CheckBackupCopyShares"
        Effect   = "Allow"
        Action   = ["ec2:DescribeSnapshots", "ec2:DescribeSnapshotAttribute", "cloudtrail:LookupEvents"]
        Resource = "*"
      },
      {
        Sid      = "ItsLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.security_alert_digest.arn}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "security_alert_digest" {
  function_name    = "security-alert-digest"
  role             = aws_iam_role.security_alert_digest.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = local.security_alert_digest_timeout
  memory_size      = 256
  filename         = data.archive_file.security_alert_digest.output_path
  source_code_hash = data.archive_file.security_alert_digest.output_base64sha256

  environment {
    variables = {
      STATE_TABLE   = aws_dynamodb_table.security_alert_digest.name
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
      # #577: where AWS Backup copies into the air-gapped vault (backup-security.tf); only those snapshot findings are held.
      BACKUP_COPY_ACCOUNT = data.aws_caller_identity.current.account_id
      BACKUP_COPY_REGIONS = "us-west-1,us-east-1"
      # Shown next to account ids in the emails. The Dolphin account is left out on purpose (test-dolphin-account.py:
      # nothing here references it); its alerts show the bare id.
      ACCOUNT_NAMES = jsonencode({
        (data.aws_caller_identity.current.account_id) = "main"
        (aws_organizations_account.dev_staging[0].id) = "dev-staging"
      })
    }
  }
  depends_on = [aws_iam_role_policy.security_alert_digest, aws_cloudwatch_log_group.security_alert_digest]
  tags       = { Name = "security-alert-digest" }
}

resource "aws_lambda_event_source_mapping" "security_alert_digest" {
  event_source_arn                   = aws_sqs_queue.security_alert_digest_inbox.arn
  function_name                      = aws_lambda_function.security_alert_digest.arn
  batch_size                         = 100
  maximum_batching_window_in_seconds = 30
  function_response_types            = ["ReportBatchItemFailures"]
  scaling_config {
    maximum_concurrency = 2
  }
}

# Sends what is due: digests, and immediate alerts held back by the one-a-minute cap.
resource "aws_cloudwatch_event_rule" "security_alert_digest_flush" {
  name                = "security-alert-digest-flush"
  description         = "Send due security alert digests (security-alert-digest.tf)"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "security_alert_digest_flush" {
  rule      = aws_cloudwatch_event_rule.security_alert_digest_flush.name
  target_id = "security-alert-digest-flush"
  arn       = aws_lambda_function.security_alert_digest.arn
  retry_policy {
    maximum_event_age_in_seconds = 300
    maximum_retry_attempts       = 2
  }
  dead_letter_config { arn = aws_sqs_queue.security_delivery_failures.arn }
  depends_on = [aws_lambda_permission.security_alert_digest_flush, aws_sqs_queue_policy.security_delivery_failures]
}

resource "aws_lambda_permission" "security_alert_digest_flush" {
  statement_id  = "AllowScheduledDigestFlush"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.security_alert_digest.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.security_alert_digest_flush.arn
}

# No Lambda retry of a failed scheduled run: the next one is a minute away, and the send lease makes an overlap harmless.
resource "aws_lambda_function_event_invoke_config" "security_alert_digest" {
  function_name                = aws_lambda_function.security_alert_digest.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

resource "aws_cloudwatch_metric_alarm" "security_alert_digest_errors" {
  alarm_name          = "security-alert-digest-errors"
  alarm_description   = "security-alert-digest failed: security alerts may be waiting unsent in its table. Read /aws/lambda/security-alert-digest."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.security_alert_digest.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

# The schedule runs it every minute; no invocation in 15 minutes means nothing is sending.
resource "aws_cloudwatch_metric_alarm" "security_alert_digest_not_running" {
  alarm_name          = "security-alert-digest-not-running"
  alarm_description   = "security-alert-digest has not run for 15 minutes (its schedule is every minute): security alerts are not being sent."
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.security_alert_digest.function_name }
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "security_alert_digest_dlq" {
  alarm_name          = "security-alert-digest-dlq"
  alarm_description   = "Security alerts the digest could not record are in security-alert-digest-dlq: read them and redrive to security-alert-digest-inbox."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.security_alert_digest_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

# The inbox drains within a minute when the function is healthy; an old message means the event source is stuck.
resource "aws_cloudwatch_metric_alarm" "security_alert_digest_inbox_age" {
  alarm_name          = "security-alert-digest-inbox-age"
  alarm_description   = "security-alert-digest-inbox holds a message older than 30 minutes: the digest is not reading its queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateAgeOfOldestMessage"
  dimensions          = { QueueName = aws_sqs_queue.security_alert_digest_inbox.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1800
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

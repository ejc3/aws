# Periodic cleanup outlives a lease and retries through Apple Silicon host scrubbing
# (up to 4.5h). A stopped/terminated instance is never reported as a released host.
# No stale calendar timestamp, active-lease gate, or permission to allocate a host.
data "archive_file" "macpbox_cleanup" {
  type        = "zip"
  output_path = "${path.module}/.terraform/macpbox-cleanup.zip"
  source {
    content  = file("${path.module}/scripts/macpbox-cleanup.py")
    filename = "index.py"
  }
}

resource "aws_iam_role" "macpbox_cleanup" {
  provider = aws.west2
  name     = "macpbox-cleanup"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "macpbox_cleanup_basic" {
  provider   = aws.west2
  role       = aws_iam_role.macpbox_cleanup.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "macpbox_cleanup" {
  provider = aws.west2
  name     = "release-expired-macpbox-only"
  role     = aws_iam_role.macpbox_cleanup.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances", "ec2:DescribeHosts"]
        Resource = "*"
        Condition = {
          StringEquals = { "ec2:Region" = local.macpbox_region }
        }
      },
      {
        Effect   = "Allow"
        Action   = "ec2:TerminateInstances"
        Resource = "arn:aws:ec2:${local.macpbox_region}:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/Name"    = "macpbox"
            "ec2:ResourceTag/Purpose" = "macpbox"
            "ec2:InstanceType"        = local.macpbox_type
          }
          Null = { "ec2:ResourceTag/LeaseId" = "false" }
        }
      },
      {
        Effect   = "Allow"
        Action   = "ec2:ReleaseHosts"
        Resource = "arn:aws:ec2:${local.macpbox_region}:${data.aws_caller_identity.current.account_id}:dedicated-host/*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/Name"       = "macpbox"
            "ec2:ResourceTag/Purpose"    = "macpbox"
            "ec2:ResourceTag/LeaseHours" = "24"
          }
          Null = { "ec2:ResourceTag/LeaseId" = "false" }
        }
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "macpbox_cleanup" {
  provider          = aws.west2
  name              = "/aws/lambda/macpbox-cleanup"
  retention_in_days = 14
}

resource "aws_lambda_function" "macpbox_cleanup" {
  provider                       = aws.west2
  function_name                  = "macpbox-cleanup"
  role                           = aws_iam_role.macpbox_cleanup.arn
  handler                        = "index.lambda_handler"
  runtime                        = "python3.12"
  timeout                        = 60
  reserved_concurrent_executions = 1
  filename                       = data.archive_file.macpbox_cleanup.output_path
  source_code_hash               = data.archive_file.macpbox_cleanup.output_base64sha256
  environment {
    variables = {
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
      SNS_REGION    = var.aws_region
    }
  }
  depends_on = [
    aws_cloudwatch_log_group.macpbox_cleanup, aws_iam_role_policy.macpbox_cleanup,
    aws_iam_role_policy_attachment.macpbox_cleanup_basic
  ]
  tags = { Name = "macpbox-cleanup", Purpose = "macpbox" }
}

resource "aws_cloudwatch_event_rule" "macpbox_cleanup" {
  provider            = aws.west2
  name                = "macpbox-cleanup"
  schedule_expression = "rate(10 minutes)"
}

resource "aws_cloudwatch_event_target" "macpbox_cleanup" {
  provider = aws.west2
  rule     = aws_cloudwatch_event_rule.macpbox_cleanup.name
  arn      = aws_lambda_function.macpbox_cleanup.arn
}

resource "aws_lambda_permission" "macpbox_cleanup" {
  provider      = aws.west2
  statement_id  = "AllowScheduledMacpboxCleanup"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.macpbox_cleanup.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.macpbox_cleanup.arn
}

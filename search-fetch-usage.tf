# Search and fetch provider usage as CloudWatch metrics (owner, 2026-10-18: "see if these providers have APIs for usage
# and if so, create a workflow that scrapes them and updates CloudWatch metrics that we can graph").
#
# search-fetch-usage (scripts/search-fetch-usage.py) runs every 30 minutes and publishes namespace SearchFetch, dimension
# Provider:
#   deepseek     GET /user/balance                Available, BalanceTotal/Granted/ToppedUp {Currency}
#   scrapingdog  GET /account                     CreditsLimit, CreditsUsed, CreditsRemaining(+Fraction), ThreadsInFlight,
#                                                 ConcurrencyLimit, ValidityDays
#   browserbase  GET /v1/projects/{id}/usage,     BrowserMinutes, ProxyBytes, ConcurrencyLimit, SessionsRunning
#                /v1/projects, /v1/sessions
#   every one    ProviderUp (1 or 0)
# Exa is not polled: it has no balance endpoint, and its usage endpoint needs a team service key Exa enables on request.
# Keys come from the providers' secrets (dev-ai-services.tf), which list this role among their readers. Responses are
# never logged (ScrapingDog's echoes the key). Alarms go to the shared alert topic.

locals {
  search_fetch_usage_namespace = "SearchFetch"
}

data "archive_file" "search_fetch_usage" {
  type        = "zip"
  output_path = "${path.module}/.terraform/search-fetch-usage.zip"
  source {
    filename = "index.py"
    content  = file("${path.module}/scripts/search-fetch-usage.py")
  }
}

resource "aws_iam_role" "search_fetch_usage" {
  name = "search-fetch-usage"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "search_fetch_usage_basic" {
  role       = aws_iam_role.search_fetch_usage.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "search_fetch_usage" {
  name = "search-fetch-usage"
  role = aws_iam_role.search_fetch_usage.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadTheProviderKeys"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = [
          aws_secretsmanager_secret.search_fetch_api_key["deepseek"].arn,
          aws_secretsmanager_secret.search_fetch_api_key["scrapingdog"].arn,
          aws_secretsmanager_secret.browserbase.arn,
        ]
      },
      {
        # PutMetricData cannot be resource scoped; the namespace condition is the scope.
        Sid       = "PublishOnlyItsNamespace"
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = local.search_fetch_usage_namespace } }
      },
    ]
  })
}

resource "aws_lambda_function" "search_fetch_usage" {
  function_name    = "search-fetch-usage"
  role             = aws_iam_role.search_fetch_usage.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 120
  filename         = data.archive_file.search_fetch_usage.output_path
  source_code_hash = data.archive_file.search_fetch_usage.output_base64sha256

  environment {
    variables = {
      METRIC_NAMESPACE      = local.search_fetch_usage_namespace
      DEEPSEEK_SECRET_ID    = aws_secretsmanager_secret.search_fetch_api_key["deepseek"].name
      SCRAPINGDOG_SECRET_ID = aws_secretsmanager_secret.search_fetch_api_key["scrapingdog"].name
      BROWSERBASE_SECRET_ID = aws_secretsmanager_secret.browserbase.name
    }
  }

  tags = { Name = "search-fetch-usage" }

  # The group exists before the first run, so Lambda never creates it outside Terraform.
  depends_on = [aws_cloudwatch_log_group.search_fetch_usage]
}

resource "aws_cloudwatch_log_group" "search_fetch_usage" {
  name              = "/aws/lambda/search-fetch-usage"
  retention_in_days = 30
}

resource "aws_cloudwatch_event_rule" "search_fetch_usage" {
  name                = "search-fetch-usage-every-30-minutes"
  description         = "Poll the search and fetch providers' usage and balance into CloudWatch"
  schedule_expression = "rate(30 minutes)"
}

resource "aws_cloudwatch_event_target" "search_fetch_usage" {
  rule = aws_cloudwatch_event_rule.search_fetch_usage.name
  arn  = aws_lambda_function.search_fetch_usage.arn
}

resource "aws_lambda_function_event_invoke_config" "search_fetch_usage" {
  function_name          = aws_lambda_function.search_fetch_usage.function_name
  maximum_retry_attempts = 0
}

resource "aws_lambda_permission" "search_fetch_usage" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.search_fetch_usage.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.search_fetch_usage.arn
}

resource "aws_cloudwatch_metric_alarm" "search_fetch_usage_errors" {
  alarm_name          = "search-fetch-usage-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 1800
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "search-fetch-usage failed two runs in a row: a provider's usage API is unreachable or changed (ProviderUp says which)."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.search_fetch_usage.function_name }
}

resource "aws_cloudwatch_metric_alarm" "search_fetch_usage_not_running" {
  alarm_name          = "search-fetch-usage-not-running"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 7200
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "search-fetch-usage has not run for two hours (it runs every 30 minutes): provider balances are not being watched."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "breaching"
  dimensions          = { FunctionName = aws_lambda_function.search_fetch_usage.function_name }
}

resource "aws_cloudwatch_metric_alarm" "search_fetch_deepseek_balance_low" {
  alarm_name          = "search-fetch-deepseek-balance-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "BalanceTotal"
  namespace           = local.search_fetch_usage_namespace
  period              = 3600
  statistic           = "Minimum"
  threshold           = 10
  alarm_description   = "DeepSeek's balance is under $10: top it up or calls start failing with 402."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  dimensions          = { Provider = "deepseek", Currency = "USD" }
}

resource "aws_cloudwatch_metric_alarm" "search_fetch_scrapingdog_credits_low" {
  alarm_name          = "search-fetch-scrapingdog-credits-low"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "CreditsRemainingFraction"
  namespace           = local.search_fetch_usage_namespace
  period              = 3600
  statistic           = "Minimum"
  threshold           = 0.1
  alarm_description   = "ScrapingDog has under 10% of its period's credits left."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  dimensions          = { Provider = "scrapingdog" }
}

resource "aws_cloudwatch_dashboard" "search_fetch" {
  dashboard_name = "search-fetch"
  dashboard_body = jsonencode({
    widgets = [
      for i, w in [
        { title = "DeepSeek balance (USD)", metrics = [[local.search_fetch_usage_namespace, "BalanceTotal", "Provider", "deepseek", "Currency", "USD"], [".", "BalanceGranted", ".", ".", ".", "."], [".", "BalanceToppedUp", ".", ".", ".", "."]] },
        { title = "ScrapingDog credits", metrics = [[local.search_fetch_usage_namespace, "CreditsRemaining", "Provider", "scrapingdog"], [".", "CreditsUsed", ".", "."], [".", "CreditsLimit", ".", "."]] },
        { title = "ScrapingDog concurrency", metrics = [[local.search_fetch_usage_namespace, "ThreadsInFlight", "Provider", "scrapingdog"], [".", "ConcurrencyLimit", ".", "."]] },
        { title = "Browserbase usage", metrics = [[local.search_fetch_usage_namespace, "BrowserMinutes", "Provider", "browserbase"], [".", "ProxyBytes", ".", ".", { yAxis = "right" }]] },
        { title = "Browserbase sessions", metrics = [[local.search_fetch_usage_namespace, "SessionsRunning", "Provider", "browserbase"], [".", "ConcurrencyLimit", ".", "."]] },
        { title = "Provider APIs up", metrics = [[local.search_fetch_usage_namespace, "ProviderUp", "Provider", "deepseek"], [".", ".", ".", "scrapingdog"], [".", ".", ".", "browserbase"]] },
        ] : {
        type   = "metric"
        x      = (i % 2) * 12
        y      = floor(i / 2) * 6
        width  = 12
        height = 6
        properties = {
          title   = w.title
          region  = var.aws_region
          stat    = "Maximum"
          period  = 1800
          view    = "timeSeries"
          metrics = w.metrics
        }
      }
    ]
  })
}

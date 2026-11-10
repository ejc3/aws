# dolphin-budgets.tf
#
# The Dolphin account's own cost budgets, on dolphin-alerts (dolphin-labs#1283's monitoring audit). The account had
# none, and its spend went from about $9 a day in October to $45-69 a day from 2026-11-06 (the podcast pipeline's GPU
# and Haiku) without anything saying so. The podcast pipeline caps its own spend (dolphin-aws podcasts.tf); these watch
# the whole account. They live here, beside the topic's policy, so the Budgets statement that lets them publish
# (dolphin-security.tf, DolphinBudgets) is in place in the same apply. Notifications reach the topic's email
# subscription (dolphin-alerts.tf); the GitHub router in dolphin-aws passes over anything not a CloudWatch alarm.
# Budgets reads costs a few hours late.

resource "aws_budgets_budget" "dolphin_daily" {
  provider     = aws.dolphin
  name         = "dolphin-daily"
  budget_type  = "COST"
  limit_amount = "100"
  limit_unit   = "USD"
  time_unit    = "DAILY"

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [data.aws_sns_topic.dolphin_alerts.arn]
  }

  depends_on = [aws_sns_topic_policy.dolphin_alerts]
}

resource "aws_budgets_budget" "dolphin_monthly" {
  provider     = aws.dolphin
  name         = "dolphin-monthly"
  budget_type  = "COST"
  limit_amount = "1500"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "FORECASTED"
    subscriber_sns_topic_arns = [data.aws_sns_topic.dolphin_alerts.arn]
  }
  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [data.aws_sns_topic.dolphin_alerts.arn]
  }

  depends_on = [aws_sns_topic_policy.dolphin_alerts]
}

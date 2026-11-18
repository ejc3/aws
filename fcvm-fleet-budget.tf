# fcvm-fleet-budget.tf
#
# A spending ceiling for the public fleet: $750 a day (owner, 2026-11-18), on the fleet's GPU hosts and EC2 tiles.
#
# This is the BACKSTOP. The primary enforcement is the coordinator sleeping the fleet from its own instance-hour
# estimate, which is fcvm's work (ejc3/fcvm, separately). AWS Budgets reads billing data, which AWS updates up to three
# times a day and which can lag a day, so by the time this budget sees $750 for a day the fleet has spent more, and an
# overrun late in a UTC day may only be seen the next day. It cannot be the only limit.
#
# - Scope: costs tagged Purpose=fcvm-fleet-host or Purpose=fcvm-fleet-tile, the tags every host and tile launch template
#   puts on its instance (modules/fcvm-fleet-region). The gpubox test hosts (Purpose=fcvm-fleet-gpubox) and the AMI
#   builders (fcvm-fleet-ami-build) run in the same account, so an account-wide budget would stop the wall for their
#   spend. Not counted: the coordinator's Fargate task, the DNS load balancer, S3, Bedrock and the hosts' and tiles'
#   untagged EBS volumes, together a few dollars a day.
# - Purpose must be an active cost allocation tag, which only the management account can set; it applies to every member
#   account. Tags count only for costs after activation, and activation takes up to 24 hours.
# - At $600 and $750, notify the fleet topic. Daily budgets evaluate completed UTC days and do not support
#   native budget actions. The reconciler reads the latest three complete UTC billing periods. A previously
#   unseen daily actual at or above $750 blocks new launches through detection day's next UTC midnight.
#   Stop, terminate and describe remain available. Intraday control belongs to the coordinator.
# - One serialized worker owns the deny attachment. The midnight reset checks its durable decision without
#   reading billing; the five-minute reconciliation also releases an expired deny after delivery or IAM outages.
#   Recorded billed days prevent duplicate or delayed messages from extending the block.

locals {
  fcvm_fleet_daily_budget_usd = 750
  fcvm_fleet_budget_purposes  = ["fcvm-fleet-host", "fcvm-fleet-tile"]
}

resource "aws_ce_cost_allocation_tag" "purpose" {
  tag_key = "Purpose"
  status  = "Active"

  lifecycle {
    # The provider deactivates this tag for every account in the organization on deletion.
    prevent_destroy = true
  }
}

resource "aws_budgets_budget" "fcvm_fleet_daily" {
  provider     = aws.fcvm_fleet
  name         = "fcvm-fleet-daily"
  budget_type  = "COST"
  limit_amount = tostring(local.fcvm_fleet_daily_budget_usd)
  limit_unit   = "USD"
  time_unit    = "DAILY"

  cost_filter {
    name   = "TagKeyValue"
    values = [for p in local.fcvm_fleet_budget_purposes : format("user:Purpose$%s", p)]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 80
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = local.fcvm_fleet_alert_topic_arns
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = local.fcvm_fleet_alert_topic_arns
  }

  lifecycle {
    precondition {
      condition     = length(local.fcvm_fleet_alert_topic_arns) > 0
      error_message = "The fleet's daily budget needs the fcvm-fleet-alerts topic, which exists only in a region with a GPU host template."
    }
  }

  depends_on = [aws_ce_cost_allocation_tag.purpose]
}

# Attached to the coordinator's task role only by the daily billing reconciler. A deny wins over the role's own grants.
resource "aws_iam_policy" "fcvm_fleet_spend_cap" {
  provider    = aws.fcvm_fleet
  name        = "fcvm-fleet-spend-cap"
  description = "Attached to fcvm-fleet-coordinator by the daily billing reconciler: no new GPU hosts or EC2 tiles"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "NoNewFleetSpend"
      Effect   = "Deny"
      Action   = ["ec2:RunInstances", "ec2:CreateFleet", "ec2:StartInstances"]
      Resource = "*"
    }]
  })
  tags = { Project = "fcvm-fleet" }
}

resource "aws_s3_object" "fcvm_fleet_spend_cap_state" {
  provider     = aws.fcvm_fleet
  bucket       = aws_s3_bucket.fcvm_fleet_coordinator_state.id
  key          = "spend-cap/daily.json"
  content      = jsonencode({ blocked_on = "", billed_days = [] })
  content_type = "application/json"
  lifecycle {
    ignore_changes  = [content]
    prevent_destroy = true
  }
}

resource "aws_iam_role" "fcvm_fleet_budget_action" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-budget-action"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_cloudwatch_log_group" "fcvm_fleet_budget_action" {
  provider          = aws.fcvm_fleet
  name              = "/aws/lambda/fcvm-fleet-spend-cap"
  retention_in_days = 14
}

resource "aws_iam_role_policy" "fcvm_fleet_budget_action" {
  provider = aws.fcvm_fleet
  name     = "reconcile-fcvm-fleet-spend-cap"
  role     = aws_iam_role.fcvm_fleet_budget_action.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "CapTheCoordinator"
        Effect    = "Allow"
        Action    = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
        Resource  = aws_iam_role.fcvm_fleet_coordinator_task.arn
        Condition = { ArnEquals = { "iam:PolicyARN" = aws_iam_policy.fcvm_fleet_spend_cap.arn } }
      },
      {
        Sid      = "CheckAttachment"
        Effect   = "Allow"
        Action   = "iam:ListAttachedRolePolicies"
        Resource = aws_iam_role.fcvm_fleet_coordinator_task.arn
      },
      {
        Sid      = "ReadFleetBudget"
        Effect   = "Allow"
        Action   = "budgets:ViewBudget"
        Resource = aws_budgets_budget.fcvm_fleet_daily.arn
      },
      {
        Sid      = "RememberDailyDecisions"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/${aws_s3_object.fcvm_fleet_spend_cap_state.key}"
      },
      {
        Sid      = "LogReconciliation"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.fcvm_fleet_budget_action.arn}:*"
      },
    ]
  })
}

data "archive_file" "fcvm_fleet_budget_action" {
  type = "zip"
  source {
    filename = "index.py"
    content  = file("${path.module}/scripts/fcvm-fleet-spend-cap.py")
  }
  output_path = "${path.module}/.terraform/fcvm-fleet-spend-cap.zip"
}

resource "aws_lambda_function" "fcvm_fleet_budget_action" {
  provider                       = aws.fcvm_fleet
  function_name                  = "fcvm-fleet-spend-cap"
  role                           = aws_iam_role.fcvm_fleet_budget_action.arn
  handler                        = "index.handler"
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  filename                       = data.archive_file.fcvm_fleet_budget_action.output_path
  source_code_hash               = data.archive_file.fcvm_fleet_budget_action.output_base64sha256
  timeout                        = 45
  reserved_concurrent_executions = 1
  environment {
    variables = {
      ACCOUNT_ID   = local.fcvm_fleet_account_id
      BUDGET_NAME  = aws_budgets_budget.fcvm_fleet_daily.name
      LIMIT_USD    = tostring(local.fcvm_fleet_daily_budget_usd)
      STATE_BUCKET = aws_s3_bucket.fcvm_fleet_coordinator_state.id
      STATE_KEY    = aws_s3_object.fcvm_fleet_spend_cap_state.key
      ROLE_NAME    = aws_iam_role.fcvm_fleet_coordinator_task.name
      POLICY_ARN   = aws_iam_policy.fcvm_fleet_spend_cap.arn
    }
  }
  depends_on = [aws_iam_role_policy.fcvm_fleet_budget_action]
}

locals {
  fcvm_fleet_budget_ticks = {
    reconcile = "cron(2/5 * * * ? *)"
    expire    = "cron(0 0 * * ? *)"
  }
}

resource "aws_cloudwatch_event_rule" "fcvm_fleet_spend_cap" {
  for_each            = local.fcvm_fleet_budget_ticks
  provider            = aws.fcvm_fleet
  name                = "fcvm-fleet-spend-cap-${each.key}"
  schedule_expression = each.value
}

resource "aws_lambda_permission" "fcvm_fleet_spend_cap" {
  for_each      = aws_cloudwatch_event_rule.fcvm_fleet_spend_cap
  provider      = aws.fcvm_fleet
  statement_id  = "FleetBudget-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fcvm_fleet_budget_action.function_name
  principal     = "events.amazonaws.com"
  source_arn    = each.value.arn
}

resource "aws_cloudwatch_event_target" "fcvm_fleet_spend_cap" {
  for_each   = aws_cloudwatch_event_rule.fcvm_fleet_spend_cap
  provider   = aws.fcvm_fleet
  rule       = each.value.name
  arn        = aws_lambda_function.fcvm_fleet_budget_action.arn
  input      = jsonencode({ action = each.key })
  depends_on = [aws_lambda_permission.fcvm_fleet_spend_cap]
}

# A successful run every five minutes is expected even on days below budget. Missing invocations,
# throttling that prevents any execution, and repeated worker errors all need the fleet operator.
resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_spend_cap_unhealthy" {
  provider            = aws.fcvm_fleet
  alarm_name          = "fcvm-fleet-spend-cap-unhealthy"
  alarm_description   = "No successful spend-cap reconciliation in 15 minutes. Check the fcvm-fleet-spend-cap Lambda in us-east-2 and the coordinator role's cap attachment."
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  evaluation_periods  = 1
  treat_missing_data  = "breaching"
  metric_query {
    id          = "success"
    expression  = "FILL(calls, 0) - FILL(errors, 0)"
    return_data = true
  }
  dynamic "metric_query" {
    for_each = { calls = "Invocations", errors = "Errors" }
    content {
      id = metric_query.key
      metric {
        metric_name = metric_query.value
        namespace   = "AWS/Lambda"
        period      = 900
        stat        = "Sum"
        dimensions  = { FunctionName = aws_lambda_function.fcvm_fleet_budget_action.function_name }
      }
    }
  }
  alarm_actions = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]
  ok_actions    = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]
  depends_on    = [aws_lambda_permission.fcvm_fleet_health_alarms]
}

output "fcvm_fleet_budget" {
  description = "The fleet's daily spend backstop (fcvm-fleet-budget.tf)"
  value = {
    budget       = aws_budgets_budget.fcvm_fleet_daily.name
    limit_usd    = local.fcvm_fleet_daily_budget_usd
    cap_policy   = aws_iam_policy.fcvm_fleet_spend_cap.name
    applies_to   = aws_iam_role.fcvm_fleet_coordinator_task.name
    tag_purposes = local.fcvm_fleet_budget_purposes
  }
}

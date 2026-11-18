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
# - At 80% ($600) and 100% ($750) of actual daily spend, a notice to fcvm-fleet-alerts (the fleet's email subscriber; see
#   fcvm-fleet-alarms.tf for why there is no pager). At 100%, AWS Budgets attaches fcvm-fleet-spend-cap to the
#   coordinator's task role: no new host or tile launches and no tile pool starts. Terminating, stopping, describing and
#   everything else stay allowed, so the coordinator can still sleep the fleet and serve the hosts already running. Its
#   launch attempts then fail, which fcvm-fleet-coordinator-degraded reports.
# - AWS documents that IAM policy actions reset at the start of each budget period (here each UTC day), not whether the
#   reset detaches the policy. After the cap trips, check the coordinator role's attached policies; to lift it, reverse
#   the action (`aws budgets execute-budget-action ... --execution-type REVERSE_BUDGET_ACTION`) as an administrator of
#   the fleet account. Reversing it stops the action from firing again until the next period.

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

# Attached to the coordinator's task role by the budget action. A deny wins over the role's own grants.
resource "aws_iam_policy" "fcvm_fleet_spend_cap" {
  provider    = aws.fcvm_fleet
  name        = "fcvm-fleet-spend-cap"
  description = "Attached to fcvm-fleet-coordinator by the fcvm-fleet-daily budget action: no new GPU hosts or EC2 tiles"
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

resource "aws_iam_role" "fcvm_fleet_budget_action" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-budget-action"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "budgets.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.fcvm_fleet_account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:budgets::${local.fcvm_fleet_account_id}:budget/*" }
      }
    }]
  })
  tags = { Project = "fcvm-fleet" }
}

# Attach and detach the cap on the coordinator's task role, and nothing else.
resource "aws_iam_role_policy" "fcvm_fleet_budget_action" {
  provider = aws.fcvm_fleet
  name     = "attach-fcvm-fleet-spend-cap"
  role     = aws_iam_role.fcvm_fleet_budget_action.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CapTheCoordinator"
      Effect    = "Allow"
      Action    = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
      Resource  = aws_iam_role.fcvm_fleet_coordinator_task.arn
      Condition = { ArnEquals = { "iam:PolicyARN" = aws_iam_policy.fcvm_fleet_spend_cap.arn } }
    }]
  })
}

resource "aws_budgets_budget_action" "fcvm_fleet_daily" {
  provider           = aws.fcvm_fleet
  budget_name        = aws_budgets_budget.fcvm_fleet_daily.name
  action_type        = "APPLY_IAM_POLICY"
  approval_model     = "AUTOMATIC"
  notification_type  = "ACTUAL"
  execution_role_arn = aws_iam_role.fcvm_fleet_budget_action.arn

  action_threshold {
    action_threshold_type  = "PERCENTAGE"
    action_threshold_value = 100
  }

  definition {
    iam_action_definition {
      policy_arn = aws_iam_policy.fcvm_fleet_spend_cap.arn
      roles      = [aws_iam_role.fcvm_fleet_coordinator_task.name]
    }
  }

  dynamic "subscriber" {
    for_each = local.fcvm_fleet_alert_topic_arns
    content {
      address           = subscriber.value
      subscription_type = "SNS"
    }
  }

  depends_on = [aws_iam_role_policy.fcvm_fleet_budget_action]
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

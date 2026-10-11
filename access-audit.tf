# Host access audit. Registered hosts report opens of registered paths
# (scripts/access-audit.py); registered principals report any AWS API use.
# Which hosts, paths and principals are registered is private operator data:
# it lives only in the access-audit/config secret and Terraform state, never in
# this repository. Rollout is in docs/access-audit.md.

variable "enable_access_audit" {
  description = "Read access-audit/config and build the audit for the hosts it registers. Populate the secret first."
  type        = bool
  default     = false
}

locals {
  access_audit_namespace = "AccessAudit"
  access_audit_log_group = "/security/access-audit"
  # { "hosts": { "<host>": { "principal": "<iam user name>", "files": [...], "ignore_executables": [...],
  #                          "service_name": "<optional>", "launchd_label": "<optional>" } } }
  # Decode first and branch on the string, so hosts with different optional keys never have to
  # unify with an empty object in a conditional.
  access_audit_hosts = jsondecode(var.enable_access_audit ? data.aws_secretsmanager_secret_version.access_audit_config[0].secret_string : "{\"hosts\":{}}").hosts
  # The registry arrives marked sensitive (secret_string), and for_each refuses a sensitive
  # collection. Only the host names are unmarked, to key instances; every value stays sensitive.
  access_audit_host_keys      = nonsensitive(toset(keys(local.access_audit_hosts)))
  access_audit_principal_arns = sort([for user in aws_iam_user.access_audit_principal : user.arn])
  access_audit_forward_pattern = jsonencode({
    "detail-type" = ["AWS API Call via CloudTrail", "AWS Console Sign In via CloudTrail"]
    detail        = { userIdentity = { arn = local.access_audit_principal_arns } }
  })
}

resource "aws_secretsmanager_secret" "access_audit_config" {
  name                    = "access-audit/config"
  description             = "Private host access audit registry (hosts, principals, paths)"
  recovery_window_in_days = 7
}

data "aws_secretsmanager_secret_version" "access_audit_config" {
  count     = var.enable_access_audit ? 1 : 0
  secret_id = aws_secretsmanager_secret.access_audit_config.id
}

# Registered principals: real keys that are allowed to do nothing. Every call
# they make is still recorded by CloudTrail, which is what the rules below need.
resource "aws_iam_user" "access_audit_principal" {
  for_each = local.access_audit_host_keys
  name     = local.access_audit_hosts[each.key].principal

  lifecycle {
    precondition {
      condition     = can(regex("^[a-z0-9][a-z0-9-]{0,39}$", each.key)) && can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", local.access_audit_hosts[each.key].principal))
      error_message = "access-audit/config host names must be lowercase DNS labels and principals valid IAM user names."
    }
  }
}

resource "aws_iam_user_policy" "access_audit_principal" {
  for_each = aws_iam_user.access_audit_principal
  name     = "default"
  user     = each.value.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Deny"
      Action   = "*"
      Resource = "*"
    }]
  })
}

resource "aws_iam_access_key" "access_audit_principal" {
  for_each = aws_iam_user.access_audit_principal
  user     = each.value.name
}

# Reporters: one credential per host, able only to write that host's audit
# stream, the audit namespace, and the operator topic.
resource "aws_iam_user" "access_audit_reporter" {
  for_each = local.access_audit_host_keys
  name     = "access-audit-${each.key}"
}

resource "aws_iam_user_policy" "access_audit_reporter" {
  for_each = aws_iam_user.access_audit_reporter
  name     = "report"
  user     = each.value.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.access_audit.arn}:log-stream:${each.key}"
      },
      {
        Effect    = "Allow"
        Action    = ["cloudwatch:PutMetricData"]
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = local.access_audit_namespace } }
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = aws_sns_topic.cost_alerts.arn
      },
    ]
  })
}

resource "aws_iam_access_key" "access_audit_reporter" {
  for_each = aws_iam_user.access_audit_reporter
  user     = each.value.name
}

resource "aws_cloudwatch_log_group" "access_audit" {
  name              = local.access_audit_log_group
  retention_in_days = 365
}

resource "aws_cloudwatch_log_stream" "access_audit" {
  for_each       = local.access_audit_host_keys
  name           = each.key
  log_group_name = aws_cloudwatch_log_group.access_audit.name
}

# Per-host install bundle, read on an admin host and piped to the installer.
resource "aws_secretsmanager_secret" "access_audit_bundles" {
  name                    = "access-audit/bundles"
  description             = "Per-host access audit install bundles (generated)"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "access_audit_bundles" {
  count     = var.enable_access_audit ? 1 : 0
  secret_id = aws_secretsmanager_secret.access_audit_bundles.id
  # service_name and launchd_label name the on-host install; they are sent only when the
  # registry sets them (the agent rejects a null), so an unset host takes the agent's default.
  secret_string = jsonencode({ for host, entry in local.access_audit_hosts : host => merge({
    version            = 1
    host               = host
    region             = var.aws_region
    topic_arn          = aws_sns_topic.cost_alerts.arn
    log_group          = aws_cloudwatch_log_group.access_audit.name
    namespace          = local.access_audit_namespace
    files              = entry.files
    ignore_executables = try(entry.ignore_executables, [])
    reporter = {
      access_key_id     = aws_iam_access_key.access_audit_reporter[host].id
      secret_access_key = aws_iam_access_key.access_audit_reporter[host].secret
    }
    principal = {
      access_key_id     = aws_iam_access_key.access_audit_principal[host].id
      secret_access_key = aws_iam_access_key.access_audit_principal[host].secret
    }
    },
    can(entry.service_name) ? { service_name = entry.service_name } : {},
    can(entry.launchd_label) ? { launchd_label = entry.launchd_label } : {},
  ) })
}

# Principal activity: every enabled Region forwards matching CloudTrail events
# to one bus here. Management reads (e.g. GetCallerIdentity) need the
# ALL_CLOUDTRAIL_MANAGEMENT_EVENTS state on both the regional and central rules.
resource "aws_cloudwatch_event_bus" "access_audit" {
  name = "access-audit"
}

resource "aws_iam_role" "access_audit_forward" {
  name = "access-audit-forward"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:events:*:${data.aws_caller_identity.current.account_id}:rule/access-audit-forward" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "access_audit_forward" {
  name = "publish-access-audit"
  role = aws_iam_role.access_audit_forward.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "events:PutEvents"
      Resource = aws_cloudwatch_event_bus.access_audit.arn
    }]
  })
}

resource "aws_cloudwatch_event_rule" "access_audit_forward" {
  for_each      = length(local.access_audit_host_keys) > 0 ? toset(local.security_delivery_regions) : toset([])
  region        = each.key
  name          = "access-audit-forward"
  description   = "Forward registered principal activity to the access-audit bus"
  event_pattern = local.access_audit_forward_pattern
  state         = "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"

  lifecycle {
    precondition {
      condition     = length(local.access_audit_forward_pattern) <= 2048
      error_message = "The access-audit pattern must fit the default 2,048-character regional quota."
    }
  }
}

resource "aws_cloudwatch_event_target" "access_audit_forward" {
  for_each  = aws_cloudwatch_event_rule.access_audit_forward
  region    = each.key
  rule      = each.value.name
  target_id = "access-audit-bus"
  arn       = aws_cloudwatch_event_bus.access_audit.arn
  role_arn  = aws_iam_role.access_audit_forward.arn
}

resource "aws_cloudwatch_event_rule" "access_audit_notify" {
  name           = "access-audit-notify"
  event_bus_name = aws_cloudwatch_event_bus.access_audit.name
  event_pattern  = jsonencode({ account = [data.aws_caller_identity.current.account_id] })
  state          = "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"
}

resource "aws_iam_role" "access_audit_notify" {
  name = "access-audit-notify"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnEquals    = { "aws:SourceArn" = aws_cloudwatch_event_rule.access_audit_notify.arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "access_audit_notify" {
  name = "publish-operator-email"
  role = aws_iam_role.access_audit_notify.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sns:Publish"
      Resource = aws_sns_topic.cost_alerts.arn
    }]
  })
}

resource "aws_cloudwatch_event_target" "access_audit_notify" {
  rule           = aws_cloudwatch_event_rule.access_audit_notify.name
  event_bus_name = aws_cloudwatch_event_bus.access_audit.name
  target_id      = "operator-email"
  arn            = aws_sns_topic.cost_alerts.arn
  role_arn       = aws_iam_role.access_audit_notify.arn

  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 185
  }

  input_transformer {
    input_paths = {
      principal = "$.detail.userIdentity.arn"
      key       = "$.detail.userIdentity.accessKeyId"
      source    = "$.detail.eventSource"
      action    = "$.detail.eventName"
      error     = "$.detail.errorCode"
      ip        = "$.detail.sourceIPAddress"
      agent     = "$.detail.userAgent"
      region    = "$.detail.awsRegion"
      time      = "$.detail.eventTime"
      id        = "$.detail.eventID"
    }
    input_template = "\"ACCESS AUDIT: registered principal used. <principal> (key <key>) called <source> <action> from <ip> in <region> at <time>. Result: <error>. User agent: <agent>. CloudTrail event ID: <id>.\""
  }
}

# Alarms. Each pages the operator topic on the transition to ALARM.
resource "aws_cloudwatch_metric_alarm" "access_audit_principal_use" {
  alarm_name          = "access-audit-principal-use"
  alarm_description   = "A registered principal made an AWS API call. See the operator email for caller and source IP."
  namespace           = "AWS/Events"
  metric_name         = "TriggeredRules"
  dimensions          = { RuleName = aws_cloudwatch_event_rule.access_audit_notify.name, EventBusName = aws_cloudwatch_event_bus.access_audit.name }
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "access_audit_delivery" {
  alarm_name          = "access-audit-delivery"
  alarm_description   = "The access-audit notification rule failed to deliver to the operator topic."
  namespace           = "AWS/Events"
  metric_name         = "FailedInvocations"
  dimensions          = { RuleName = aws_cloudwatch_event_rule.access_audit_notify.name, EventBusName = aws_cloudwatch_event_bus.access_audit.name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "access_audit_file_read" {
  alarm_name          = "access-audit-file-read"
  alarm_description   = "A registered path was opened on a host. Details: ${local.access_audit_log_group} and the operator email."
  namespace           = local.access_audit_namespace
  metric_name         = "FileRead"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "access_audit_tamper" {
  alarm_name          = "access-audit-tamper"
  alarm_description   = "A registered path was modified, replaced or removed, or a host sensor degraded."
  namespace           = local.access_audit_namespace
  metric_name         = "Tamper"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

# A silent host is an alarm: stopping the agent must not stop the audit.
resource "aws_cloudwatch_metric_alarm" "access_audit_heartbeat" {
  for_each            = local.access_audit_host_keys
  alarm_name          = "access-audit-heartbeat-${each.key}"
  alarm_description   = "The access-audit agent on ${each.key} stopped reporting."
  namespace           = local.access_audit_namespace
  metric_name         = "Heartbeat"
  dimensions          = { Host = each.key }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_dashboard" "access_audit" {
  dashboard_name = "access-audit"
  dashboard_body = jsonencode({
    widgets = concat([
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title  = "Opens and changes of registered paths"
          region = var.aws_region
          stat   = "Sum"
          period = 60
          metrics = [
            [local.access_audit_namespace, "FileRead"],
            [local.access_audit_namespace, "Tamper"],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title   = "Registered principal activity"
          region  = var.aws_region
          stat    = "Sum"
          period  = 60
          metrics = [["AWS/Events", "TriggeredRules", "RuleName", "access-audit-notify", "EventBusName", "access-audit"]]
        }
      },
      {
        type = "log", x = 0, y = 12, width = 24, height = 8
        properties = {
          title  = "Recent host reports"
          region = var.aws_region
          query  = "SOURCE '${local.access_audit_log_group}' | fields @timestamp, @logStream, kind, path, reason, exe, user, ssh_connection | sort @timestamp desc | limit 50"
        }
      },
      ], length(local.access_audit_host_keys) == 0 ? [] : [{
        type = "metric", x = 0, y = 20, width = 24, height = 6
        properties = {
          title   = "Host heartbeats"
          region  = var.aws_region
          stat    = "Sum"
          period  = 300
          metrics = [for host in sort(local.access_audit_host_keys) : [local.access_audit_namespace, "Heartbeat", "Host", host]]
        }
    }])
  })
}

# fcvm-fleet-alarms.tf
#
# Health alarms for the fleet's coordinator, in its region (us-east-2, the fcvm-fleet account). Before these, the only
# fleet alarm was the accepted on-demand launch (fcvm-fleet.tf). Each alarm here goes to the same place as that one:
# the forwarder Lambda (fcvm-fleet.tf, fcvm-fleet-on-demand-notify) publishes it to the fcvm-fleet-alerts topic, whose one
# subscriber is the owner's email. That is the fleet's only alert path: the account boundary (fcvm-fleet-account.tf)
# keeps it off the management account's cost-alerts topic, and no SMS, pager or chat subscriber exists anywhere in this
# repository. A pager goes on fcvm-fleet-alerts (modules/fcvm-fleet-region) and then reaches every alarm here.
#
# All of them exist only while the coordinator runs (local.fcvm_fleet_coordinator_running): with the service at zero tasks
# on purpose, the first two would sit in ALARM.
#
# - The service has no running task: AWS/ECS CPUUtilization for the service is published once a minute while a task runs
#   and not at all while none does, so five minutes without a sample is "no task" (missing data breaches). This avoids
#   turning on Container Insights for RunningTaskCount. A deployment stops the old task before the new one starts
#   (deployment_minimum_healthy_percent = 0); five minutes leaves the replacement time to start (not measured here: the
#   jumpbox should read the gap on the next release and raise the window only with that number).
# - The DNS load balancer has no healthy target (fcvm-fleet-dns.tf): HealthyHostCount, at most zero across the load
#   balancer's nodes for five minutes. The launch names' subzone does not resolve while it holds.
# - Log lines of the coordinator (ejc3/fcvm src/fleet/coord), matched exactly as fcvm main writes them, and of the
#   cloudflared container (the cloudflared_version pin, strings checked in the 2026.9.3 binary). Both containers log to
#   the one log group, so the filters see both; the phrases do not overlap.

locals {
  fcvm_fleet_log_alarms = {
    "fcvm-fleet-state-not-written" = {
      metric = "StateNotWritten"
      # store.rs: a failed write of the state object (kept for the next write); mod.rs: the last state at shutdown.
      pattern     = "?\"state not written\" ?\"the last state was not stored\""
      periods     = 1
      ok          = true
      description = "The coordinator could not write its state object (fcvm-fleet-coordinator-state bucket, state.json). It keeps the newest state in memory and retries; a restart now loses it. Read the coordinator log group /ecs/fcvm-fleet-coordinator for 'state not written'."
    }
    "fcvm-fleet-coordinator-degraded" = {
      metric = "CoordinatorDegraded"
      # The failures that put the coordinator's `scale` component in its degraded set (hosts.rs) and its `text_check`
      # component (text_check.rs). A spot capacity refusal is not one of them: that is "spot launch refused".
      pattern     = "?\"listing the GPU hosts failed\" ?\"terminating GPU hosts failed\" ?\"launching GPU hosts failed\" ?\"EC2 refused a GPU host launch\" ?\"asking a lost GPU host launch again failed\" ?\"text check failed\""
      periods     = 2
      ok          = true
      description = "The coordinator has reported a degraded state for 10 minutes: GPU host listing, launch or termination is failing, or the viewer word check is. Hosts may not wake, or may keep running while the fleet sleeps. Read /ecs/fcvm-fleet-coordinator; the budget backstop (fcvm-fleet-budget.tf) also shows up here as failing launches."
    }
    "fcvm-fleet-certificate-renewal-failed" = {
      metric = "CertificateRenewalFailed"
      # cert.rs: the renewal itself, storing the renewed certificate, and reading the certificate secret before it.
      pattern     = "?\"certificate renewal failed\" ?\"the renewed certificate was not stored\" ?\"the certificate secret could not be read\""
      periods     = 1
      ok          = false
      description = "The coordinator failed to renew or store the fleet's *.tiles-stage.fcvm.dev certificate (fcvm-fleet/stage/tls). Hosts and tiles read it at boot; when it expires, every viewer connection fails TLS. Read /ecs/fcvm-fleet-coordinator."
    }
    "fcvm-fleet-tunnel-down" = {
      metric = "TunnelConnectionErrors"
      # cloudflared: a tunnel connection failed and is being retried. One dropped connection of four recovers in seconds
      # and does not hold for two windows.
      pattern     = "?\"Serve tunnel error\" ?\"Unable to establish connection\" ?\"Retrying connection in up to\""
      periods     = 2
      ok          = true
      description = "The coordinator's cloudflared container has failed tunnel connections for 10 minutes. Browsers and host agents reach the coordinator only through this tunnel (fleet-api-stage.fcvm.dev). Read the cloudflared streams of /ecs/fcvm-fleet-coordinator and the tunnel's status in Cloudflare."
    }
  }

  fcvm_fleet_health_alarm_names = local.fcvm_fleet_coordinator_running ? concat(
    ["fcvm-fleet-coordinator-not-running", "fcvm-fleet-dns-no-healthy-target"],
    keys(local.fcvm_fleet_log_alarms),
  ) : []
  # Built from the names, as fcvm_fleet_on_demand_alarm_arn is: the Lambda permission names the alarm before it exists.
  fcvm_fleet_health_alarm_arns = {
    for n in concat(local.fcvm_fleet_health_alarm_names, ["fcvm-fleet-spend-cap-unhealthy"]) :
    n => "arn:aws:cloudwatch:${local.fcvm_fleet_home_region}:${local.fcvm_fleet_account_id}:alarm:${n}"
  }
}

resource "aws_lambda_permission" "fcvm_fleet_health_alarms" {
  for_each       = local.fcvm_fleet_health_alarm_arns
  provider       = aws.fcvm_fleet
  statement_id   = "Alarm-${each.key}"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.fcvm_fleet_on_demand_notify.function_name
  principal      = "lambda.alarms.cloudwatch.amazonaws.com"
  source_account = local.fcvm_fleet_account_id
  source_arn     = each.value
}

resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_coordinator_not_running" {
  count               = local.fcvm_fleet_coordinator_running ? 1 : 0
  provider            = aws.fcvm_fleet
  alarm_name          = "fcvm-fleet-coordinator-not-running"
  alarm_description   = "The fcvm-fleet-coordinator ECS service has had no running task for 5 minutes. Nothing wakes or sleeps the fleet, and no viewer can connect. Check the service's events and stopped tasks in the fcvm-fleet cluster (us-east-2)."
  comparison_operator = "LessThanThreshold"
  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  dimensions = {
    ClusterName = aws_ecs_cluster.fcvm_fleet.name
    ServiceName = "fcvm-fleet-coordinator" # Stable ECS name; the alarm can deploy independently of task revisions.
  }
  statistic           = "SampleCount"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]
  ok_actions          = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]
  depends_on          = [aws_lambda_permission.fcvm_fleet_health_alarms]
}

resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_dns_no_healthy_target" {
  count               = local.fcvm_fleet_coordinator_running ? 1 : 0
  provider            = aws.fcvm_fleet
  alarm_name          = "fcvm-fleet-dns-no-healthy-target"
  alarm_description   = "The fcvm-fleet-dns load balancer has had no healthy target for 5 minutes, so ${local.fcvm_fleet_names_zone} does not resolve and no launched tile can be named. Check the fcvm-fleet-dns target group and the coordinator's DNS server (port ${local.fcvm_fleet_dns_port})."
  comparison_operator = "LessThanThreshold"
  namespace           = "AWS/NetworkELB"
  metric_name         = "HealthyHostCount"
  dimensions = {
    LoadBalancer = aws_lb.fcvm_fleet_dns.arn_suffix
    TargetGroup  = aws_lb_target_group.fcvm_fleet_dns.arn_suffix
  }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]
  ok_actions          = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]
  depends_on          = [aws_lambda_permission.fcvm_fleet_health_alarms]
}

resource "aws_cloudwatch_log_metric_filter" "fcvm_fleet_health" {
  for_each       = local.fcvm_fleet_log_alarms
  provider       = aws.fcvm_fleet
  name           = each.key
  log_group_name = aws_cloudwatch_log_group.fcvm_fleet_coordinator.name
  pattern        = each.value.pattern

  metric_transformation {
    name          = each.value.metric
    namespace     = "FCVM/Fleet"
    value         = "1"
    default_value = 0
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_health_logs" {
  for_each            = local.fcvm_fleet_coordinator_running ? local.fcvm_fleet_log_alarms : {}
  provider            = aws.fcvm_fleet
  alarm_name          = each.key
  alarm_description   = each.value.description
  comparison_operator = "GreaterThanOrEqualToThreshold"
  namespace           = aws_cloudwatch_log_metric_filter.fcvm_fleet_health[each.key].metric_transformation[0].namespace
  metric_name         = aws_cloudwatch_log_metric_filter.fcvm_fleet_health[each.key].metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = each.value.periods
  datapoints_to_alarm = each.value.periods
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]
  ok_actions          = each.value.ok ? [aws_lambda_function.fcvm_fleet_on_demand_notify.arn] : []
  depends_on          = [aws_lambda_permission.fcvm_fleet_health_alarms]
}

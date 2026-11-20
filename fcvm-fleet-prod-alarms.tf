# fcvm-fleet-prod-alarms.tf
#
# The production fleet's alarms (ejc3/fcvm fleet production plan, A5), behind var.fcvm_fleet_prod_enabled like the stack
# they watch (fcvm-fleet-prod.tf): with it off this file creates nothing. Every alarm is in us-east-2, where the prod
# coordinator runs, and goes where staging's go (fcvm-fleet-alarms.tf): the forwarder Lambda fcvm-fleet-on-demand-notify
# publishes it to the fcvm-fleet-alerts topic, whose subscriber is the owner's email. fcvm-fleet.tf lists these alarms in
# the forwarder's ALARM_ARNS.
#
# What each alarm reads:
# - AWS/ECS and AWS/NetworkELB, as staging's: the prod service has no running task for 5 minutes; the prod DNS load
#   balancer has no healthy target for 5 minutes.
# - The coordinator's embedded metrics (ejc3/fcvm src/fleet/coord/metrics.rs): namespace FCVM/Fleet, dimension
#   Fleet=prod, one line every 30 s. Power is 0 asleep, 1 waking, 2 awake. SpendUsdToday is present with
#   --daily-spend-usd, BroadcastLive with a broadcast channel, CertDaysLeft once the certificate secret has been read.
# - Log lines of the prod coordinator's log group, matched as ejc3/fcvm main writes them
#   (scripts/fixtures/fcvm-coordinator-prod-log-lines.txt, which CI checks against the source at its pinned revision).
#   Log filters cannot carry a dimension from unstructured text, so their metrics are named Prod* to keep them apart from
#   staging's in the same namespace.
# - fcvm-fleet-prod-watch (scripts/fcvm-fleet-prod-watch.py): a synthetic check of the public page and the /live hello
#   every 5 minutes; a poll of the Cloudflare audit log every 5 minutes for writes to the prod Worker by anything but
#   the prod release token (owner, 2026-11-19: one Cloudflare account, so the staging deploy token can deploy prod, and
#   an alarm is the control); and CloudTrail events for the prod service's UpdateService and the prod launch templates'
#   ModifyLaunchTemplate and CreateLaunchTemplateVersion by anything but the release roles.
#
# The watch function exempts the release roles and the account token defined by fcvm-fleet-prod-ci.tf.
# A Terraform apply that changes the prod service or templates alarms. Cloudflare audit entries must
# identify the immutable release token ID to be exempt; a matching display name is insufficient.
# The synthetic check opens /live until plan A6 serves the Worker at fcvm.dev and sets fcvm_fleet_prod_page_public.
#
# Cost while on: 22 alarms ($0.10 a month each; the three metric math ones read two metrics, $0.20), two Lambda
# invocations every 5 minutes, and the three custom metrics the watch function puts ($0.30 a month each).

locals {
  fcvm_fleet_prod_alarm_prefix = local.fcvm_fleet_prod_stack.prefix # fcvm-fleet-prod
  fcvm_fleet_prod_dimension    = { Fleet = local.fcvm_fleet_prod_stack.fleet }

  fcvm_fleet_prod_release_roles = {
    "ecs.amazonaws.com" = aws_iam_role.fcvm_coordinator_release_prod_fleet[*].arn
    "ec2.amazonaws.com" = aws_iam_role.fcvm_ami_prod_release_fleet[*].arn
  }
  fcvm_fleet_prod_worker = "fcvm-fleet" # ejc3/fcvm fleet/web/wrangler.jsonc, env.production
  # Plan A6 serves the Worker at fcvm.dev and sets this.
  fcvm_fleet_prod_page_public     = false
  fcvm_fleet_prod_template_prefix = "${local.fcvm_fleet_prod_stack.prefix}-"
  fcvm_fleet_prod_template_ids    = var.fcvm_fleet_prod_enabled ? [for t in module.fcvm_fleet_prod_east1[0].image_templates : t.id] : []

  fcvm_fleet_prod_log_alarms = {
    "${local.fcvm_fleet_prod_alarm_prefix}-state-not-written" = {
      metric = "ProdStateNotWritten"
      # store.rs and mod.rs: a failed write of the state object, and the last state at shutdown.
      pattern     = "?\"state not written\" ?\"the last state was not stored\""
      period      = 300
      periods     = 1
      threshold   = 1
      ok          = true
      description = "The prod coordinator could not write its state object (prod/state.json in the coordinator state bucket). It keeps the newest state in memory and retries; a restart now loses it. Read /ecs/fcvm-fleet-prod-coordinator for 'state not written'."
    }
    "${local.fcvm_fleet_prod_alarm_prefix}-state-write-lost" = {
      metric = "ProdStateWriteLost"
      # store.rs: the conditional write got 412; another coordinator owns the object, and this one writes nothing more.
      pattern     = "\"FLEET_STATE_WRITE_LOST\""
      period      = 300
      periods     = 1
      threshold   = 1
      ok          = false
      description = "The prod coordinator's conditional write of prod/state.json got 412: another coordinator has written it since this one read it, and this one has stopped writing. Check for a second prod coordinator task, then read /ecs/fcvm-fleet-prod-coordinator for FLEET_STATE_WRITE_LOST."
    }
    "${local.fcvm_fleet_prod_alarm_prefix}-state-unreadable" = {
      metric = "ProdStateUnreadable"
      # store.rs: prod/state.json would not parse; the coordinator resumed from an earlier version.
      pattern     = "\"FLEET_STATE_UNREADABLE\""
      period      = 300
      periods     = 1
      threshold   = 1
      ok          = false
      description = "The stored prod/state.json would not parse. The prod coordinator resumed from an earlier version of the object, or failed to start if none parses; changes stored after that version are lost, today's spend ledger included. Read /ecs/fcvm-fleet-prod-coordinator for FLEET_STATE_UNREADABLE."
    }
    "${local.fcvm_fleet_prod_alarm_prefix}-coordinator-degraded" = {
      metric = "ProdCoordinatorDegraded"
      # hosts.rs and text_check.rs, as staging's (fcvm-fleet-alarms.tf).
      pattern     = "?\"listing the GPU hosts failed\" ?\"terminating GPU hosts failed\" ?\"launching GPU hosts failed\" ?\"EC2 refused a GPU host launch\" ?\"asking a lost GPU host launch again failed\" ?\"text check failed\""
      period      = 300
      periods     = 2
      threshold   = 1
      ok          = true
      description = "The prod coordinator has reported a degraded state for 10 minutes: GPU host listing, launch or termination is failing, or the viewer word check is. Hosts may not wake, or may keep running while the fleet sleeps. Read /ecs/fcvm-fleet-prod-coordinator."
    }
    "${local.fcvm_fleet_prod_alarm_prefix}-certificate-renewal-failed" = {
      metric = "ProdCertificateRenewalFailed"
      # cert.rs: the renewal, storing the renewed certificate, and reading the secret before it.
      pattern     = "?\"certificate renewal failed\" ?\"the renewed certificate was not stored\" ?\"the certificate secret could not be read\""
      period      = 300
      periods     = 1
      threshold   = 1
      ok          = false
      description = "The prod coordinator failed to renew or store the *.tiles.fcvm.dev certificate (fcvm-fleet/prod/tls). Hosts and tiles read it at boot; when it expires every viewer's tile connection fails TLS. Read /ecs/fcvm-fleet-prod-coordinator."
    }
    "${local.fcvm_fleet_prod_alarm_prefix}-tunnel-down" = {
      metric = "ProdTunnelConnectionErrors"
      # cloudflared, as staging's.
      pattern     = "?\"Serve tunnel error\" ?\"Unable to establish connection\" ?\"Retrying connection in up to\""
      period      = 300
      periods     = 2
      threshold   = 1
      ok          = true
      description = "The prod coordinator's cloudflared container has failed tunnel connections for 10 minutes. Viewers and host agents reach the prod coordinator only through this tunnel (fleet-api.fcvm.dev). Read the cloudflared streams of /ecs/fcvm-fleet-prod-coordinator and the tunnel's status in Cloudflare."
    }
    "${local.fcvm_fleet_prod_alarm_prefix}-agent-refused" = {
      metric = "ProdAgentRefused"
      # agents.rs and live.rs: an /agent connection whose Access token does not verify, or whose hello names a host that
      # is not a Fleet=prod instance it comes from. Logged as the field marker="fleet-agent-refused".
      pattern     = "\"fleet-agent-refused\""
      period      = 300
      periods     = 1
      threshold   = 1
      ok          = false
      description = "The prod coordinator refused a host agent: its Access token did not verify for the prod agents' application, or the host its hello named is not a Fleet=prod instance at the connection's address. A forged or misplaced agent, or a prod host whose agent token is wrong. Read /ecs/fcvm-fleet-prod-coordinator for marker=\"fleet-agent-refused\"."
    }
    "${local.fcvm_fleet_prod_alarm_prefix}-turnstile-siteverify-errors" = {
      metric = "ProdTurnstileSiteverifyErrors"
      # live.rs: siteverify failed or timed out (not a token it rejected). Fails closed: the visitor stays unverified.
      # One timeout is routine; five in 5 minutes is Cloudflare's siteverify or the coordinator's egress failing.
      pattern     = "\"turnstile-siteverify-error\""
      period      = 300
      periods     = 1
      threshold   = 5
      ok          = true
      description = "Turnstile siteverify failed or timed out at least 5 times in 5 minutes for the prod coordinator. Visitors cannot pass the check, so none can take a seat or wake the fleet. Read /ecs/fcvm-fleet-prod-coordinator for marker=\"turnstile-siteverify-error\" and Cloudflare's status."
    }
    "${local.fcvm_fleet_prod_alarm_prefix}-forced-sleep" = {
      metric = "ProdForcedSleep"
      # mod.rs: "fleet asleep" carries reason=Some("budget") when the spend ceiling put the fleet to sleep. No other
      # field of that line can hold the word.
      pattern     = "\"fleet asleep\" \"budget\""
      period      = 300
      periods     = 1
      threshold   = 1
      ok          = false
      description = "The prod coordinator put the fleet to sleep at the daily spend ceiling ($750, --daily-spend-usd): every claim ended and every host is terminated. Viewers see 'Resting until 00:00 UTC' and nothing wakes it before then. Read /ecs/fcvm-fleet-prod-coordinator for 'fleet asleep'."
    }
    "${local.fcvm_fleet_prod_alarm_prefix}-on-demand-host-launches" = {
      metric = "ProdOnDemandHostLaunches"
      # hosts.rs, as staging's fcvm_fleet_on_demand_launches: an accepted on-demand launch, not an attempt.
      pattern     = "\"wake: host launch accepted\" \"on-demand\""
      period      = 60
      periods     = 1
      threshold   = 1
      ok          = false
      description = "The prod coordinator accepted an on-demand GPU host launch. A g4dn.metal costs $7.824/hour in us-east-1 and runs until the fleet sleeps; the spend ceiling counts it. This counts launch events, not running hosts."
    }
    "${local.fcvm_fleet_prod_alarm_prefix}-coordinator-restarts" = {
      metric = "ProdCoordinatorStarts"
      # mod.rs: once per start, when /live is served. Two starts in one 10-minute period is more than one restart (a
      # release or a replaced task is one start). A task that dies before serving is not counted here; the
      # not-running alarm sees a service that keeps no task.
      pattern     = "\"coordinator serving /live\""
      period      = 600
      periods     = 1
      threshold   = 2
      ok          = true
      description = "The prod coordinator started at least twice in 10 minutes, so its task is restarting. Viewers lose their seats on every restart. Check the stopped tasks of the fcvm-fleet-prod-coordinator service (us-east-2) and the end of each task's log in /ecs/fcvm-fleet-prod-coordinator."
    }
  }

  fcvm_fleet_prod_alarm_names = var.fcvm_fleet_prod_enabled ? concat(
    [for n in [
      "coordinator-not-running", "dns-no-healthy-target", "spend-80-percent", "hosts-short", "wake-slow", "certificate-expiring",
      "synthetic-check-failed", "worker-changed-outside-release", "changed-outside-release",
    ] : "${local.fcvm_fleet_prod_alarm_prefix}-${n}"],
    local.fcvm_fleet_prod_broadcast == 1 ? ["${local.fcvm_fleet_prod_alarm_prefix}-broadcast-off"] : [],
    keys(local.fcvm_fleet_prod_log_alarms),
  ) : []
  # Built from the names, as fcvm_fleet_health_alarm_arns is: the forwarder's permission names each alarm before it exists.
  fcvm_fleet_prod_alarm_arns = {
    for n in local.fcvm_fleet_prod_alarm_names :
    n => "arn:aws:cloudwatch:${local.fcvm_fleet_home_region}:${local.fcvm_fleet_account_id}:alarm:${n}"
  }
  fcvm_fleet_prod_notify = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]
}

resource "aws_lambda_permission" "fcvm_fleet_prod_alarms" {
  for_each       = local.fcvm_fleet_prod_alarm_arns
  provider       = aws.fcvm_fleet
  statement_id   = "Alarm-${each.key}"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.fcvm_fleet_on_demand_notify.function_name
  principal      = "lambda.alarms.cloudwatch.amazonaws.com"
  source_account = local.fcvm_fleet_account_id
  source_arn     = each.value
}

# ------------------------------------------------------------------------------------------------ the service and DNS
# Staging's fcvm_fleet_coordinator_not_running for the prod service: no CPU sample for 5 minutes is no running task.
resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_coordinator_not_running" {
  count               = local.fcvm_fleet_prod
  provider            = aws.fcvm_fleet
  alarm_name          = "${local.fcvm_fleet_prod_alarm_prefix}-coordinator-not-running"
  alarm_description   = "The fcvm-fleet-prod-coordinator ECS service has had no running task for 5 minutes. fcvm.dev's wall is down: nothing wakes or sleeps the prod fleet, the spend ceiling is not enforced by the coordinator, and no viewer can connect. Check the service's events and stopped tasks in the fcvm-fleet-prod cluster (us-east-2)."
  comparison_operator = "LessThanThreshold"
  namespace           = "AWS/ECS"
  metric_name         = "CPUUtilization"
  dimensions = {
    ClusterName = aws_ecs_cluster.fcvm_fleet_prod[0].name
    ServiceName = local.fcvm_fleet_prod_coordinator
  }
  statistic           = "SampleCount"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = local.fcvm_fleet_prod_notify
  ok_actions          = local.fcvm_fleet_prod_notify
  depends_on          = [aws_lambda_permission.fcvm_fleet_prod_alarms]
}

resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_dns_no_healthy_target" {
  count               = local.fcvm_fleet_prod
  provider            = aws.fcvm_fleet
  alarm_name          = "${local.fcvm_fleet_prod_alarm_prefix}-dns-no-healthy-target"
  alarm_description   = "The fcvm-fleet-prod-dns load balancer has had no healthy target for 5 minutes, so ${local.fcvm_fleet_prod_names_zone} does not resolve: no viewer can reach a tile by name and the certificate's DNS-01 renewal cannot be answered. Check the fcvm-fleet-prod-dns target group and the prod coordinator's DNS server (port ${local.fcvm_fleet_dns_port})."
  comparison_operator = "LessThanThreshold"
  namespace           = "AWS/NetworkELB"
  metric_name         = "HealthyHostCount"
  dimensions = {
    LoadBalancer = aws_lb.fcvm_fleet_prod_dns[0].arn_suffix
    TargetGroup  = aws_lb_target_group.fcvm_fleet_prod_dns[0].arn_suffix
  }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = local.fcvm_fleet_prod_notify
  ok_actions          = local.fcvm_fleet_prod_notify
  depends_on          = [aws_lambda_permission.fcvm_fleet_prod_alarms]
}

# ------------------------------------------------------------------------------------------------ the coordinator's metrics
# Today's spend reached 80% of the ceiling. SpendUsdToday restarts from 0 at 00:00 UTC, which clears it.
resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_spend_80_percent" {
  count               = local.fcvm_fleet_prod
  provider            = aws.fcvm_fleet
  alarm_name          = "${local.fcvm_fleet_prod_alarm_prefix}-spend-80-percent"
  alarm_description   = format("The prod fleet has spent at least 80%% of today's $%d ceiling (SpendUsdToday, UTC day). At the ceiling less 15 minutes of the current burn the coordinator puts the fleet to sleep until 00:00 UTC.", local.fcvm_fleet_prod_daily_spend_usd)
  comparison_operator = "GreaterThanOrEqualToThreshold"
  namespace           = "FCVM/Fleet"
  metric_name         = "SpendUsdToday"
  dimensions          = local.fcvm_fleet_prod_dimension
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = local.fcvm_fleet_prod_daily_spend_usd * 0.8
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.fcvm_fleet_prod_notify
  ok_actions          = local.fcvm_fleet_prod_notify
  depends_on          = [aws_lambda_permission.fcvm_fleet_prod_alarms]
}

# Awake (Power 2 for the whole minute) with fewer than two hosts serving, for 5 minutes. Missing data breaches: a
# coordinator that stops reporting is not known to be well.
resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_hosts_short" {
  count               = local.fcvm_fleet_prod
  provider            = aws.fcvm_fleet
  alarm_name          = "${local.fcvm_fleet_prod_alarm_prefix}-hosts-short"
  alarm_description   = "The prod fleet has been awake with fewer than ${local.fcvm_fleet_prod_awake_hosts} serving GPU hosts (AwakeHosts) for 5 minutes, or its metrics stopped. Viewers see a coarser wall or dark tiles. A spot reclaim whose replacement did not join, or no G capacity in the free zones: read /ecs/fcvm-fleet-prod-coordinator for the host launches."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  threshold           = 1
  treat_missing_data  = "breaching"
  metric_query {
    id          = "short"
    expression  = "IF(power == 2 AND hosts < ${local.fcvm_fleet_prod_awake_hosts}, 1, 0)"
    return_data = true
  }
  dynamic "metric_query" {
    for_each = { power = ["Power", "Minimum"], hosts = ["AwakeHosts", "Maximum"] }
    content {
      id = metric_query.key
      metric {
        namespace   = "FCVM/Fleet"
        metric_name = metric_query.value[0]
        dimensions  = local.fcvm_fleet_prod_dimension
        period      = 60
        stat        = metric_query.value[1]
      }
    }
  }
  alarm_actions = local.fcvm_fleet_prod_notify
  ok_actions    = local.fcvm_fleet_prod_notify
  depends_on    = [aws_lambda_permission.fcvm_fleet_prod_alarms]
}

# Waking (Power 1 through every sample of the minute) for 5 minutes. A wake measures about 2 minutes.
resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_wake_slow" {
  count               = local.fcvm_fleet_prod
  provider            = aws.fcvm_fleet
  alarm_name          = "${local.fcvm_fleet_prod_alarm_prefix}-wake-slow"
  alarm_description   = "The prod fleet has been waking for more than 5 minutes (Power 1). Viewers are watching the wake reel. Read the 'wake:' lines of /ecs/fcvm-fleet-prod-coordinator for the phase that is stuck."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  threshold           = 1
  treat_missing_data  = "notBreaching"
  metric_query {
    id          = "waking"
    expression  = "IF(low == 1 AND high == 1, 1, 0)"
    return_data = true
  }
  dynamic "metric_query" {
    for_each = { low = "Minimum", high = "Maximum" }
    content {
      id = metric_query.key
      metric {
        namespace   = "FCVM/Fleet"
        metric_name = "Power"
        dimensions  = local.fcvm_fleet_prod_dimension
        period      = 60
        stat        = metric_query.value
      }
    }
  }
  alarm_actions = local.fcvm_fleet_prod_notify
  ok_actions    = local.fcvm_fleet_prod_notify
  depends_on    = [aws_lambda_permission.fcvm_fleet_prod_alarms]
}

# The certificate is read hourly; renewal is due well before 21 days. Missing data breaches: a coordinator that cannot
# read its certificate secret reports no CertDaysLeft.
resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_certificate_expiring" {
  count               = local.fcvm_fleet_prod
  provider            = aws.fcvm_fleet
  alarm_name          = "${local.fcvm_fleet_prod_alarm_prefix}-certificate-expiring"
  alarm_description   = "The *.tiles.fcvm.dev certificate in fcvm-fleet/prod/tls expires in under 21 days (CertDaysLeft), or the prod coordinator reported no expiry for an hour. Renewal has not happened; when it expires every tile connection fails TLS. Read /ecs/fcvm-fleet-prod-coordinator for 'certificate'."
  comparison_operator = "LessThanThreshold"
  namespace           = "FCVM/Fleet"
  metric_name         = "CertDaysLeft"
  dimensions          = local.fcvm_fleet_prod_dimension
  statistic           = "Minimum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 21
  treat_missing_data  = "breaching"
  alarm_actions       = local.fcvm_fleet_prod_notify
  ok_actions          = local.fcvm_fleet_prod_notify
  depends_on          = [aws_lambda_permission.fcvm_fleet_prod_alarms]
}

# Awake for 5 minutes with IVS saying the prod channel is not live (fcvm-fleet-prod-broadcast.tf).
resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_broadcast_off" {
  count               = local.fcvm_fleet_prod_broadcast
  provider            = aws.fcvm_fleet
  alarm_name          = "${local.fcvm_fleet_prod_alarm_prefix}-broadcast-off"
  alarm_description   = "The prod fleet has been awake for 5 minutes and its IVS channel is not live (BroadcastLive 0): viewers past the direct capacity see 'broadcast unavailable'. 0 also means IVS could not be asked. Read /ecs/fcvm-fleet-prod-coordinator for the broadcast aggregator."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  threshold           = 1
  treat_missing_data  = "notBreaching"
  metric_query {
    id          = "off"
    expression  = "IF(power == 2 AND live == 0, 1, 0)"
    return_data = true
  }
  dynamic "metric_query" {
    for_each = { power = ["Power", "Minimum"], live = ["BroadcastLive", "Maximum"] }
    content {
      id = metric_query.key
      metric {
        namespace   = "FCVM/Fleet"
        metric_name = metric_query.value[0]
        dimensions  = local.fcvm_fleet_prod_dimension
        period      = 60
        stat        = metric_query.value[1]
      }
    }
  }
  alarm_actions = local.fcvm_fleet_prod_notify
  ok_actions    = local.fcvm_fleet_prod_notify
  depends_on    = [aws_lambda_permission.fcvm_fleet_prod_alarms]
}

# ------------------------------------------------------------------------------------------------ the coordinator's log
resource "aws_cloudwatch_log_metric_filter" "fcvm_fleet_prod" {
  for_each       = { for k, v in local.fcvm_fleet_prod_log_alarms : k => v if var.fcvm_fleet_prod_enabled }
  provider       = aws.fcvm_fleet
  name           = each.key
  log_group_name = aws_cloudwatch_log_group.fcvm_fleet_prod_coordinator[0].name
  pattern        = each.value.pattern

  metric_transformation {
    name          = each.value.metric
    namespace     = "FCVM/Fleet"
    value         = "1"
    default_value = 0
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_logs" {
  for_each            = aws_cloudwatch_log_metric_filter.fcvm_fleet_prod
  provider            = aws.fcvm_fleet
  alarm_name          = each.key
  alarm_description   = local.fcvm_fleet_prod_log_alarms[each.key].description
  comparison_operator = "GreaterThanOrEqualToThreshold"
  namespace           = each.value.metric_transformation[0].namespace
  metric_name         = each.value.metric_transformation[0].name
  statistic           = "Sum"
  period              = local.fcvm_fleet_prod_log_alarms[each.key].period
  evaluation_periods  = local.fcvm_fleet_prod_log_alarms[each.key].periods
  datapoints_to_alarm = local.fcvm_fleet_prod_log_alarms[each.key].periods
  threshold           = local.fcvm_fleet_prod_log_alarms[each.key].threshold
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.fcvm_fleet_prod_notify
  ok_actions          = local.fcvm_fleet_prod_log_alarms[each.key].ok ? local.fcvm_fleet_prod_notify : []
  depends_on          = [aws_lambda_permission.fcvm_fleet_prod_alarms]
}

# ------------------------------------------------------------------------------------------------ the watch function
# The Cloudflare token it reads the account's audit log with: Account Settings Read, the one permission the audit log
# accepts (Cloudflare's audit logs v2). It can change nothing.
resource "cloudflare_account_token" "fcvm_fleet_prod_audit_read" {
  count      = local.fcvm_fleet_prod
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "fcvm-fleet-prod-audit-read"
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.account_settings_read }]
    resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
  }]
}

resource "aws_secretsmanager_secret" "fcvm_fleet_prod_audit_read" {
  count                   = local.fcvm_fleet_prod
  provider                = aws.fcvm_fleet
  name                    = "fcvm-fleet/prod/cloudflare-audit-read"
  description             = "Read-only Cloudflare token (Account Settings Read) the prod watch function reads the account audit log with. Written by Terraform; see fcvm-fleet-prod-alarms.tf."
  recovery_window_in_days = 7
  tags                    = merge(local.fcvm_fleet_prod_tags, { Name = "fcvm-fleet/prod/cloudflare-audit-read" })
}

resource "aws_secretsmanager_secret_policy" "fcvm_fleet_prod_audit_read" {
  count      = local.fcvm_fleet_prod
  provider   = aws.fcvm_fleet
  secret_arn = aws_secretsmanager_secret.fcvm_fleet_prod_audit_read[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTheWatchFunctionAndTerraform"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.fcvm_fleet_prod_audit_read[0].arn
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = [aws_iam_role.fcvm_fleet_prod_watch[0].arn, local.fcvm_fleet_access_role_arn] } }
    }]
  })
}

resource "aws_secretsmanager_secret_version" "fcvm_fleet_prod_audit_read" {
  count         = local.fcvm_fleet_prod
  provider      = aws.fcvm_fleet
  secret_id     = aws_secretsmanager_secret.fcvm_fleet_prod_audit_read[0].id
  secret_string = cloudflare_account_token.fcvm_fleet_prod_audit_read[0].value
}

resource "aws_iam_role" "fcvm_fleet_prod_watch" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "${local.fcvm_fleet_prod_alarm_prefix}-watch"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = local.fcvm_fleet_prod_tags
}

resource "aws_cloudwatch_log_group" "fcvm_fleet_prod_watch" {
  count             = local.fcvm_fleet_prod
  provider          = aws.fcvm_fleet
  name              = "/aws/lambda/${local.fcvm_fleet_prod_alarm_prefix}-watch"
  retention_in_days = 30
  tags              = local.fcvm_fleet_prod_tags
}

resource "aws_iam_role_policy" "fcvm_fleet_prod_watch" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "watch-the-prod-fleet"
  role     = aws_iam_role.fcvm_fleet_prod_watch[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "Log"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.fcvm_fleet_prod_watch[0].arn}:*"
      },
      {
        Sid       = "PutFleetMetricsOnly"
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "FCVM/Fleet" } }
      },
      {
        Sid      = "ReadTheAuditToken"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.fcvm_fleet_prod_audit_read[0].arn
      },
    ]
  })
}

data "archive_file" "fcvm_fleet_prod_watch" {
  type        = "zip"
  output_path = "${path.module}/.terraform/fcvm-fleet-prod-watch.zip"
  source {
    content  = file("${path.module}/scripts/fcvm-fleet-prod-watch.py")
    filename = "watch.py"
  }
}

resource "aws_lambda_function" "fcvm_fleet_prod_watch" {
  count            = local.fcvm_fleet_prod
  provider         = aws.fcvm_fleet
  function_name    = "${local.fcvm_fleet_prod_alarm_prefix}-watch"
  filename         = data.archive_file.fcvm_fleet_prod_watch.output_path
  source_code_hash = data.archive_file.fcvm_fleet_prod_watch.output_base64sha256
  role             = aws_iam_role.fcvm_fleet_prod_watch[0].arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "watch.handler"
  timeout          = 60
  memory_size      = 128
  environment {
    variables = {
      FLEET = local.fcvm_fleet_prod_stack.fleet
      # The synthetic check: the page from A6 on, and /live as a visitor's page opens it.
      PAGE_URL = local.fcvm_fleet_prod_page_public ? "${local.fcvm_fleet_prod_site_origin}/" : ""
      LIVE_URL = "wss://${local.fcvm_dev_api_prod_hostname}/live"
      ORIGIN   = local.fcvm_fleet_prod_site_origin
      # The audit log poll: 20 minutes read every 5, so a late entry is still seen.
      CF_TOKEN_SECRET  = aws_secretsmanager_secret.fcvm_fleet_prod_audit_read[0].arn
      CF_ACCOUNT_ID    = var.cloudflare_account_id
      WORKER           = local.fcvm_fleet_prod_worker
      RELEASE_TOKEN_ID = cloudflare_account_token.fcvm_prod_workers_deploy[0].id
      WINDOW_MINUTES   = "20"
      # Changes outside a release.
      PROD_SERVICE         = local.fcvm_fleet_prod_coordinator
      PROD_TEMPLATE_IDS    = jsonencode(local.fcvm_fleet_prod_template_ids)
      PROD_TEMPLATE_PREFIX = local.fcvm_fleet_prod_template_prefix
      RELEASE_ROLES        = jsonencode(local.fcvm_fleet_prod_release_roles)
    }
  }
  tags       = local.fcvm_fleet_prod_tags
  depends_on = [aws_iam_role_policy.fcvm_fleet_prod_watch, aws_cloudwatch_log_group.fcvm_fleet_prod_watch]
}

resource "aws_cloudwatch_event_rule" "fcvm_fleet_prod_watch_schedule" {
  for_each            = var.fcvm_fleet_prod_enabled ? toset(["synthetic", "worker-deploys"]) : toset([])
  provider            = aws.fcvm_fleet
  name                = "${local.fcvm_fleet_prod_alarm_prefix}-watch-${each.key}"
  schedule_expression = "rate(5 minutes)"
  tags                = local.fcvm_fleet_prod_tags
}

resource "aws_cloudwatch_event_target" "fcvm_fleet_prod_watch_schedule" {
  for_each   = aws_cloudwatch_event_rule.fcvm_fleet_prod_watch_schedule
  provider   = aws.fcvm_fleet
  rule       = each.value.name
  arn        = aws_lambda_function.fcvm_fleet_prod_watch[0].arn
  input      = jsonencode({ check = each.key })
  depends_on = [aws_lambda_permission.fcvm_fleet_prod_watch]
}

# ------------------------------------------------------------------------------------------------ changes outside a release
# CloudTrail's management events reach EventBridge in the region of the call (the organization trail,
# security-monitoring.tf). The prod service is in us-east-2, the prod templates in us-east-1; the us-east-1 rule forwards
# its matches to us-east-2's default bus, where the second rule hands them to the watch function. Some callers have no
# userIdentity.arn. Forward all matching changes and let the function compare each API's release-role issuer.
locals {
  fcvm_fleet_prod_change_patterns = {
    service = {
      region = local.fcvm_fleet_home_region
      pattern = {
        source        = ["aws.ecs"]
        "detail-type" = ["AWS API Call via CloudTrail"]
        detail = {
          eventSource       = ["ecs.amazonaws.com"]
          eventName         = ["UpdateService"]
          requestParameters = { service = [local.fcvm_fleet_prod_coordinator, { suffix = "/${local.fcvm_fleet_prod_coordinator}" }] }
        }
      }
    }
    templates = {
      region = local.fcvm_fleet_prod_region
      pattern = {
        source        = ["aws.ec2"]
        "detail-type" = ["AWS API Call via CloudTrail"]
        detail = {
          eventSource = ["ec2.amazonaws.com"]
          eventName   = ["ModifyLaunchTemplate", "CreateLaunchTemplateVersion"]
          awsRegion   = [local.fcvm_fleet_prod_region]
        }
      }
    }
  }
}

# us-east-2: the service's changes, and the template changes us-east-1 forwards.
resource "aws_cloudwatch_event_rule" "fcvm_fleet_prod_changes" {
  for_each      = { for k, v in local.fcvm_fleet_prod_change_patterns : k => v if var.fcvm_fleet_prod_enabled }
  provider      = aws.fcvm_fleet
  name          = "${local.fcvm_fleet_prod_alarm_prefix}-${each.key}-changes"
  description   = "Prod ${each.key} changes by anything but the prod release role (fcvm-fleet-prod-alarms.tf)"
  event_pattern = jsonencode(each.value.pattern)
  tags          = local.fcvm_fleet_prod_tags
}

resource "aws_cloudwatch_event_target" "fcvm_fleet_prod_changes" {
  for_each   = aws_cloudwatch_event_rule.fcvm_fleet_prod_changes
  provider   = aws.fcvm_fleet
  rule       = each.value.name
  arn        = aws_lambda_function.fcvm_fleet_prod_watch[0].arn
  depends_on = [aws_lambda_permission.fcvm_fleet_prod_watch]
}

resource "aws_cloudwatch_event_rule" "fcvm_fleet_prod_template_changes_east1" {
  count         = local.fcvm_fleet_prod
  provider      = aws.fcvm_fleet_east1
  name          = "${local.fcvm_fleet_prod_alarm_prefix}-templates-changes"
  description   = "Prod launch template changes by anything but the prod image release role, forwarded to us-east-2 (fcvm-fleet-prod-alarms.tf)"
  event_pattern = jsonencode(local.fcvm_fleet_prod_change_patterns.templates.pattern)
  tags          = local.fcvm_fleet_prod_tags
}

resource "aws_iam_role" "fcvm_fleet_prod_change_forward" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "${local.fcvm_fleet_prod_alarm_prefix}-change-forward"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = local.fcvm_fleet_prod_tags
}

resource "aws_iam_role_policy" "fcvm_fleet_prod_change_forward" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "forward-to-the-home-region-bus"
  role     = aws_iam_role.fcvm_fleet_prod_change_forward[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "events:PutEvents"
      Resource = "arn:aws:events:${local.fcvm_fleet_home_region}:${local.fcvm_fleet_account_id}:event-bus/default"
    }]
  })
}

resource "aws_cloudwatch_event_target" "fcvm_fleet_prod_template_changes_east1" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet_east1
  rule     = aws_cloudwatch_event_rule.fcvm_fleet_prod_template_changes_east1[0].name
  arn      = "arn:aws:events:${local.fcvm_fleet_home_region}:${local.fcvm_fleet_account_id}:event-bus/default"
  role_arn = aws_iam_role.fcvm_fleet_prod_change_forward[0].arn
}

resource "aws_lambda_permission" "fcvm_fleet_prod_watch" {
  for_each = var.fcvm_fleet_prod_enabled ? merge(
    { for k in ["synthetic", "worker-deploys"] : "schedule-${k}" => "arn:aws:events:${local.fcvm_fleet_home_region}:${local.fcvm_fleet_account_id}:rule/${local.fcvm_fleet_prod_alarm_prefix}-watch-${k}" },
    { for k in keys(local.fcvm_fleet_prod_change_patterns) : "changes-${k}" => "arn:aws:events:${local.fcvm_fleet_home_region}:${local.fcvm_fleet_account_id}:rule/${local.fcvm_fleet_prod_alarm_prefix}-${k}-changes" },
  ) : {}
  provider      = aws.fcvm_fleet
  statement_id  = "Events-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fcvm_fleet_prod_watch[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = each.value
}

# ------------------------------------------------------------------------------------------------ the watch function's alarms
# The synthetic check fails, or did not run, in two consecutive 5-minute periods (one missed run does not alarm).
resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_synthetic" {
  count               = local.fcvm_fleet_prod
  provider            = aws.fcvm_fleet
  alarm_name          = "${local.fcvm_fleet_prod_alarm_prefix}-synthetic-check-failed"
  alarm_description   = "The prod synthetic check failed or did not run for 10 minutes: wss://${local.fcvm_dev_api_prod_hostname}/live did not open from ${local.fcvm_fleet_prod_site_origin} with a hello asking for Turnstile${local.fcvm_fleet_prod_page_public ? ", or ${local.fcvm_fleet_prod_site_origin}/ did not answer 200 with HTML" : ""}. This is what a visitor meets first. Read /aws/lambda/${local.fcvm_fleet_prod_alarm_prefix}-watch for synthetic_failure."
  comparison_operator = "LessThanThreshold"
  namespace           = "FCVM/Fleet"
  metric_name         = "SyntheticOk"
  dimensions          = local.fcvm_fleet_prod_dimension
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = local.fcvm_fleet_prod_notify
  ok_actions          = local.fcvm_fleet_prod_notify
  depends_on          = [aws_lambda_permission.fcvm_fleet_prod_alarms]
}

# Any write to the prod Worker by anything but the prod release token, or a poll that failed (no data) for 15 minutes.
resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_worker_changes" {
  count               = local.fcvm_fleet_prod
  provider            = aws.fcvm_fleet
  alarm_name          = "${local.fcvm_fleet_prod_alarm_prefix}-worker-changed-outside-release"
  alarm_description   = "The Cloudflare audit log shows a write to the prod Worker (${local.fcvm_fleet_prod_worker}: an upload, a version, a deployment, settings or secrets) by something other than the prod release token (${cloudflare_account_token.fcvm_prod_workers_deploy[0].name}), or the audit log could not be read for 15 minutes. The staging deploy token can deploy prod; check what fcvm.dev serves (wrangler deployments list --name ${local.fcvm_fleet_prod_worker}) and roll back by version id if it is not the release's. Who and what: /aws/lambda/${local.fcvm_fleet_prod_alarm_prefix}-watch, worker_change_outside_release."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  namespace           = "FCVM/Fleet"
  metric_name         = "WorkerChangesOutsideRelease"
  dimensions          = local.fcvm_fleet_prod_dimension
  statistic           = "Maximum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = local.fcvm_fleet_prod_notify
  ok_actions          = local.fcvm_fleet_prod_notify
  depends_on          = [aws_lambda_permission.fcvm_fleet_prod_alarms]
}

# Each change outside a release, once (an event, as the on-demand launch alarm).
resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_changed_outside_release" {
  count               = local.fcvm_fleet_prod
  provider            = aws.fcvm_fleet
  alarm_name          = "${local.fcvm_fleet_prod_alarm_prefix}-changed-outside-release"
  alarm_description   = "Something other than the prod release roles called UpdateService on ${local.fcvm_fleet_prod_coordinator} or ModifyLaunchTemplate or CreateLaunchTemplateVersion on a prod launch template (a Terraform apply included). Prod may now run a coordinator or image staging's smoke never passed. Who and what: /aws/lambda/${local.fcvm_fleet_prod_alarm_prefix}-watch, prod_change_outside_release."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  namespace           = "FCVM/Fleet"
  metric_name         = "ProdChangesOutsideRelease"
  dimensions          = local.fcvm_fleet_prod_dimension
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.fcvm_fleet_prod_notify
  depends_on          = [aws_lambda_permission.fcvm_fleet_prod_alarms]
}

# CloudWatch dashboard and metric alarms for the claude-master server.
#
# Data path: claude-master --otlp-endpoint -> the CloudWatch agent on the box (127.0.0.1:4318) -> namespace
# ClaudeMaster (claude-master-server.tf). What CloudWatch does with it, measured with synthetic OTLP through the
# live agent on 2026-10-01 (not read from documentation, which does not say):
#   * every attribute becomes a dimension, plus the resource's service.name, and every distinct combination is
#     its own billable custom metric. The proxy therefore never crosses its axes: each metric carries at most
#     three attributes, one projection per axis (docs/claude-master.md in ejc3/CLIProxyAPI);
#   * counters arrive as DELTAS (a Sum is a count);
#   * histograms arrive as approximate statistic sets (Sum, SampleCount, Min, Max; no percentiles), so latency
#     percentiles are the proxy's own duration_quantile gauges;
#   * Metrics Insights queries (SELECT ... FROM "ClaudeMaster" GROUP BY ...) aggregate across all the
#     dimension combinations, so the panels below need no dimension lists.
#
# Alarms here are Metrics Insights QUERY alarms with no dimension names, on purpose: an alarm that selects
# dimensions the series does not have sees no data and, with notBreaching, stays green forever (the memory
# alarms had exactly that bug in review). A query alarm cannot.

locals {
  cm_ns = local.claude_master_metrics_namespace

  # One panel per entry. queries: label and Metrics Insights query. Grouped queries get one line per group.
  claude_master_panels = [
    {
      title   = "Requests by user (client_account)"
      stacked = true
      queries = [{ label = "", q = "SELECT SUM(\"claude_master.inference.requests\") FROM \"${local.cm_ns}\" GROUP BY client_account" }]
    },
    {
      title   = "Requests by subscription (profile)"
      stacked = true
      queries = [{ label = "", q = "SELECT SUM(\"claude_master.inference.requests\") FROM \"${local.cm_ns}\" GROUP BY profile" }]
    },
    {
      title   = "Requests by box (client)"
      stacked = true
      queries = [{ label = "", q = "SELECT SUM(\"claude_master.inference.requests.by_client\") FROM \"${local.cm_ns}\" GROUP BY client" }]
    },
    {
      title   = "Requests by model"
      stacked = true
      queries = [{ label = "", q = "SELECT SUM(\"claude_master.inference.requests.by_model\") FROM \"${local.cm_ns}\" GROUP BY model" }]
    },
    {
      title   = "Outcome (status class)"
      stacked = true
      queries = [{ label = "", q = "SELECT SUM(\"claude_master.inference.requests\") FROM \"${local.cm_ns}\" GROUP BY status_class" }]
    },
    {
      title   = "Errors from Anthropic by status"
      stacked = true
      queries = [{ label = "", q = "SELECT SUM(\"claude_master.inference.errors\") FROM \"${local.cm_ns}\" GROUP BY status" }]
    },
    {
      title   = "Latency percentiles, recent requests (ms)"
      stacked = false
      queries = [
        { label = "p50", q = "SELECT MAX(\"claude_master.inference.duration_quantile\") FROM \"${local.cm_ns}\" WHERE quantile = '0.5'" },
        { label = "p95", q = "SELECT MAX(\"claude_master.inference.duration_quantile\") FROM \"${local.cm_ns}\" WHERE quantile = '0.95'" },
        { label = "p99", q = "SELECT MAX(\"claude_master.inference.duration_quantile\") FROM \"${local.cm_ns}\" WHERE quantile = '0.99'" },
      ]
    },
    {
      title   = "Average time to first byte by subscription (ms)"
      stacked = false
      queries = [{ label = "", q = "SELECT AVG(\"claude_master.inference.ttfb\") FROM \"${local.cm_ns}\" GROUP BY profile" }]
    },
    {
      title   = "Average duration by model (ms)"
      stacked = false
      queries = [{ label = "", q = "SELECT AVG(\"claude_master.inference.duration.by_model\") FROM \"${local.cm_ns}\" GROUP BY model" }]
    },
    {
      title   = "Anthropic's own first-byte time vs claude-master's added time (ms)"
      stacked = false
      queries = [
        { label = "Anthropic first byte", q = "SELECT AVG(\"claude_master.inference.upstream_ttfb\") FROM \"${local.cm_ns}\"" },
        { label = "claude-master overhead", q = "SELECT AVG(\"claude_master.proxy.overhead\") FROM \"${local.cm_ns}\"" },
      ]
    },
    {
      title   = "Weekly allowance used, by subscription (0 to 1; the last tenth is a reserve)"
      stacked = false
      queries = [{ label = "", q = "SELECT MAX(\"claude_master.quota.used_fraction\") FROM \"${local.cm_ns}\" GROUP BY profile" }]
    },
    {
      title   = "Anthropic's own utilization, by subscription and window"
      stacked = false
      queries = [{ label = "", q = "SELECT MAX(\"claude_master.anthropic.ratelimit\") FROM \"${local.cm_ns}\" WHERE measure = 'utilization' GROUP BY profile, \"window\"" }]
    },
    {
      title   = "Hours until each weekly allowance resets"
      stacked = false
      divide  = 3600 # the proxy publishes seconds; Metrics Insights cannot divide, metric math can
      queries = [{ label = "", q = "SELECT MAX(\"claude_master.quota.resets_in_seconds\") FROM \"${local.cm_ns}\" GROUP BY profile" }]
    },
    {
      title   = "Conversations moved between subscriptions (by reason)"
      stacked = true
      queries = [{ label = "", q = "SELECT SUM(\"claude_master.routing.switches\") FROM \"${local.cm_ns}\" GROUP BY reason" }]
    },
    {
      title   = "Rate limits from Anthropic, API-key backup use"
      stacked = false
      queries = [
        { label = "rate limited", q = "SELECT SUM(\"claude_master.quota.rate_limited\") FROM \"${local.cm_ns}\"" },
        { label = "API backup requests", q = "SELECT SUM(\"claude_master.routing.backup_requests\") FROM \"${local.cm_ns}\"" },
      ]
    },
    {
      title   = "Login health: seconds until each token expires"
      stacked = false
      queries = [{ label = "", q = "SELECT MIN(\"claude_master.auth.token_expires_in_seconds\") FROM \"${local.cm_ns}\" GROUP BY profile" }]
    },
    {
      title   = "Login refreshes and usage polls (by result)"
      stacked = true
      queries = [
        { label = "refresh", q = "SELECT SUM(\"claude_master.auth.refresh\") FROM \"${local.cm_ns}\" GROUP BY \"result\"" },
        { label = "usage poll", q = "SELECT SUM(\"claude_master.usage.polls\") FROM \"${local.cm_ns}\" GROUP BY \"result\"" },
      ]
    },
    {
      title   = "Proxy connections (by result) and refused handshakes"
      stacked = true
      queries = [
        { label = "connections", q = "SELECT SUM(\"claude_master.proxy.connections\") FROM \"${local.cm_ns}\" GROUP BY \"result\"" },
        { label = "TLS handshake errors", q = "SELECT SUM(\"claude_master.proxy.tls_handshake_errors\") FROM \"${local.cm_ns}\"" },
      ]
    },
    {
      title   = "Host memory and swap (%)"
      stacked = false
      queries = [
        { label = "memory", q = "SELECT MAX(mem_used_percent) FROM \"${local.cm_ns}\"" },
        { label = "swap", q = "SELECT MAX(swap_used_percent) FROM \"${local.cm_ns}\"" },
      ]
    },
    {
      title   = "Process: heap (bytes) and goroutines"
      stacked = false
      queries = [
        { label = "heap", q = "SELECT MAX(\"claude_master.process.heap_bytes\") FROM \"${local.cm_ns}\"" },
        { label = "goroutines", q = "SELECT MAX(\"claude_master.process.goroutines\") FROM \"${local.cm_ns}\"" },
      ]
    },
    {
      title   = "Alarms from the proxy's own log"
      stacked = false
      queries = [
        { label = "login rejected", q = "SELECT SUM(CredentialRejected) FROM \"${local.cm_ns}/Logs\"" },
        { label = "no account available", q = "SELECT SUM(NoAccountAvailable) FROM \"${local.cm_ns}/Logs\"" },
      ]
    },
  ]

  claude_master_dashboard_widgets = concat(
    [{
      type = "alarm", x = 0, y = 0, width = 24, height = 3
      properties = {
        title = "claude-master alarms"
        alarms = concat(
          [for a in aws_cloudwatch_metric_alarm.claude_master_server_status : a.arn],
          [for a in aws_cloudwatch_metric_alarm.claude_master_server_memory : a.arn],
          [for a in aws_cloudwatch_metric_alarm.claude_master_server_swap : a.arn],
          [for a in aws_cloudwatch_metric_alarm.claude_master_server_log : a.arn],
          [for a in aws_cloudwatch_metric_alarm.claude_master_pool : a.arn],
        )
      }
    }],
    [for i, p in local.claude_master_panels : {
      type = "metric", x = (i % 2) * 12, y = 3 + floor(i / 2) * 6, width = 12, height = 6
      properties = {
        title   = p.title
        region  = var.aws_region
        view    = "timeSeries"
        stacked = p.stacked
        period  = 300
        stat    = "Average"
        metrics = concat([for n, q in p.queries : (lookup(p, "divide", 0) > 0 ? [
          [{ expression = q.q, label = "", id = "q${i}_${n}", region = var.aws_region, visible = false }],
          [{ expression = "q${i}_${n}/${p.divide}", label = q.label, id = "e${i}_${n}", region = var.aws_region, visible = true }],
          ] : [
          [{ expression = q.q, label = q.label, id = "q${i}_${n}", region = var.aws_region, visible = true }],
        ])]...)
        yAxis = { left = { min = 0 } }
      }
    }],
  )
}

resource "aws_cloudwatch_dashboard" "claude_master" {
  count          = var.enable_claude_master_server ? 1 : 0
  dashboard_name = "claude-master"
  dashboard_body = jsonencode({ widgets = local.claude_master_dashboard_widgets })
}

# The pool is nearly used up: even the LEAST used subscription is past 90% of its weekly allowance, so the paid
# API-key backup is about to carry everything (or clients are about to be refused).
resource "aws_cloudwatch_metric_alarm" "claude_master_pool" {
  for_each            = var.enable_claude_master_server ? { pool_exhausted = 0.9, upstream_errors = 20 } : {}
  alarm_name          = each.key == "pool_exhausted" ? "claude-master-pool-nearly-exhausted" : "claude-master-upstream-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = each.key == "pool_exhausted" ? 3 : 1
  threshold           = each.value
  alarm_description   = each.key == "pool_exhausted" ? "Every claude-master subscription is past 90% of its weekly allowance: the paid API-key backup is about to carry everything or clients will be refused." : "More than 20 Anthropic error responses in 10 minutes through claude-master: an Anthropic incident, a rejected login, or a bug."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  metric_query {
    id          = "q"
    label       = each.key
    return_data = true
    expression  = each.key == "pool_exhausted" ? "SELECT MIN(\"claude_master.quota.used_fraction\") FROM \"${local.cm_ns}\"" : "SELECT SUM(\"claude_master.inference.errors\") FROM \"${local.cm_ns}\""
    # One ten-minute datapoint for the error count (two consecutive five-minute ones would each have to pass
    # 20 on their own); the allowance is judged over three five-minute points.
    period = each.key == "pool_exhausted" ? 300 : 600
  }
}

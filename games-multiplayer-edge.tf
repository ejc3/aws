# games-multiplayer-edge.tf
#
# The public edge of play.cc-games.app: the first AWS resource this account exposes to people
# outside the family. games-multiplayer.tf holds the ALB, router and engines; this file holds
# what stands in front of and beside them: a WAF web ACL, ALB access logs, router
# autoscaling, and health alarms. docs/games-multiplayer.md "Security design and threat model"
# describes the whole surface.
#
# WAF (REGIONAL, on the ALB). The router already verifies every join token and rate-limits
# per IP (MP_RATE_PER_SEC 50, burst 100; MP_MAX_CONNS_PER_IP 20; per task). The WAF adds what
# the router cannot: dropping floods and known-bad sources before they cost router CPU or
# ALB capacity.
#   rate-per-ip    BLOCK above 6,000 requests per IP per 60 s (100/s). WebSocket frames after
#                  the upgrade are not HTTP requests, so a player on WebSocket costs one request
#                  per connection. The HTTP-polling fallback is up to ~10 requests/s a player,
#                  so ~10 kids behind one home NAT still fit. 100/s is also what two router
#                  tasks would accept from one IP (50/s each), so the WAF only drops traffic the
#                  routers would largely refuse anyway, and does it at the edge.
#   ip-reputation  AWSManagedRulesAmazonIpReputationList, BLOCK.
#   bad-inputs     AWSManagedRulesKnownBadInputsRuleSet, BLOCK (Log4j, SSRF-style payloads).
#   common         AWSManagedRulesCommonRuleSet in COUNT for now. Its body-size and
#                  cross-site rules can misfire on WebSocket upgrades and JSON game traffic;
#                  flip to BLOCK once its matches in aws-waf-logs-games-play look clean.
# Only BLOCK and COUNT decisions are logged, to keep log volume (and cost) proportional to
# trouble, not to play.

resource "aws_wafv2_web_acl" "games_play" {
  name        = "games-play"
  description = "play.cc-games.app: per-IP rate limit and AWS managed protections in front of mp-router"
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "rate-per-ip"
    priority = 0
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit                 = 6000
        evaluation_window_sec = 60
        aggregate_key_type    = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "games-play-rate-per-ip"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "ip-reputation"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "games-play-ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "bad-inputs"
    priority = 2
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "games-play-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "common"
    priority = 3
    # COUNT, not BLOCK, until its matches on real game traffic have been reviewed (see above).
    override_action {
      count {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesCommonRuleSet"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "games-play-common"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "games-play"
    sampled_requests_enabled   = true
  }

  tags = { Name = "games-play", Project = "games-multiplayer" }
}

resource "aws_wafv2_web_acl_association" "games_play" {
  resource_arn = aws_lb.games_play.arn
  web_acl_arn  = aws_wafv2_web_acl.games_play.arn
}

# WAF logs to CloudWatch Logs must go to a group named aws-waf-logs-*.
resource "aws_cloudwatch_log_group" "games_play_waf" {
  name              = "aws-waf-logs-games-play"
  retention_in_days = 30
  tags              = { Name = "aws-waf-logs-games-play", Project = "games-multiplayer" }
}

resource "aws_wafv2_web_acl_logging_configuration" "games_play" {
  resource_arn            = aws_wafv2_web_acl.games_play.arn
  log_destination_configs = [aws_cloudwatch_log_group.games_play_waf.arn]

  logging_filter {
    default_behavior = "DROP"
    filter {
      behavior    = "KEEP"
      requirement = "MEETS_ANY"
      condition {
        action_condition {
          action = "BLOCK"
        }
      }
      condition {
        action_condition {
          action = "COUNT"
        }
      }
    }
  }
}

# -------------------------------------------------------------------------------------
# ALB access logs: who connected, from where, and what the ALB answered. ALB log delivery
# supports SSE-S3 only (not KMS). us-west-1 predates August 2022, so delivery is granted to
# the region's Elastic Load Balancing account (data.aws_elb_service_account), not the newer
# service principal.
# -------------------------------------------------------------------------------------

data "aws_elb_service_account" "current" {}

resource "aws_s3_bucket" "games_play_alb_logs" {
  bucket = "games-play-alb-logs-${data.aws_caller_identity.current.account_id}"
  tags   = { Name = "games-play-alb-logs", Project = "games-multiplayer" }
}

resource "aws_s3_bucket_public_access_block" "games_play_alb_logs" {
  bucket                  = aws_s3_bucket.games_play_alb_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "games_play_alb_logs" {
  bucket = aws_s3_bucket.games_play_alb_logs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "games_play_alb_logs" {
  bucket = aws_s3_bucket.games_play_alb_logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "games_play_alb_logs" {
  bucket = aws_s3_bucket.games_play_alb_logs.id
  rule {
    id     = "expire-access-logs"
    status = "Enabled"
    filter {}
    expiration { days = 30 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

resource "aws_s3_bucket_policy" "games_play_alb_logs" {
  bucket = aws_s3_bucket.games_play_alb_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ElbLogDelivery"
        Effect    = "Allow"
        Principal = { AWS = data.aws_elb_service_account.current.arn }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.games_play_alb_logs.arn}/games-play/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.games_play_alb_logs.arn, "${aws_s3_bucket.games_play_alb_logs.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.games_play_alb_logs]
}

# -------------------------------------------------------------------------------------
# Router capacity. The router is stateless (it verifies the token and proxies to the match's
# task), so more tasks is the whole scaling story. Two at minimum: one task is a single
# point of failure for every live match, and a deployment or a spike should not be. Target
# tracking on CPU adds tasks under load, up to 6. Emergency off switch:
#   terraform apply -var mp_router_min_count=0 -var mp_router_max_count=0
# -------------------------------------------------------------------------------------

resource "aws_appautoscaling_target" "games_mp_router" {
  count              = local.mp_router_enabled ? 1 : 0
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.games.name}/${aws_ecs_service.games_mp_router[0].name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.mp_router_min_count
  max_capacity       = var.mp_router_max_count
}

resource "aws_appautoscaling_policy" "games_mp_router_cpu" {
  count              = local.mp_router_enabled ? 1 : 0
  name               = "mp-router-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.games_mp_router[0].service_namespace
  resource_id        = aws_appautoscaling_target.games_mp_router[0].resource_id
  scalable_dimension = aws_appautoscaling_target.games_mp_router[0].scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = 60
    scale_in_cooldown  = 300
    scale_out_cooldown = 60
    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

# -------------------------------------------------------------------------------------
# Health alarms, to the same topic as every other alert.
# -------------------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "games_play_unhealthy_router" {
  count               = local.mp_router_enabled ? 1 : 0
  alarm_name          = "games-play-unhealthy-router"
  alarm_description   = "play.cc-games.app has an unhealthy mp-router target for 5 minutes"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "UnHealthyHostCount"
  dimensions          = { LoadBalancer = aws_lb.games_play.arn_suffix, TargetGroup = aws_lb_target_group.games_mp_router.arn_suffix }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "games_play_no_healthy_router" {
  count               = local.mp_router_enabled ? 1 : 0
  alarm_name          = "games-play-no-healthy-router"
  alarm_description   = "play.cc-games.app has NO healthy mp-router target: every match is unreachable"
  namespace           = "AWS/ApplicationELB"
  metric_name         = "HealthyHostCount"
  dimensions          = { LoadBalancer = aws_lb.games_play.arn_suffix, TargetGroup = aws_lb_target_group.games_mp_router.arn_suffix }
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# 5xx as a share of requests, so a quiet evening with one failed request does not page.
resource "aws_cloudwatch_metric_alarm" "games_play_5xx_rate" {
  for_each = {
    target = { metric = "HTTPCode_Target_5XX_Count", what = "the router (or an engine behind it)" }
    elb    = { metric = "HTTPCode_ELB_5XX_Count", what = "the load balancer itself" }
  }
  alarm_name          = "games-play-${each.key}-5xx-rate"
  alarm_description   = "Over 5% of play.cc-games.app requests got a 5xx from ${each.value.what} for 10 minutes (with at least 20 requests)"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]

  metric_query {
    id          = "rate"
    expression  = "IF(requests >= 20, 100 * FILL(errors, 0) / requests, 0)"
    label       = "5xx percent"
    return_data = true
  }
  metric_query {
    id = "errors"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = each.value.metric
      dimensions  = { LoadBalancer = aws_lb.games_play.arn_suffix }
      stat        = "Sum"
      period      = 300
    }
  }
  metric_query {
    id = "requests"
    metric {
      namespace   = "AWS/ApplicationELB"
      metric_name = "RequestCount"
      dimensions  = { LoadBalancer = aws_lb.games_play.arn_suffix }
      stat        = "Sum"
      period      = 300
    }
  }
}

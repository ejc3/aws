# Account-wide security visibility. Public SSH and Eternal Terminal are an explicit
# operator requirement and remain unchanged. Cloudflare service-token access is an
# independent, intentional automation path and is not altered by these controls.

locals {
  # Workload posture approved separately by the owner on 2026-09-12, after the
  # monitoring foundation. Keep its coverage limited to the five account/regions.
  # This does not gate future-volume encryption or change any existing disk.
  security_posture_enabled = true

  security_account_ids = [
    data.aws_caller_identity.current.account_id,
    aws_organizations_account.dev_staging[0].id,
  ]
  security_trail_name = "ejc3-security-audit"
  security_trail_arn  = "arn:aws:cloudtrail:us-west-1:${data.aws_caller_identity.current.account_id}:trail/${local.security_trail_name}"
  security_config_role_arns = [
    for account in local.security_account_ids : "arn:aws:iam::${account}:role/security-config-*"
  ]
}

# Locked archive for CloudTrail, selected object-access events, and VPC flow logs.
# Config/SSM use a separate versioned bucket below; Config cannot deliver to a
# bucket with Object Lock default retention. The trail also uses its own CMK.
resource "aws_s3_bucket" "security_audit" {
  bucket              = "ejc3-security-audit-${data.aws_caller_identity.current.account_id}"
  object_lock_enabled = true
  tags                = { Name = "security-audit", Managed = "terraform" }
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "security_audit" {
  bucket                  = aws_s3_bucket.security_audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "security_audit" {
  bucket = aws_s3_bucket.security_audit.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "security_audit" {
  bucket = aws_s3_bucket.security_audit.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_versioning" "security_audit" {
  bucket = aws_s3_bucket.security_audit.id
  versioning_configuration { status = "Enabled" }
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_object_lock_configuration" "security_audit" {
  bucket = aws_s3_bucket.security_audit.id
  rule {
    default_retention {
      mode = "GOVERNANCE"
      days = 90
    }
  }
  depends_on = [aws_s3_bucket_versioning.security_audit]
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_lifecycle_configuration" "security_audit" {
  bucket = aws_s3_bucket.security_audit.id
  rule {
    id     = "one-year-audit-history"
    status = "Enabled"
    filter {}
    expiration { days = 365 }
    noncurrent_version_expiration { noncurrent_days = 365 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
  depends_on = [aws_s3_bucket_versioning.security_audit]
}

resource "aws_s3_bucket_policy" "security_audit" {
  bucket = aws_s3_bucket.security_audit.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RequireTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.security_audit.arn, "${aws_s3_bucket.security_audit.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
      },
      {
        Sid       = "CloudTrailBucketCheck"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.security_audit.arn
        Condition = { StringEquals = { "aws:SourceArn" = local.security_trail_arn } }
      },
      {
        Sid       = "OrganizationCloudTrailDelivery"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource = [
          "${aws_s3_bucket.security_audit.arn}/cloudtrail/AWSLogs/${data.aws_caller_identity.current.account_id}/*",
          "${aws_s3_bucket.security_audit.arn}/cloudtrail/AWSLogs/${data.aws_organizations_organization.current.id}/*",
        ]
        Condition = { StringEquals = {
          "s3:x-amz-acl"  = "bucket-owner-full-control"
          "aws:SourceArn" = local.security_trail_arn
        } }
      },
      {
        Sid       = "VPCFlowBucketCheck"
        Effect    = "Allow"
        Principal = { Service = "delivery.logs.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.security_audit.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
          ArnLike      = { "aws:SourceArn" = "arn:aws:logs:*:${data.aws_caller_identity.current.account_id}:*" }
        }
      },
      {
        Sid       = "VPCFlowDelivery"
        Effect    = "Allow"
        Principal = { Service = "delivery.logs.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.security_audit.arn}/vpc-flow/AWSLogs/${data.aws_caller_identity.current.account_id}/*"
        Condition = {
          StringEquals = {
            "aws:SourceAccount" = data.aws_caller_identity.current.account_id
            "s3:x-amz-acl"      = "bucket-owner-full-control"
          }
          ArnLike = { "aws:SourceArn" = "arn:aws:logs:*:${data.aws_caller_identity.current.account_id}:*" }
        }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.security_audit, aws_s3_bucket_ownership_controls.security_audit]
}

# AWS Config explicitly rejects Object Lock default-retention destinations:
# https://docs.aws.amazon.com/config/latest/developerguide/manage-delivery-channel.html
# Versioning preserves earlier history/transcripts; only write access is given
# to recorders/instances; the bucket and versioning have Terraform destroy guards.
resource "aws_s3_bucket" "security_config" {
  bucket = "ejc3-security-records-${data.aws_caller_identity.current.account_id}"
  tags   = { Name = "security-records", Managed = "terraform" }
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "security_config" {
  bucket                  = aws_s3_bucket.security_config.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "security_config" {
  bucket = aws_s3_bucket.security_config.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "security_config" {
  bucket = aws_s3_bucket.security_config.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_versioning" "security_config" {
  bucket = aws_s3_bucket.security_config.id
  versioning_configuration { status = "Enabled" }
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_lifecycle_configuration" "security_config" {
  bucket = aws_s3_bucket.security_config.id
  rule {
    id     = "one-year-security-records"
    status = "Enabled"
    filter {}
    expiration { days = 365 }
    noncurrent_version_expiration { noncurrent_days = 365 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
  depends_on = [aws_s3_bucket_versioning.security_config]
}

resource "aws_s3_bucket_policy" "security_config" {
  bucket = aws_s3_bucket.security_config.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RequireTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.security_config.arn, "${aws_s3_bucket.security_config.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
      },
      {
        Sid       = "ConfigBucketCheck"
        Effect    = "Allow"
        Principal = { Service = "config.amazonaws.com" }
        Action    = ["s3:GetBucketAcl", "s3:ListBucket"]
        Resource  = aws_s3_bucket.security_config.arn
        Condition = { StringEquals = { "aws:SourceAccount" = local.security_account_ids } }
      },
      {
        Sid       = "ConfigServiceDelivery"
        Effect    = "Allow"
        Principal = { Service = "config.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = [for account in local.security_account_ids : "${aws_s3_bucket.security_config.arn}/config/AWSLogs/${account}/Config/*"]
        Condition = { StringEquals = {
          "aws:SourceAccount" = local.security_account_ids
          "s3:x-amz-acl"      = "bucket-owner-full-control"
        } }
      },
      {
        Sid       = "ConfigRoleBucketCheck"
        Effect    = "Allow"
        Principal = { AWS = [for account in local.security_account_ids : "arn:aws:iam::${account}:root"] }
        Action    = ["s3:GetBucketAcl", "s3:ListBucket"]
        Resource  = aws_s3_bucket.security_config.arn
        Condition = { ArnLike = { "aws:PrincipalArn" = local.security_config_role_arns } }
      },
      {
        Sid       = "ConfigRoleDelivery"
        Effect    = "Allow"
        Principal = { AWS = [for account in local.security_account_ids : "arn:aws:iam::${account}:root"] }
        Action    = "s3:PutObject"
        Resource  = [for account in local.security_account_ids : "${aws_s3_bucket.security_config.arn}/config/AWSLogs/${account}/Config/*"]
        Condition = {
          ArnLike      = { "aws:PrincipalArn" = local.security_config_role_arns }
          StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" }
        }
      }
    ]
  })
  depends_on = [
    aws_s3_bucket_public_access_block.security_config,
    aws_s3_bucket_ownership_controls.security_config,
    aws_s3_bucket_server_side_encryption_configuration.security_config,
    aws_s3_bucket_versioning.security_config,
  ]
}

resource "aws_kms_key" "security_cloudtrail" {
  description             = "CloudTrail audit encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "CloudTrailEncryption"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "kms:GenerateDataKey*"
        Resource  = "*"
        Condition = {
          StringEquals = { "aws:SourceArn" = local.security_trail_arn }
          StringLike   = { "kms:EncryptionContext:aws:cloudtrail:arn" = [for account in local.security_account_ids : "arn:aws:cloudtrail:*:${account}:trail/*"] }
        }
      },
      {
        Sid       = "CloudTrailDescribeKey"
        Effect    = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action    = "kms:DescribeKey"
        Resource  = "*"
        Condition = { StringEquals = { "aws:SourceArn" = local.security_trail_arn } }
      }
    ]
  })
  tags = { Managed = "terraform", Purpose = "security-audit" }
  lifecycle { prevent_destroy = true }
}

resource "aws_kms_alias" "security_cloudtrail" {
  name          = "alias/security-cloudtrail"
  target_key_id = aws_kms_key.security_cloudtrail.key_id
}

resource "aws_cloudtrail" "security" {
  name                          = local.security_trail_name
  s3_bucket_name                = aws_s3_bucket.security_audit.id
  s3_key_prefix                 = "cloudtrail"
  kms_key_id                    = aws_kms_key.security_cloudtrail.arn
  is_multi_region_trail         = true
  is_organization_trail         = true
  include_global_service_events = true
  enable_log_file_validation    = true
  enable_logging                = true
  advanced_event_selector {
    name = "All management events"
    field_selector {
      field  = "eventCategory"
      equals = ["Management"]
    }
  }
  advanced_event_selector {
    name = "Sensitive S3 object access"
    field_selector {
      field  = "eventCategory"
      equals = ["Data"]
    }
    field_selector {
      field  = "resources.type"
      equals = ["AWS::S3::Object"]
    }
    field_selector {
      field = "resources.ARN"
      starts_with = [
        "arn:aws:s3:::ejc3-terraform-state/",
        "arn:aws:s3:::ejc3-dev-scripts/",
      ]
    }
  }
  depends_on = [aws_organizations_organization.security, aws_s3_bucket_policy.security_audit, aws_s3_bucket_object_lock_configuration.security_audit]
  lifecycle { prevent_destroy = true }
}

resource "aws_flow_log" "security_main" {
  for_each = merge(
    { dev = local.vpc_id },
    var.enable_github_runner ? { runner = aws_vpc.runner[0].id } : {},
  )
  vpc_id                   = each.value
  traffic_type             = "ALL"
  log_destination_type     = "s3"
  log_destination          = "${aws_s3_bucket.security_audit.arn}/vpc-flow"
  max_aggregation_interval = 600
  tags                     = { Name = "security-${each.key}-flow-log", Managed = "terraform" }
  depends_on               = [aws_s3_bucket_policy.security_audit]
}

resource "aws_flow_log" "security_west2" {
  provider                 = aws.west2
  vpc_id                   = data.aws_vpc.west2_default.id
  traffic_type             = "ALL"
  log_destination_type     = "s3"
  log_destination          = "${aws_s3_bucket.security_audit.arn}/vpc-flow"
  max_aggregation_interval = 600
  tags                     = { Name = "security-west2-flow-log", Managed = "terraform" }
  depends_on               = [aws_s3_bucket_policy.security_audit]
}

resource "aws_cloudwatch_event_bus" "security_alerts" {
  name = "security-alerts"
}

resource "aws_cloudwatch_event_bus_policy" "security_alerts" {
  event_bus_name = aws_cloudwatch_event_bus.security_alerts.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "SecurityForwardingRolesOnly"
      Effect    = "Allow"
      Principal = { AWS = [for account in local.security_account_ids : "arn:aws:iam::${account}:root"] }
      Action    = "events:PutEvents"
      Resource  = aws_cloudwatch_event_bus.security_alerts.arn
      Condition = { ArnEquals = { "aws:PrincipalArn" = [
        aws_iam_role.security_forward.arn,
        aws_iam_role.security_forward_staging.arn,
      ] } }
    }]
  })
}

resource "aws_iam_role" "security_forward" {
  name = "security-findings-forward"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike = { "aws:SourceArn" = [for rule in ["security-forward-findings", "security-forward-audit"] :
          "arn:aws:events:*:${data.aws_caller_identity.current.account_id}:rule/${rule}"
        ] }
      }
    }]
  })
}

resource "aws_iam_role_policy" "security_forward" {
  name = "publish-central-security-events"
  role = aws_iam_role.security_forward.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "events:PutEvents"
      Resource = aws_cloudwatch_event_bus.security_alerts.arn
    }]
  })
}

resource "aws_iam_role" "security_forward_staging" {
  provider = aws.staging
  name     = "security-findings-forward"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = aws_organizations_account.dev_staging[0].id }
        ArnLike = { "aws:SourceArn" = [for rule in ["security-forward-findings", "security-forward-audit"] :
          "arn:aws:events:*:${aws_organizations_account.dev_staging[0].id}:rule/${rule}"
        ] }
      }
    }]
  })
}

resource "aws_iam_role_policy" "security_forward_staging" {
  provider = aws.staging
  name     = "publish-central-security-events"
  role     = aws_iam_role.security_forward_staging.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "events:PutEvents"
      Resource = aws_cloudwatch_event_bus.security_alerts.arn
    }]
  })
}

resource "aws_cloudwatch_event_rule" "security_notify" {
  name           = "security-alerts-notify"
  event_bus_name = aws_cloudwatch_event_bus.security_alerts.name
  event_pattern  = jsonencode({ account = local.security_account_ids })
  # Forwarded CloudTrail reads need this state on the custom-bus rule too.
  state = "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"
}

# Use an execution role for SNS delivery: its trust pins the exact originating
# rule, without relying on the legacy SNS service-principal condition behavior.
resource "aws_iam_role" "security_notify" {
  name = "security-alerts-notify"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnEquals    = { "aws:SourceArn" = aws_cloudwatch_event_rule.security_notify.arn }
      }
    }]
  })
}

resource "aws_iam_role_policy" "security_notify" {
  name = "publish-security-notifications"
  role = aws_iam_role.security_notify.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "sns:Publish"
      Resource = aws_sns_topic.cost_alerts.arn
    }]
  })
}

resource "aws_cloudwatch_event_target" "security_notify" {
  event_bus_name = aws_cloudwatch_event_bus.security_alerts.name
  rule           = aws_cloudwatch_event_rule.security_notify.name
  target_id      = "confirmed-operator-email"
  arn            = aws_sns_topic.cost_alerts.arn
  role_arn       = aws_iam_role.security_notify.arn
  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 185
  }
  dead_letter_config { arn = aws_sqs_queue.security_delivery_failures.arn }
  depends_on = [aws_iam_role_policy.security_notify, aws_sqs_queue_policy.security_delivery_failures]
}

resource "aws_sqs_queue" "security_delivery_failures" {
  name                      = "security-central-delivery-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
  tags                      = { Managed = "terraform", Purpose = "security-delivery" }
}

resource "aws_sqs_queue_policy" "security_delivery_failures" {
  queue_url = aws_sqs_queue.security_delivery_failures.url
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.security_delivery_failures.arn
        Condition = { ArnEquals = { "aws:SourceArn" = [
          aws_cloudwatch_event_rule.security_notify.arn,
          aws_cloudwatch_event_rule.security_delivery_health.arn,
        ] } }
      },
      {
        Effect    = "Deny"
        Principal = "*"
        Action    = "sqs:*"
        Resource  = aws_sqs_queue.security_delivery_failures.arn
        Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
      }
    ]
  })
}

locals {
  # Match the explicit regional modules below. New opted-in Regions must be added
  # to both places; the observer refuses an empty/unbounded region list.
  # 72 GetMetricData metric queries per five minutes = 622,080 per 30 days.
  # At 2026-09 rates ($0.014/1k in Sao Paulo, $0.01/1k elsewhere here),
  # retrieval is ~$6.36/month; four metrics + five alarms add ~$1.70/month.
  # Lambda, SQS and logs are additional usage charges; no free tier assumed.
  security_delivery_regions = [
    "ap-south-1", "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-southeast-1", "ap-southeast-2", "ca-central-1", "eu-central-1",
    "eu-north-1", "eu-west-1", "eu-west-2", "eu-west-3", "sa-east-1",
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
  ]
  security_delivery_watchdog = <<-PYTHON
    import concurrent.futures
    import datetime
    import json
    import math
    import os
    import re
    import boto3
    from botocore.config import Config

    CONFIG = Config(connect_timeout=2, read_timeout=3, retries={"total_max_attempts": 1})
    NAMESPACE = "SecurityDelivery"
    QUEUE = "security-findings-delivery-dlq"

    FORWARD_RULES = ["security-forward-findings", "security-forward-audit"]

    def expected_target(rule, account, region, main_account, sns_topic):
        if rule in FORWARD_RULES:
            return {"Id": "central-security-alerts", "Arn": "arn:aws:events:us-west-1:"+main_account+":event-bus/security-alerts",
                "RoleArn": "arn:aws:iam::"+account+":role/security-findings-forward",
                "DeadLetterConfig": {"Arn": "arn:aws:sqs:"+region+":"+account+":"+QUEUE}}
        target = {"DeadLetterConfig": {"Arn": "arn:aws:sqs:us-west-1:"+main_account+":security-central-delivery-dlq"}}
        if rule == "security-alerts-notify":
            target.update({"Id": "confirmed-operator-email", "Arn": sns_topic,
                "RoleArn": "arn:aws:iam::"+main_account+":role/security-alerts-notify",
                "RetryPolicy": {"MaximumEventAgeInSeconds": 86400, "MaximumRetryAttempts": 185}})
        elif rule == "security-delivery-health":
            target.update({"Id": rule, "Arn": "arn:aws:lambda:us-west-1:"+main_account+":function:security-delivery-health",
                "RetryPolicy": {"MaximumEventAgeInSeconds": 300, "MaximumRetryAttempts": 2}})
        else:
            raise ValueError("Unexpected monitored rule")
        return target

    def parse_pattern(text):
        def unique_object(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate event-pattern key")
                result[key] = value
            return result
        value = json.loads(text, object_pairs_hook=unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Invalid JSON constant")))
        if not isinstance(value, dict) or not value:
            raise ValueError("Invalid event-pattern object")
        # Ignore object-key order/formatting, but preserve array operator ordering
        # and scalar types: JSON false is not the numeric value zero.
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)

    def inspect_region(cloudwatch, sqs, events, account, region, main_account, sns_topic, now, patterns):
        rules = [(name, None) for name in FORWARD_RULES]
        queues = [QUEUE]
        if account == main_account and region == "us-west-1":
            rules.extend([("security-alerts-notify", "security-alerts"), ("security-delivery-health", None)])
            queues.append("security-central-delivery-dlq")
        # Both rules' topology and the shared queue are checked every five
        # minutes. Alternate only failure-metric reads in UTC epoch slots: each
        # rule is revisited every ten minutes with a 15-minute overlap. CloudWatch
        # bills underlying metrics even when SUM returns one series; this keeps
        # the reviewed 72 metrics/run without pretending aggregation is free.
        sampled_rule = FORWARD_RULES[int(now.timestamp()) // 300 % len(FORWARD_RULES)]
        metric_rules = [(name, bus) for name, bus in rules if bus or name == "security-delivery-health" or name == sampled_rule]
        queries = []
        for rule, bus in metric_rules:
            dimensions = [{"Name": "RuleName", "Value": rule}]
            if bus:
                dimensions.append({"Name": "EventBusName", "Value": bus})
            # Casing follows the AWS/Events metrics reference ("Dlq", not "DLQ").
            for metric in ["FailedInvocations", "InvocationsFailedToBeSentToDlq"]:
                queries.append({"Id": "m" + str(len(queries)), "ReturnData": True, "MetricStat": {
                    "Metric": {"Namespace": "AWS/Events", "MetricName": metric, "Dimensions": dimensions},
                    "Period": 300, "Stat": "Sum"}})
        failures, depth, errors = 0, 0, 0
        # Sparse failure metrics cannot distinguish a quiet rule from a deleted
        # rule/target. Independently verify the enabled, exact delivery topology.
        # Never follow pagination or accept additional/redirected/transformed targets.
        for rule, bus in rules:
            scope = {"EventBusName": bus} if bus else {}
            expected_arn = "arn:aws:events:"+region+":"+account+":rule/"+(bus+"/" if bus else "")+rule
            try:
                state = events.describe_rule(Name=rule, **scope)
                expected_state = "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS" if rule in ("security-forward-audit", "security-alerts-notify") else "ENABLED"
                if state.get("State") != expected_state or state.get("Name") != rule or state.get("Arn") != expected_arn:
                    errors += 1
                if rule == "security-delivery-health" and state.get("ScheduleExpression") != "rate(5 minutes)":
                    errors += 1
                if rule == "security-delivery-health":
                    if state.get("EventPattern"):
                        errors += 1
                elif state.get("ScheduleExpression") or parse_pattern(state.get("EventPattern")) != patterns[rule]:
                    errors += 1
            except Exception:
                errors += 1
            try:
                response = events.list_targets_by_rule(Rule=rule, Limit=2, **scope)
                if response.get("NextToken") or response.get("Targets") != [expected_target(rule, account, region, main_account, sns_topic)]:
                    errors += 1
            except Exception:
                errors += 1
        try:
            response = cloudwatch.get_metric_data(
                MetricDataQueries=queries, StartTime=now-datetime.timedelta(minutes=15), EndTime=now,
                MaxDatapoints=100, ScanBy="TimestampDescending")
            results = response.get("MetricDataResults", [])
            if response.get("NextToken") or response.get("Messages") or len(results) != len(queries):
                errors += 1
            seen = set()
            for result in results:
                identifier = result.get("Id")
                if identifier not in {query["Id"] for query in queries} or identifier in seen:
                    errors += 1
                    continue
                seen.add(identifier)
                if result.get("StatusCode") != "Complete" or result.get("Messages"):
                    errors += 1
                values = result.get("Values", [])
                if not isinstance(values, list) or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values):
                    errors += 1
                else:
                    failures += sum(values)
        except Exception:
            errors += 1
        for queue in queues:
            try:
                attributes = sqs.get_queue_attributes(
                    QueueUrl="https://sqs."+region+".amazonaws.com/"+account+"/"+queue,
                    AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible", "ApproximateNumberOfMessagesDelayed"])["Attributes"]
                counts = [int(attributes[name]) for name in ["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible", "ApproximateNumberOfMessagesDelayed"]]
                if any(count < 0 for count in counts):
                    raise ValueError("Invalid queue counters")
                depth += sum(counts)
            except Exception:
                errors += 1
        return {"account": account, "region": region, "failures": failures, "depth": depth, "errors": errors}

    def handler(event, context):
        main_account, staging_account = os.environ["MAIN_ACCOUNT"], os.environ["STAGING_ACCOUNT"]
        regions = json.loads(os.environ["REGIONS"])
        sns_topic = os.environ["SNS_TOPIC_ARN"]
        configured_patterns = json.loads(os.environ["EVENT_PATTERNS"])
        if not isinstance(configured_patterns, dict) or set(configured_patterns) != set(FORWARD_RULES + ["security-alerts-notify"]):
            raise ValueError("Invalid expected event patterns")
        patterns = {name: parse_pattern(value) for name, value in configured_patterns.items()}
        if (not isinstance(regions, list) or not 1 <= len(regions) <= 17
                or any(not isinstance(region, str) or not re.fullmatch(r"[a-z]{2}-[a-z]+-[1-9]", region) for region in regions)
                or len(set(regions)) != len(regions)
                or not re.fullmatch(r"[0-9]{12}", main_account) or not re.fullmatch(r"[0-9]{12}", staging_account)
                or main_account == staging_account
                or not re.fullmatch(r"arn:aws:sns:us-west-1:"+main_account+r":[A-Za-z0-9_-]{1,256}", sns_topic)):
            raise ValueError("Invalid security region coverage")
        now = datetime.datetime.now(datetime.timezone.utc)
        main = boto3.Session(region_name="us-west-1")
        sessions = [(main_account, main)]
        errors = 0
        try:
            creds = main.client("sts", config=CONFIG).assume_role(
                RoleArn="arn:aws:iam::"+staging_account+":role/security-delivery-observer",
                RoleSessionName="security-delivery-health", DurationSeconds=900)["Credentials"]
            sessions.append((staging_account, boto3.Session(aws_access_key_id=creds["AccessKeyId"],
                aws_secret_access_key=creds["SecretAccessKey"], aws_session_token=creds["SessionToken"])))
        except Exception:
            errors += 1
        rows = []
        tasks = []
        # Sessions are not thread-safe. Create all clients serially, then share
        # only the independent regional clients with the bounded worker pool.
        for account, session in sessions:
            for region in regions:
                try:
                    tasks.append((session.client("cloudwatch", region_name=region, config=CONFIG),
                        session.client("sqs", region_name=region, config=CONFIG),
                        session.client("events", region_name=region, config=CONFIG), account, region, main_account, sns_topic, now, patterns))
                except Exception:
                    errors += 1
        # 34 account/region jobs, six concurrent readers, short SDK timeouts and
        # no retries. Lambda enforces the hard 90-second cap; an overdue check
        # fails closed through the independent heartbeat. No queue data is read.
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(inspect_region, *task) for task in tasks]
            for future in concurrent.futures.as_completed(futures):
                try:
                    row = future.result()
                    rows.append(row)
                    errors += row["errors"]
                except Exception:
                    errors += 1
        # Gauges over the last 15 minutes, not a lifetime count. Missing API access
        # is an error, never a healthy zero. Heartbeat is emitted only after publishing.
        values = {"FailedDeliveries": sum(row["failures"] for row in rows),
            "QueuedEvents": sum(row["depth"] for row in rows), "ObserverErrors": errors, "Heartbeat": 1}
        main.client("cloudwatch", region_name="us-west-1", config=CONFIG).put_metric_data(
            Namespace=NAMESPACE, MetricData=[{"MetricName": key, "Value": value, "Unit": "Count", "Timestamp": now}
                for key, value in values.items()])
        print(json.dumps({"checked_regions": len(rows), "unhealthy": [row for row in rows if row["failures"] or row["depth"] or row["errors"]]}))
        return values
  PYTHON
}

resource "aws_iam_role" "security_delivery_health" {
  name = "security-delivery-health"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role" "security_delivery_observer_staging" {
  provider = aws.staging
  name     = "security-delivery-observer"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { AWS = aws_iam_role.security_delivery_health.arn }, Action = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "security_delivery_observer_staging" {
  provider = aws.staging
  name     = "read-delivery-health-only"
  role     = aws_iam_role.security_delivery_observer_staging.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow", Action = "cloudwatch:GetMetricData", Resource = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.security_delivery_regions } }
      },
      {
        Effect   = "Allow", Action = "sqs:GetQueueAttributes"
        Resource = [for region in local.security_delivery_regions : "arn:aws:sqs:${region}:${aws_organizations_account.dev_staging[0].id}:security-findings-delivery-dlq"]
      },
      {
        Effect = "Allow", Action = ["events:DescribeRule", "events:ListTargetsByRule"]
        Resource = flatten([for region in local.security_delivery_regions : [for rule in ["security-forward-findings", "security-forward-audit"] :
          "arn:aws:events:${region}:${aws_organizations_account.dev_staging[0].id}:rule/${rule}"
        ]])
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "security_delivery_health" {
  name              = "/aws/lambda/security-delivery-health"
  retention_in_days = 30
}

resource "aws_iam_role_policy" "security_delivery_health" {
  name = "observe-security-delivery"
  role = aws_iam_role.security_delivery_health.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow", Action = "cloudwatch:GetMetricData", Resource = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.security_delivery_regions } }
      },
      {
        Effect   = "Allow", Action = "sqs:GetQueueAttributes"
        Resource = concat([aws_sqs_queue.security_delivery_failures.arn], [for region in local.security_delivery_regions : "arn:aws:sqs:${region}:${data.aws_caller_identity.current.account_id}:security-findings-delivery-dlq"])
      },
      {
        Effect = "Allow", Action = ["events:DescribeRule", "events:ListTargetsByRule"]
        Resource = concat([
          aws_cloudwatch_event_rule.security_notify.arn,
          aws_cloudwatch_event_rule.security_delivery_health.arn,
          ], flatten([for region in local.security_delivery_regions : [for rule in ["security-forward-findings", "security-forward-audit"] :
            "arn:aws:events:${region}:${data.aws_caller_identity.current.account_id}:rule/${rule}"
        ]]))
      },
      {
        Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.security_delivery_observer_staging.arn
      },
      {
        Effect    = "Allow", Action = "cloudwatch:PutMetricData", Resource = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "SecurityDelivery" } }
      },
      {
        Effect   = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.security_delivery_health.arn}:*"
      }
    ]
  })
}

data "archive_file" "security_delivery_health" {
  type        = "zip"
  output_path = "${path.module}/.terraform/security-delivery-health.zip"
  source {
    filename = "lambda_function.py"
    content  = local.security_delivery_watchdog
  }
}

resource "aws_lambda_function" "security_delivery_health" {
  filename                       = data.archive_file.security_delivery_health.output_path
  source_code_hash               = data.archive_file.security_delivery_health.output_base64sha256
  function_name                  = "security-delivery-health"
  role                           = aws_iam_role.security_delivery_health.arn
  handler                        = "lambda_function.handler"
  runtime                        = "python3.12"
  timeout                        = 90
  memory_size                    = 256
  reserved_concurrent_executions = 1
  environment {
    variables = {
      MAIN_ACCOUNT    = data.aws_caller_identity.current.account_id
      STAGING_ACCOUNT = aws_organizations_account.dev_staging[0].id
      REGIONS         = jsonencode(local.security_delivery_regions)
      SNS_TOPIC_ARN   = aws_sns_topic.cost_alerts.arn
      EVENT_PATTERNS = jsonencode(merge(module.security_main_us_west_1.event_patterns, {
        security-alerts-notify = aws_cloudwatch_event_rule.security_notify.event_pattern
      }))
    }
  }
  depends_on = [aws_iam_role_policy.security_delivery_health, aws_iam_role_policy.security_delivery_observer_staging]
}

resource "aws_cloudwatch_event_rule" "security_delivery_health" {
  name                = "security-delivery-health"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_lambda_permission" "security_delivery_health" {
  statement_id  = "AllowScheduledSecurityHealthRead"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.security_delivery_health.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.security_delivery_health.arn
}

resource "aws_cloudwatch_event_target" "security_delivery_health" {
  rule      = aws_cloudwatch_event_rule.security_delivery_health.name
  target_id = "security-delivery-health"
  arn       = aws_lambda_function.security_delivery_health.arn
  retry_policy {
    maximum_event_age_in_seconds = 300
    maximum_retry_attempts       = 2
  }
  dead_letter_config { arn = aws_sqs_queue.security_delivery_failures.arn }
  # Do not start polling until every declared queue/forwarder exists. Explicit
  # modules are required because provider aliases cannot be selected dynamically.
  depends_on = [
    aws_sqs_queue_policy.security_delivery_failures,
    aws_lambda_permission.security_delivery_health,
    module.security_main_ap_south_1, module.security_main_ca_central_1,
    module.security_main_eu_central_1, module.security_main_us_west_1,
    module.security_main_us_west_2, module.security_main_eu_north_1,
    module.security_main_eu_west_3, module.security_main_eu_west_2,
    module.security_main_eu_west_1, module.security_main_ap_northeast_3,
    module.security_main_ap_northeast_2, module.security_main_ap_northeast_1,
    module.security_main_sa_east_1, module.security_main_ap_southeast_1,
    module.security_main_ap_southeast_2, module.security_main_us_east_1,
    module.security_main_us_east_2,
    module.security_staging_ap_south_1, module.security_staging_ca_central_1,
    module.security_staging_eu_central_1, module.security_staging_us_west_1,
    module.security_staging_us_west_2, module.security_staging_eu_north_1,
    module.security_staging_eu_west_3, module.security_staging_eu_west_2,
    module.security_staging_eu_west_1, module.security_staging_ap_northeast_3,
    module.security_staging_ap_northeast_2, module.security_staging_ap_northeast_1,
    module.security_staging_sa_east_1, module.security_staging_ap_southeast_1,
    module.security_staging_ap_southeast_2, module.security_staging_us_east_1,
    module.security_staging_us_east_2,
  ]
}

resource "aws_cloudwatch_metric_alarm" "security_delivery" {
  for_each            = toset(["FailedDeliveries", "QueuedEvents", "ObserverErrors"])
  alarm_name          = "security-delivery-${each.key}"
  alarm_description   = "Security event delivery is unhealthy; inspect security-delivery-health logs and retained regional DLQs."
  namespace           = "SecurityDelivery"
  metric_name         = each.key
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "security_delivery_heartbeat" {
  alarm_name        = "security-delivery-heartbeat"
  alarm_description = "No completed security delivery check for 15 minutes; do not assume regional alert delivery is healthy."
  # FILL prevents CloudWatch's extended missing-data lookback from reusing an
  # old successful pulse. Three consecutive empty five-minute periods alarm.
  metric_query {
    id          = "pulse"
    return_data = false
    metric {
      namespace   = "SecurityDelivery"
      metric_name = "Heartbeat"
      stat        = "Sum"
      period      = 300
    }
  }
  metric_query {
    id          = "heartbeat"
    expression  = "FILL(pulse, 0)"
    return_data = true
  }
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "security_delivery_errors" {
  alarm_name          = "security-delivery-lambda-errors"
  alarm_description   = "Security delivery observer failed; the independent heartbeat also detects missing invocations or failed metrics."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.security_delivery_health.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

# Account-default Standard_Stream sessions log to the protected S3 archive.
# Public SSH/ET remains available; neither SSH nor port-forwarded SSM sessions
# produce a terminal transcript through this setting.
resource "aws_ssm_document" "security_sessions_main" {
  name            = "SSM-SessionManagerRunShell"
  document_type   = "Session"
  document_format = "JSON"
  content = jsonencode({
    schemaVersion = "1.0"
    description   = "Default Session Manager shell with protected audit logging"
    sessionType   = "Standard_Stream"
    inputs = {
      s3BucketName                = aws_s3_bucket.security_config.id
      s3KeyPrefix                 = "sessions/us-west-1"
      s3EncryptionEnabled         = true
      cloudWatchLogGroupName      = ""
      cloudWatchEncryptionEnabled = false
      cloudWatchStreamingEnabled  = false
      kmsKeyId                    = ""
      runAsEnabled                = false
      runAsDefaultUser            = ""
      idleSessionTimeout          = "60"
      maxSessionDuration          = "720"
      shellProfile                = { windows = "", linux = "" }
    }
  })
  tags       = { Managed = "terraform" }
  depends_on = [aws_iam_role_policy.security_session_logs]
}

resource "aws_ssm_document" "security_sessions_west2" {
  provider        = aws.west2
  name            = "SSM-SessionManagerRunShell"
  document_type   = "Session"
  document_format = "JSON"
  content         = replace(aws_ssm_document.security_sessions_main.content, "sessions/us-west-1", "sessions/us-west-2")
  tags            = { Managed = "terraform" }
}

resource "aws_iam_role_policy" "security_session_logs" {
  for_each = merge(
    {
      dev     = aws_iam_role.dev_server.name
      nextjs  = aws_iam_role.nextjs_dev.name
      working = aws_iam_role.dev_ebs_only.name
    },
    local.jumpbox_admin_iam_needed ? { jumpbox = aws_iam_role.jumpbox_admin[0].name } : {},
    var.enable_github_runner ? {
      runner      = aws_iam_role.runner[0].name
      ami_builder = aws_iam_role.ami_builder[0].name
    } : {},
  )
  name = "write-session-audit-logs"
  role = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetBucketLocation", "s3:GetEncryptionConfiguration"]
        Resource = aws_s3_bucket.security_config.arn
      },
      {
        Effect   = "Allow"
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.security_config.arn}/sessions/*"
      }
    ]
  })
}

# Provider aliases are shared with the separately owned regional account defaults
# in security-regions.tf. These modules own monitoring only, not those defaults.

module "security_main_ap_south_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_ap_south_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_ca_central_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_ca_central_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_eu_central_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_eu_central_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_us_west_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = local.security_posture_enabled
  record_global_iam     = true
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_us_west_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.west2 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = local.security_posture_enabled
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_eu_north_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_eu_north_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_eu_west_3" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_eu_west_3 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_eu_west_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_eu_west_2 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_eu_west_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_eu_west_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_ap_northeast_3" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_ap_northeast_3 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_ap_northeast_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_ap_northeast_2 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_ap_northeast_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_ap_northeast_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_sa_east_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_sa_east_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_ap_southeast_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_ap_southeast_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_ap_southeast_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_ap_southeast_2 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_us_east_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dr }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = local.security_posture_enabled
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_main_us_east_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_main_us_east_2 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_ap_south_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_ap_south_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_ca_central_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_ca_central_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_eu_central_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_eu_central_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_us_west_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.staging }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = local.security_posture_enabled
  record_global_iam     = true
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_us_west_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_us_west_2 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_eu_north_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_eu_north_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_eu_west_3" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_eu_west_3 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_eu_west_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_eu_west_2 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_eu_west_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_eu_west_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_ap_northeast_3" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_ap_northeast_3 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_ap_northeast_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_ap_northeast_2 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_ap_northeast_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_ap_northeast_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_sa_east_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_sa_east_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_ap_southeast_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_ap_southeast_1 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_ap_southeast_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_ap_southeast_2 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_us_east_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.staging_dr }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  # The verified single-history pipeline now keeps its final recovery vault here.
  posture_enabled   = local.security_posture_enabled
  record_global_iam = false
  depends_on        = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

module "security_staging_us_east_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.security_staging_us_east_2 }
  central_event_bus_arn = aws_cloudwatch_event_bus.security_alerts.arn
  forwarding_role_arn   = aws_iam_role.security_forward_staging.arn
  audit_bucket_name     = aws_s3_bucket.security_config.id
  audit_bucket_arn      = aws_s3_bucket.security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.security_config, aws_cloudwatch_event_bus_policy.security_alerts, aws_iam_role_policy.security_forward, aws_iam_role_policy.security_forward_staging]
}

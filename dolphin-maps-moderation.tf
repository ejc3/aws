# dolphin-maps-moderation.tf
#
# Lets the dolphin-maps Workers check what people send before it is kept or shown: pictures with Amazon Rekognition's
# DetectModerationLabels and short text with Amazon Comprehend's DetectToxicContent, the colton-games pattern
# (colton-games-moderation.tf) for a Cloudflare-only site. Exactly two permissions, neither scoped to a resource by AWS:
#
#   rekognition:DetectModerationLabels   us-west-1, as colton-games. The picture travels in the request (Image.Bytes, up to
#                                        5 MB); nothing is stored and no bucket is involved.
#   comprehend:DetectToxicContent        us-west-2. Comprehend is not offered in us-west-1 at all; us-west-2 is the nearest
#                                        region with toxicity detection (us-east-1, us-west-2, eu-west-1, ap-southeast-2).
#                                        Up to 10 segments of 1 KB per request, English only.
#
# Each permission is pinned to its own region with aws:RequestedRegion. Both regions are among the Dolphin account's
# monitored regions, which its region SCP (dolphin-region-scp.tf) allows; a precondition below keeps it that way.
#
# Everything here is in the DOLPHIN account (provider aws.dolphin), as dolphin-redis.tf's secrets are: the calls are billed
# and audited there, and no key of this (management) account is ever held in the Dolphin account (AGENTS.md, "The Dolphin
# account": nothing here trusts it). dolphin-labs-hq/dolphin-aws keeps no secret value in its state by design, so the keys
# Terraform makes are written from here, like the Redis tokens.
#
#   dolphin-maps-prod   IAM user dolphin-maps-moderation-worker-prod; its key goes only into workers-prod/dolphin-maps/terraform,
#                       which scripts/workers-prod-secrets.sh merges with the out-of-band workers-prod/dolphin-maps.
#   dolphin-maps-stage  IAM user dolphin-maps-moderation-worker-stage; its key goes only into
#                       workers-stage/dolphin-maps/terraform, which scripts/workers-stage-secrets.sh merges with
#                       workers-stage/dolphin-maps. A Worker cannot present a Vercel OIDC token, hence keys.
#
# Each container holds MODERATION_AWS_ACCESS_KEY_ID, MODERATION_AWS_SECRET_ACCESS_KEY, MODERATION_AWS_REGION (Rekognition's)
# and MODERATION_TEXT_REGION (Comprehend's). The keys are in Terraform state, as colton-games' Worker keys are.
#   Rotate: terraform apply -replace='aws_iam_access_key.dolphin_maps_moderation_worker["prod"]' (or "stage"), then
#   scripts/workers-prod-secrets.sh dolphin-maps (or scripts/workers-stage-secrets.sh dolphin-maps), after a deploy has
#   finished.
#
# Spend: Rekognition about $1 per 1,000 pictures; Comprehend $0.0001 per 100 characters, at least 300 per request (about
# $0.0003 for a short text, $0.01 for a full 10 KB request). An alarm fires on dolphin-alerts when an hour sees more than
# local.dolphin_maps_moderation_hourly_alarm picture checks.
locals {
  dolphin_maps_moderation_region      = "us-west-1"
  dolphin_maps_moderation_text_region = "us-west-2"
  # dolphin-maps is a family app: a few people, a handful of pictures a day. 500 an hour is far above any real use and
  # alerts on an unnoticed runaway (a retry loop, a leaked key) at roughly $0.50 of picture calls per hour.
  # It is a notification, not a spend cap; colton-games, with many kids, uses a threshold of 2,000.
  dolphin_maps_moderation_hourly_alarm = 500

  # One user and one Terraform-written container per Worker: a staging key never reaches production, and the two rotate
  # apart.
  dolphin_maps_moderation_workers = {
    prod  = { user = "dolphin-maps-moderation-worker-prod", worker = "dolphin-maps-prod", container = "workers-prod/dolphin-maps/terraform" }
    stage = { user = "dolphin-maps-moderation-worker-stage", worker = "dolphin-maps-stage", container = "workers-stage/dolphin-maps/terraform" }
  }

  # Administration of the Dolphin account, as dolphin-aws names it for its own containers: the organization's way in
  # (what this repository and the loaders assume), the dolphin-aws apply role and the owner's Identity Center admin sessions.
  dolphin_maps_moderation_admins = [
    "arn:aws:iam::${data.aws_caller_identity.dolphin.account_id}:role/OrganizationAccountAccessRole",
    local.dolphin_terraform_role_arn,
    local.dolphin_sso_admin_role_arns,
  ]
}

data "aws_iam_policy_document" "dolphin_maps_moderation" {
  statement {
    sid       = "CheckPicturesForInappropriateContent"
    actions   = ["rekognition:DetectModerationLabels"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.dolphin_maps_moderation_region]
    }
  }

  statement {
    sid       = "CheckTextForToxicContent"
    actions   = ["comprehend:DetectToxicContent"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.dolphin_maps_moderation_text_region]
    }
  }
}

resource "aws_iam_user" "dolphin_maps_moderation_worker" {
  provider = aws.dolphin
  for_each = local.dolphin_maps_moderation_workers
  name     = each.value.user
  tags     = { Name = each.value.user, Project = "dolphin-maps" }
}

resource "aws_iam_user_policy" "dolphin_maps_moderation_worker" {
  provider = aws.dolphin
  for_each = local.dolphin_maps_moderation_workers
  name     = "moderation"
  user     = aws_iam_user.dolphin_maps_moderation_worker[each.key].name
  policy   = data.aws_iam_policy_document.dolphin_maps_moderation.json
}

resource "aws_iam_access_key" "dolphin_maps_moderation_worker" {
  provider = aws.dolphin
  for_each = local.dolphin_maps_moderation_workers
  user     = aws_iam_user.dolphin_maps_moderation_worker[each.key].name
}

resource "aws_secretsmanager_secret" "dolphin_maps_moderation_worker" {
  provider                = aws.dolphin
  for_each                = local.dolphin_maps_moderation_workers
  name                    = each.value.container
  description             = "Runtime secrets of the ${each.value.worker} Worker that Terraform makes (JSON: the moderation key and regions). Written by ejc3/aws dolphin-maps-moderation.tf; merged by the loader with the out-of-band container."
  recovery_window_in_days = 7
  tags                    = { Name = each.value.container, Project = "dolphin-maps" }
}

resource "aws_secretsmanager_secret_policy" "dolphin_maps_moderation_worker" {
  provider            = aws.dolphin
  for_each            = local.dolphin_maps_moderation_workers
  secret_arn          = aws_secretsmanager_secret.dolphin_maps_moderation_worker[each.key].arn
  block_public_policy = true
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue"]
      Resource  = "*"
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.dolphin_maps_moderation_admins } }
    }]
  })
}

resource "aws_secretsmanager_secret_version" "dolphin_maps_moderation_worker" {
  provider  = aws.dolphin
  for_each  = local.dolphin_maps_moderation_workers
  secret_id = aws_secretsmanager_secret.dolphin_maps_moderation_worker[each.key].id
  secret_string = jsonencode({
    MODERATION_AWS_ACCESS_KEY_ID     = aws_iam_access_key.dolphin_maps_moderation_worker[each.key].id
    MODERATION_AWS_SECRET_ACCESS_KEY = aws_iam_access_key.dolphin_maps_moderation_worker[each.key].secret
    MODERATION_AWS_REGION            = local.dolphin_maps_moderation_region
    MODERATION_TEXT_REGION           = local.dolphin_maps_moderation_text_region
  })

  lifecycle {
    # Production's key only in production's container, staging's only in staging's.
    precondition {
      condition     = startswith(each.value.container, each.key == "prod" ? "workers-prod/" : "workers-stage/") && endswith(each.value.worker, "-${each.key}")
      error_message = "A moderation key must go only to its own environment's container (dolphin-maps-moderation.tf)."
    }
    # The Dolphin account's region SCP denies every other region, so a call there would fail closed in the Worker.
    precondition {
      condition     = alltrue([for r in [local.dolphin_maps_moderation_region, local.dolphin_maps_moderation_text_region] : contains(local.dolphin_security_regions, r)])
      error_message = "Both moderation regions must be in local.dolphin_security_regions, the regions the Dolphin account's SCP allows (dolphin-region-scp.tf)."
    }
  }
}

# Spend guard: a runaway loop or a leaked Worker key shows up as an hour of far more picture checks than the family makes.
# Rekognition's metrics are per account and region, so this counts dolphin-maps' calls only (colton-games' are in this
# account). It counts pictures only: no per-call CloudWatch metric for Comprehend's DetectToxicContent was confirmed to
# alarm on, so a text alarm waits until the account's own metrics show one. At most a cent per full request, text checks
# are bounded by the Worker's own limits meanwhile.
resource "aws_cloudwatch_metric_alarm" "dolphin_maps_moderation_volume" {
  provider            = aws.dolphin
  alarm_name          = "dolphin-maps-moderation-volume"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = local.dolphin_maps_moderation_hourly_alarm
  alarm_description   = "More than ${local.dolphin_maps_moderation_hourly_alarm} Rekognition DetectModerationLabels calls in an hour (about $1 per 1,000): a loop in dolphin-maps or a leaked key (ejc3/aws dolphin-maps-moderation.tf)"
  alarm_actions       = [data.aws_sns_topic.dolphin_alerts.arn]
  treat_missing_data  = "notBreaching"
  namespace           = "AWS/Rekognition"
  metric_name         = "SuccessfulRequestCount"
  dimensions          = { Operation = "DetectModerationLabels" }
  statistic           = "Sum"
  period              = 3600

  lifecycle {
    precondition {
      condition     = var.aws_region == local.dolphin_maps_moderation_region
      error_message = "The alarm is made in the aws.dolphin provider's region, which must be Rekognition's (dolphin-maps-moderation.tf)."
    }
  }
}

output "dolphin_maps_moderation" {
  description = "How the dolphin-maps Workers check pictures and text: the regions, and the Dolphin-account containers Terraform writes their keys into."
  value = {
    picture_region = local.dolphin_maps_moderation_region
    text_region    = local.dolphin_maps_moderation_text_region
    containers     = { for env, w in local.dolphin_maps_moderation_workers : w.worker => aws_secretsmanager_secret.dolphin_maps_moderation_worker[env].name }
  }
}

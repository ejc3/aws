# colton-games-moderation.tf
#
# Lets colton-games check pictures for anything inappropriate with Amazon Rekognition's DetectModerationLabels (owner,
# 2026-10-11: "add an entitlement so that colton games can scan photos for anything inappropriate"). Exactly one
# permission everywhere: rekognition:DetectModerationLabels, an action AWS does not scope to a resource. The picture
# travels in the request (Image.Bytes, up to 5 MB); nothing is stored and no bucket is involved. Region us-west-1.
#
#   Vercel production and preview: role colton-games-moderation[-preview], assumed through the existing Vercel OIDC
#     provider (games-multiplayer.tf) with the same audience and per-environment subject as the multiplayer launcher, so
#     no key is stored anywhere. Terraform writes MODERATION_ROLE_ARN (per target) and MODERATION_AWS_REGION into the
#     project.
#   Cloudflare colton-games-stage: a Worker cannot present a Vercel OIDC token, so an IAM user with the same single
#     permission. Its key is in `colton-games/moderation-worker-key` (administration only, JSON of the Worker's variable
#     names), copied into workers-stage/colton-games and loaded with scripts/workers-stage-secrets.sh (AGENTS.md, "Sites
#     also deploying to Cloudflare Workers"). The key is in Terraform state, as imagine-stage-store.tf's Worker key is.
#     Rotate with `terraform apply -replace=aws_iam_access_key.colton_games_moderation_worker`, then copy and reload.
#   nextjs-dev: the dev box's role may call it too, so the feature can be built and tried locally.
#   Spend: about $1 per 1,000 pictures. An alarm fires when an hour sees more than local.moderation_hourly_alarm calls.
locals {
  colton_games_moderation_roles = {
    production = "colton-games-moderation"
    preview    = "colton-games-moderation-preview"
  }
  moderation_region        = "us-west-1"
  moderation_hourly_alarm  = 2000
  moderation_policy_action = "rekognition:DetectModerationLabels"
}

resource "aws_iam_role" "colton_games_moderation" {
  for_each             = local.colton_games_moderation_roles
  name                 = each.value
  description          = "Assumed by colton-games ${each.key} deployments via Vercel OIDC to check pictures with Rekognition DetectModerationLabels"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.vercel.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.vercel_oidc_host}:aud" = "https://vercel.com/${local.vercel_team_slug}"
          "${local.vercel_oidc_host}:sub" = "owner:${local.vercel_team_slug}:project:${local.vercel_project_name}:environment:${each.key}"
        }
      }
    }]
  })
  tags = { Name = each.value, Project = "colton-games" }
}

data "aws_iam_policy_document" "moderation" {
  statement {
    sid       = "CheckPicturesForInappropriateContent"
    actions   = [local.moderation_policy_action]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.moderation_region]
    }
  }
}

resource "aws_iam_role_policy" "colton_games_moderation" {
  for_each = local.colton_games_moderation_roles
  name     = "detect-moderation-labels"
  role     = aws_iam_role.colton_games_moderation[each.key].id
  policy   = data.aws_iam_policy_document.moderation.json
}

# The staging Worker's identity: the same single permission, a key instead of OIDC.
resource "aws_iam_user" "colton_games_moderation_worker" {
  name = "colton-games-moderation-worker"
  tags = { Name = "colton-games-moderation-worker", Project = "colton-games" }
}

resource "aws_iam_user_policy" "colton_games_moderation_worker" {
  name   = "detect-moderation-labels"
  user   = aws_iam_user.colton_games_moderation_worker.name
  policy = data.aws_iam_policy_document.moderation.json
}

resource "aws_iam_access_key" "colton_games_moderation_worker" {
  user = aws_iam_user.colton_games_moderation_worker.name
}

resource "aws_secretsmanager_secret" "colton_games_moderation_worker_key" {
  name                    = "colton-games/moderation-worker-key"
  description             = "The colton-games-stage Worker's Rekognition DetectModerationLabels key (JSON of its variable names). Administration only; see colton-games-moderation.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "colton-games/moderation-worker-key", Project = "colton-games" }
}

resource "aws_secretsmanager_secret_policy" "colton_games_moderation_worker_key" {
  secret_arn = aws_secretsmanager_secret.colton_games_moderation_worker_key.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.colton_games_moderation_worker_key.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

resource "aws_secretsmanager_secret_version" "colton_games_moderation_worker_key" {
  secret_id = aws_secretsmanager_secret.colton_games_moderation_worker_key.id
  secret_string = jsonencode({
    MODERATION_AWS_ACCESS_KEY_ID     = aws_iam_access_key.colton_games_moderation_worker.id
    MODERATION_AWS_SECRET_ACCESS_KEY = aws_iam_access_key.colton_games_moderation_worker.secret
    MODERATION_AWS_REGION            = local.moderation_region
  })
}

# The dev box, for building the feature locally with its instance role.
resource "aws_iam_role_policy" "nextjs_dev_moderation" {
  name   = "detect-moderation-labels"
  role   = aws_iam_role.nextjs_dev.id
  policy = data.aws_iam_policy_document.moderation.json
}

# The Vercel deployments' variables. Not sensitive: a role ARN and a region are not secrets.
resource "vercel_project_environment_variable" "colton_games_moderation" {
  for_each = {
    "MODERATION_ROLE_ARN/production" = { targets = ["production"], value = aws_iam_role.colton_games_moderation["production"].arn }
    "MODERATION_ROLE_ARN/preview"    = { targets = ["preview"], value = aws_iam_role.colton_games_moderation["preview"].arn }
    "MODERATION_AWS_REGION"          = { targets = ["production", "preview"], value = local.moderation_region }
  }
  team_id    = var.vercel_team_id
  project_id = local.colton_games_vercel_project_id
  key        = split("/", each.key)[0]
  value      = each.value.value
  target     = each.value.targets
  sensitive  = false
  comment    = "picture moderation; managed by ejc3/aws colton-games-moderation.tf"
}

# Spend guard: a runaway loop or a leaked Worker key shows up as an hour of far more checks than kids' uploads make.
resource "aws_cloudwatch_metric_alarm" "colton_games_moderation_volume" {
  alarm_name          = "colton-games-moderation-volume"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = local.moderation_hourly_alarm
  alarm_description   = "More than ${local.moderation_hourly_alarm} Rekognition DetectModerationLabels calls in an hour (about $1 per 1,000): a loop in colton-games or a leaked key (colton-games-moderation.tf)"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  namespace           = "AWS/Rekognition"
  metric_name         = "SuccessfulRequestCount"
  dimensions          = { Operation = "DetectModerationLabels" }
  statistic           = "Sum"
  period              = 3600
}

output "colton_games_moderation" {
  description = "How colton-games checks pictures: the per-environment role ARNs (Vercel OIDC), the region, and where the staging Worker's key is."
  value = {
    roles      = { for env, r in aws_iam_role.colton_games_moderation : env => r.arn }
    region     = local.moderation_region
    worker_key = aws_secretsmanager_secret.colton_games_moderation_worker_key.name
  }
}

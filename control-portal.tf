# Owner-private credential setup and existing emergency patch administration.
# The direct Worker uses Cloudflare's authenticated invocation context. It never
# proxies to the archived private Sites publication or requires a ChatGPT login.
resource "cloudflare_zero_trust_access_policy" "control_portal" {
  account_id       = var.cloudflare_account_id
  name             = "Control Room: owner only"
  decision         = "allow"
  session_duration = "12h"
  include          = [{ email = { email = nonsensitive(local.people.owner) } }]
}

resource "cloudflare_zero_trust_access_application" "control_portal" {
  account_id       = var.cloudflare_account_id
  name             = "Control Room"
  domain           = "control.cc-games.dev"
  type             = "self_hosted"
  session_duration = "12h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )
  policies = [{ id = cloudflare_zero_trust_access_policy.control_portal.id, precedence = 1 }]
}

# Returned by the Sites custom-domain API for this exact project/hostname.
resource "cloudflare_dns_record" "control_portal" {
  zone_id    = var.cc_games_zone_id
  name       = "control.cc-games.dev"
  type       = "CNAME"
  content    = "custom-domains.chatgpt.site"
  proxied    = true
  ttl        = 1
  depends_on = [cloudflare_zero_trust_access_application.control_portal]
}
resource "cloudflare_dns_record" "control_portal_verification" {
  for_each = {
    "_openai-site-verification.control.cc-games.dev" = "openai-site-verification=aokDLPHdulJcDAGAIL2LiVIcz_i9imNlq1ceadylVpg"
    "_cf-custom-hostname.control.cc-games.dev"       = "85192c11-6f3d-4332-86fb-d8156ff3144e"
  }
  zone_id = var.cc_games_zone_id
  name    = each.key
  type    = "TXT"
  content = each.value
  ttl     = 60
}

# Workers do not have an AWS workload identity here. A dedicated server-only key
# follows the existing dashboard pattern; no personal login or admin key is copied.
# It cannot send SSM commands, invoke the broker, read Terraform state, or read secrets.
resource "aws_iam_user" "control_portal" {
  name = "control-portal"
  tags = { Managed = "terraform", Purpose = "private-owner-admin-portal" }
}
resource "aws_iam_user_policy" "control_portal" {
  name = "doctor-status-and-owner-controls"
  user = aws_iam_user.control_portal.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.patchdoctor.arn },
      {
        Effect   = "Allow", Action = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = [aws_dynamodb_table.patchdoctor.arn, format("%s/index/active-cases", aws_dynamodb_table.patchdoctor.arn)]
      },
      {
        Effect   = "Allow", Action = "logs:FilterLogEvents"
        Resource = format("%s:*", trimsuffix(aws_cloudwatch_log_group.patchdoctor["worker"].arn, ":*"))
      }
    ]
  })
}
resource "aws_iam_access_key" "control_portal" {
  user = aws_iam_user.control_portal.name
}
resource "aws_secretsmanager_secret" "control_portal" {
  name                    = "control-portal/runtime"
  description             = "Dedicated Control Room server identity; Terraform installs private Cloudflare Worker bindings."
  recovery_window_in_days = 7
}
resource "aws_secretsmanager_secret_policy" "control_portal" {
  secret_arn = aws_secretsmanager_secret.control_portal.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny", Principal = "*", Action = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.control_portal.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}
resource "aws_secretsmanager_secret_version" "control_portal" {
  secret_id = aws_secretsmanager_secret.control_portal.id
  secret_string = jsonencode({
    AWS_ACCESS_KEY_ID     = aws_iam_access_key.control_portal.id
    AWS_SECRET_ACCESS_KEY = aws_iam_access_key.control_portal.secret
    AWS_REGION            = var.aws_region
    CONTROL_LAMBDA_NAME   = aws_lambda_function.patchdoctor.function_name
    TABLE_NAME            = aws_dynamodb_table.patchdoctor.name
    PROJECT_NAME          = aws_codebuild_project.patchdoctor.name
    LOG_GROUP             = aws_cloudwatch_log_group.patchdoctor["worker"].name
    OWNER_EMAIL           = local.people.owner
  })
}

# Reuse the existing owner-only Access app and its audience. The route answers
# every request itself; the old private Sites publication remains unreachable
# through this hostname and is not made public to bypass its second login.
resource "cloudflare_workers_script" "control_portal" {
  account_id         = var.cloudflare_account_id
  script_name        = "control-room"
  main_module        = "index.mjs"
  content_file       = "${path.module}/control-portal/index.mjs"
  content_sha256     = filesha256("${path.module}/control-portal/index.mjs")
  compatibility_date = "2026-11-01"
  bindings = [
    { name = "ACCESS_AUD", type = "plain_text", text = cloudflare_zero_trust_access_application.control_portal.aud },
    { name = "OWNER_EMAIL", type = "secret_text", text = local.people.owner },
    { name = "AWS_ACCESS_KEY_ID", type = "secret_text", text = aws_iam_access_key.control_portal.id },
    { name = "AWS_SECRET_ACCESS_KEY", type = "secret_text", text = aws_iam_access_key.control_portal.secret },
    { name = "AWS_REGION", type = "plain_text", text = var.aws_region },
    { name = "CONTROL_LAMBDA_NAME", type = "plain_text", text = aws_lambda_function.patchdoctor.function_name },
    { name = "TABLE_NAME", type = "plain_text", text = aws_dynamodb_table.patchdoctor.name },
    { name = "PROJECT_NAME", type = "plain_text", text = aws_codebuild_project.patchdoctor.name },
    { name = "LOG_GROUP", type = "plain_text", text = aws_cloudwatch_log_group.patchdoctor["worker"].name },
  ]
}

resource "cloudflare_workers_script_subdomain" "control_portal" {
  account_id       = var.cloudflare_account_id
  script_name      = cloudflare_workers_script.control_portal.script_name
  enabled          = false
  previews_enabled = false
}

resource "cloudflare_workers_route" "control_portal" {
  zone_id = var.cc_games_zone_id
  pattern = "control.cc-games.dev/*"
  script  = cloudflare_workers_script.control_portal.script_name
  depends_on = [
    cloudflare_zero_trust_access_application.control_portal,
    cloudflare_workers_script_subdomain.control_portal,
  ]
}

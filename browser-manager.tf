# A separate single-owner application; do not attach the cc-games family/service policies
# or the Dolphin GitHub organization policy. An exact hostname wins over *.cc-games.dev.
locals {
  browser_manager_hostname = "browsers.cc-games.dev"
  browser_manager_owner    = "ej.campbell@gmail.com"
  browser_manager_issuer   = "https://ejc3.cloudflareaccess.com"
}

# Dedicated machine credential for the local OpenClaw viewer. Do not reuse the broad
# cc-games automation token: this credential reaches only the browser-manager app and can
# be revoked without affecting any other service.
resource "cloudflare_zero_trust_access_service_token" "browser_manager_openclaw" {
  account_id = var.cloudflare_account_id
  name       = "browser-manager-openclaw"
  duration   = "8760h"

  lifecycle {
    create_before_destroy = true
    # A replacement changes the client ID pinned by the origin. Require an
    # explicitly coordinated origin deployment before replacing this identity.
    prevent_destroy = true
  }
}

resource "cloudflare_zero_trust_access_policy" "browser_manager_openclaw" {
  account_id       = var.cloudflare_account_id
  name             = "browser-manager OpenClaw client"
  decision         = "non_identity"
  session_duration = "12h"

  include = [{
    service_token = {
      token_id = cloudflare_zero_trust_access_service_token.browser_manager_openclaw.id
    }
  }]
}

resource "cloudflare_zero_trust_access_policy" "browser_manager_owner" {
  account_id       = var.cloudflare_account_id
  name             = "browser-manager owner only"
  decision         = "allow"
  session_duration = "12h"
  include          = [{ email = { email = local.browser_manager_owner } }]
}

resource "cloudflare_zero_trust_access_application" "browser_manager" {
  account_id       = var.cloudflare_account_id
  name             = "Private browser manager"
  domain           = local.browser_manager_hostname
  type             = "self_hosted"
  session_duration = "12h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.browser_manager_owner.id
      precedence = 1
    },
    {
      id         = cloudflare_zero_trust_access_policy.browser_manager_openclaw.id
      precedence = 2
    },
  ]
}

resource "random_bytes" "browser_manager_tunnel_secret" {
  # Unlike random_id, these outputs are sensitive and no secret-derived ID is
  # printed in Terraform's apply/refresh progress messages.
  length = 32
  # Bump only for an intentional, coordinated connector-token rotation.
  keepers = { rotation = "2026-09-12" }
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "browser_manager" {
  account_id = var.cloudflare_account_id
  name       = "browser-manager"
  config_src = "cloudflare"
  # Cloudflare PATCH rotates the remote-managed connector token in place.
  # The secret and token stay in protected Terraform state / Secrets Manager.
  tunnel_secret = random_bytes.browser_manager_tunnel_secret.base64

  lifecycle { prevent_destroy = true }
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "browser_manager" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.browser_manager.id
  config = {
    ingress = [
      {
        hostname = local.browser_manager_hostname
        service  = "http://127.0.0.1:3210"
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.browser_manager.aud]
          }
        }
      },
      { service = "http_status:404" },
    ]
  }

  # Routing is not installed until the owner policy has been attached to Access.
  depends_on = [cloudflare_zero_trust_access_application.browser_manager]
}

resource "cloudflare_dns_record" "browser_manager" {
  zone_id = var.cc_games_zone_id
  name    = local.browser_manager_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.browser_manager.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Owner-only browser-manager tunnel"

  depends_on = [
    cloudflare_zero_trust_access_application.browser_manager,
    cloudflare_zero_trust_tunnel_cloudflared_config.browser_manager,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "browser_manager" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.browser_manager.id
  # Refresh after the PATCH, before publishing the replacement secret version.
  depends_on = [cloudflare_zero_trust_tunnel_cloudflared.browser_manager]
}

# Publish the existing connector token for the ARM/x86 dev hosts. The value is already
# sensitive Terraform state; Secrets Manager provides retrieval with their instance role.
resource "aws_secretsmanager_secret" "browser_manager_tunnel_token" {
  name                    = "browser-manager-tunnel-token"
  description             = "Cloudflare connector token for the private browser-manager tunnel"
  recovery_window_in_days = 7

  tags = { Name = "browser-manager-tunnel-token", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "browser_manager_tunnel_token" {
  secret_id     = aws_secretsmanager_secret.browser_manager_tunnel_token.id
  secret_string = data.cloudflare_zero_trust_tunnel_cloudflared_token.browser_manager.token
}

resource "aws_iam_role_policy" "dev_server_browser_manager" {
  name = "browser-manager-tunnel-read"
  role = aws_iam_role.dev_server.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.browser_manager_tunnel_token.arn
    }]
  })
}

# Cloudflare returns a service-token secret only at creation. Keep the pair in Secrets
# Manager and give the local operator a narrow role instead of creating a long-lived IAM
# access key for the Mac. The existing AWS SSO AdministratorAccess session may assume this
# role; the role itself can read only the AWS and Mac viewer secrets.
resource "aws_secretsmanager_secret" "browser_manager_openclaw_access" {
  name                    = "browser-manager-openclaw-access"
  description             = "Cloudflare Access service token for the local OpenClaw browser viewer"
  recovery_window_in_days = 30

  tags = { Name = "browser-manager-openclaw-access", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "browser_manager_openclaw_access" {
  secret_id = aws_secretsmanager_secret.browser_manager_openclaw_access.id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.browser_manager_openclaw.client_id
    client_secret = cloudflare_zero_trust_access_service_token.browser_manager_openclaw.client_secret
    audience      = cloudflare_zero_trust_access_application.browser_manager.aud
    issuer        = local.browser_manager_issuer
    base_url      = "https://${local.browser_manager_hostname}"
  })
}

resource "aws_iam_role" "browser_manager_openclaw_client" {
  name = "browser-manager-openclaw-client"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
      Condition = {
        ArnLike = {
          "aws:PrincipalArn" = [
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*",
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AdministratorAccess_*",
          ]
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "browser_manager_openclaw_client" {
  name = "read-browser-manager-openclaw-access"
  role = aws_iam_role.browser_manager_openclaw_client.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "secretsmanager:GetSecretValue"
      Resource = [
        aws_secretsmanager_secret.browser_manager_openclaw_access.arn,
        aws_secretsmanager_secret.browser_manager_mac_openclaw_access.arn,
      ]
    }]
  })
}

resource "aws_secretsmanager_secret_policy" "browser_manager_openclaw_access" {
  for_each = {
    aws = aws_secretsmanager_secret.browser_manager_openclaw_access.arn
    mac = aws_secretsmanager_secret.browser_manager_mac_openclaw_access.arn
  }
  secret_arn = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheViewerCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = each.value
      Condition = {
        ArnNotEquals = {
          "aws:PrincipalArn" = [
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
            aws_iam_role.jumpbox_admin[0].arn,
            aws_iam_role.browser_manager_openclaw_client.arn,
          ]
        }
      }
    }]
  })
}

output "browser_manager_tunnel_secret_name" {
  description = "Secrets Manager name for the connector token, readable by ARM/x86 dev hosts"
  value       = aws_secretsmanager_secret.browser_manager_tunnel_token.name
}

output "browser_manager_openclaw_access_secret_name" {
  description = "Secrets Manager name for the local OpenClaw viewer's Cloudflare Access token"
  value       = aws_secretsmanager_secret.browser_manager_openclaw_access.name
}

output "browser_manager_openclaw_client_role_arn" {
  description = "Narrow AWS role the local SSO profile assumes to read the OpenClaw viewer token"
  value       = aws_iam_role.browser_manager_openclaw_client.arn
}

# Retained for apply-host compatibility. Dev hosts fetch the Secrets Manager value;
# only cloudflared receives the local 0600 token file path.
output "browser_manager_tunnel_token" {
  description = "Dedicated connector credential; save to a private token file, never argv"
  value       = data.cloudflare_zero_trust_tunnel_cloudflared_token.browser_manager.token
  sensitive   = true
}

output "browser_manager_env" {
  description = "Public application settings for the installer's private EnvironmentFile"
  value       = <<-ENV
    BM_BASE_URL=https://${local.browser_manager_hostname}
    BM_ACCESS_AUD=${cloudflare_zero_trust_access_application.browser_manager.aud}
    BM_ACCESS_ISSUER=${local.browser_manager_issuer}
    BM_ACCESS_SERVICE_TOKEN_ID=${cloudflare_zero_trust_access_service_token.browser_manager_openclaw.client_id}
    BM_OWNER_EMAIL=${local.browser_manager_owner}
    BM_PORT=3210
  ENV
}

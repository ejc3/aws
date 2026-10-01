# claude-master-tunnel.tf
#
# A long-lived Cloudflare tunnel from the shared claude-master server (claude-master-server.tf) to
# machines OUTSIDE the VPC that have no AWS access and are awkward to enrol with a certificate: the
# kids' Macs. The inference host stays closed -- no inbound port -- and cloudflared on it dials OUT.
#
#   Mac:    claude  ->  claude-master connect --open 127.0.0.1:8444 --ca ca.pem
#                       cloudflared access tcp  (127.0.0.1:8444, presents an Access service token)
#           Cloudflare edge: Access enforces the service token for inference.cc-games.dev
#   Server: cloudflared (outbound tunnel)  ->  tcp://127.0.0.1:8444  ->  claude-master's OPEN listener
#
# THE OPEN LISTENER asks for no client certificate and is bound to LOOPBACK ONLY
# (`claude-master serve --open-loopback 127.0.0.1:8444`); the trust is the tunnel in front of it, and
# the only thing that can reach that address is cloudflared on this box (and administrators logged in
# to it). The certificate listener on the private address is unchanged and still demands a
# certificate, so the dev boxes keep their per-box identities. Nothing here weakens that.
#
# ACCESS. An exact hostname beats the *.cc-games.dev wildcard application, so this is its own
# application with ONE policy: a Cloudflare Access service token (non_identity). No person can log
# in to it and no allowlist applies. The token is long-lived (a year) so a Mac is set up once;
# revoking every Mac is deleting the token, and rotating it is a new token handed out again.
#
# SECRETS. The connector token (what cloudflared on the server runs with) is readable by the server's
# role alone. The service token (what a Mac presents) is readable by administrators alone; the owner
# hands it to a Mac with scripts/claude-master-mac-bundle.sh.
locals {
  claude_master_tunnel_hostname = "inference.cc-games.dev"
  claude_master_open_port       = 8444 # loopback on the server; never opened in any security group

  cloudflared_version          = "2026.9.3"
  cloudflared_sha256_linux_arm = "aaeb2d7d0da3614634c7e03ab13487a1522c2e79165ed2929cfe23d5e95b326d"
}

resource "random_bytes" "claude_master_tunnel_secret" {
  length = 32
  # Bump only for an intentional, coordinated connector-token rotation.
  keepers = { rotation = "2026-10-01" }
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "claude_master" {
  count      = var.enable_claude_master_server ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "claude-master"
  config_src = "cloudflare"

  tunnel_secret = random_bytes.claude_master_tunnel_secret.base64

  lifecycle { prevent_destroy = true }
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "claude_master" {
  count      = var.enable_claude_master_server ? 1 : 0
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.claude_master[0].id
  config = {
    ingress = [
      {
        hostname = local.claude_master_tunnel_hostname
        service  = "tcp://127.0.0.1:${local.claude_master_open_port}"
      },
      { service = "http_status:404" },
    ]
  }

  # Routing is not installed until Access protects the hostname.
  depends_on = [cloudflare_zero_trust_access_application.claude_master]
}

resource "cloudflare_dns_record" "claude_master" {
  count   = var.enable_claude_master_server ? 1 : 0
  zone_id = var.cc_games_zone_id
  name    = local.claude_master_tunnel_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.claude_master[0].id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "claude-master inference tunnel (service-token Access only)"

  depends_on = [
    cloudflare_zero_trust_access_application.claude_master,
    cloudflare_zero_trust_tunnel_cloudflared_config.claude_master,
  ]
}

# The credential a Mac presents. Reaches only this application and can be revoked without touching
# any other service. Do not reuse the cc-games automation token.
resource "cloudflare_zero_trust_access_service_token" "claude_master_macs" {
  account_id = var.cloudflare_account_id
  name       = "claude-master-macs"
  duration   = "8760h" # 1 year

  lifecycle {
    create_before_destroy = true
  }
}

resource "cloudflare_zero_trust_access_policy" "claude_master_macs" {
  account_id       = var.cloudflare_account_id
  name             = "claude-master: Macs with the service token"
  decision         = "non_identity"
  session_duration = "24h"

  include = [{
    service_token = { token_id = cloudflare_zero_trust_access_service_token.claude_master_macs.id }
  }]
}

resource "cloudflare_zero_trust_access_application" "claude_master" {
  account_id       = var.cloudflare_account_id
  name             = "claude-master inference (Macs)"
  domain           = local.claude_master_tunnel_hostname
  type             = "self_hosted"
  session_duration = "24h"
  policies = [{
    id         = cloudflare_zero_trust_access_policy.claude_master_macs.id
    precedence = 1
  }]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "claude_master" {
  count      = var.enable_claude_master_server ? 1 : 0
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.claude_master[0].id
  depends_on = [cloudflare_zero_trust_tunnel_cloudflared.claude_master]
}

resource "aws_secretsmanager_secret" "claude_master_tunnel_token" {
  count                   = var.enable_claude_master_server ? 1 : 0
  name                    = "claude-master/tunnel-token"
  description             = "Cloudflare connector token for the claude-master tunnel; read by the server's role only"
  recovery_window_in_days = 7
  tags                    = { Name = "claude-master/tunnel-token", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "claude_master_tunnel_token" {
  count         = var.enable_claude_master_server ? 1 : 0
  secret_id     = aws_secretsmanager_secret.claude_master_tunnel_token[0].id
  secret_string = data.cloudflare_zero_trust_tunnel_cloudflared_token.claude_master[0].token
}

resource "aws_secretsmanager_secret" "claude_master_mac_access" {
  name                    = "claude-master/mac-access"
  description             = "Cloudflare Access service token a Mac presents to reach the claude-master tunnel (administrators only; hand out with scripts/claude-master-mac-bundle.sh)"
  recovery_window_in_days = 30
  tags                    = { Name = "claude-master/mac-access", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "claude_master_mac_access" {
  secret_id = aws_secretsmanager_secret.claude_master_mac_access.id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.claude_master_macs.client_id
    client_secret = cloudflare_zero_trust_access_service_token.claude_master_macs.client_secret
    hostname      = local.claude_master_tunnel_hostname
    local_port    = local.claude_master_open_port
  })
}

resource "aws_secretsmanager_secret_policy" "claude_master_tunnel_token" {
  count      = var.enable_claude_master_server ? 1 : 0
  secret_arn = aws_secretsmanager_secret.claude_master_tunnel_token[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheServerCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.claude_master_tunnel_token[0].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.claude_master_server[0].arn]) } }
    }]
  })
}

resource "aws_secretsmanager_secret_policy" "claude_master_mac_access" {
  secret_arn = aws_secretsmanager_secret.claude_master_mac_access.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.claude_master_mac_access.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

resource "aws_iam_role_policy" "claude_master_server_tunnel_token" {
  count = var.enable_claude_master_server ? 1 : 0
  name  = "tunnel-token"
  role  = aws_iam_role.claude_master_server[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadItsOwnConnectorToken"
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.claude_master_tunnel_token[0].arn
    }]
  })
}

output "claude_master_tunnel_hostname" {
  description = "The hostname a Mac's `cloudflared access tcp` connects to"
  value       = local.claude_master_tunnel_hostname
}

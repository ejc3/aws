# macOS Screen Sharing on EJ's MacBook Pro, reachable through Cloudflare for skevh.
#
# The MacBook dials out to its own tunnel; port 5900 is never exposed. The hostname is a
# TCP route, so a browser cannot use it directly. The client runs
#
#   cloudflared access tcp --hostname macbook-vnc.cc-games.dev --url localhost:5901
#
# then opens vnc://localhost:5901 in Screen Sharing and signs in as the Mac account
# skevh, which gets its own macOS session. Cloudflare's in-browser VNC renderer is not
# used: it only speaks the legacy single-password VNC auth, which shares the console
# session instead of logging a user into their own.
#
# Access is the same GitHub org membership as *.dolphin-labs.dev, so it follows the
# dolphin-labs-hq roster. The Mac account itself (user, Screen Sharing allow-list, power
# settings) is configured on the Mac, not here.
#
# Like browser-manager-mac, this is a separate tunnel. Never run its connector with any
# other tunnel's token.

locals {
  macbook_vnc_hostname = "macbook-vnc.cc-games.dev"
  # Rides on the dolphin GitHub identity provider, so it exists only while the zone does.
  macbook_vnc_enabled = local.dolphin_enabled
}

# Adding count moved every address to [0]; these keep the live resources instead of
# replacing them.
moved {
  from = cloudflare_zero_trust_access_policy.macbook_vnc
  to   = cloudflare_zero_trust_access_policy.macbook_vnc[0]
}

moved {
  from = cloudflare_zero_trust_access_application.macbook_vnc
  to   = cloudflare_zero_trust_access_application.macbook_vnc[0]
}

moved {
  from = cloudflare_zero_trust_tunnel_cloudflared.macbook_vnc
  to   = cloudflare_zero_trust_tunnel_cloudflared.macbook_vnc[0]
}

moved {
  from = cloudflare_zero_trust_tunnel_cloudflared_config.macbook_vnc
  to   = cloudflare_zero_trust_tunnel_cloudflared_config.macbook_vnc[0]
}

moved {
  from = cloudflare_dns_record.macbook_vnc
  to   = cloudflare_dns_record.macbook_vnc[0]
}

moved {
  from = aws_secretsmanager_secret.macbook_vnc_tunnel
  to   = aws_secretsmanager_secret.macbook_vnc_tunnel[0]
}

moved {
  from = aws_secretsmanager_secret_version.macbook_vnc_tunnel
  to   = aws_secretsmanager_secret_version.macbook_vnc_tunnel[0]
}

moved {
  from = aws_secretsmanager_secret_policy.macbook_vnc_tunnel
  to   = aws_secretsmanager_secret_policy.macbook_vnc_tunnel[0]
}

resource "cloudflare_zero_trust_access_policy" "macbook_vnc" {
  count = local.macbook_vnc_enabled ? 1 : 0

  account_id       = var.cloudflare_account_id
  name             = "MacBook Screen Sharing (dolphin-labs-hq members)"
  decision         = "allow"
  session_duration = "24h"
  include = [
    {
      github_organization = {
        identity_provider_id = cloudflare_zero_trust_access_identity_provider.github[0].id
        name                 = var.dolphin_allowed_github_org
      }
    }
  ]
}

resource "cloudflare_zero_trust_access_application" "macbook_vnc" {
  count = local.macbook_vnc_enabled ? 1 : 0

  account_id                = var.cloudflare_account_id
  name                      = "MacBook Screen Sharing"
  domain                    = local.macbook_vnc_hostname
  type                      = "self_hosted"
  session_duration          = "24h"
  allowed_idps              = [cloudflare_zero_trust_access_identity_provider.github[0].id]
  auto_redirect_to_identity = true

  # The policy attachment MUST be declared here; see cc_games_dev in cloudflare.tf.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.macbook_vnc[0].id
      precedence = 1
    },
  ]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "macbook_vnc" {
  count = local.macbook_vnc_enabled ? 1 : 0

  account_id = var.cloudflare_account_id
  name       = "macbook-vnc"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "macbook_vnc" {
  count = local.macbook_vnc_enabled ? 1 : 0

  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.macbook_vnc[0].id
  config = {
    ingress = [
      {
        hostname = local.macbook_vnc_hostname
        service  = "tcp://localhost:5900"
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.macbook_vnc[0].aud]
          }
        }
      },
      { service = "http_status:404" },
    ]
  }
  depends_on = [cloudflare_zero_trust_access_application.macbook_vnc]
}

# Overrides the *.cc-games.dev wildcard (which points at the nextjs-dev tunnel).
resource "cloudflare_dns_record" "macbook_vnc" {
  count = local.macbook_vnc_enabled ? 1 : 0

  zone_id = var.cc_games_zone_id
  name    = local.macbook_vnc_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.macbook_vnc[0].id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "MacBook Screen Sharing for skevh; GitHub org-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.macbook_vnc,
    cloudflare_zero_trust_tunnel_cloudflared_config.macbook_vnc,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "macbook_vnc" {
  count = local.macbook_vnc_enabled ? 1 : 0

  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.macbook_vnc[0].id
}

# Connector token only: it runs this one tunnel and cannot administer the account.
resource "aws_secretsmanager_secret" "macbook_vnc_tunnel" {
  count = local.macbook_vnc_enabled ? 1 : 0

  name                    = "macbook-vnc-tunnel-token"
  description             = "Connector token for the MacBook Screen Sharing tunnel only"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = "macbook-vnc-tunnel-token" }
}

resource "aws_secretsmanager_secret_version" "macbook_vnc_tunnel" {
  count = local.macbook_vnc_enabled ? 1 : 0

  secret_id     = aws_secretsmanager_secret.macbook_vnc_tunnel[0].id
  secret_string = data.cloudflare_zero_trust_tunnel_cloudflared_token.macbook_vnc[0].token
}

resource "aws_secretsmanager_secret_policy" "macbook_vnc_tunnel" {
  count = local.macbook_vnc_enabled ? 1 : 0

  secret_arn = aws_secretsmanager_secret.macbook_vnc_tunnel[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.macbook_vnc_tunnel[0].arn
      Condition = {
        ArnNotLike = {
          "aws:PrincipalArn" = [
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
            aws_iam_role.jumpbox_admin[0].arn,
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*",
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AdministratorAccess_*",
          ]
        }
      }
    }]
  })
}

output "macbook_vnc" {
  description = "MacBook Screen Sharing route (no credentials); null while the dolphin zone is off"
  value = local.macbook_vnc_enabled ? {
    hostname           = local.macbook_vnc_hostname
    tunnel_secret_name = aws_secretsmanager_secret.macbook_vnc_tunnel[0].name
    client_command     = "cloudflared access tcp --hostname ${local.macbook_vnc_hostname} --url localhost:5901  # then open vnc://localhost:5901"
  } : null
}

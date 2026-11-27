# dolphin-costume.tf
#
# Costume Party's staging host. The app moved into dolphin-labs (dolphin-labs-hq/dolphin-labs#2503), and its server runs in
# the Dolphin account (dolphin-aws costume.tf), as one Fargate task whose Cloudflare connector dials out. What lives on
# Cloudflare is built here, because Cloudflare credentials stay on the jumpboxes, as for Dolphin's Redis
# (dolphin-redis.tf):
#
#   costume-stage.dolphin-labs.dev -> tunnel dolphin-costume -> cloudflared in the task -> http://127.0.0.1:8081
#
# On the host:
#   - /lattice/native/login (the path every phone build opens to sign in): behind Access for the owner alone (its own
#     policy, as Lattice staging's sign-in, where it lived until now), and the connector checks the Access token too.
#   - ^/(lattice/v1/costume(/|$)|healthz$): to the server with no Access; it checks its own Bearer tokens.
#   - everything else: a 404 at the connector. The server's handover route (its sentinel's, on the task's loopback) is
#     never routed.
# The connector's token goes into the Dolphin account (costume/nonprod/tunnel, key TUNNEL_TOKEN), and the host's values the
# task needs (the Access team and audience tag) into the parameter /costume/stage there. Nothing in either account takes
# an inbound connection.

locals {
  costume_stage = {
    hostname   = "costume-stage.dolphin-labs.dev"
    port       = 8081
    login_path = "/lattice/native/login"
    api_regex  = "^/(lattice/v1/costume(/|$)|healthz$)"
  }
  costume_stage_login = "${local.costume_stage.hostname}${local.costume_stage.login_path}"
}

# Who may sign in: the owner. Its own policy, so someone added elsewhere never lands here by the way.
resource "cloudflare_zero_trust_access_policy" "costume_stage_owner" {
  account_id       = var.cloudflare_account_id
  name             = "Costume Party staging: owner"
  decision         = "allow"
  session_duration = "12h"
  include          = [{ email = { email = local.browser_manager_owner } }]
}

resource "cloudflare_zero_trust_access_application" "costume_stage_login" {
  account_id       = var.cloudflare_account_id
  name             = "Costume Party staging sign-in"
  type             = "self_hosted"
  domain           = local.costume_stage_login
  destinations     = [{ type = "public", uri = local.costume_stage_login }]
  session_duration = "12h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.github[*].id,
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )
  policies = [{ id = cloudflare_zero_trust_access_policy.costume_stage_owner.id, precedence = 1 }]
}

resource "cloudflare_zero_trust_access_policy" "costume_stage_public" {
  account_id = var.cloudflare_account_id
  name       = "Costume Party staging (public; the server's own token)"
  decision   = "bypass"
  include    = [{ everyone = {} }]
}

resource "cloudflare_zero_trust_access_application" "costume_stage_public" {
  account_id   = var.cloudflare_account_id
  name         = "Costume Party staging (public; the server's own token)"
  type         = "self_hosted"
  domain       = local.costume_stage.hostname
  destinations = [{ type = "public", uri = local.costume_stage.hostname }]
  policies     = [{ id = cloudflare_zero_trust_access_policy.costume_stage_public.id, precedence = 1 }]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "dolphin_costume" {
  account_id = var.cloudflare_account_id
  name       = "dolphin-costume"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "dolphin_costume" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.dolphin_costume.id
  config = {
    ingress = [
      {
        # The sign-in: behind Access, and the connector checks the Access token too.
        hostname = local.costume_stage.hostname
        path     = "^${local.costume_stage.login_path}"
        service  = "http://127.0.0.1:${local.costume_stage.port}"
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.costume_stage_login.aud]
          }
        }
      },
      {
        hostname = local.costume_stage.hostname
        path     = local.costume_stage.api_regex
        service  = "http://127.0.0.1:${local.costume_stage.port}"
      },
      { service = "http_status:404" },
    ]
  }
}

resource "cloudflare_dns_record" "costume_stage" {
  zone_id = var.dolphin_zone_id
  name    = local.costume_stage.hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.dolphin_costume.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Costume Party staging (Dolphin account); dolphin-costume.tf"
  # The URL exists only after its Access applications and the tunnel's rules do.
  depends_on = [
    cloudflare_zero_trust_access_application.costume_stage_login,
    cloudflare_zero_trust_access_application.costume_stage_public,
    cloudflare_zero_trust_tunnel_cloudflared_config.dolphin_costume,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "dolphin_costume" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.dolphin_costume.id
}

# The connector's token, in the Dolphin account, where the costume task's execution role reads it.
resource "aws_secretsmanager_secret" "dolphin_costume_tunnel" {
  provider                = aws.dolphin
  name                    = "costume/nonprod/tunnel"
  description             = "Connector token for the dolphin-costume Cloudflare tunnel only (dolphin-costume.tf in ejc3/aws); key TUNNEL_TOKEN"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "dolphin_costume_tunnel" {
  provider      = aws.dolphin
  secret_id     = aws_secretsmanager_secret.dolphin_costume_tunnel.id
  secret_string = jsonencode({ TUNNEL_TOKEN = data.cloudflare_zero_trust_tunnel_cloudflared_token.dolphin_costume.token })
}

# What the task needs to know about its host (no credential): dolphin-aws costume.tf reads it.
resource "aws_ssm_parameter" "dolphin_costume_stage" {
  provider = aws.dolphin
  name     = "/costume/stage"
  type     = "String"
  value = jsonencode({
    hostname           = local.costume_stage.hostname
    access_team_domain = local.browser_manager_issuer
    access_aud         = cloudflare_zero_trust_access_application.costume_stage_login.aud
    tunnel_secret_name = aws_secretsmanager_secret.dolphin_costume_tunnel.name
  })
}

output "dolphin_costume" {
  description = "Costume Party's staging host (the connector token: costume/nonprod/tunnel in the Dolphin account)"
  value       = "https://${local.costume_stage.hostname}"
}

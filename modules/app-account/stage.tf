# The app's staging host: one remote-managed tunnel to the app's task (its connector runs there,
# with the token written into the app's account below), one hostname, and Access. As astryx-stage.tf:
# the sign-in path is behind Access; the API's own paths bypass it (the API checks its own tokens);
# zero-cache, when there is one, takes everything else; anything else is a 404.

locals {
  stage_on   = var.stage != null
  login_path = local.stage_on ? "${var.stage.hostname}${var.stage.login_path}" : null
  stage_pointer = local.stage_on ? {
    hostname           = var.stage.hostname
    access_team_domain = var.stage.access_issuer
    access_aud         = cloudflare_zero_trust_access_application.login[0].aud
    tunnel_secret_name = aws_secretsmanager_secret.tunnel[0].name
  } : null
}

resource "cloudflare_zero_trust_access_application" "login" {
  count            = local.stage_on ? 1 : 0
  account_id       = var.stage.cloudflare_account_id
  name             = "${var.ou_name} staging sign-in"
  type             = "self_hosted"
  domain           = local.login_path
  destinations     = [{ type = "public", uri = local.login_path }]
  session_duration = "12h"
  allowed_idps     = var.stage.idp_ids
  policies         = [for i, id in var.stage.login_policy_ids : { id = id, precedence = i + 1 }]
}

resource "cloudflare_zero_trust_access_policy" "public" {
  count      = local.stage_on ? 1 : 0
  account_id = var.stage.cloudflare_account_id
  name       = "${var.ou_name} staging (public; the API's own token)"
  decision   = "bypass"
  include    = [{ everyone = {} }]
}

resource "cloudflare_zero_trust_access_application" "public" {
  count        = local.stage_on ? 1 : 0
  account_id   = var.stage.cloudflare_account_id
  name         = "${var.ou_name} staging (public; the API's own token)"
  type         = "self_hosted"
  domain       = var.stage.hostname
  destinations = [{ type = "public", uri = var.stage.hostname }]
  policies     = [{ id = cloudflare_zero_trust_access_policy.public[0].id, precedence = 1 }]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "stage" {
  count      = local.stage_on ? 1 : 0
  account_id = var.stage.cloudflare_account_id
  name       = "${var.app}-stage"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "stage" {
  count      = local.stage_on ? 1 : 0
  account_id = var.stage.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.stage[0].id
  config = {
    ingress = concat(
      [
        {
          # The sign-in: behind Access, and the connector checks the Access token too.
          hostname = var.stage.hostname
          path     = "^${replace(var.stage.login_path, ".", "\\.")}"
          service  = "http://localhost:${var.stage.api_port}"
          origin_request = {
            access = {
              required  = true
              team_name = var.stage.team_name
              aud_tag   = [cloudflare_zero_trust_access_application.login[0].aud]
            }
          }
        },
        {
          hostname = var.stage.hostname
          path     = var.stage.api_path_regex
          service  = "http://localhost:${var.stage.api_port}"
        },
      ],
      var.stage.zero_port == null ? [] : [{
        hostname = var.stage.hostname
        service  = "http://localhost:${var.stage.zero_port}"
      }],
      [{ service = "http_status:404" }],
    )
  }
}

resource "cloudflare_dns_record" "stage" {
  count   = local.stage_on ? 1 : 0
  zone_id = var.stage.zone_id
  name    = var.stage.hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.stage[0].id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "${var.app} staging (${var.app} account); ${var.stage.login_path} behind Access"
  # The URL exists only after its Access applications and the tunnel's rules do.
  depends_on = [
    cloudflare_zero_trust_access_application.login,
    cloudflare_zero_trust_access_application.public,
    cloudflare_zero_trust_tunnel_cloudflared_config.stage,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "stage" {
  count      = local.stage_on ? 1 : 0
  account_id = var.stage.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.stage[0].id
}

# The connector's token, in the app's account, where its task's execution role reads it.
resource "aws_secretsmanager_secret" "tunnel" {
  count                   = local.stage_on ? 1 : 0
  provider                = aws.app
  name                    = "${var.app}/nonprod/tunnel"
  description             = "Connector token for the ${var.app}-stage Cloudflare tunnel only (ejc3/aws modules/app-account); key TUNNEL_TOKEN"
  recovery_window_in_days = 7
}
resource "aws_secretsmanager_secret_version" "tunnel" {
  count         = local.stage_on ? 1 : 0
  provider      = aws.app
  secret_id     = aws_secretsmanager_secret.tunnel[0].id
  secret_string = jsonencode({ TUNNEL_TOKEN = data.cloudflare_zero_trust_tunnel_cloudflared_token.stage[0].token })
}

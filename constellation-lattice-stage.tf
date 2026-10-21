# constellation-lattice-stage.tf
#
# Lattice staging (constellation's CRM example, ejc3/constellation docs/CRM.md; its issue #8, step 4), behind Cloudflare
# Access like the other drafts. The service runs in the constellation account (constellation-account.tf) as an ECS task
# that constellation's own Terraform builds from fcvm-arm; what lives on Cloudflare is built here, because Cloudflare
# credentials stay on the jumpboxes:
#
#   lattice-stage.dolphin-labs.dev  -> tunnel lattice-stage -> cloudflared sidecar in the task -> http://localhost:8080
#
# The tunnel dials OUT from the task, so nothing in either account takes an inbound connection. Its connector token is
# written into the constellation account (constellation/nonprod/tunnel, key TUNNEL_TOKEN) for the task to read; this
# account holds no copy outside Terraform state. Everything else constellation needs (hostname, port, the Access team
# domain and audience tag for the API's own JWT check) is in /infra/constellation (constellation-account.tf).
#
# Who gets in:
#   - the whole host: the owner, through the browser-manager owner policy, signing in with GitHub, Google or a one-time
#     PIN. Constellation's design opens the site to anyone who signs in (Lattice checks workspace membership itself);
#     opening a draft to the internet is the owner's call, so this starts owner-only.
#   - /lattice/live and /lattice/v1/*: bypassed. The iOS and macOS apps and the MCP server send the API's own signed
#     Bearer token there, which the API verifies; those tokens are minted only at /lattice/native/login, which stays
#     behind Access. So the connector cannot require an Access token itself (a bypassed request carries none): the
#     edge enforces Access on every other path, and the API checks the Access JWT (ACCESS_AUD) on them as well.

locals {
  lattice_stage_hostname = "lattice-stage.dolphin-labs.dev"
  # The API's port inside the task (API_PORT): the sidecar shares the task's network namespace, so localhost reaches it.
  lattice_stage_api_port = 8080
  lattice_stage_bypass_paths = [
    "${local.lattice_stage_hostname}/lattice/live",
    "${local.lattice_stage_hostname}/lattice/v1/*",
  ]
  lattice_stage_pointer = {
    hostname           = local.lattice_stage_hostname
    api_port           = local.lattice_stage_api_port
    access_team_domain = "https://ejc3.cloudflareaccess.com"
    access_aud         = cloudflare_zero_trust_access_application.lattice_stage.aud
    tunnel_secret_name = aws_secretsmanager_secret.lattice_stage_tunnel.name
    tunnel_secret_key  = "TUNNEL_TOKEN"
  }
}

resource "cloudflare_zero_trust_access_application" "lattice_stage" {
  account_id       = var.cloudflare_account_id
  name             = "Lattice staging (constellation)"
  domain           = local.lattice_stage_hostname
  type             = "self_hosted"
  session_duration = "12h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.github[*].id,
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )

  # The policy attachment MUST be declared here; see cc_games_dev in cloudflare.tf.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.browser_manager_owner.id
      precedence = 1
    },
  ]
}

resource "cloudflare_zero_trust_access_policy" "lattice_stage_bearer_paths" {
  account_id = var.cloudflare_account_id
  name       = "Lattice staging: API bearer-token paths (the API is the gate)"
  decision   = "bypass"
  include    = [{ everyone = {} }]
}

# A more specific path wins over the host's application, so only these two paths skip Access.
resource "cloudflare_zero_trust_access_application" "lattice_stage_bearer_paths" {
  account_id   = var.cloudflare_account_id
  name         = "Lattice staging: API bearer-token paths"
  type         = "self_hosted"
  domain       = local.lattice_stage_bypass_paths[0]
  destinations = [for uri in local.lattice_stage_bypass_paths : { type = "public", uri = uri }]

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.lattice_stage_bearer_paths.id
      precedence = 1
    },
  ]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "lattice_stage" {
  account_id = var.cloudflare_account_id
  name       = "lattice-stage"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "lattice_stage" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.lattice_stage.id
  config = {
    ingress = [
      {
        hostname = local.lattice_stage_hostname
        service  = "http://localhost:${local.lattice_stage_api_port}"
      },
      { service = "http_status:404" },
    ]
  }
}

# Overrides the *.dolphin-labs.dev wildcard. Created after both Access applications, so the host is never reachable
# without them.
resource "cloudflare_dns_record" "lattice_stage" {
  zone_id = var.dolphin_zone_id
  name    = local.lattice_stage_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.lattice_stage.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Lattice staging (constellation account); Access-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.lattice_stage,
    cloudflare_zero_trust_access_application.lattice_stage_bearer_paths,
    cloudflare_zero_trust_tunnel_cloudflared_config.lattice_stage,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "lattice_stage" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.lattice_stage.id
}

# In the constellation account, where the ECS task reads it (its execution role, built by constellation). The token runs
# a connector for this one tunnel and nothing else: whoever holds it can serve this hostname's traffic, never get past Access.
resource "aws_secretsmanager_secret" "lattice_stage_tunnel" {
  provider                = aws.constellation
  name                    = "constellation/nonprod/tunnel"
  description             = "Connector token for the lattice-stage Cloudflare tunnel only (ejc3/aws constellation-lattice-stage.tf); key TUNNEL_TOKEN"
  recovery_window_in_days = 7
  tags                    = { Managed = "ejc3/aws", Name = "constellation/nonprod/tunnel" }
}

resource "aws_secretsmanager_secret_version" "lattice_stage_tunnel" {
  provider      = aws.constellation
  secret_id     = aws_secretsmanager_secret.lattice_stage_tunnel.id
  secret_string = jsonencode({ TUNNEL_TOKEN = data.cloudflare_zero_trust_tunnel_cloudflared_token.lattice_stage.token })
}

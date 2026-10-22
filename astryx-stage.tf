# astryx-stage.tf
#
# Astryx sync staging. The service runs in the astryx account (astryx-account.tf) as one ECS task that ejc3/astryx-infra
# builds: the sync API (:8080), zero-cache (:4848) and a cloudflared sidecar, on one localhost. What lives on Cloudflare
# is built here, because Cloudflare credentials stay on the jumpboxes:
#
#   astryx-stage.dolphin-labs.dev -> tunnel astryx-stage -> cloudflared in the task -> /api/*, /native/*, /healthz: localhost:8080
#                                                                                    everything else: localhost:4848
#
# The tunnel dials OUT from the task, so nothing in either account takes an inbound connection. Its connector token is
# written into the astryx account (astryx/nonprod/tunnel, key TUNNEL_TOKEN) for the task to read; this account holds no
# copy outside Terraform state. Everything else astryx-infra needs (hostname, the Access team domain and audience tag for
# the API's own JWT check, the secret's name) is in /infra/astryx (astryx-account.tf).
#
# Who gets in:
#   - /native/login (and below it): Access, open to ANYONE who signs in with GitHub, Google or a one-time PIN (owner's
#     request: TestFlight testers sign in to sync their own data; the API keeps each person to their own rows). This is
#     the one place a session token is minted: the API reads the Access JWT (ACCESS_AUD) there and hands the app its own
#     signed token. The connector also requires a valid Access token on that path (its first ingress rule), so if the
#     application were ever dropped the path still refuses a request without one.
#   - the rest of the host: no Access application. The apps call /api/* and zero-cache's socket with the API's own Bearer
#     token, which the API verifies (zero-cache forwards it to /api/zero/query and /api/zero/mutate); a request without one
#     reads and writes nothing. /healthz says whether sync is on.

locals {
  astryx_stage_hostname   = "astryx-stage.dolphin-labs.dev"
  astryx_stage_api_port   = 8080
  astryx_stage_zero_port  = 4848
  astryx_stage_login_path = "${local.astryx_stage_hostname}/native/login"
  # The connector's rules. The login regex is a superset of the Access application's path (prefix match), so the connector
  # never lets through less protected what Access covers. The API regex sends the API's paths to the API; zero-cache gets
  # the rest (its sync socket and keepalive).
  astryx_stage_login_regex = "^/native/login"
  astryx_stage_api_regex   = "^/(api/|native/|healthz$)"
  astryx_stage_pointer = {
    hostname           = local.astryx_stage_hostname
    access_team_domain = local.browser_manager_issuer
    access_aud         = cloudflare_zero_trust_access_application.astryx_stage_login.aud
    tunnel_secret_name = aws_secretsmanager_secret.astryx_stage_tunnel.name
  }
}

# Anyone who signs in (constellation's own idiom: allow, include everyone, the login methods on the application).
resource "cloudflare_zero_trust_access_policy" "astryx_stage_anyone_signed_in" {
  account_id       = var.cloudflare_account_id
  name             = "Astryx staging sign-in: anyone who signs in"
  decision         = "allow"
  session_duration = "12h"
  include          = [{ everyone = {} }]
}

resource "cloudflare_zero_trust_access_application" "astryx_stage_login" {
  account_id       = var.cloudflare_account_id
  name             = "Astryx staging sign-in"
  type             = "self_hosted"
  domain           = local.astryx_stage_login_path
  destinations     = [{ type = "public", uri = local.astryx_stage_login_path }]
  session_duration = "12h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.github[*].id,
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )

  # The policy attachment MUST be declared here; see cc_games_dev in cloudflare.tf.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.astryx_stage_anyone_signed_in.id
      precedence = 1
    },
  ]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "astryx_stage" {
  account_id = var.cloudflare_account_id
  name       = "astryx-stage"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "astryx_stage" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.astryx_stage.id
  config = {
    ingress = [
      {
        hostname = local.astryx_stage_hostname
        path     = local.astryx_stage_login_regex
        service  = "http://localhost:${local.astryx_stage_api_port}"
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.astryx_stage_login.aud]
          }
        }
      },
      {
        hostname = local.astryx_stage_hostname
        path     = local.astryx_stage_api_regex
        service  = "http://localhost:${local.astryx_stage_api_port}"
      },
      {
        hostname = local.astryx_stage_hostname
        service  = "http://localhost:${local.astryx_stage_zero_port}"
      },
      { service = "http_status:404" },
    ]
  }
}

# Overrides the *.dolphin-labs.dev wildcard. Created after the sign-in application and the tunnel's rules, so the login
# path is never reachable without Access.
resource "cloudflare_dns_record" "astryx_stage" {
  zone_id = var.dolphin_zone_id
  name    = local.astryx_stage_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.astryx_stage.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Astryx sync staging (astryx account); /native/login behind Access"
  depends_on = [
    cloudflare_zero_trust_access_application.astryx_stage_login,
    cloudflare_zero_trust_tunnel_cloudflared_config.astryx_stage,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "astryx_stage" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.astryx_stage.id
}

# In the astryx account, where the ECS task reads it (its execution role, built by astryx-infra). The token runs a connector
# for this one tunnel and nothing else: whoever holds it can serve this hostname's traffic, never get past Access.
resource "aws_secretsmanager_secret" "astryx_stage_tunnel" {
  provider                = aws.astryx
  name                    = "astryx/nonprod/tunnel"
  description             = "Connector token for the astryx-stage Cloudflare tunnel only (ejc3/aws astryx-stage.tf); key TUNNEL_TOKEN"
  recovery_window_in_days = 7
  tags                    = { Managed = "ejc3/aws", Name = "astryx/nonprod/tunnel" }
}

resource "aws_secretsmanager_secret_version" "astryx_stage_tunnel" {
  provider      = aws.astryx
  secret_id     = aws_secretsmanager_secret.astryx_stage_tunnel.id
  secret_string = jsonencode({ TUNNEL_TOKEN = data.cloudflare_zero_trust_tunnel_cloudflared_token.astryx_stage.token })
}

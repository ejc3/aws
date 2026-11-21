# fcvm-dev-prod.tf
#
# The Cloudflare side of the PRODUCTION fcvm fleet (fcvm.dev), gated on var.fcvm_fleet_prod_enabled.
# Turning the switch off omits these resources. Staging (fcvm-dev.tf: stage.fcvm.dev, fleet-api-stage.fcvm.dev) is untouched and
# stays behind Access. Owner decisions, 2026-11-18: a separate production stack at fcvm.dev in the same fcvm-fleet account
# as staging (tagged Fleet=prod); only Turnstile-verified visitors may wake the fleet or take a seat; all modes public.
#
# With the switch on:
#   fleet-api.fcvm.dev  -> tunnel fcvm-fleet-prod-coordinator -> the prod coordinator in its task (localhost:8080).
#                          /live and /poster.jpg are public (no Access application, no connector check): the page at
#                          fcvm.dev opens /live from the viewer's browser, and the coordinator refuses every intent until
#                          the connection has passed Turnstile (fleet/PROTOCOL.md, "verify"). /agent takes only the prod
#                          agents' service token, checked by Access and again by the connector. Any other path is a 404.
#   fleet-ops.fcvm.dev  -> the same coordinator, every path behind Access: the owner, or the prod CI service token (the
#                          promotion smoke). /agent is a 404 there, so neither credential can act as a GPU host.
#   A Turnstile widget for fcvm.dev (managed mode); its site key and secret are written for the prod coordinator only.
#
# NOT here, each for the go-live PR, which needs the owner's go-ahead: the fcvm.dev apex record, a Worker custom domain on
# fcvm.dev, and www. Until then fleet-api.fcvm.dev has no page that uses it.
#
# Written into the fcvm-fleet account (us-east-2), as staging's are, under fcvm-fleet/prod/:
#   fcvm-fleet/prod/coordinator-tunnel-token  the prod tunnel's connector token (key TUNNEL_TOKEN), for the cloudflared
#                                             container of the prod coordinator task
#   fcvm-fleet/prod/agent-access-token        the service token the prod GPU hosts' agents present at /agent (client_id,
#                                             client_secret)
#   fcvm-fleet/prod/ci-access-token           the service token prod CI presents at fleet-ops.fcvm.dev (client_id,
#                                             client_secret)
#   fcvm-fleet/prod/turnstile                 FLEET_TURNSTILE_SITE_KEY and FLEET_TURNSTILE_SECRET, the environment names
#                                             the coordinator reads them from (fcvm fleet/PROTOCOL.md, "Running it")

variable "fcvm_fleet_prod_enabled" {
  description = "Create the production fcvm fleet (fcvm.dev): its Cloudflare resources, fleet secrets and AWS stack."
  type        = bool
  default     = true
}

locals {
  fcvm_fleet_prod             = var.fcvm_fleet_prod_enabled ? 1 : 0
  fcvm_dev_api_prod_hostname  = "fleet-api.fcvm.dev"
  fcvm_dev_ops_prod_hostname  = "fleet-ops.fcvm.dev"
  fcvm_dev_agent_prod_path    = "${local.fcvm_dev_api_prod_hostname}/agent"
  fcvm_fleet_prod_tags        = { Managed = "ejc3/aws", Fleet = "prod" }
  fcvm_fleet_prod_coordinator = "fcvm-fleet-prod-coordinator"
  # The prod stack's coordinator roles, by the names that PR creates (none exists yet; a name in a Deny condition needs no
  # role behind it), in the account the secret lives in. The coordinator reads the Turnstile secret and site key from its
  # environment, which ECS fills from the secret through the execution role; the task role is listed for a coordinator
  # that reads the secret itself.
  fcvm_fleet_prod_turnstile_readers = [for a in data.aws_caller_identity.fcvm_fleet_prod[*].account_id : [
    "arn:aws:iam::${a}:role/${local.fcvm_fleet_prod_coordinator}-execution",
    "arn:aws:iam::${a}:role/${local.fcvm_fleet_prod_coordinator}",
  ]]
}

data "aws_caller_identity" "fcvm_fleet_prod" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
}

# ------------------------------------------------------------------------------------------------ Access
resource "cloudflare_zero_trust_access_policy" "fcvm_dev_prod_owner" {
  count            = local.fcvm_fleet_prod
  account_id       = var.cloudflare_account_id
  name             = "fcvm.dev prod: owner"
  decision         = "allow"
  session_duration = "12h"
  include          = [{ email = { email = local.browser_manager_owner } }]
}

# The prod GPU hosts' agents: their own service token, never staging's, so a staging host cannot join the prod fleet.
resource "cloudflare_zero_trust_access_service_token" "fcvm_fleet_prod_agents" {
  count      = local.fcvm_fleet_prod
  account_id = var.cloudflare_account_id
  name       = "fcvm-fleet-prod-agents"
  duration   = "8760h"

  lifecycle {
    create_before_destroy = true
  }
}

resource "cloudflare_zero_trust_access_policy" "fcvm_fleet_prod_agents" {
  count            = local.fcvm_fleet_prod
  account_id       = var.cloudflare_account_id
  name             = "fcvm fleet prod: agents with their service token"
  decision         = "non_identity"
  session_duration = "24h"
  include          = [{ service_token = { token_id = cloudflare_zero_trust_access_service_token.fcvm_fleet_prod_agents[0].id } }]
}

# fleet-api.fcvm.dev has no application of its own (its /live and /poster.jpg are public); /agent has this one.
resource "cloudflare_zero_trust_access_application" "fcvm_dev_agent_prod" {
  count        = local.fcvm_fleet_prod
  account_id   = var.cloudflare_account_id
  name         = "fcvm fleet API prod: agents"
  type         = "self_hosted"
  domain       = local.fcvm_dev_agent_prod_path
  destinations = [{ type = "public", uri = local.fcvm_dev_agent_prod_path }]
  policies     = [{ id = cloudflare_zero_trust_access_policy.fcvm_fleet_prod_agents[0].id, precedence = 1 }]
}

# Prod CI (the promotion smoke after a green staging smoke): its own token, admitted at fleet-ops.fcvm.dev only.
resource "cloudflare_zero_trust_access_service_token" "fcvm_prod_ci" {
  count      = local.fcvm_fleet_prod
  account_id = var.cloudflare_account_id
  name       = "fcvm-prod-ci"
  duration   = "8760h"

  lifecycle {
    create_before_destroy = true
  }
}

resource "cloudflare_zero_trust_access_policy" "fcvm_prod_ci" {
  count      = local.fcvm_fleet_prod
  account_id = var.cloudflare_account_id
  name       = "fcvm prod: main-only CI"
  decision   = "non_identity"
  include    = [{ service_token = { token_id = cloudflare_zero_trust_access_service_token.fcvm_prod_ci[0].id } }]
}

resource "cloudflare_zero_trust_access_application" "fcvm_dev_ops_prod" {
  count            = local.fcvm_fleet_prod
  account_id       = var.cloudflare_account_id
  name             = "fcvm fleet ops prod"
  domain           = local.fcvm_dev_ops_prod_hostname
  type             = "self_hosted"
  session_duration = "12h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.github[*].id,
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )
  policies = [
    { id = cloudflare_zero_trust_access_policy.fcvm_dev_prod_owner[0].id, precedence = 1 },
    { id = cloudflare_zero_trust_access_policy.fcvm_prod_ci[0].id, precedence = 2 },
  ]
}

# ------------------------------------------------------------------------------------------------ Turnstile
# Managed mode: Cloudflare decides whether a visitor sees a checkbox. fcvm.dev and its subdomains may use the widget.
resource "cloudflare_turnstile_widget" "fcvm_dev_prod" {
  count      = local.fcvm_fleet_prod
  account_id = var.cloudflare_account_id
  name       = "fcvm.dev fleet"
  domains    = ["fcvm.dev"]
  mode       = "managed"
}

# ------------------------------------------------------------------------------------------------ the prod coordinator tunnel
resource "cloudflare_zero_trust_tunnel_cloudflared" "fcvm_fleet_prod_coordinator" {
  count      = local.fcvm_fleet_prod
  account_id = var.cloudflare_account_id
  name       = local.fcvm_fleet_prod_coordinator
  config_src = "cloudflare"
}

# Rules match in order. The public routes name their exact paths; the connector verifies Access on every other route.
resource "cloudflare_zero_trust_tunnel_cloudflared_config" "fcvm_fleet_prod_coordinator" {
  count      = local.fcvm_fleet_prod
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.fcvm_fleet_prod_coordinator[0].id
  config = {
    ingress = [
      {
        hostname = local.fcvm_dev_api_prod_hostname
        path     = "^/agent$"
        service  = "http://localhost:${local.fcvm_fleet_coordinator_port}"
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.fcvm_dev_agent_prod[0].aud]
          }
        }
      },
      {
        hostname = local.fcvm_dev_api_prod_hostname
        path     = "^/(live|poster\\.jpg)$"
        service  = "http://localhost:${local.fcvm_fleet_coordinator_port}"
      },
      { hostname = local.fcvm_dev_api_prod_hostname, service = "http_status:404" },
      { hostname = local.fcvm_dev_ops_prod_hostname, path = "^/agent", service = "http_status:404" },
      {
        hostname = local.fcvm_dev_ops_prod_hostname
        service  = "http://localhost:${local.fcvm_fleet_coordinator_port}"
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.fcvm_dev_ops_prod[0].aud]
          }
        }
      },
      { service = "http_status:404" },
    ]
  }
}

# Created after the applications and the ingress, so neither host is reachable without them. fleet-api.fcvm.dev serves
# its public paths only once a prod connector runs (the prod stack PR); until then Cloudflare answers with its tunnel error.
resource "cloudflare_dns_record" "fcvm_dev_api_prod" {
  count   = local.fcvm_fleet_prod
  zone_id = var.fcvm_dev_zone_id
  name    = local.fcvm_dev_api_prod_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.fcvm_fleet_prod_coordinator[0].id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "fcvm fleet coordinator, prod (fcvm-fleet account); /live and /poster.jpg public, /agent Access-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.fcvm_dev_agent_prod,
    cloudflare_zero_trust_tunnel_cloudflared_config.fcvm_fleet_prod_coordinator,
  ]
}

resource "cloudflare_dns_record" "fcvm_dev_ops_prod" {
  count   = local.fcvm_fleet_prod
  zone_id = var.fcvm_dev_zone_id
  name    = local.fcvm_dev_ops_prod_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.fcvm_fleet_prod_coordinator[0].id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "fcvm fleet coordinator, prod operator and CI host; Access-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.fcvm_dev_ops_prod,
    cloudflare_zero_trust_tunnel_cloudflared_config.fcvm_fleet_prod_coordinator,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "fcvm_fleet_prod_coordinator" {
  count      = local.fcvm_fleet_prod
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.fcvm_fleet_prod_coordinator[0].id
}

# ------------------------------------------------------------------------------------------------ into the fleet account
resource "aws_secretsmanager_secret" "fcvm_fleet_prod_coordinator_tunnel" {
  count                   = local.fcvm_fleet_prod
  provider                = aws.fcvm_fleet
  name                    = "fcvm-fleet/prod/coordinator-tunnel-token"
  description             = "Connector token for the fcvm-fleet-prod-coordinator Cloudflare tunnel only (ejc3/aws fcvm-dev-prod.tf); key TUNNEL_TOKEN"
  recovery_window_in_days = 7
  tags                    = merge(local.fcvm_fleet_prod_tags, { Name = "fcvm-fleet/prod/coordinator-tunnel-token" })
}

resource "aws_secretsmanager_secret_version" "fcvm_fleet_prod_coordinator_tunnel" {
  count         = local.fcvm_fleet_prod
  provider      = aws.fcvm_fleet
  secret_id     = aws_secretsmanager_secret.fcvm_fleet_prod_coordinator_tunnel[0].id
  secret_string = jsonencode({ TUNNEL_TOKEN = data.cloudflare_zero_trust_tunnel_cloudflared_token.fcvm_fleet_prod_coordinator[0].token })
}

resource "aws_secretsmanager_secret" "fcvm_fleet_prod_agent_access" {
  count                   = local.fcvm_fleet_prod
  provider                = aws.fcvm_fleet
  name                    = "fcvm-fleet/prod/agent-access-token"
  description             = "Cloudflare Access service token the prod fleet's agents present at fleet-api.fcvm.dev/agent (ejc3/aws fcvm-dev-prod.tf); keys client_id, client_secret"
  recovery_window_in_days = 7
  tags                    = merge(local.fcvm_fleet_prod_tags, { Name = "fcvm-fleet/prod/agent-access-token" })
}

resource "aws_secretsmanager_secret_version" "fcvm_fleet_prod_agent_access" {
  count     = local.fcvm_fleet_prod
  provider  = aws.fcvm_fleet
  secret_id = aws_secretsmanager_secret.fcvm_fleet_prod_agent_access[0].id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.fcvm_fleet_prod_agents[0].client_id
    client_secret = cloudflare_zero_trust_access_service_token.fcvm_fleet_prod_agents[0].client_secret
  })
}

resource "aws_secretsmanager_secret" "fcvm_fleet_prod_ci_access" {
  count                   = local.fcvm_fleet_prod
  provider                = aws.fcvm_fleet
  name                    = "fcvm-fleet/prod/ci-access-token"
  description             = "Cloudflare Access service token prod CI presents at fleet-ops.fcvm.dev (ejc3/aws fcvm-dev-prod.tf); keys client_id, client_secret"
  recovery_window_in_days = 7
  tags                    = merge(local.fcvm_fleet_prod_tags, { Name = "fcvm-fleet/prod/ci-access-token" })
}

resource "aws_secretsmanager_secret_version" "fcvm_fleet_prod_ci_access" {
  count     = local.fcvm_fleet_prod
  provider  = aws.fcvm_fleet
  secret_id = aws_secretsmanager_secret.fcvm_fleet_prod_ci_access[0].id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.fcvm_prod_ci[0].client_id
    client_secret = cloudflare_zero_trust_access_service_token.fcvm_prod_ci[0].client_secret
  })
}

resource "aws_secretsmanager_secret" "fcvm_fleet_prod_turnstile" {
  count                   = local.fcvm_fleet_prod
  provider                = aws.fcvm_fleet
  name                    = "fcvm-fleet/prod/turnstile"
  description             = "Turnstile widget for fcvm.dev (ejc3/aws fcvm-dev-prod.tf); keys FLEET_TURNSTILE_SITE_KEY, FLEET_TURNSTILE_SECRET; readable by the prod coordinator only"
  recovery_window_in_days = 7
  tags                    = merge(local.fcvm_fleet_prod_tags, { Name = "fcvm-fleet/prod/turnstile" })
}

resource "aws_secretsmanager_secret_version" "fcvm_fleet_prod_turnstile" {
  count     = local.fcvm_fleet_prod
  provider  = aws.fcvm_fleet
  secret_id = aws_secretsmanager_secret.fcvm_fleet_prod_turnstile[0].id
  secret_string = jsonencode({
    FLEET_TURNSTILE_SITE_KEY = cloudflare_turnstile_widget.fcvm_dev_prod[0].sitekey
    FLEET_TURNSTILE_SECRET   = cloudflare_turnstile_widget.fcvm_dev_prod[0].secret
  })
}

# Nobody else in the account reads the value, whatever their IAM policy says: only the prod coordinator's roles and the
# role this Terraform applies through, which reads the version back on every refresh. The prod roles still need their
# own Allow (the prod stack PR); until they exist, nothing but Terraform can read it.
resource "aws_secretsmanager_secret_policy" "fcvm_fleet_prod_turnstile" {
  count      = local.fcvm_fleet_prod
  provider   = aws.fcvm_fleet
  secret_arn = aws_secretsmanager_secret.fcvm_fleet_prod_turnstile[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTheProdCoordinatorAndTerraformCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.fcvm_fleet_prod_turnstile[0].arn
      Condition = { ArnNotEquals = { "aws:PrincipalArn" = concat(local.fcvm_fleet_prod_turnstile_readers[0], [local.fcvm_fleet_access_role_arn]) } }
    }]
  })
}

output "fcvm_dev_prod" {
  description = "fcvm.dev prod (off until fcvm_fleet_prod_enabled): hostnames, Access audiences and the fleet secrets' names (no values)"
  value = var.fcvm_fleet_prod_enabled ? {
    api_host         = local.fcvm_dev_api_prod_hostname
    ops_host         = local.fcvm_dev_ops_prod_hostname
    coordinator_port = local.fcvm_fleet_coordinator_port
    agent_aud        = cloudflare_zero_trust_access_application.fcvm_dev_agent_prod[0].aud
    ops_aud          = cloudflare_zero_trust_access_application.fcvm_dev_ops_prod[0].aud
    secrets = {
      coordinator_tunnel = aws_secretsmanager_secret.fcvm_fleet_prod_coordinator_tunnel[0].name
      agent_access       = aws_secretsmanager_secret.fcvm_fleet_prod_agent_access[0].name
      ci_access          = aws_secretsmanager_secret.fcvm_fleet_prod_ci_access[0].name
      turnstile          = aws_secretsmanager_secret.fcvm_fleet_prod_turnstile[0].name
    }
  } : null
}

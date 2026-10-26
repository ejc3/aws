# fcvm-dev.tf
#
# fcvm.dev, the GPU VM wall's zone (ejc3/aws#607; design: ejc3/fcvm#1136). Registered through Cloudflare Registrar on
# 2026-10-22 (USD 8.20 the first year, 12.20 to renew, auto-renew off, WHOIS redaction), so the zone was created by the
# registration and is managed here. What lives on Cloudflare is built here, because Cloudflare credentials stay on the
# jumpboxes; what the fleet reads is written into the fcvm-fleet account (fcvm-fleet-account.tf):
#
#   fcvm-fleet/cloudflare-dns-token      a token that may edit DNS records in fcvm.dev and nothing else: the coordinator
#                                        writes the _acme-challenge TXT records for the wildcard certificate, and once
#                                        deletes the launch names it wrote here before it served them from
#                                        tiles-stage.fcvm.dev (fcvm-fleet-dns.tf)
#   fcvm-fleet/coordinator-tunnel-token  the connector token of tunnel fcvm-fleet-coordinator, for the cloudflared container
#                                        in the coordinator's Fargate task (TUNNEL_TOKEN)
#   fcvm-fleet/agent-access-token        the Access service token the GPU hosts' agents present at /agent (client_id,
#                                        client_secret)
#
# STAGING ONLY, behind Access, the gate created before either hostname exists (owner-only, as Lattice staging):
#   fleet-api-stage.fcvm.dev  -> tunnel fcvm-fleet-coordinator -> the coordinator in its task (localhost:8080). The
#                                connector requires an Access token on every path: the owner's, or at /agent the agents'
#                                service token (its own application, so it is never a bypass).
#   stage.fcvm.dev            -> the fcvm-fleet-stage Worker (deployed from ejc3/fcvm), gated by its own application.
# The public apex and fleet-api.fcvm.dev go live only on the owner's go-ahead: neither has a record or an application here.
#
# CREDENTIAL: the provider token (cloudflare-tunnel-token) has DNS Write account-wide and Zone Settings Write on this zone
# (added 2026-10-22 with one PUT of the token's own definition, as for mapslink.app). The DNS token is minted through the
# token_minter alias (workers-deploy.tf) and its value is in Terraform state, as the Workers deploy token's is.

variable "fcvm_dev_zone_id" {
  description = "Zone id for fcvm.dev (registered through Cloudflare Registrar, so the zone was created automatically)"
  type        = string
  default     = "d4a86875c2375781adb2a3e901552902"
}

locals {
  fcvm_dev_api_stage_hostname = "fleet-api-stage.fcvm.dev"
  fcvm_dev_site_stage_host    = "stage.fcvm.dev"
  # The coordinator's port inside its task: the cloudflared container shares the task's network namespace.
  fcvm_fleet_coordinator_port = 8080
  # Agents dial wss://<api host>/agent (ARCHITECTURE.md, "Coordinator").
  fcvm_dev_agent_path = "${local.fcvm_dev_api_stage_hostname}/agent"
  dns_write           = "4755a26eedb94da69e1066d98aa820be" # Cloudflare's "DNS Write" permission group (zone scope)
}

# ------------------------------------------------------------------------------------------------ the zone
# The registry publishes the DS record and Cloudflare reports active. Keep status managed so a later
# DNSSEC disablement is detected by the plan (ejc3/aws#623).
resource "cloudflare_zone_dnssec" "fcvm_dev" {
  zone_id = var.fcvm_dev_zone_id
  status  = "active"
}

# Every proxied origin presents the fleet's wildcard certificate, so Cloudflare verifies it (ARCHITECTURE.md). .dev is on
# the HSTS preload list; Always Use HTTPS covers anything else.
resource "cloudflare_zone_setting" "fcvm_dev_ssl" {
  zone_id    = var.fcvm_dev_zone_id
  setting_id = "ssl"
  value      = "strict"
}

resource "cloudflare_zone_setting" "fcvm_dev_always_use_https" {
  zone_id    = var.fcvm_dev_zone_id
  setting_id = "always_use_https"
  value      = "on"
}

resource "cloudflare_zone_setting" "fcvm_dev_min_tls" {
  zone_id    = var.fcvm_dev_zone_id
  setting_id = "min_tls_version"
  value      = "1.2"
}

# ------------------------------------------------------------------------------------------------ the DNS token
resource "cloudflare_account_token" "fcvm_fleet_dns" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "fcvm-fleet-dns-fcvm-dev"
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.dns_write }]
    resources         = jsonencode({ "com.cloudflare.api.account.zone.${var.fcvm_dev_zone_id}" = "*" })
  }]
}

# ------------------------------------------------------------------------------------------------ Access
resource "cloudflare_zero_trust_access_policy" "fcvm_dev_stage_owner" {
  account_id       = var.cloudflare_account_id
  name             = "fcvm.dev staging: owner"
  decision         = "allow"
  session_duration = "12h"
  include          = [{ email = { email = local.browser_manager_owner } }]
}

resource "cloudflare_zero_trust_access_application" "fcvm_dev_api_stage" {
  account_id       = var.cloudflare_account_id
  name             = "fcvm fleet API staging"
  domain           = local.fcvm_dev_api_stage_hostname
  type             = "self_hosted"
  session_duration = "12h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.github[*].id,
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )
  policies = [
    { id = cloudflare_zero_trust_access_policy.fcvm_dev_stage_owner.id, precedence = 1 },
    { id = cloudflare_zero_trust_access_policy.fcvm_stage_ci.id, precedence = 2 },
  ]
}

resource "cloudflare_zero_trust_access_application" "fcvm_dev_site_stage" {
  account_id       = var.cloudflare_account_id
  name             = "fcvm.dev site staging"
  domain           = local.fcvm_dev_site_stage_host
  type             = "self_hosted"
  session_duration = "12h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.github[*].id,
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )
  policies = [
    { id = cloudflare_zero_trust_access_policy.fcvm_dev_stage_owner.id, precedence = 1 },
    { id = cloudflare_zero_trust_access_policy.fcvm_stage_ci.id, precedence = 2 },
  ]
}

# The GPU hosts' agents: a service token, never a person (non_identity). A year long, as claw-master's.
resource "cloudflare_zero_trust_access_service_token" "fcvm_fleet_agents" {
  account_id = var.cloudflare_account_id
  name       = "fcvm-fleet-agents"
  duration   = "8760h"

  lifecycle {
    create_before_destroy = true
  }
}

resource "cloudflare_zero_trust_access_policy" "fcvm_fleet_agents" {
  account_id       = var.cloudflare_account_id
  name             = "fcvm fleet: agents with their service token"
  decision         = "non_identity"
  session_duration = "24h"
  include          = [{ service_token = { token_id = cloudflare_zero_trust_access_service_token.fcvm_fleet_agents.id } }]
}

# A more specific path wins over the host's application: /agent takes the agents' token, and still nobody else.
resource "cloudflare_zero_trust_access_application" "fcvm_dev_agent" {
  account_id   = var.cloudflare_account_id
  name         = "fcvm fleet API staging: agents"
  type         = "self_hosted"
  domain       = local.fcvm_dev_agent_path
  destinations = [{ type = "public", uri = local.fcvm_dev_agent_path }]
  policies     = [{ id = cloudflare_zero_trust_access_policy.fcvm_fleet_agents.id, precedence = 1 }]
}

# ------------------------------------------------------------------------------------------------ the stage.fcvm.dev Worker
# ejc3/fcvm's fleet-web-deploy workflow (push to main, cloudflare-deploy environment) deploys fcvm-fleet-stage with
# workers_dev and preview_urls false (fleet/web/wrangler.jsonc). Terraform owns its envelope, as workers-stage.tf does for
# the other staging Workers (ejc3/aws#627): both URL switches held off, and the stage.fcvm.dev custom domain, created after
# the owner-only Access application above already covers the host. The custom domain needs the Worker to exist, so this
# was added after the first deploy. Wrangler never routes the host: the deploy token has no DNS or routes.
locals {
  fcvm_fleet_stage_worker = "fcvm-fleet-stage" # ejc3/fcvm fleet/web/wrangler.jsonc
}

import {
  to = cloudflare_workers_script_subdomain.fcvm_fleet_stage
  id = "${var.cloudflare_account_id}/${local.fcvm_fleet_stage_worker}"
}

# Not in workers-stage.tf's map: that Access application admits the cc-games family, and this Worker is owner-only. With
# workers.dev and previews off, the custom domain is its only URL, and Access gates that host.
resource "cloudflare_workers_script_subdomain" "fcvm_fleet_stage" {
  account_id       = var.cloudflare_account_id
  script_name      = local.fcvm_fleet_stage_worker
  enabled          = false
  previews_enabled = false
}

resource "cloudflare_workers_custom_domain" "fcvm_fleet_stage" {
  account_id = var.cloudflare_account_id
  zone_id    = var.fcvm_dev_zone_id
  hostname   = local.fcvm_dev_site_stage_host
  service    = local.fcvm_fleet_stage_worker
  depends_on = [
    cloudflare_zero_trust_access_application.fcvm_dev_site_stage,
    cloudflare_workers_script_subdomain.fcvm_fleet_stage,
  ]
}

# ------------------------------------------------------------------------------------------------ the coordinator tunnel
resource "cloudflare_zero_trust_tunnel_cloudflared" "fcvm_fleet_coordinator" {
  account_id = var.cloudflare_account_id
  name       = "fcvm-fleet-coordinator"
  config_src = "cloudflare"
}

# Every request needs an Access token for one of the two applications: the connector refuses anything else even if an
# application were dropped.
resource "cloudflare_zero_trust_tunnel_cloudflared_config" "fcvm_fleet_coordinator" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.fcvm_fleet_coordinator.id
  config = {
    ingress = [
      {
        hostname = local.fcvm_dev_api_stage_hostname
        service  = "http://localhost:${local.fcvm_fleet_coordinator_port}"
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.fcvm_dev_api_stage.aud, cloudflare_zero_trust_access_application.fcvm_dev_agent.aud]
          }
        }
      },
      { service = "http_status:404" },
    ]
  }
}

# Created after both applications, so the host is never reachable without them.
resource "cloudflare_dns_record" "fcvm_dev_api_stage" {
  zone_id = var.fcvm_dev_zone_id
  name    = local.fcvm_dev_api_stage_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.fcvm_fleet_coordinator.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "fcvm fleet coordinator, staging (fcvm-fleet account); Access-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.fcvm_dev_api_stage,
    cloudflare_zero_trust_access_application.fcvm_dev_agent,
    cloudflare_zero_trust_tunnel_cloudflared_config.fcvm_fleet_coordinator,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "fcvm_fleet_coordinator" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.fcvm_fleet_coordinator.id
}

# ------------------------------------------------------------------------------------------------ into the fleet account
resource "aws_secretsmanager_secret" "fcvm_fleet_dns_token" {
  provider                = aws.fcvm_fleet
  name                    = "fcvm-fleet/cloudflare-dns-token"
  description             = "Cloudflare token: DNS Write on fcvm.dev only (ejc3/aws fcvm-dev.tf)"
  recovery_window_in_days = 7
  tags                    = { Managed = "ejc3/aws", Name = "fcvm-fleet/cloudflare-dns-token" }
}

resource "aws_secretsmanager_secret_version" "fcvm_fleet_dns_token" {
  provider      = aws.fcvm_fleet
  secret_id     = aws_secretsmanager_secret.fcvm_fleet_dns_token.id
  secret_string = cloudflare_account_token.fcvm_fleet_dns.value
}

resource "aws_secretsmanager_secret" "fcvm_fleet_coordinator_tunnel" {
  provider                = aws.fcvm_fleet
  name                    = "fcvm-fleet/coordinator-tunnel-token"
  description             = "Connector token for the fcvm-fleet-coordinator Cloudflare tunnel only (ejc3/aws fcvm-dev.tf); key TUNNEL_TOKEN"
  recovery_window_in_days = 7
  tags                    = { Managed = "ejc3/aws", Name = "fcvm-fleet/coordinator-tunnel-token" }
}

resource "aws_secretsmanager_secret_version" "fcvm_fleet_coordinator_tunnel" {
  provider      = aws.fcvm_fleet
  secret_id     = aws_secretsmanager_secret.fcvm_fleet_coordinator_tunnel.id
  secret_string = jsonencode({ TUNNEL_TOKEN = data.cloudflare_zero_trust_tunnel_cloudflared_token.fcvm_fleet_coordinator.token })
}

resource "aws_secretsmanager_secret" "fcvm_fleet_agent_access" {
  provider                = aws.fcvm_fleet
  name                    = "fcvm-fleet/agent-access-token"
  description             = "Cloudflare Access service token the fleet's agents present at /agent (ejc3/aws fcvm-dev.tf); keys client_id, client_secret"
  recovery_window_in_days = 7
  tags                    = { Managed = "ejc3/aws", Name = "fcvm-fleet/agent-access-token" }
}

resource "aws_secretsmanager_secret_version" "fcvm_fleet_agent_access" {
  provider  = aws.fcvm_fleet
  secret_id = aws_secretsmanager_secret.fcvm_fleet_agent_access.id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.fcvm_fleet_agents.client_id
    client_secret = cloudflare_zero_trust_access_service_token.fcvm_fleet_agents.client_secret
  })
}

output "fcvm_dev" {
  description = "fcvm.dev staging: hostnames, the coordinator's port, Access settings and the fleet secrets' names (no values)"
  value = {
    api_stage_host     = local.fcvm_dev_api_stage_hostname
    site_stage_host    = local.fcvm_dev_site_stage_host
    coordinator_port   = local.fcvm_fleet_coordinator_port
    access_team_domain = local.browser_manager_issuer
    api_stage_aud      = cloudflare_zero_trust_access_application.fcvm_dev_api_stage.aud
    agent_aud          = cloudflare_zero_trust_access_application.fcvm_dev_agent.aud
    secrets = {
      dns_token          = aws_secretsmanager_secret.fcvm_fleet_dns_token.name
      coordinator_tunnel = aws_secretsmanager_secret.fcvm_fleet_coordinator_tunnel.name
      agent_access       = aws_secretsmanager_secret.fcvm_fleet_agent_access.name
    }
  }
}

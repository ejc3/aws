# dolphin-site-probe.tf
#
# A read-only Cloudflare token for the site probe in the Dolphin account (dolphin-labs-hq/dolphin-aws monitoring.tf,
# lambda/site_probe.py), which alarms on what fans would notice and nothing watched before (dolphin-labs#1283's audit):
# every five minutes it counts the production Worker's (dolphin-labs-prod) failed invocations by event type (the minute's
# cron, the live capture's Durable Object alarms, requests), its console.error lines, and the 5xx answers on
# gamesworthwatching.com.
#
#   Individual Workers Metadata Read-Only   dolphin-labs-prod alone (Workers Logs)
#   Analytics Read                          gamesworthwatching.com alone (GraphQL httpRequestsAdaptiveGroups)
# On 2026-11-13 the scoped token returned production counts; querying another Worker gave zero and an unfiltered query
# still counted only production. Zone analytics works on this Free zone.
#
# The container, its reader and its policy are dolphin-aws's (monitoring.tf, modules/admin-secret: no version,
# administration-only writes, OrganizationAccountAccessRole among administration). This file writes only its value,
# through aws.dolphin. ORDER: dolphin-aws is applied first (it creates the container); then this merges and the
# jumpbox applies it once. The token's value is in state, as with the
# other tokens Terraform mints; state is administration only. ROTATE with
# `terraform apply -replace=cloudflare_account_token.dolphin_site_probe`; the same apply rewrites the secret.

locals {
  dolphin_site_probe_secret      = "dolphin-site-probe/cloudflare"
  dolphin_site_probe_worker_id   = "95ade2c68e2c4c0388494692d9aaa38b" # Immutable ID of dolphin-labs-prod, not its name.
  dolphin_site_probe_worker_read = "afa629b86102452b8b744a5485b870e1" # Individual Workers Metadata Read-Only.
}

resource "cloudflare_account_token" "dolphin_site_probe" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "dolphin-site-probe"
  policies = [
    {
      effect            = "allow"
      permission_groups = [{ id = local.dolphin_site_probe_worker_read }]
      resources = jsonencode({
        "com.cloudflare.api.account.${var.cloudflare_account_id}" = {
          "com.cloudflare.edge.worker.script.${local.dolphin_site_probe_worker_id}" = "*"
        }
      })
    },
    {
      effect            = "allow"
      permission_groups = [{ id = local.cloudflare_observability_zone_groups.analytics_read }]
      resources         = jsonencode({ "com.cloudflare.api.account.zone.${var.gww_zone_id}" = "*" })
    },
  ]
}

# Written unconditionally: dolphin-aws (monitoring.tf) creates the container, and is applied before this file merges
# (the order is in AGENTS.md, "Dolphin site probe token"). A missing container fails this apply, loudly.
resource "aws_secretsmanager_secret_version" "dolphin_site_probe" {
  provider  = aws.dolphin
  secret_id = local.dolphin_site_probe_secret
  secret_string = jsonencode({
    CLOUDFLARE_API_TOKEN  = cloudflare_account_token.dolphin_site_probe.value
    CLOUDFLARE_ACCOUNT_ID = var.cloudflare_account_id
    CLOUDFLARE_ZONE_ID    = var.gww_zone_id
  })
}

output "dolphin_site_probe" {
  description = "The site probe's Cloudflare token, written to this secret in the Dolphin account."
  value       = { secret = local.dolphin_site_probe_secret }
}

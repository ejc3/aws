# dolphin-site-probe.tf
#
# A read-only Cloudflare token for the site probe in the Dolphin account (dolphin-labs-hq/dolphin-aws monitoring.tf,
# lambda/site_probe.py), which alarms on what fans would notice and nothing watched before (dolphin-labs#1283's audit):
# every five minutes it counts the production Worker's (dolphin-labs-prod) failed invocations by event type (the minute's
# cron, the live capture's Durable Object alarms, requests), its console.error lines, and the 5xx answers on
# gamesworthwatching.com.
#
#   Workers Observability Read  account   the telemetry query (Workers Logs). Cloudflare scopes it to the account, so it
#                                         reads every Worker's logs, as dolphin-labs-ci-logs.tf's token does
#   Analytics Read              the zone  GraphQL httpRequestsAdaptiveGroups for gamesworthwatching.com alone
# Nothing that can write. Checked 2026-11-10 with the observability token, which holds both: the telemetry query and the
# zone's adaptive groups answer for a Free zone.
#
# The container, its reader and its policy are dolphin-aws's (monitoring.tf, modules/admin-secret: no version,
# administration-only writes, OrganizationAccountAccessRole among administration). This file writes only its value,
# through aws.dolphin, as dolphin-labs-podcasts.tf writes the podcasts' R2 credentials: until dolphin-aws has created the
# container nothing is written, and the next apply after it writes the value. The token's value is in state, as with the
# other tokens Terraform mints; state is administration only. ROTATE with
# `terraform apply -replace=cloudflare_account_token.dolphin_site_probe`; the same apply rewrites the secret.

locals {
  dolphin_site_probe_secret = "dolphin-site-probe/cloudflare"
}

resource "cloudflare_account_token" "dolphin_site_probe" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "dolphin-site-probe"
  policies = [
    {
      effect            = "allow"
      permission_groups = [{ id = local.cloudflare_observability_account_groups.workers_observability_read }]
      resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
    },
    {
      effect            = "allow"
      permission_groups = [{ id = local.cloudflare_observability_zone_groups.analytics_read }]
      resources         = jsonencode({ "com.cloudflare.api.account.zone.${var.gww_zone_id}" = "*" })
    },
  ]
}

data "aws_secretsmanager_secrets" "dolphin_site_probe" {
  provider = aws.dolphin
  filter {
    name   = "name"
    values = [local.dolphin_site_probe_secret]
  }
}

locals {
  # ListSecrets matches the name as a prefix; only the exact name turns this on.
  dolphin_site_probe_secret_ready = contains(data.aws_secretsmanager_secrets.dolphin_site_probe.names, local.dolphin_site_probe_secret)
}

resource "aws_secretsmanager_secret_version" "dolphin_site_probe" {
  provider  = aws.dolphin
  count     = local.dolphin_site_probe_secret_ready ? 1 : 0
  secret_id = local.dolphin_site_probe_secret
  secret_string = jsonencode({
    CLOUDFLARE_API_TOKEN  = cloudflare_account_token.dolphin_site_probe.value
    CLOUDFLARE_ACCOUNT_ID = var.cloudflare_account_id
    CLOUDFLARE_ZONE_ID    = var.gww_zone_id
  })
}

output "dolphin_site_probe" {
  description = "The site probe's Cloudflare token: whether it is written to dolphin-site-probe/cloudflare in the Dolphin account (false until dolphin-aws creates that container)."
  value = {
    secret         = local.dolphin_site_probe_secret
    secret_written = local.dolphin_site_probe_secret_ready
  }
}

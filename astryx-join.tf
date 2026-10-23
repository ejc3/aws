# astryx-join.tf
#
# Self-serve TestFlight invitations for Astryx Swift: anyone with an @meta.com address scans a QR code, proves they own
# the address, and is added as an external tester of the app (owner, 2026-10-23: "I want anyone with a meta.com email
# address to get a TestFlight invite").
#
#   astryx-join.dolphin-labs.dev -> Cloudflare Access (@meta.com, one-time PIN) -> Worker astryx-join -> App Store Connect
#
#   GET  /      who you are signed in as, and one button
#   POST /join  creates the tester in the beta group "Meta (self-serve)" (or adds an existing tester to it)
#
# The address is never typed. Access mails a one-time PIN to it (which proves ownership) and admits only an address at
# meta.com; the Worker (scripts/astryx-join.mjs, tests in scripts/test-astryx-join.mjs) verifies the Access token itself
# (the team's keys, this application's audience, the team issuer) and requires @meta.com again before it calls Apple.
#
# Unlike the site Workers (workers-stage.tf), Terraform deploys this Worker's code too: it is one file with no build, and
# its App Store Connect key is a secret binding that only an upload of the code can set (the pinned provider has no
# resource that sets a Worker secret on its own; claw-master-dashboard-site.tf). So Cloudflare credentials and the key
# both stay on the jumpbox. The shape of the host is Dolphin Maps' (dolphin-labs-zone.tf): a proxied placeholder record
# and a Workers route, and an Access application of its own for exactly this host, which wins over *.dolphin-labs.dev.
#
# THE KEY: a dedicated App Store Connect API key with the App Manager role (the lowest role that manages testers), in
# Secrets Manager as astryx/testflight-asc-key (JSON: ISSUER_ID, KEY_ID, PRIVATE_KEY). Never the broader Admin key
# (games-worth-watching/app-store-connect, dev-ai-services.tf): a Worker holds this one, and it can do no more than
# testers and builds. The owner creates the secret with its value (README, "Astryx Swift: self-serve TestFlight
# invitations"); Terraform adopts it here with an administration-only read policy and binds it to the Worker.
#
# A SWITCH THAT IS THE SECRET. Nothing below exists until that secret does: an apply before it creates nothing, and the
# first apply after it builds the whole host. Until then astryx-join.dolphin-labs.dev falls to the * wildcard, behind
# the GitHub organization gate, so it never answers anyone outside it.
#
# ORDER, inside the one apply: the record (so the name stops reaching the previews tunnel), then the Access application
# (a @meta.com sign-in must never be admitted to the previews tunnel), then the Worker (it needs the application's
# audience), then the route. The Worker's workers.dev and preview URLs stay off: the route is its only address.
#
# LIMITS: Apple allows 10,000 external testers per app (the page says so when Apple refuses on it). Each person who
# signs in takes a Cloudflare Zero Trust seat until removed (the Free plan has 50). New build versions for external
# testers need Beta App Review before this group sees them.

locals {
  astryx_join_hostname = "astryx-join.dolphin-labs.dev"
  astryx_join_worker   = "astryx-join"
  astryx_join_module   = "${path.module}/scripts/astryx-join.mjs"
  # The App Manager key's secret (above). Its existence is the switch.
  astryx_join_asc_secret = "astryx/testflight-asc-key"
  # Who may sign in: an address at exactly this domain (Access's email_domain rule; the Worker checks it again).
  astryx_join_email_domain = "meta.com"
  # The beta group "Meta (self-serve)" of the app Astryx Swift (an external group, created in App Store Connect). Not a
  # secret: the key is what grants access to it.
  astryx_join_beta_group_id = "56c54804-5285-40de-8206-1553a0c43898"
}

# A list, not a single-secret lookup: a missing secret leaves it empty instead of failing every plan.
data "aws_secretsmanager_secrets" "astryx_join_asc_key" {
  filter {
    name   = "name"
    values = [local.astryx_join_asc_secret]
  }
}

locals {
  # ListSecrets matches the name as a prefix; only the exact name turns this on.
  astryx_join_ready = contains(data.aws_secretsmanager_secrets.astryx_join_asc_key.names, local.astryx_join_asc_secret)
  astryx_join_asc   = local.astryx_join_ready ? try(jsondecode(data.aws_secretsmanager_secret_version.astryx_join_asc_key[0].secret_string), {}) : {}
}

data "aws_secretsmanager_secret_version" "astryx_join_asc_key" {
  count     = local.astryx_join_ready ? 1 : 0
  secret_id = local.astryx_join_asc_secret
}

data "aws_secretsmanager_secret" "astryx_join_asc_key" {
  count = local.astryx_join_ready ? 1 : 0
  name  = local.astryx_join_asc_secret
}

# Administration only: the Worker gets the key through Terraform, and no box needs it.
resource "aws_secretsmanager_secret_policy" "astryx_join_asc_key" {
  count      = local.astryx_join_ready ? 1 : 0
  secret_arn = data.aws_secretsmanager_secret.astryx_join_asc_key[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = data.aws_secretsmanager_secret.astryx_join_asc_key[0].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

# The name exists only so the route has a host to match; the route answers every request, so the content (TEST-NET-1)
# is never contacted. A name of its own, so the * wildcard (the previews tunnel) no longer answers for it.
resource "cloudflare_dns_record" "astryx_join" {
  count   = local.astryx_join_ready ? 1 : 0
  zone_id = var.dolphin_zone_id
  name    = local.astryx_join_hostname
  type    = "A"
  content = "192.0.2.1"
  proxied = true
  ttl     = 1
  comment = "proxied; Worker ${local.astryx_join_worker} route serves all; origin unreachable on purpose"
}

resource "cloudflare_zero_trust_access_policy" "astryx_join_meta" {
  count            = local.astryx_join_ready ? 1 : 0
  account_id       = var.cloudflare_account_id
  name             = "Astryx Swift TestFlight invitations: @meta.com"
  decision         = "allow"
  session_duration = "1h"

  include = [{
    email_domain = { domain = local.astryx_join_email_domain }
  }]
}

resource "cloudflare_zero_trust_access_application" "astryx_join" {
  count            = local.astryx_join_ready ? 1 : 0
  account_id       = var.cloudflare_account_id
  name             = "Astryx Swift TestFlight invitations"
  type             = "self_hosted"
  domain           = local.astryx_join_hostname
  destinations     = [{ type = "public", uri = local.astryx_join_hostname }]
  session_duration = "1h"

  # The one-time PIN only, straight to it: the PIN mailed to the address is the proof of ownership. Not Google or
  # GitHub, whose addresses this host has no use for.
  allowed_idps               = [cloudflare_zero_trust_access_identity_provider.onetimepin.id]
  auto_redirect_to_identity  = true
  app_launcher_visible       = false
  http_only_cookie_attribute = true
  same_site_cookie_attribute = "lax"

  # The policy attachment MUST be declared here; see cc_games_dev in cloudflare.tf. A person only: no service token.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.astryx_join_meta[0].id
      precedence = 1
    },
  ]

  # After the record: while the name still fell to the * wildcard, this application would admit a @meta.com sign-in
  # to the previews tunnel.
  depends_on = [cloudflare_dns_record.astryx_join]
}

resource "cloudflare_workers_script" "astryx_join" {
  count              = local.astryx_join_ready ? 1 : 0
  account_id         = var.cloudflare_account_id
  script_name        = local.astryx_join_worker
  main_module        = "astryx-join.mjs"
  content_file       = local.astryx_join_module
  content_sha256     = filesha256(local.astryx_join_module)
  compatibility_date = "2026-10-01"

  bindings = [
    { name = "ACCESS_TEAM_DOMAIN", type = "plain_text", text = local.browser_manager_issuer },
    { name = "ACCESS_AUD", type = "plain_text", text = cloudflare_zero_trust_access_application.astryx_join[0].aud },
    { name = "ASC_BETA_GROUP_ID", type = "plain_text", text = local.astryx_join_beta_group_id },
    { name = "ASC_ISSUER_ID", type = "secret_text", text = local.astryx_join_asc["ISSUER_ID"] },
    { name = "ASC_KEY_ID", type = "secret_text", text = local.astryx_join_asc["KEY_ID"] },
    { name = "ASC_PRIVATE_KEY", type = "secret_text", text = local.astryx_join_asc["PRIVATE_KEY"] },
  ]

  lifecycle {
    precondition {
      # A yes or no about the secret's shape, never its value.
      condition = nonsensitive(alltrue([
        for k in ["ISSUER_ID", "KEY_ID", "PRIVATE_KEY"] : can(regex("\\S", local.astryx_join_asc[k]))
      ]) && can(regex("-----BEGIN PRIVATE KEY-----", local.astryx_join_asc["PRIVATE_KEY"])))
      error_message = "astryx/testflight-asc-key must be a JSON object with ISSUER_ID, KEY_ID and PRIVATE_KEY (the .p8 file's whole text). Put it again (README, \"Astryx Swift: self-serve TestFlight invitations\")."
    }
  }
}

# workers.dev and preview URLs off: the route below is the Worker's only address.
resource "cloudflare_workers_script_subdomain" "astryx_join" {
  count            = local.astryx_join_ready ? 1 : 0
  account_id       = var.cloudflare_account_id
  script_name      = cloudflare_workers_script.astryx_join[0].script_name
  enabled          = false
  previews_enabled = false
}

resource "cloudflare_workers_route" "astryx_join" {
  count   = local.astryx_join_ready ? 1 : 0
  zone_id = var.dolphin_zone_id
  pattern = "${local.astryx_join_hostname}/*"
  script  = cloudflare_workers_script.astryx_join[0].script_name

  depends_on = [cloudflare_zero_trust_access_application.astryx_join, cloudflare_workers_script_subdomain.astryx_join]
}

output "astryx_join" {
  description = "Astryx Swift's self-serve TestFlight invitations (no credentials)"
  value = {
    url    = "https://${local.astryx_join_hostname}"
    status = local.astryx_join_ready ? "live: @meta.com addresses sign in with a one-time PIN" : "off: put the App Manager key in ${local.astryx_join_asc_secret}, then apply"
    secret = local.astryx_join_asc_secret
  }
}

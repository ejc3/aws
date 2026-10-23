# astryx-join.tf
#
# Self-serve TestFlight invitations for Astryx Swift: anyone with an @meta.com address scans a QR code, types the
# address, and is added as an external tester of the app (owner, 2026-10-23: "I want anyone with a meta.com email
# address to get a TestFlight invite").
#
#   astryx-join.dolphin-labs.dev -> Worker astryx-join (Turnstile, @meta.com, rate limits) -> App Store Connect
#
#   GET  /      a form: "Your @meta.com email", the Turnstile widget, one button
#   POST /join  creates the tester in the beta group "Meta (self-serve)" (or adds an existing tester to it)
#
# PUBLIC, with no sign-in (owner, 2026-10-23: "Turnstile please. Qr code will stay quiet, and I trust meta engineers").
# An Access gate would take a Zero Trust seat per person, about $7 a user a month past the Free plan's 50. An address
# is therefore not proven when it is typed; the invitation goes to that inbox and can only be accepted from it. What
# bounds abuse (scripts/astryx-join.mjs, tests in scripts/test-astryx-join.mjs): a same-origin form post; a Turnstile
# token verified server-side (siteverify with the widget's secret, the visitor's IP and an idempotency key) before
# anything else; an address at exactly meta.com; 5 requests per IP an hour and 500 a day overall (counters in the KV
# namespace below); then App Store Connect with a dedicated App Manager key.
#
# Terraform deploys this Worker's code too, unlike the site Workers (workers-stage.tf): it is one file with no build,
# and its secrets (the App Store Connect key, the Turnstile secret) are secret bindings that only an upload of the code
# can set (the pinned provider has no resource that sets a Worker secret on its own; claw-master-dashboard-site.tf).
# The shape of the host is Dolphin Maps' (dolphin-labs-zone.tf): a proxied placeholder record, a Workers route, and an
# Access application of its own for exactly this host with a bypass-everyone policy, which wins over the
# *.dolphin-labs.dev GitHub gate for this one name and no other.
#
# THE KEY: a dedicated App Store Connect API key with the App Manager role (the lowest role that manages testers), in
# Secrets Manager as astryx/testflight-asc-key (JSON: ISSUER_ID, KEY_ID, PRIVATE_KEY). Never the broader Admin key
# (games-worth-watching/app-store-connect, dev-ai-services.tf). The owner creates the secret with its value (README,
# "Astryx Swift: self-serve TestFlight invitations"); Terraform adopts it with an administration-only read policy.
# The Turnstile widget's site key and secret come from the widget resource: no step of the owner's.
#
# A SWITCH THAT IS THE SECRET. Nothing below exists until that secret does: an apply before it creates nothing, and the
# first apply after it builds the whole host. Until then astryx-join.dolphin-labs.dev falls to the * wildcard, behind
# the GitHub organization gate.
#
# ORDER, inside the one apply: the record (so the name stops reaching the previews tunnel), the Worker, its workers.dev
# and preview URLs off, the route, and only then the bypass: a bypass that came before the record would make the
# previews tunnel public under this name. Until the bypass exists the name stays behind the GitHub gate (fails closed).
#
# CREDENTIAL: the Turnstile widget needs Turnstile write on the provider token (cloudflare-tunnel-token), which no
# resource here used before; add it with one PUT of the token's definition, as Workers KV Storage Write was. It already
# has Workers Scripts, Workers KV Storage, Access, DNS and this zone's Workers Routes write.
#
# LIMITS: Apple allows 10,000 external testers per app (the page says so when Apple refuses on it). New build versions
# for external testers need Beta App Review before this group sees them.

locals {
  astryx_join_hostname = "astryx-join.dolphin-labs.dev"
  astryx_join_worker   = "astryx-join"
  astryx_join_module   = "${path.module}/scripts/astryx-join.mjs"
  # The App Manager key's secret (above). Its existence is the switch.
  astryx_join_asc_secret = "astryx/testflight-asc-key"
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

# The two counters (per IP an hour, all of them a day) the Worker keeps; its LIMITS binding. Holds no address: an IP
# hash and a count per key, each expiring.
resource "cloudflare_workers_kv_namespace" "astryx_join_limits" {
  count      = local.astryx_join_ready ? 1 : 0
  account_id = var.cloudflare_account_id
  title      = "astryx-join-limits"
}

# Managed mode: Cloudflare decides whether a visitor sees a checkbox. Only this hostname may use the widget.
resource "cloudflare_turnstile_widget" "astryx_join" {
  count      = local.astryx_join_ready ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Astryx Swift TestFlight invitations"
  domains    = [local.astryx_join_hostname]
  mode       = "managed"
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
    { name = "TURNSTILE_SITE_KEY", type = "plain_text", text = cloudflare_turnstile_widget.astryx_join[0].sitekey },
    { name = "TURNSTILE_SECRET", type = "secret_text", text = cloudflare_turnstile_widget.astryx_join[0].secret },
    { name = "LIMITS", type = "kv_namespace", namespace_id = cloudflare_workers_kv_namespace.astryx_join_limits[0].id },
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

  depends_on = [cloudflare_workers_script_subdomain.astryx_join]
}

# Public: this one host is exempt from the *.dolphin-labs.dev GitHub gate. The same pattern as dolphin_maps_public in
# dolphin-labs-zone.tf, its own policy and application for exactly this host.
resource "cloudflare_zero_trust_access_policy" "astryx_join_public" {
  count      = local.astryx_join_ready ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Astryx Swift TestFlight invitations (public)"
  decision   = "bypass"

  include = [{
    everyone = {}
  }]
}

resource "cloudflare_zero_trust_access_application" "astryx_join_public" {
  count      = local.astryx_join_ready ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "Astryx Swift TestFlight invitations (public)"
  type       = "self_hosted"
  domain     = local.astryx_join_hostname

  destinations = [
    { type = "public", uri = local.astryx_join_hostname },
  ]

  # Declared here, as on every application: an apply drops an attachment that is not.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.astryx_join_public[0].id
      precedence = 1
    },
  ]

  # After the record and the route: without the record the name falls to the * wildcard, the previews tunnel, which a
  # bypass would make public.
  depends_on = [cloudflare_dns_record.astryx_join, cloudflare_workers_route.astryx_join]
}

output "astryx_join" {
  description = "Astryx Swift's self-serve TestFlight invitations (no credentials)"
  value = {
    url    = "https://${local.astryx_join_hostname}"
    status = local.astryx_join_ready ? "live: public form, Turnstile, @meta.com only, 5 an hour per IP, 500 a day" : "off: put the App Manager key in ${local.astryx_join_asc_secret}, then apply"
    secret = local.astryx_join_asc_secret
  }
}

# family-stage-public.tf
#
# Family's privacy policy and support pages, public: App Store Connect needs both URLs reachable without a sign-in
# (the app's privacy policy URL and support URL, for TestFlight external testing and the App Store). The pages are
# part of Family's web Worker `family-stage` (dolphin-labs-hq/dolphin-labs apps/family/web, routes /privacy/ and
# /support/), which workers-stage.tf gates with a Worker-native Access application. That application follows every
# request to the Worker and cannot exempt a path, so the pages get a hostname of their own:
#
#   https://family.dolphin-labs.dev/privacy/
#   https://family.dolphin-labs.dev/support/
#
# On that host, two hostname applications ("public" destinations), which take precedence over the Worker-native one:
#   - family_stage_public_pages: BYPASS for /privacy*, /support* and /_next/static/* (the export's compiled CSS,
#     JavaScript and fonts, which the two pages load; the same reason family_wall_assets in cloudflare.tf is open);
#   - family_stage_public_host: the rest of the host, with the same three policies as the staging Workers
#     (workers-stage.tf), so nothing else on it opens, and it does not fall to the *.dolphin-labs.dev wildcard's
#     different GitHub-organization gate. Access picks the most specific path, so the bypass wins only on its paths.
# workers.dev and the version previews stay behind workers-stage.tf as before.
#
# The hostname is a Workers custom domain. Wrangler never routes it (the deploy token has no DNS or routes), and a
# deploy leaves it in place. It is created after both applications, so the host is never open beyond these paths.

locals {
  family_stage_public_hostname = "family.dolphin-labs.dev"
  family_stage_public_paths    = ["privacy*", "support*", "_next/static/*"]
}

resource "cloudflare_zero_trust_access_policy" "family_stage_public_pages" {
  account_id = var.cloudflare_account_id
  name       = "Family privacy and support pages (public)"
  decision   = "bypass"

  include = [{
    everyone = {}
  }]
}

resource "cloudflare_zero_trust_access_application" "family_stage_public_pages" {
  account_id = var.cloudflare_account_id
  name       = "Family privacy and support pages (public)"
  type       = "self_hosted"
  domain     = "${local.family_stage_public_hostname}/privacy*"

  destinations = [
    for path in local.family_stage_public_paths : { type = "public", uri = "${local.family_stage_public_hostname}/${path}" }
  ]

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.family_stage_public_pages.id
      precedence = 1
    },
  ]
}

resource "cloudflare_zero_trust_access_application" "family_stage_public_host" {
  account_id       = var.cloudflare_account_id
  name             = "Family staging Worker (custom host)"
  type             = "self_hosted"
  domain           = local.family_stage_public_hostname
  session_duration = "24h"

  destinations = [
    { type = "public", uri = local.family_stage_public_hostname },
  ]

  # The same as cloudflare_zero_trust_access_application.workers_stage. Declared here, never left out: an
  # application with no policy attached lets every request through.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.cc_games_allowed.id
      precedence = 1
    },
    {
      id         = cloudflare_zero_trust_access_policy.cc_games_service.id
      precedence = 2
    },
    {
      id         = cloudflare_zero_trust_access_policy.workers_stage_agents.id
      precedence = 3
    },
  ]
}

resource "cloudflare_workers_custom_domain" "family_stage_public" {
  account_id = var.cloudflare_account_id
  zone_id    = var.dolphin_zone_id
  hostname   = local.family_stage_public_hostname
  service    = "family-stage"
  depends_on = [
    cloudflare_zero_trust_access_application.family_stage_public_host,
    cloudflare_zero_trust_access_application.family_stage_public_pages,
    cloudflare_zero_trust_access_application.workers_stage,
  ]
}

output "family_public_urls" {
  description = "Family's public pages for App Store Connect (privacy policy URL, support URL); the rest of the host is gated"
  value = {
    privacy = "https://${local.family_stage_public_hostname}/privacy/"
    support = "https://${local.family_stage_public_hostname}/support/"
  }
}

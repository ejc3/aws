# This custom domain serves two standalone HTML documents for App Store Connect.
# Cloudflare Access public destination paths also match descendants. The dedicated
# Worker tests the hostname and exact path before selecting a document. It has
# neither a binding to the Family application nor an upstream origin.
locals {
  family_stage_public_hostname = "family.dolphin-labs.dev"
  # Generated in the private Family app by its export:public command. The two
  # digests are checked before Terraform includes either document in the Worker.
  family_stage_public_commit = "a816f5be389943bc9c86218d3cadfdde4e05b777"
  family_stage_public_digests = {
    privacy = "cbc1cb2fd7d8715fecac9b4b2b6d72a94463ae6969ea901352562b24b6e3043f"
    support = "c386a4b18369a96f4db30a6853bcc6f00a20b90ff283b3ae69e853419d8db893"
  }
  family_stage_public_html = {
    for name, page in data.github_repository_file.family_public_page : "/${name}/" => page.content
  }
}

data "github_repository_file" "family_public_page" {
  provider   = github.dolphin_labs_ci
  for_each   = local.family_stage_public_digests
  repository = local.dolphin_labs_ci_repository
  branch     = local.family_stage_public_commit
  file       = "apps/family/web/legal-release/${each.key}.html"

  lifecycle {
    postcondition {
      condition     = sha256(self.content) == each.value
      error_message = "The Family ${each.key} document differs from its reviewed release digest."
    }
  }
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
  domain     = "${local.family_stage_public_hostname}/privacy/"

  destinations = [
    for name in keys(local.family_stage_public_digests) : { type = "public", uri = "${local.family_stage_public_hostname}/${name}/" }
  ]

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.family_stage_public_pages.id
      precedence = 1
    },
  ]
  # The custom host starts behind Access. Verify these bytes with a service token
  # before applying the narrower public application.
  depends_on = [cloudflare_workers_custom_domain.family_stage_public]
}

resource "cloudflare_zero_trust_access_application" "family_stage_public_host" {
  account_id       = var.cloudflare_account_id
  name             = "Family public information (protected host)"
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

resource "cloudflare_workers_script" "family_public_pages" {
  account_id         = var.cloudflare_account_id
  script_name        = "family-public-pages"
  main_module        = "worker.mjs"
  compatibility_date = "2026-10-01"
  content = join("\n", [
    file("${path.module}/workers/family-public-pages/worker.mjs"),
    "export default familyPublicPages(${jsonencode(local.family_stage_public_hostname)}, ${jsonencode(local.family_stage_public_html)});",
  ])
}

resource "cloudflare_workers_script_subdomain" "family_public_pages" {
  account_id       = var.cloudflare_account_id
  script_name      = cloudflare_workers_script.family_public_pages.script_name
  enabled          = false
  previews_enabled = false
}

resource "cloudflare_workers_custom_domain" "family_stage_public" {
  account_id = var.cloudflare_account_id
  zone_id    = var.dolphin_zone_id
  hostname   = local.family_stage_public_hostname
  service    = cloudflare_workers_script.family_public_pages.script_name
  depends_on = [
    cloudflare_zero_trust_access_application.family_stage_public_host,
    cloudflare_workers_script_subdomain.family_public_pages,
  ]
}

output "family_public_urls" {
  description = "Family's public pages for App Store Connect (privacy policy URL, support URL)"
  value = {
    privacy = "https://${local.family_stage_public_hostname}/privacy/"
    support = "https://${local.family_stage_public_hostname}/support/"
  }
}

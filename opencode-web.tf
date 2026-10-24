# The owner's existing OpenCode 2 shared service, reachable from a phone's browser. The exact-host Access application
# overrides the family's wildcard application. Publish the container setup only after this gate exists.
locals {
  opencode_web = {
    hostname = "opencode.${local.nextjs_domain}"
    user     = one(local.nextjs_ct_owners)
    port     = 49374 # OpenCode 2's Linux shared-service port
  }
}

resource "cloudflare_zero_trust_access_policy" "opencode_web_owner" {
  account_id       = var.cloudflare_account_id
  name             = "OpenCode: owner only"
  decision         = "allow"
  session_duration = "24h"
  include          = [{ email = { email = local.browser_manager_owner } }]
}

resource "cloudflare_zero_trust_access_application" "opencode_web" {
  account_id       = var.cloudflare_account_id
  name             = "OpenCode web"
  domain           = local.opencode_web.hostname
  type             = "self_hosted"
  session_duration = "24h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.github[*].id,
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )
  policies = [{ id = cloudflare_zero_trust_access_policy.opencode_web_owner.id, precedence = 1 }]
}

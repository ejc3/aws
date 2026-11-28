# constellation-lights-hub-releases.tf
#
# Where Lights hubs and Raspberry Pi Imager download the hub's releases. constellation is private, so its
# GitHub releases are not reachable by a hub or by Imager. The lights-hub workflow's release, image and
# imager jobs (in the main-only `lights-hub-release` environment, constellation-lights-hub-release.tf) put
# every file here instead:
#
#   releases/lights-hub.json, lights-hub.json.sig    the signed manifest (Ed25519; the hub checks it)
#   releases/lights-hub-<arch>-linux-b<build>        the binaries the manifest names, by sha256 and size
#   images/…, lights-hub-imager.json                 the card images and Imager's list
#
# The Lights site Worker binds this private bucket (as HUB_RELEASES, in constellation's
# apps/lights-web/wrangler.jsonc) and serves it read-only under /api/v1/hub/releases/*, which already skips
# Access (lights-signin.tf). No public bucket domain: one host, one set of rules.
#
# Trust: a hub installs only what its compiled-in key signed, so a public host cannot hand it a bad build.
# A first card rests on HTTPS and the sha256 Imager's list carries, and only this bucket token (main-only
# environment) can write here.
locals {
  lights_hub_releases_bucket = "lights-hub-releases"
  lights_hub_releases_credentials = {
    R2_ACCOUNT_ID        = var.cloudflare_account_id
    R2_ACCESS_KEY_ID     = cloudflare_account_token.lights_hub_releases_r2.id
    R2_SECRET_ACCESS_KEY = sha256(cloudflare_account_token.lights_hub_releases_r2.value)
    R2_BUCKET            = cloudflare_r2_bucket.lights_hub_releases.name
  }
}

resource "cloudflare_r2_bucket" "lights_hub_releases" {
  account_id    = var.cloudflare_account_id
  name          = local.lights_hub_releases_bucket
  location      = "wnam"
  storage_class = "Standard"

  # Hubs in the field update from it; never destroyed by a plan.
  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_r2_managed_domain" "lights_hub_releases" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.lights_hub_releases.name
  enabled     = false
}

resource "cloudflare_account_token" "lights_hub_releases_r2" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "constellation-lights-hub-releases-r2"
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.r2_bucket_item_read }, { id = local.r2_bucket_item_write }]
    resources = jsonencode({
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.lights_hub_releases.name}" = "*"
    })
  }]

  # The credential is returned once. Do not silently replace it after state loss.
  lifecycle {
    prevent_destroy = true
  }
}

# Only the release, image and imager jobs, which run in the main-only environment, can write releases.
resource "github_actions_environment_secret" "lights_hub_releases" {
  provider = github.agents_md
  for_each = toset(["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"])

  repository      = "constellation"
  environment     = github_repository_environment.lights_hub_release.environment
  secret_name     = each.key
  plaintext_value = local.lights_hub_releases_credentials[each.key]

  depends_on = [github_repository_environment_deployment_policy.lights_hub_release_main]
}

# Not a credential: the public base the hub's update check and Imager's list use.
resource "github_actions_variable" "lights_hub_updates" {
  provider      = github.agents_md
  repository    = "constellation"
  variable_name = "LIGHTS_HUB_UPDATES"
  value         = "https://${local.lights_stage_hostname}/api/v1/hub/releases/"
}

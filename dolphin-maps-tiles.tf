# Nationwide road tiles for dolphin-labs-hq/dolphin-maps (ejc3/aws#747).
# Workers bind this private bucket as TILES in a later Maps change; this file does
# not deploy a Worker or change its routes. The uploader writes tiles/<id>.json.
# Only the bucket-scoped token enters protected state and the main-only GitHub
# environment. The existing token_minter reads its administration token ephemerally.

locals {
  dolphin_maps_tiles_bucket      = "dolphin-maps-tiles"
  dolphin_maps_tiles_repository  = "dolphin-maps"
  dolphin_maps_tiles_environment = "osm-tiles"
  dolphin_maps_tiles_credentials = {
    R2_ACCOUNT_ID        = var.cloudflare_account_id
    R2_ACCESS_KEY_ID     = cloudflare_account_token.dolphin_maps_tiles_r2.id
    R2_SECRET_ACCESS_KEY = sha256(cloudflare_account_token.dolphin_maps_tiles_r2.value)
    R2_BUCKET            = cloudflare_r2_bucket.dolphin_maps_tiles.name
  }
}

resource "cloudflare_r2_bucket" "dolphin_maps_tiles" {
  account_id    = var.cloudflare_account_id
  name          = local.dolphin_maps_tiles_bucket
  location      = "wnam"
  storage_class = "Standard"

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_r2_managed_domain" "dolphin_maps_tiles" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.dolphin_maps_tiles.name
  enabled     = false
}

resource "cloudflare_account_token" "dolphin_maps_tiles_r2" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "dolphin-maps-tiles-r2"
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.r2_bucket_item_read }, { id = local.r2_bucket_item_write }]
    resources = jsonencode({
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.dolphin_maps_tiles.name}" = "*"
    })
  }]

  # The credential is returned once. Do not silently replace it after state loss.
  lifecycle {
    prevent_destroy = true
  }
}

resource "github_repository_environment" "dolphin_maps_tiles" {
  provider          = github.dolphin_labs_ci
  repository        = local.dolphin_maps_tiles_repository
  environment       = local.dolphin_maps_tiles_environment
  can_admins_bypass = true
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "dolphin_maps_tiles_main" {
  provider       = github.dolphin_labs_ci
  repository     = local.dolphin_maps_tiles_repository
  environment    = github_repository_environment.dolphin_maps_tiles.environment
  branch_pattern = "main"
}

resource "github_actions_environment_secret" "dolphin_maps_tiles" {
  provider = github.dolphin_labs_ci
  for_each = toset(["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"])

  repository      = local.dolphin_maps_tiles_repository
  environment     = github_repository_environment.dolphin_maps_tiles.environment
  secret_name     = each.key
  plaintext_value = local.dolphin_maps_tiles_credentials[each.key]

  # Do not expose a credential while the new environment still admits all refs.
  depends_on = [github_repository_environment_deployment_policy.dolphin_maps_tiles_main]
}

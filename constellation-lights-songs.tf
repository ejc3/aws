# constellation-lights-songs.tf
#
# The Lights app's song library: public-domain compositions in recordings we generated (each song's
# licence basis is in constellation's song catalogue). The app streams them instead of bundling
# them (the owner's decision, 2026-11-28: "Let's stream from site. R2 bucket. Keep bundle tidy").
#
# The Lights site Worker binds this private bucket as SONGS (constellation's
# apps/lights-web/wrangler.jsonc) and serves it read-only under /api/v1/songs/*, which already skips
# Access (lights-signin.tf). Phones and hubs pre-cache what they play and check each file's sha256
# against the engine's catalogue. Only this bucket token, held in the main-only lights-songs environment, can write.
locals {
  lights_songs_bucket = "lights-songs"
  lights_songs_credentials = {
    R2_ACCOUNT_ID        = var.cloudflare_account_id
    R2_ACCESS_KEY_ID     = cloudflare_account_token.lights_songs_r2.id
    R2_SECRET_ACCESS_KEY = sha256(cloudflare_account_token.lights_songs_r2.value)
    R2_BUCKET            = cloudflare_r2_bucket.lights_songs.name
  }
}

resource "cloudflare_r2_bucket" "lights_songs" {
  account_id    = var.cloudflare_account_id
  name          = local.lights_songs_bucket
  location      = "wnam"
  storage_class = "Standard"

  # Phones and hubs stream from it; never destroyed by a plan.
  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_r2_managed_domain" "lights_songs" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.lights_songs.name
  enabled     = false
}

resource "cloudflare_account_token" "lights_songs_r2" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "constellation-lights-songs-r2"
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.r2_bucket_item_read }, { id = local.r2_bucket_item_write }]
    resources = jsonencode({
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.lights_songs.name}" = "*"
    })
  }]

  # The credential is returned once. Do not silently replace it after state loss.
  lifecycle {
    prevent_destroy = true
  }
}

# The song upload's own environment, main only, so its R2_* secrets never meet the hub releases'.
resource "github_repository_environment" "lights_songs" {
  provider          = github.agents_md
  repository        = "constellation"
  environment       = "lights-songs"
  can_admins_bypass = false
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "lights_songs_main" {
  provider       = github.agents_md
  repository     = "constellation"
  environment    = github_repository_environment.lights_songs.environment
  branch_pattern = "main"
}

# Only the song upload job, which runs in that environment, can write songs.
resource "github_actions_environment_secret" "lights_songs" {
  provider = github.agents_md
  for_each = toset(["R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"])

  repository      = "constellation"
  environment     = github_repository_environment.lights_songs.environment
  secret_name     = each.key
  plaintext_value = local.lights_songs_credentials[each.key]

  depends_on = [github_repository_environment_deployment_policy.lights_songs_main]
}

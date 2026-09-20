# Staging-only shared scores for Skyhook in CoderColton/colton-games; see ejc3/aws#147.
# Terraform owns storage and credentials, never Worker code/versions/bindings or SQL.
# Before the first apply, follow README's backend-versioning and existing-store checks.
# Cloudflare allows one Secrets Store per account; adopt it if one already exists.

locals {
  skyhook_leaderboard_stage_url = "https://${local.colton_games_worker_name}.${cloudflare_workers_subdomain.cc_games.subdomain}.workers.dev"
}

resource "cloudflare_d1_database" "skyhook_leaderboard_stage" {
  account_id            = var.cloudflare_account_id
  name                  = "skyhook-leaderboard-stage"
  primary_location_hint = "wnam"
  read_replication      = { mode = "disabled" }

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_secrets_store" "games" {
  account_id = var.cloudflare_account_id
  name       = "games"

  lifecycle {
    prevent_destroy = true
  }
}

resource "random_password" "skyhook_leaderboard_stage" {
  length  = 64
  special = false

  depends_on = [aws_s3_bucket_versioning.terraform_state]

  # Rotation invalidates existing runner cookies. Never rotate on a routine deploy.
  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_secrets_store_secret" "skyhook_leaderboard_stage" {
  account_id = var.cloudflare_account_id
  store_id   = cloudflare_secrets_store.games.id
  name       = "skyhook-leaderboard-stage"
  comment    = "Cookie-signing key for the staging-only Skyhook leaderboard"
  scopes     = ["workers"]
  value      = random_password.skyhook_leaderboard_stage.result

  lifecycle {
    prevent_destroy = true
  }
}

# This is a runtime Access credential, NOT a Cloudflare management API token or the
# signing key. Worker-native Access is Worker-wide (including its previews), not
# path-scoped. The Node proxy must separately allowlist only Skyhook API routes.
# Do not attach this policy to the shared *.cc-games.dev application.
resource "cloudflare_zero_trust_access_service_token" "skyhook_dev" {
  account_id = var.cloudflare_account_id
  name       = "skyhook-stage-dev-proxy"
  duration   = "8760h"

  depends_on = [aws_s3_bucket_versioning.terraform_state]
}

resource "cloudflare_zero_trust_access_policy" "skyhook_dev" {
  account_id       = var.cloudflare_account_id
  name             = "Skyhook staging dev proxy"
  decision         = "non_identity"
  session_duration = "24h"

  include = [{
    service_token = {
      token_id = cloudflare_zero_trust_access_service_token.skyhook_dev.id
    }
  }]
}

resource "aws_secretsmanager_secret" "skyhook_leaderboard_stage_proxy" {
  name                    = "skyhook-leaderboard-stage-proxy"
  description             = "Server-only Access credential for nextjs-dev to reach the staging Worker"
  recovery_window_in_days = 30
  tags                    = { Name = "skyhook-leaderboard-stage-proxy", Managed = "terraform" }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_secretsmanager_secret_version" "skyhook_leaderboard_stage_proxy" {
  secret_id = aws_secretsmanager_secret.skyhook_leaderboard_stage_proxy.id
  secret_string = jsonencode({
    upstream_url  = local.skyhook_leaderboard_stage_url
    client_id     = cloudflare_zero_trust_access_service_token.skyhook_dev.client_id
    client_secret = cloudflare_zero_trust_access_service_token.skyhook_dev.client_secret
  })
}

# The shared dev instance role can fetch this one runtime secret, not the signing
# key, state, other Access tokens, or Cloudflare control-plane credentials. This is
# an instance boundary, not Unix-user isolation on a host with shared sudo access.
resource "aws_iam_role_policy" "nextjs_skyhook_leaderboard" {
  count = var.enable_nextjs_dev ? 1 : 0
  name  = "nextjs-skyhook-leaderboard-stage"
  role  = aws_iam_role.nextjs_dev.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadSkyhookStagingProxyCredential"
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.skyhook_leaderboard_stage_proxy.arn
    }]
  })
}

output "skyhook_leaderboard_stage" {
  description = "Non-secret application handoff only; does not mean the Worker, schema, or Node proxy is deployed"
  value = {
    upstream_url     = local.skyhook_leaderboard_stage_url
    proxy_secret_arn = aws_secretsmanager_secret.skyhook_leaderboard_stage_proxy.arn
    wrangler = {
      d1_databases = [{
        binding        = "SKYHOOK_LEADERBOARD_DB"
        database_name  = cloudflare_d1_database.skyhook_leaderboard_stage.name
        database_id    = cloudflare_d1_database.skyhook_leaderboard_stage.id
        migrations_dir = "migrations/skyhook"
      }]
      secrets_store_secrets = [{
        binding     = "SKYHOOK_LEADERBOARD_SECRET"
        store_id    = cloudflare_secrets_store.games.id
        secret_name = cloudflare_secrets_store_secret.skyhook_leaderboard_stage.name
      }]
    }
  }
}

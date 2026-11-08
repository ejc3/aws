# dolphin-labs-live-logs.tf
#
# The R2 bucket for dolphin-labs-hq/dolphin-labs' live capture (docs/ENGINE.md "Live capture" there): one Durable
# Object a league in the dolphin-labs Workers polls every provider about each game in play and keeps each provider's
# raw answers in its own storage. Once a day is over, the object writes that day's logs here
# (live/<league>/<ET date>/<provider>.jsonl) and deletes them from its storage; without the bucket they stay in Durable
# Object storage, which grows about 12 MB a live game-hour (measured 2026-10-30), some 29 GB in a month of NBA, NHL,
# NFL and college games.
#
# The Workers bind it as LIVE_LOGS (web/wrangler.jsonc, top level and env.production), in a change that follows this
# apply: a binding to a bucket that does not exist fails the deploy. The deploy token (workers-deploy.tf: Workers
# Scripts Write) is expected to suffice for the binding, as it does for constellation-mcp-stage's binding to a KV
# namespace this file's neighbours create; the first deploy with the binding is the check.
#
# Nothing is public: no custom domain, no r2.dev URL. The objects are raw provider answers, which the repository keeps
# out of git.
resource "cloudflare_r2_bucket" "dolphin_labs_live_logs" {
  account_id = var.cloudflare_account_id
  name       = "dolphin-labs-live-logs"

  lifecycle {
    # The Workers' binding names this bucket; a replacement would leave it bound to a deleted one.
    prevent_destroy = true
  }
}

# ---------------------------------------------------------------------------------------------------------------------
# Read access from nextjs-dev (owner, 2026-11-08: "Yes, add a read-only R2 token"): the engine's parity work reads the
# logs there.
#
#   dolphin-labs/live-logs-r2   Secrets Manager (us-west-1) in the Dolphin account: the bucket's S3 API credentials as
#                               JSON {R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET, R2_ENDPOINT}.
#                               The container and its policy are dolphin-aws's (live-logs.tf there: administration
#                               only, which nextjs-dev reaches through dolphin-terraform). This file writes only its
#                               value, through aws.dolphin, as dolphin-labs-podcasts.tf does; until dolphin-aws has
#                               created the container nothing is written, and the next apply after it writes the value.
#
# The token is an account token with Workers R2 Storage Bucket Item Read on this one bucket only: it can list and read
# objects there, and cannot write or delete one, list buckets or reach any other bucket. The S3 pair is derived from it
# as in dolphin-labs-podcasts.tf (key id = token id, secret = SHA-256 of the value). Not IP-pinned: the dev boxes egress
# from several addresses. ROTATE with `terraform apply -replace=cloudflare_account_token.dolphin_labs_live_logs_read`;
# the same apply rewrites the secret.

locals {
  dolphin_labs_live_logs_read_secret = "dolphin-labs/live-logs-r2"
}

resource "cloudflare_account_token" "dolphin_labs_live_logs_read" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "dolphin-labs-live-logs-read"
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.r2_bucket_item_read }] # dolphin-labs-podcasts.tf
    resources = jsonencode({
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.dolphin_labs_live_logs.name}" = "*"
    })
  }]
}

data "aws_secretsmanager_secrets" "dolphin_labs_live_logs_read" {
  provider = aws.dolphin
  filter {
    name   = "name"
    values = [local.dolphin_labs_live_logs_read_secret]
  }
}

locals {
  # ListSecrets matches the name as a prefix; only the exact name turns this on.
  dolphin_labs_live_logs_read_secret_ready = contains(data.aws_secretsmanager_secrets.dolphin_labs_live_logs_read.names, local.dolphin_labs_live_logs_read_secret)
}

resource "aws_secretsmanager_secret_version" "dolphin_labs_live_logs_read" {
  provider  = aws.dolphin
  count     = local.dolphin_labs_live_logs_read_secret_ready ? 1 : 0
  secret_id = local.dolphin_labs_live_logs_read_secret
  secret_string = jsonencode({
    R2_ACCOUNT_ID        = var.cloudflare_account_id
    R2_ACCESS_KEY_ID     = cloudflare_account_token.dolphin_labs_live_logs_read.id
    R2_SECRET_ACCESS_KEY = sha256(cloudflare_account_token.dolphin_labs_live_logs_read.value)
    R2_BUCKET            = cloudflare_r2_bucket.dolphin_labs_live_logs.name
    R2_ENDPOINT          = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
  })
}

output "dolphin_labs_live_logs_read" {
  description = "dolphin-labs live logs: whether the read-only S3 credentials are written to dolphin-labs/live-logs-r2 in the Dolphin account (false until dolphin-aws creates that container)."
  value = {
    bucket         = cloudflare_r2_bucket.dolphin_labs_live_logs.name
    secret         = local.dolphin_labs_live_logs_read_secret
    secret_written = local.dolphin_labs_live_logs_read_secret_ready
  }
}

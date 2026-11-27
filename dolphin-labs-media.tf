# dolphin-labs-media.tf
#
# The R2 bucket where dolphin-labs keeps every game's media record for good, and the one credential that writes it.
# The owner, 2026-11-27, of a 250-item cap a game's photos had in the shared Redis: "Why would you limit to 250?!?!!?!".
#
#   dolphin-labs-media      R2 bucket in this Cloudflare account (var.cloudflare_account_id), the account that runs the
#                           Workers dolphin-labs-prod and dolphin-labs-stage. Private: no r2.dev address and no custom
#                           domain. It holds each game's media record (every photo and clip the news pipeline found,
#                           with the model's reading of it) and what the site serves of it, under
#                           <namespace>/media/records/<game>.json and <namespace>/media/served/<game>.json (dolphin-labs
#                           web/lib/news/media/record.ts). About 2 KB an item, a few hundred items a game: well under
#                           a GB a season. The Workers bind it as MEDIA (dolphin-labs web/wrangler.jsonc) and serve
#                           pages of it. NO expiry rule: the archive serves past games' photos, so objects are kept.
#
#   dolphin-labs/media-r2   Secrets Manager (us-west-1) in the Dolphin account: the bucket's S3 API credentials as JSON
#                           {R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET}, for the news X function
#                           there, which writes the records. The container, its readers and its policy are dolphin-aws's
#                           (news.tf there, modules/admin-secret). This file writes only its value, through aws.dolphin,
#                           as dolphin-labs-podcasts.tf does; until dolphin-aws has created the container nothing is
#                           written, and the next apply after it writes the value.
#
# The token is an account token with Workers R2 Storage Bucket Item Read and Write on this one bucket only, as the
# podcasts' is (dolphin-labs-podcasts.tf: the S3 key id is the token's id, the secret the SHA-256 of its value). ROTATE
# with `terraform apply -replace=cloudflare_account_token.dolphin_labs_media_r2`; the same apply rewrites the secret.

locals {
  dolphin_labs_media_bucket = "dolphin-labs-media"
  dolphin_labs_media_secret = "dolphin-labs/media-r2"
}

resource "cloudflare_r2_bucket" "dolphin_labs_media" {
  account_id = var.cloudflare_account_id
  name       = local.dolphin_labs_media_bucket
  location   = "wnam" # the writer runs in us-west-1; a hint honoured only at creation
}

resource "cloudflare_r2_managed_domain" "dolphin_labs_media" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.dolphin_labs_media.name
  enabled     = false
}

resource "cloudflare_account_token" "dolphin_labs_media_r2" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "dolphin-labs-media-r2"
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.r2_bucket_item_read }, { id = local.r2_bucket_item_write }]
    resources = jsonencode({
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.dolphin_labs_media.name}" = "*"
    })
  }]
}

data "aws_secretsmanager_secrets" "dolphin_labs_media_r2" {
  provider = aws.dolphin
  filter {
    name   = "name"
    values = [local.dolphin_labs_media_secret]
  }
}

locals {
  # ListSecrets matches the name as a prefix; only the exact name turns this on.
  dolphin_labs_media_secret_ready = contains(data.aws_secretsmanager_secrets.dolphin_labs_media_r2.names, local.dolphin_labs_media_secret)
}

resource "aws_secretsmanager_secret_version" "dolphin_labs_media_r2" {
  provider  = aws.dolphin
  count     = local.dolphin_labs_media_secret_ready ? 1 : 0
  secret_id = local.dolphin_labs_media_secret
  secret_string = jsonencode({
    R2_ACCOUNT_ID        = var.cloudflare_account_id
    R2_ACCESS_KEY_ID     = cloudflare_account_token.dolphin_labs_media_r2.id
    R2_SECRET_ACCESS_KEY = sha256(cloudflare_account_token.dolphin_labs_media_r2.value)
    R2_BUCKET            = cloudflare_r2_bucket.dolphin_labs_media.name
  })
}

output "dolphin_labs_media" {
  description = "dolphin-labs media: the R2 bucket, and whether its S3 credentials are written to dolphin-labs/media-r2 in the Dolphin account (false until dolphin-aws creates that container)."
  value = {
    bucket         = cloudflare_r2_bucket.dolphin_labs_media.name
    account_id     = var.cloudflare_account_id
    secret         = local.dolphin_labs_media_secret
    secret_written = local.dolphin_labs_media_secret_ready
  }
}

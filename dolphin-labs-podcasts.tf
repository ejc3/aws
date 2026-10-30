# dolphin-labs-podcasts.tf
#
# The R2 bucket for dolphin-labs' team podcasts (ejc3/aws#728) and the one credential that writes to it.
#
#   dolphin-labs-podcasts   R2 bucket in this Cloudflare account (var.cloudflare_account_id), the account that runs the
#                           Workers dolphin-labs-prod and dolphin-labs-stage. Private: no r2.dev address and no custom
#                           domain. It holds episode transcripts and dynamic-ad alignments, about 150 KB an episode,
#                           under production/transcripts/<id>.json and production/alignment/<id>.json. The production
#                           Worker binds it as PODCASTS (dolphin-labs web/wrangler.jsonc) and serves from it. Objects
#                           expire after 180 days; the pipeline keeps 120 days of episodes.
#
#   dolphin-labs/podcasts-r2   Secrets Manager (us-west-1) in the Dolphin account: the bucket's S3 API credentials as
#                           JSON {R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET}, for the podcast
#                           function there, which writes the objects. The container, its readers and its policy are
#                           dolphin-aws's (podcasts.tf there, modules/admin-secret: no version, administration-only
#                           writes, OrganizationAccountAccessRole among administration). This file writes only its value,
#                           through aws.dolphin, as dolphin-redis.tf writes its secrets there; until dolphin-aws has
#                           created the container nothing is written, and the next apply after it writes the value.
#
# The token is an account token with Workers R2 Storage Bucket Item Read and Write on this one bucket only: it can read
# and write objects there and cannot list, create or delete buckets or reach any other one. R2's S3 credentials are
# derived from it (Cloudflare, "R2 API tokens"): the access key id is the token's id, the secret access key the SHA-256 of
# its value. Not IP-pinned: the function runs in Lambda, with no stable address. The value is in state, as with the other
# tokens Terraform mints (workers-deploy.tf); state is administration only. ROTATE with
# `terraform apply -replace=cloudflare_account_token.dolphin_labs_podcasts_r2`; the same apply rewrites the secret.
#
# CREDENTIAL: the bucket, its lifecycle and its r2.dev setting need Workers R2 Storage Write (permission group
# bf7481a1826f439697cb59a20b22293e) on the provider token (cloudflare-tunnel-token), which no resource here used before;
# add it with one PUT of the token's definition, as Workers KV Storage Write was (AGENTS.md).

locals {
  dolphin_labs_podcasts_bucket = "dolphin-labs-podcasts"
  dolphin_labs_podcasts_secret = "dolphin-labs/podcasts-r2"

  # Permission group ids (GET /accounts/<id>/tokens/permission_groups), pinned by scripts/test-dolphin-labs-podcasts.py.
  r2_bucket_item_read  = "6a018a9f2fc74eb6b293b0c548f38b39"
  r2_bucket_item_write = "2efd5506f9c8494dacb1fa10a3e7d5b6"
}

resource "cloudflare_r2_bucket" "dolphin_labs_podcasts" {
  account_id = var.cloudflare_account_id
  name       = local.dolphin_labs_podcasts_bucket
  location   = "wnam" # the writer runs in us-west-1; a hint honoured only at creation
}

resource "cloudflare_r2_managed_domain" "dolphin_labs_podcasts" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.dolphin_labs_podcasts.name
  enabled     = false
}

resource "cloudflare_r2_bucket_lifecycle" "dolphin_labs_podcasts" {
  account_id  = var.cloudflare_account_id
  bucket_name = cloudflare_r2_bucket.dolphin_labs_podcasts.name
  rules = [{
    id         = "expire-after-180-days"
    enabled    = true
    conditions = { prefix = "" }
    delete_objects_transition = {
      condition = { type = "Age", max_age = 180 * 24 * 3600 }
    }
    # R2's default rule for a new bucket, which a managed lifecycle would otherwise remove.
    abort_multipart_uploads_transition = {
      condition = { type = "Age", max_age = 7 * 24 * 3600 }
    }
  }]
}

resource "cloudflare_account_token" "dolphin_labs_podcasts_r2" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "dolphin-labs-podcasts-r2"
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.r2_bucket_item_read }, { id = local.r2_bucket_item_write }]
    resources = jsonencode({
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${cloudflare_r2_bucket.dolphin_labs_podcasts.name}" = "*"
    })
  }]
}

data "aws_secretsmanager_secrets" "dolphin_labs_podcasts_r2" {
  provider = aws.dolphin
  filter {
    name   = "name"
    values = [local.dolphin_labs_podcasts_secret]
  }
}

locals {
  # ListSecrets matches the name as a prefix; only the exact name turns this on.
  dolphin_labs_podcasts_secret_ready = contains(data.aws_secretsmanager_secrets.dolphin_labs_podcasts_r2.names, local.dolphin_labs_podcasts_secret)
}

resource "aws_secretsmanager_secret_version" "dolphin_labs_podcasts_r2" {
  provider  = aws.dolphin
  count     = local.dolphin_labs_podcasts_secret_ready ? 1 : 0
  secret_id = local.dolphin_labs_podcasts_secret
  secret_string = jsonencode({
    R2_ACCOUNT_ID        = var.cloudflare_account_id
    R2_ACCESS_KEY_ID     = cloudflare_account_token.dolphin_labs_podcasts_r2.id
    R2_SECRET_ACCESS_KEY = sha256(cloudflare_account_token.dolphin_labs_podcasts_r2.value)
    R2_BUCKET            = cloudflare_r2_bucket.dolphin_labs_podcasts.name
  })
}

output "dolphin_labs_podcasts" {
  description = "dolphin-labs podcasts: the R2 bucket, and whether its S3 credentials are written to dolphin-labs/podcasts-r2 in the Dolphin account (false until dolphin-aws creates that container)."
  value = {
    bucket         = cloudflare_r2_bucket.dolphin_labs_podcasts.name
    account_id     = var.cloudflare_account_id
    secret         = local.dolphin_labs_podcasts_secret
    secret_written = local.dolphin_labs_podcasts_secret_ready
  }
}

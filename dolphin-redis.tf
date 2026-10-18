# dolphin-redis.tf: what this repository still owns of the Dolphin sites' Redis. The store itself runs on ECS Fargate in the
# Dolphin account, built by dolphin-labs-hq/dolphin-aws (redis.tf there): Valkey on EFS, serverless-redis-http for
# production (db 0) and staging (db 1), and cloudflared, in one task, since 2026-10-15 11:55 UTC. (Before that it ran on a
# t4g.micro built here, retired on 2026-10-16 once ECS had served a day cleanly.)
#
# What stays here, because it is Cloudflare's and Cloudflare credentials stay off the dev boxes (owner, 2026-10-12):
#   the tunnel and its ingress (kv.gamesworthwatching.com -> 127.0.0.1:8079, kv-stage -> :8078: the task's REST servers),
#   the two DNS records, the two REST tokens, and the two secrets in the Dolphin account the task and the consumers read.
# The REST token is the only credential, exactly as with Upstash. The hostnames are on gamesworthwatching.com and not
# dolphin-labs.dev because *.dolphin-labs.dev is behind an Access application for people. Nothing here trusts or reaches
# back into this account.
#
# Consumers take the URL and token from `dolphin-redis/clients` in the Dolphin account (one JSON per environment):
# the production Worker (through vercel-env/dolphin-labs/production, its secrets' source), the staging Worker
# (workers-stage/dolphin-labs), the news pass (dolphin-labs/news) and the dolphin-labs repository's KV_REST_API_* secrets.
# Rotating a token: AGENTS.md, "The Dolphin Redis".

locals {
  dolphin_redis = {
    prod  = { host = "kv.gamesworthwatching.com", port = 8079, db = 0 }
    stage = { host = "kv-stage.gamesworthwatching.com", port = 8078, db = 1 }
  }
}

resource "random_password" "dolphin_redis_token" {
  for_each = local.dolphin_redis
  length   = 48
  special  = false
}

# ----------------------------------------------------------------------------------------------------- Cloudflare side

resource "cloudflare_zero_trust_tunnel_cloudflared" "dolphin_redis" {
  account_id = var.cloudflare_account_id
  name       = "dolphin-redis"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "dolphin_redis" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.dolphin_redis.id
  config = {
    ingress = concat(
      [for env, r in local.dolphin_redis : { hostname = r.host, service = "http://127.0.0.1:${r.port}" }],
      [{ service = "http_status:404" }],
    )
  }
}

resource "cloudflare_dns_record" "dolphin_redis" {
  for_each   = local.dolphin_redis
  zone_id    = var.gww_zone_id
  name       = each.value.host
  type       = "CNAME"
  content    = "${cloudflare_zero_trust_tunnel_cloudflared.dolphin_redis.id}.cfargotunnel.com"
  proxied    = true
  ttl        = 1
  comment    = "Dolphin Redis REST (${each.key}); dolphin-redis.tf"
  depends_on = [cloudflare_zero_trust_tunnel_cloudflared_config.dolphin_redis]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "dolphin_redis" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.dolphin_redis.id
}

# ---------------------------------------------------------------------------------------------- Dolphin account side

# What the Redis task reads at start (dolphin-aws redis.tf): the connector token and both REST tokens. The name is kept from
# the instance it was made for; renaming would replace it.
resource "aws_secretsmanager_secret" "dolphin_redis_instance" {
  provider                = aws.dolphin
  name                    = "dolphin-redis/instance"
  description             = "Dolphin Redis instance: tunnel connector token and the two REST tokens (dolphin-redis.tf in ejc3/aws)"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "dolphin_redis_instance" {
  provider  = aws.dolphin
  secret_id = aws_secretsmanager_secret.dolphin_redis_instance.id
  secret_string = jsonencode({
    tunnel_token = data.cloudflare_zero_trust_tunnel_cloudflared_token.dolphin_redis.token
    prod_token   = random_password.dolphin_redis_token["prod"].result
    stage_token  = random_password.dolphin_redis_token["stage"].result
  })
}

# What the consumers are given, per environment (administration only; copied into each consumer's own container).
resource "aws_secretsmanager_secret" "dolphin_redis_clients" {
  provider                = aws.dolphin
  name                    = "dolphin-redis/clients"
  description             = "Dolphin Redis REST URL and token per environment, for the sites' KV_REST_API_URL / KV_REST_API_TOKEN (dolphin-redis.tf in ejc3/aws)"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "dolphin_redis_clients" {
  provider  = aws.dolphin
  secret_id = aws_secretsmanager_secret.dolphin_redis_clients.id
  secret_string = jsonencode({
    for env, r in local.dolphin_redis : env => {
      KV_REST_API_URL   = "https://${r.host}"
      KV_REST_API_TOKEN = random_password.dolphin_redis_token[env].result
    }
  })
}

output "dolphin_redis" {
  description = "Dolphin Redis REST hostnames (tokens: dolphin-redis/clients in the Dolphin account)"
  value       = { for env, r in local.dolphin_redis : env => "https://${r.host}" }
}

# dolphin-redis.tf: the Dolphin sites' Redis, in the Dolphin account (owner, 2026-10-15: "standup a cheap aws one inside
# the dolphin owned account").
#
# WHY. dolphin-labs' Redis (live alert state, push subscriptions, the news pass) was an Upstash store provisioned through
# the Vercel Marketplace on the dolphin-labs Vercel team, which is being retired; Vercel cannot transfer Upstash stores
# between teams, and deleting the team deletes them. This replaces both stores (production and staging) with one small
# instance the Dolphin account owns.
#
# SHAPE. One t4g.micro in the Dolphin account's default VPC, no inbound rule at all, running three containers:
#   valkey   Redis-compatible store, append-only file on the root volume (daily snapshots below)
#   srh      serverless-redis-http, Upstash's REST protocol in front of it, one per environment (production db 0 on
#            127.0.0.1:8079, staging db 1 on 127.0.0.1:8078), each with its own bearer token. The sites' code uses
#            @upstash/redis over HTTP (KV_REST_API_URL / KV_REST_API_TOKEN) and runs unchanged.
#   cloudflared  dials OUT to Cloudflare; the tunnel's ingress maps kv.gamesworthwatching.com and
#            kv-stage.gamesworthwatching.com to the two REST ports. Workers, the news Lambda and CI reach it there.
# The REST token is the only credential, exactly as with Upstash. The hostnames are on gamesworthwatching.com and not
# dolphin-labs.dev because *.dolphin-labs.dev is behind an Access application for people.
#
# Built from this repository with the Dolphin provider (as dolphin-security.tf is) because the tunnel and DNS are
# Cloudflare's and Cloudflare credentials stay off the dev boxes (owner, 2026-10-12). Nothing here trusts or reaches back
# into this account.
#
# Consumers take the URL and token from `dolphin-redis/clients` in the Dolphin account (one JSON per environment):
# the production Worker and Vercel's record (vercel-env/dolphin-labs/production), the staging Worker
# (workers-stage/dolphin-labs), the news pass (dolphin-labs/news) and the dolphin-labs repository's KV_REST_API_* secrets.

locals {
  # serverless-redis-http tag 0.0.10; the image is built from this commit (see local.dolphin_redis_images.srh).
  dolphin_redis_srh_commit = "794c5a372fa3997e08320250235f32e5a86338a3"
  dolphin_redis = {
    prod  = { host = "kv.gamesworthwatching.com", port = 8079, db = 0 }
    stage = { host = "kv-stage.gamesworthwatching.com", port = 8078, db = 1 }
  }
  dolphin_redis_images = {
    valkey = "valkey/valkey:8.1.10-alpine"
    # serverless-redis-http 0.0.10 built on the instance with scripts/dolphin-redis/srh-auth.patch (the build step in the boot
    # script below): the request-body limit goes from Plug's 8 MB default to 64 MB, because the news pass writes a hash
    # of about 8 MB in one request and the stock image refuses that with an empty 500 (2026-10-15 09:37; Upstash allowed
    # 10 MB), and the token is checked before the body is read, so only a caller holding it can make the server parse one.
    srh         = "srh-local:0.0.10-auth-body64mb"
    cloudflared = "cloudflare/cloudflared:2026.9.3"
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

# What the instance reads at boot: the connector token and both REST tokens. Readable by the instance and administration.
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

data "aws_vpc" "dolphin_default" {
  provider = aws.dolphin
  default  = true
}

data "aws_subnets" "dolphin_default" {
  provider = aws.dolphin
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.dolphin_default.id]
  }
  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

data "aws_ssm_parameter" "dolphin_redis_ami" {
  provider = aws.dolphin
  name     = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_security_group" "dolphin_redis" {
  provider    = aws.dolphin
  name        = "dolphin-redis"
  description = "Dolphin Redis: no inbound; outbound for the tunnel, images and AWS APIs"
  vpc_id      = data.aws_vpc.dolphin_default.id
  egress {
    description = "Cloudflare tunnel, container images, Secrets Manager and SSM"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "dolphin-redis" }
}

resource "aws_iam_role" "dolphin_redis" {
  provider = aws.dolphin
  name     = "dolphin-redis"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "dolphin_redis_ssm" {
  provider   = aws.dolphin
  role       = aws_iam_role.dolphin_redis.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "dolphin_redis_secret" {
  provider = aws.dolphin
  name     = "read-instance-secret"
  role     = aws_iam_role.dolphin_redis.id
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = aws_secretsmanager_secret.dolphin_redis_instance.arn }]
  })
}

resource "aws_iam_instance_profile" "dolphin_redis" {
  provider = aws.dolphin
  name     = "dolphin-redis"
  role     = aws_iam_role.dolphin_redis.name
}

locals {
  # Runs once, at first boot (cloud-init). The containers restart on their own after a reboot; to change a pin or a token,
  # re-run this script over SSM (it is idempotent: it re-reads the secret and recreates the containers). The data lives in
  # /var/lib/valkey on the root volume, so a reboot or a re-run loses nothing.
  dolphin_redis_user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail
    { set +x; } 2>/dev/null
    dnf install -y docker jq
    systemctl enable --now docker
    if [ ! -f /swapfile ]; then fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile; fi
    swapon /swapfile 2>/dev/null || true
    grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap defaults 0 0' >> /etc/fstab
    install -d -m 0700 /var/lib/valkey
    # Build the REST server (see local.dolphin_redis_images.srh). The Erlang VM needs more memory than a t4g.micro has
    # to compile it, so a temporary swap file is added for the build (the data store itself always runs with --oom-score-adj -1000).
    if ! docker image inspect ${local.dolphin_redis_images.srh} >/dev/null 2>&1; then
      dnf install -y git
      swapoff /swap-build 2>/dev/null || true; rm -f /swap-build
      fallocate -l 3G /swap-build && chmod 600 /swap-build && mkswap /swap-build >/dev/null && swapon /swap-build
      rm -rf /opt/srh-src && git clone -q https://github.com/hiett/serverless-redis-http /opt/srh-src
      git -C /opt/srh-src checkout -q ${local.dolphin_redis_srh_commit}
      echo '${base64encode(file("${path.module}/scripts/dolphin-redis/srh-auth.patch"))}' | base64 -d > /opt/srh.patch
      git -C /opt/srh-src apply /opt/srh.patch
      (cd /opt/srh-src && nice -n 19 docker build -q -t ${local.dolphin_redis_images.srh} .)
      swapoff /swap-build; rm -f /swap-build
    fi
    # The role's policy can take a minute to reach a new instance: retry the read for up to five minutes.
    for i in $(seq 1 30); do
      S=$(aws secretsmanager get-secret-value --region ${var.aws_region} --secret-id ${aws_secretsmanager_secret.dolphin_redis_instance.name} --query SecretString --output text 2>/dev/null) && break
      sleep 10
    done
    [ -n "$${S:-}" ] || { echo "dolphin-redis: cannot read ${aws_secretsmanager_secret.dolphin_redis_instance.name}" >&2; exit 1; }
    docker network inspect kv >/dev/null 2>&1 || docker network create kv
    docker rm -f valkey srh-prod srh-stage cloudflared 2>/dev/null || true
    docker run -d --name valkey --network kv --restart unless-stopped --oom-score-adj -1000 -v /var/lib/valkey:/data \
      ${local.dolphin_redis_images.valkey} valkey-server --appendonly yes --appendfsync everysec \
      --save 900 1 --maxmemory 384mb --maxmemory-policy noeviction
    %{for env, r in local.dolphin_redis~}
    printf 'SRH_MODE=env\nSRH_TOKEN=%s\nSRH_CONNECTION_STRING=redis://valkey:6379/${r.db}\n' "$(printf '%s' "$S" | jq -r .${env}_token)" > /run/srh-${env}.env
    chmod 600 /run/srh-${env}.env
    docker run -d --name srh-${env} --network kv --restart unless-stopped -p 127.0.0.1:${r.port}:80 \
      --env-file /run/srh-${env}.env ${local.dolphin_redis_images.srh}
    rm -f /run/srh-${env}.env
    %{endfor~}
    printf 'TUNNEL_TOKEN=%s\n' "$(printf '%s' "$S" | jq -r .tunnel_token)" > /run/cloudflared.env
    chmod 600 /run/cloudflared.env
    docker run -d --name cloudflared --network host --restart unless-stopped --env-file /run/cloudflared.env \
      ${local.dolphin_redis_images.cloudflared} tunnel --no-autoupdate run
    rm -f /run/cloudflared.env
    unset S
  EOT
}

resource "aws_instance" "dolphin_redis" {
  provider                    = aws.dolphin
  ami                         = data.aws_ssm_parameter.dolphin_redis_ami.value
  instance_type               = "t4g.micro"
  subnet_id                   = sort(data.aws_subnets.dolphin_default.ids)[0]
  vpc_security_group_ids      = [aws_security_group.dolphin_redis.id]
  iam_instance_profile        = aws_iam_instance_profile.dolphin_redis.name
  associate_public_ip_address = true
  user_data                   = local.dolphin_redis_user_data
  user_data_replace_on_change = false

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = 16
    encrypted             = true
    delete_on_termination = false
    tags                  = { Name = "dolphin-redis", Backup = "dolphin-redis" }
  }

  tags = { Name = "dolphin-redis" }

  lifecycle {
    # The data is on this instance's root volume.
    prevent_destroy = true
    ignore_changes  = [ami, user_data]
  }
  depends_on = [
    aws_secretsmanager_secret_version.dolphin_redis_instance,
    aws_iam_role_policy.dolphin_redis_secret,
    aws_iam_role_policy_attachment.dolphin_redis_ssm,
  ]
}

# Daily snapshots of the root volume (the append-only file), kept a week.
resource "aws_iam_role" "dolphin_redis_dlm" {
  provider = aws.dolphin
  name     = "dolphin-redis-dlm"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "dlm.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "dolphin_redis_dlm" {
  provider   = aws.dolphin
  role       = aws_iam_role.dolphin_redis_dlm.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSDataLifecycleManagerServiceRole"
}

resource "aws_dlm_lifecycle_policy" "dolphin_redis" {
  provider           = aws.dolphin
  description        = "Dolphin Redis root volume daily snapshots kept 7 days"
  execution_role_arn = aws_iam_role.dolphin_redis_dlm.arn
  state              = "ENABLED"
  policy_details {
    resource_types = ["VOLUME"]
    target_tags    = { Backup = "dolphin-redis" }
    schedule {
      name      = "daily"
      copy_tags = true
      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["11:00"]
      }
      retain_rule {
        count = 7
      }
    }
  }
}

output "dolphin_redis" {
  description = "Dolphin Redis REST hostnames (tokens: dolphin-redis/clients in the Dolphin account)"
  value       = { for env, r in local.dolphin_redis : env => "https://${r.host}" }
}

# SSH to EJ's MacBook Pro through Cloudflare: the tunnel and Access in front of its Remote Login,
# so a connection needs both Cloudflare Access (a second factor) and an SSH key. The runbook,
# both phases with their checks, is README "MacBook: Moving SSH behind Cloudflare Access".
#
#   macbook-ssh.cc-games.dev  -> the MacBook's Remote Login (ssh://localhost:22), keys only
#
# Who gets in through Access:
#   EJ                 -- the browser-manager owner policy (Google or one-time PIN)
#   fcvm-arm's agents  -- one service token, `macbook-ssh-access` in Secrets Manager
#
# Client (fcvm-arm): `ssh macbook` with ProxyCommand ~/.ssh/macbook-access-proxy, which reads
# the service token from Secrets Manager into TUNNEL_SERVICE_TOKEN_ID/_SECRET and execs
# `cloudflared access ssh --hostname %h`. Never pass the token as --id/--secret: arguments
# are visible to every user of the box in `ps`.
#
# Bring-up, phase 1 (this file): apply. Nothing changes for the MacBook yet: the reverse SSH tunnel
# (mac-reverse-tunnels.tf) still carries `ssh macbook`. Phase 2, from fcvm-arm over that tunnel:
# install the connector on the MacBook with the token below, check a connection through Cloudflare
# succeeds with the service token and is refused without it, then move `Host macbook` to it
# (mac-reverse-tunnels.tf), retire the reverse tunnel, and close the MacBook's port 22 to
# everything but itself (pf).
#
# Both credentials are readable by the dev_server role (fcvm-arm's, and also fcvm-x86's): the
# connector token so it can be installed on the MacBook over the existing route (no other path
# reaches the Mac), the service token because fcvm-arm is the client. The connector token runs
# this one tunnel and nothing else.

locals {
  macbook_ssh_hostname = "macbook-ssh.cc-games.dev"
  macbook_ssh_readers = [
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
    aws_iam_role.jumpbox_admin[0].arn,
    aws_iam_role.dev_server.arn,
  ]
}

resource "cloudflare_zero_trust_access_service_token" "macbook_ssh_fcvm" {
  account_id = var.cloudflare_account_id
  name       = "macbook-ssh-fcvm-arm"
  duration   = "8760h"
  lifecycle {
    create_before_destroy = true
  }
}

resource "cloudflare_zero_trust_access_policy" "macbook_ssh_fcvm" {
  account_id       = var.cloudflare_account_id
  name             = "MacBook SSH: fcvm-arm agents"
  decision         = "non_identity"
  session_duration = "12h"
  include = [{
    service_token = { token_id = cloudflare_zero_trust_access_service_token.macbook_ssh_fcvm.id }
  }]
}

resource "cloudflare_zero_trust_access_application" "macbook_ssh" {
  account_id       = var.cloudflare_account_id
  name             = "MacBook SSH"
  domain           = local.macbook_ssh_hostname
  type             = "self_hosted"
  session_duration = "12h"
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )

  # The policy attachment MUST be declared here; see cc_games_dev in cloudflare.tf.
  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.browser_manager_owner.id
      precedence = 1
    },
    {
      id         = cloudflare_zero_trust_access_policy.macbook_ssh_fcvm.id
      precedence = 2
    },
  ]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "macbook_ssh" {
  account_id = var.cloudflare_account_id
  name       = "macbook-ssh"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "macbook_ssh" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.macbook_ssh.id
  config = {
    ingress = [
      {
        hostname = local.macbook_ssh_hostname
        service  = "ssh://localhost:22"
        # The connector itself refuses a request without a valid Access token for this application.
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.macbook_ssh.aud]
          }
        }
      },
      { service = "http_status:404" },
    ]
  }
}

# Overrides the *.cc-games.dev wildcard (which points at the nextjs-dev tunnel).
resource "cloudflare_dns_record" "macbook_ssh" {
  zone_id = var.cc_games_zone_id
  name    = local.macbook_ssh_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.macbook_ssh.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "MacBook SSH; Access-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.macbook_ssh,
    cloudflare_zero_trust_tunnel_cloudflared_config.macbook_ssh,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "macbook_ssh" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.macbook_ssh.id
}

resource "aws_secretsmanager_secret" "macbook_ssh_tunnel" {
  name                    = "macbook-ssh-tunnel-token"
  description             = "Connector token for the MacBook SSH tunnel only (installed on the MacBook as a LaunchAgent)"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = "macbook-ssh-tunnel-token" }
}

resource "aws_secretsmanager_secret_version" "macbook_ssh_tunnel" {
  secret_id     = aws_secretsmanager_secret.macbook_ssh_tunnel.id
  secret_string = data.cloudflare_zero_trust_tunnel_cloudflared_token.macbook_ssh.token
}

resource "aws_secretsmanager_secret" "macbook_ssh_access" {
  name                    = "macbook-ssh-access"
  description             = "Cloudflare Access service token for fcvm-arm's SSH to the MacBook (JSON: client_id, client_secret, hostname)"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = "macbook-ssh-access" }
}

resource "aws_secretsmanager_secret_version" "macbook_ssh_access" {
  secret_id = aws_secretsmanager_secret.macbook_ssh_access.id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.macbook_ssh_fcvm.client_id
    client_secret = cloudflare_zero_trust_access_service_token.macbook_ssh_fcvm.client_secret
    hostname      = local.macbook_ssh_hostname
  })
}

resource "aws_iam_role_policy" "dev_server_macbook_ssh" {
  # Only while fcvm-arm exists: the grant names its instance.
  count = var.enable_firecracker_instance ? 1 : 0
  name  = "read-macbook-ssh"
  role  = aws_iam_role.dev_server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = [aws_secretsmanager_secret.macbook_ssh_tunnel.arn, aws_secretsmanager_secret.macbook_ssh_access.arn]
      # dev_server is fcvm-x86's role too: only credentials issued to fcvm-arm's own instance may read these.
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn } }
    }]
  })
}

resource "aws_secretsmanager_secret_policy" "macbook_ssh" {
  for_each = {
    tunnel = aws_secretsmanager_secret.macbook_ssh_tunnel.arn
    access = aws_secretsmanager_secret.macbook_ssh_access.arn
  }

  secret_arn = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndFcvmArmCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = each.value
      Condition = {
        ArnNotEquals = { "aws:PrincipalArn" = local.macbook_ssh_readers }
      }
    }]
  })
}

output "macbook_ssh" {
  description = "MacBook SSH through Cloudflare (no credentials)"
  value = {
    hostname           = local.macbook_ssh_hostname
    tunnel_secret_name = aws_secretsmanager_secret.macbook_ssh_tunnel.name
    access_secret_name = aws_secretsmanager_secret.macbook_ssh_access.name
  }
}

# SSH to EJ's MacBook Pro through Cloudflare: the tunnel and Access in front of the Mac's
# desktop-session sshd, so a connection needs both Cloudflare Access (a second factor) and an SSH
# key. The runbook is README "MacBook: Moving SSH behind Cloudflare Access".
#
#   macbook-ssh.cc-games.dev  -> the Mac's desktop-session sshd (ssh://127.0.0.1:22222), keys only
#
# That sshd (LaunchAgent com.ejcampbell.fcvm-macbook.sshd, ~/.ssh/fcvm-macbook/sshd_config,
# 127.0.0.1 only) runs in the logged-in desktop session and has Full Disk Access, so one session
# reaches the external drive, sends Apple Events and uses the login keychain. It is the one SSH
# endpoint on the Mac; macOS Remote Login (port 22) is no longer a route.
#
# Who gets in through Access:
#   EJ                 -- the browser-manager owner policy (Google or one-time PIN)
#   fcvm-arm's agents  -- one service token, `macbook-ssh-access` in Secrets Manager
#
# Client (fcvm-arm): `ssh mac` (mac-reverse-tunnels.tf) with ProxyCommand ~/.ssh/mac-access-proxy,
# which reads the service token from Secrets Manager into TUNNEL_SERVICE_TOKEN_ID/_SECRET and execs
# `cloudflared access ssh --hostname %h`. Never pass the token as --id/--secret: arguments are
# visible to every user of the box in `ps`.
#
# Both credentials are readable only by fcvm-arm's instance (the dev_server role, which fcvm-x86
# shares, with ec2:SourceInstanceARN pinned to fcvm-arm): the connector token so it can be
# installed on the MacBook over the existing route, the service token because fcvm-arm is the
# client. The connector token runs this one tunnel and nothing else.

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
        # The desktop-session sshd, not Remote Login (22): see the header. 127.0.0.1, its only ListenAddress.
        service = "ssh://127.0.0.1:22222"
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

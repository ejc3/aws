# SSH to Colton's Mac through Cloudflare, built exactly like EJ's MacBook (macbook-ssh.tf): a tunnel and Access in front
# of the Mac's Remote Login, so a connection needs both Cloudflare Access and an SSH key the Mac authorizes. No inbound
# port on the Mac or in AWS, and no reverse tunnel.
#
#   colton-mac-ssh.cc-games.dev  -> macOS Remote Login (ssh://127.0.0.1:22) on Colton's Mac
#
# Who gets in (owner, 2026-10-19: "Colton and I ... from the nextjs server, and just me from fcvm"): service tokens only,
# one per client account, each in its own secret, plus EJ in a browser through the browser-manager owner policy.
#
#   fcvm-arm, ubuntu       colton-mac-ssh-access-fcvm           (fcvm-arm's instance only)
#   nextjs-dev, colton     colton-mac-ssh-access-nextjs-colton  (nextjs-dev-role)
#   nextjs-dev, ejc3       colton-mac-ssh-access-nextjs-ejc3    (nextjs-dev-role)
#
# The nextjs secrets are readable by nextjs-dev-role, so by every account there with sudo: convenience, not isolation,
# as for ejc3's MacBook token. Each opens only the Access gate; the Mac still wants that account's own key
# (~/.ssh/to-colton-mac, made on the box by mac-ssh-client.tf as that account; its private half never leaves the box).
# The clients are mac-ssh-client.tf's installer with alias colton-mac.
#
# The connector token is ADMINISTRATION-ONLY (unlike the MacBook's, which fcvm-arm may read): nothing on a dev box can
# run a connector for this tunnel. scripts/mac-ssh-connector-setup.sh installs it on the Mac from a jumpbox, through
# ssh stdin. It runs this one tunnel and nothing else; every request still has to pass Access.

locals {
  colton_mac_ssh_hostname = "colton-mac-ssh.cc-games.dev"
  colton_mac_ssh_admins = [
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
    aws_iam_role.jumpbox_admin[0].arn,
  ]
  # One service token and one secret per client account. `reader` is the one role besides administration that may read it.
  colton_mac_ssh_clients = {
    fcvm          = { token_name = "colton-mac-ssh-fcvm-arm", secret = "colton-mac-ssh-access-fcvm", reader = aws_iam_role.dev_server.arn, who = "fcvm-arm ubuntu" }
    nextjs-colton = { token_name = "colton-mac-ssh-nextjs-colton", secret = "colton-mac-ssh-access-nextjs-colton", reader = aws_iam_role.nextjs_dev.arn, who = "nextjs colton" }
    nextjs-ejc3   = { token_name = "colton-mac-ssh-nextjs-ejc3", secret = "colton-mac-ssh-access-nextjs-ejc3", reader = aws_iam_role.nextjs_dev.arn, who = "nextjs ejc3" }
  }
}

resource "cloudflare_zero_trust_access_service_token" "colton_mac_ssh" {
  for_each   = local.colton_mac_ssh_clients
  account_id = var.cloudflare_account_id
  name       = each.value.token_name
  duration   = "8760h"
  lifecycle {
    create_before_destroy = true
  }
}

# The clients' way in: their service tokens, and no person.
resource "cloudflare_zero_trust_access_policy" "colton_mac_ssh_clients" {
  account_id       = var.cloudflare_account_id
  name             = "Colton's Mac SSH: clients' service tokens"
  decision         = "non_identity"
  session_duration = "12h"
  include          = [for k in sort(keys(local.colton_mac_ssh_clients)) : { service_token = { token_id = cloudflare_zero_trust_access_service_token.colton_mac_ssh[k].id } }]
}

resource "cloudflare_zero_trust_access_application" "colton_mac_ssh" {
  account_id       = var.cloudflare_account_id
  name             = "Colton's Mac SSH"
  domain           = local.colton_mac_ssh_hostname
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
      id         = cloudflare_zero_trust_access_policy.colton_mac_ssh_clients.id
      precedence = 2
    },
  ]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "colton_mac_ssh" {
  account_id = var.cloudflare_account_id
  name       = "colton-mac-ssh"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "colton_mac_ssh" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.colton_mac_ssh.id
  config = {
    ingress = [
      {
        hostname = local.colton_mac_ssh_hostname
        service  = "ssh://127.0.0.1:22"
        # The connector itself refuses a request without a valid Access token for this application.
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.colton_mac_ssh.aud]
          }
        }
      },
      { service = "http_status:404" },
    ]
  }
}

# Overrides the *.cc-games.dev wildcard (which points at the nextjs-dev tunnel).
resource "cloudflare_dns_record" "colton_mac_ssh" {
  zone_id = var.cc_games_zone_id
  name    = local.colton_mac_ssh_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.colton_mac_ssh.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Colton's Mac SSH; Access-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.colton_mac_ssh,
    cloudflare_zero_trust_tunnel_cloudflared_config.colton_mac_ssh,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "colton_mac_ssh" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.colton_mac_ssh.id
}

resource "aws_secretsmanager_secret" "colton_mac_ssh_tunnel" {
  name                    = "colton-mac-ssh-tunnel-token"
  description             = "Connector token for Colton's Mac SSH tunnel only (installed on that Mac by scripts/mac-ssh-connector-setup.sh); administration only"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = "colton-mac-ssh-tunnel-token" }
}

resource "aws_secretsmanager_secret_version" "colton_mac_ssh_tunnel" {
  secret_id     = aws_secretsmanager_secret.colton_mac_ssh_tunnel.id
  secret_string = data.cloudflare_zero_trust_tunnel_cloudflared_token.colton_mac_ssh.token
}

resource "aws_secretsmanager_secret_policy" "colton_mac_ssh_tunnel" {
  secret_arn = aws_secretsmanager_secret.colton_mac_ssh_tunnel.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.colton_mac_ssh_tunnel.arn
      Condition = {
        ArnNotEquals = { "aws:PrincipalArn" = local.colton_mac_ssh_admins }
      }
    }]
  })
}

resource "aws_secretsmanager_secret" "colton_mac_ssh_access" {
  for_each                = local.colton_mac_ssh_clients
  name                    = each.value.secret
  description             = "Cloudflare Access service token for ${each.value.who}'s SSH to Colton's Mac (JSON: client_id, client_secret, hostname)"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = each.value.secret }
}

resource "aws_secretsmanager_secret_version" "colton_mac_ssh_access" {
  for_each  = local.colton_mac_ssh_clients
  secret_id = aws_secretsmanager_secret.colton_mac_ssh_access[each.key].id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.colton_mac_ssh[each.key].client_id
    client_secret = cloudflare_zero_trust_access_service_token.colton_mac_ssh[each.key].client_secret
    hostname      = local.colton_mac_ssh_hostname
  })
}

resource "aws_secretsmanager_secret_policy" "colton_mac_ssh_access" {
  for_each   = local.colton_mac_ssh_clients
  secret_arn = aws_secretsmanager_secret.colton_mac_ssh_access[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndItsClientCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.colton_mac_ssh_access[each.key].arn
      Condition = {
        ArnNotEquals = { "aws:PrincipalArn" = concat(local.colton_mac_ssh_admins, [each.value.reader]) }
      }
    }]
  })
}

resource "aws_iam_role_policy" "dev_server_colton_mac_ssh" {
  # Only while fcvm-arm exists: the grant names its instance.
  count = var.enable_firecracker_instance ? 1 : 0
  name  = "read-colton-mac-ssh"
  role  = aws_iam_role.dev_server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.colton_mac_ssh_access["fcvm"].arn
      # dev_server is fcvm-x86's role too: only credentials issued to fcvm-arm's own instance may read it.
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn } }
    }]
  })
}

resource "aws_iam_role_policy" "nextjs_dev_colton_mac_ssh" {
  name = "read-colton-mac-ssh"
  role = aws_iam_role.nextjs_dev.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = "secretsmanager:GetSecretValue"
      Resource = [
        aws_secretsmanager_secret.colton_mac_ssh_access["nextjs-colton"].arn,
        aws_secretsmanager_secret.colton_mac_ssh_access["nextjs-ejc3"].arn,
      ]
    }]
  })
}

output "colton_mac_ssh" {
  description = "Colton's Mac SSH through Cloudflare (no credentials)"
  value = {
    hostname           = local.colton_mac_ssh_hostname
    tunnel_secret_name = aws_secretsmanager_secret.colton_mac_ssh_tunnel.name
    access_secrets     = { for k, s in aws_secretsmanager_secret.colton_mac_ssh_access : k => s.name }
  }
}

# SSH to EJ's Mac mini (the host itself, not its VMs) through Cloudflare, built like Colton's Mac (colton-mac-ssh.tf): a
# tunnel and Access in front of the mini's Remote Login, so a connection needs both Cloudflare Access and an SSH key the
# mini authorizes. No inbound port on the mini or in AWS, and no reverse tunnel.
#
#   mini-ssh.cc-games.dev  -> macOS Remote Login (ssh://127.0.0.1:22) on the mini, account ejcampbell
#
# Who gets in (owner, 2026-11-14: "I also want the non-vm to be available to ejc3 from AWS for me"): service tokens only,
# one per client account, each in its own secret, plus EJ in a browser through the browser-manager owner policy.
#
#   fcvm-arm, ubuntu       mini-ssh-access-fcvm           (fcvm-arm's instance only)
#   nextjs-ct, ejc3        mini-ssh-access-nextjs-ejc3    (nextjs-dev-role's ct-ejc3 session only)
#
# The mini also hosts two Tart macOS VMs, Steve's (`skevh`) and CI's (`ci`), each with its own tunnel (mac-vms.tf). The
# mini's Remote Login reaches the host account, and from there `tart exec` reaches inside either VM: holding `ssh mini`
# is holding both VMs too. The clients are mac-ssh-client.tf's installer with alias mini, and the connector is installed
# on the mini by scripts/mac-ssh-connector-setup.sh mini (README "The Mac mini").

locals {
  mini_ssh_hostname = "mini-ssh.cc-games.dev"
  mini_ssh_admins = [
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
    aws_iam_role.jumpbox_admin[0].arn,
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*",
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AdministratorAccess_*",
  ]
  # One service token per client. On Next.js the role is shared, so bind each grant to the account's IMDS session.
  mini_ssh_clients = {
    fcvm        = { token_name = "mini-ssh-fcvm-arm", secret = "mini-ssh-access-fcvm", reader = aws_iam_role.dev_server.arn, who = "fcvm-arm ubuntu", nextjs_user = null }
    nextjs-ejc3 = { token_name = "mini-ssh-nextjs-ejc3", secret = "mini-ssh-access-nextjs-ejc3", reader = aws_iam_role.nextjs_dev.arn, who = "nextjs ejc3", nextjs_user = "ejc3" }
  }
  nextjs_mac_ssh_secrets = concat(
    [for k, c in local.mini_ssh_clients : { arn = aws_secretsmanager_secret.mini_ssh_access[k].arn, user = c.nextjs_user } if c.nextjs_user != null],
    [for k, c in local.mac_vm_ssh_clients : { arn = aws_secretsmanager_secret.mac_vm_ssh_access[k].arn, user = c.nextjs_user } if c.nextjs_user != null],
  )
  # The host IMDS broker assigns ct-<user> from the container's gateway; container sessions cannot re-assume the role.
  nextjs_mac_ssh_userid_prefix = "${aws_iam_role.nextjs_dev.unique_id}:ct-"
  nextjs_mac_ssh_deny_wrong_session = {
    Sid       = "DenyOtherNextjsSessions"
    Effect    = "Deny"
    Principal = "*"
    Action    = "secretsmanager:GetSecretValue"
  }
}

resource "cloudflare_zero_trust_access_service_token" "mini_ssh" {
  for_each   = local.mini_ssh_clients
  account_id = var.cloudflare_account_id
  name       = each.value.token_name
  duration   = "8760h"
  lifecycle {
    create_before_destroy = true
  }
}

# The clients' way in: their service tokens, and no person.
resource "cloudflare_zero_trust_access_policy" "mini_ssh_clients" {
  account_id       = var.cloudflare_account_id
  name             = "Mac mini SSH: clients' service tokens"
  decision         = "non_identity"
  session_duration = "12h"
  include          = [for k in sort(keys(local.mini_ssh_clients)) : { service_token = { token_id = cloudflare_zero_trust_access_service_token.mini_ssh[k].id } }]
}

resource "cloudflare_zero_trust_access_application" "mini_ssh" {
  account_id       = var.cloudflare_account_id
  name             = "Mac mini SSH"
  domain           = local.mini_ssh_hostname
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
      id         = cloudflare_zero_trust_access_policy.mini_ssh_clients.id
      precedence = 2
    },
  ]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "mini_ssh" {
  account_id = var.cloudflare_account_id
  name       = "mini-ssh"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "mini_ssh" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.mini_ssh.id
  config = {
    ingress = [
      {
        hostname = local.mini_ssh_hostname
        service  = "ssh://127.0.0.1:22"
        # The connector itself refuses a request without a valid Access token for this application.
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.mini_ssh.aud]
          }
        }
      },
      { service = "http_status:404" },
    ]
  }
}

# Overrides the *.cc-games.dev wildcard (which points at the nextjs-dev tunnel).
resource "cloudflare_dns_record" "mini_ssh" {
  zone_id = var.cc_games_zone_id
  name    = local.mini_ssh_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.mini_ssh.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Mac mini SSH; Access-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.mini_ssh,
    cloudflare_zero_trust_tunnel_cloudflared_config.mini_ssh,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "mini_ssh" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.mini_ssh.id
}

# Administration only. The installer (scripts/mac-ssh-connector-setup.sh mini) reads it from a jumpbox, or from a Mac on
# the mini's LAN signed in to Identity Center AdministratorAccess (--lan), and sends it over ssh stdin.
resource "aws_secretsmanager_secret" "mini_ssh_tunnel" {
  name                    = "mini-ssh-tunnel-token"
  description             = "Connector token for the Mac mini's SSH tunnel only (installed on the mini by scripts/mac-ssh-connector-setup.sh); readable here by administration only"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = "mini-ssh-tunnel-token" }
}

resource "aws_secretsmanager_secret_version" "mini_ssh_tunnel" {
  secret_id     = aws_secretsmanager_secret.mini_ssh_tunnel.id
  secret_string = data.cloudflare_zero_trust_tunnel_cloudflared_token.mini_ssh.token
}

resource "aws_secretsmanager_secret_policy" "mini_ssh_tunnel" {
  secret_arn = aws_secretsmanager_secret.mini_ssh_tunnel.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.mini_ssh_tunnel.arn
      Condition = {
        ArnNotLike = { "aws:PrincipalArn" = local.mini_ssh_admins }
      }
    }]
  })
}

resource "aws_secretsmanager_secret" "mini_ssh_access" {
  for_each                = local.mini_ssh_clients
  name                    = each.value.secret
  description             = "Cloudflare Access service token for ${each.value.who}'s SSH to the Mac mini (JSON: client_id, client_secret, hostname)"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = each.value.secret }
}

resource "aws_secretsmanager_secret_version" "mini_ssh_access" {
  for_each  = local.mini_ssh_clients
  secret_id = aws_secretsmanager_secret.mini_ssh_access[each.key].id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.mini_ssh[each.key].client_id
    client_secret = cloudflare_zero_trust_access_service_token.mini_ssh[each.key].client_secret
    hostname      = local.mini_ssh_hostname
  })

  # Publish credentials only after the secret has its read boundary.
  depends_on = [aws_secretsmanager_secret_policy.mini_ssh_access]
}

resource "aws_secretsmanager_secret_policy" "mini_ssh_access" {
  for_each   = local.mini_ssh_clients
  secret_arn = aws_secretsmanager_secret.mini_ssh_access[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      [{
        Sid       = "OnlyAdministrationAndItsClientCanRead"
        Effect    = "Deny"
        Principal = "*"
        Action    = "secretsmanager:GetSecretValue"
        Resource  = aws_secretsmanager_secret.mini_ssh_access[each.key].arn
        Condition = {
          ArnNotLike = { "aws:PrincipalArn" = concat(local.mini_ssh_admins, [each.value.reader]) }
        }
      }],
      [for user in [each.value.nextjs_user] : merge(local.nextjs_mac_ssh_deny_wrong_session, {
        Resource = aws_secretsmanager_secret.mini_ssh_access[each.key].arn
        Condition = {
          ArnEquals       = { "aws:PrincipalArn" = aws_iam_role.nextjs_dev.arn }
          StringNotEquals = { "aws:userid" = "${local.nextjs_mac_ssh_userid_prefix}${user}" }
        }
      }) if user != null],
    )
  })
}

# fcvm-arm's read of its token is a statement in assume-constellation-terraform (constellation-account.tf): dev-server-role's
# inline policies are at IAM's 10,240-character total. Bind the Next.js role's new allows to their exact sessions;
# older sessions may not carry the newer secret denials from nextjs-ct-user-data.tf.
resource "aws_iam_role_policy" "nextjs_dev_mini_ssh" {
  name = "read-mini-ssh"
  role = aws_iam_role.nextjs_dev.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [for s in local.nextjs_mac_ssh_secrets : {
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = s.arn
      Condition = {
        StringEquals = { "aws:userid" = "${local.nextjs_mac_ssh_userid_prefix}${s.user}" }
      }
    }]
  })
}

output "mini_ssh" {
  description = "The Mac mini's SSH through Cloudflare (no credentials)"
  value = {
    hostname           = local.mini_ssh_hostname
    tunnel_secret_name = aws_secretsmanager_secret.mini_ssh_tunnel.name
    access_secrets     = { for k, s in aws_secretsmanager_secret.mini_ssh_access : k => s.name }
  }
}

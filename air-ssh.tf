# SSH to EJ's MacBook Air (M2), the owner's second Mac, built like Colin's Mac's (colin-mac-ssh.tf): a tunnel and Access in
# front of the Mac's Remote Login, so a connection needs both Cloudflare Access and an SSH key the Mac authorizes. No
# inbound port on the Mac or in AWS, and no standing reverse tunnel (one is used once, to put the connector on: the pcboot
# account, mac-ssh-client.tf, and scripts/mac-ssh-connector-setup.sh --bootstrap-port).
#
#   air-ssh.cc-games.dev  -> macOS Remote Login (ssh://127.0.0.1:22) on the Air
#
# Who gets in (owner, 2026-11-27: the owner's own Mac, as `ssh mac` is): service tokens only, one per client account, each
# in its own secret, plus EJ in a browser through the browser-manager owner policy.
#
#   fcvm-arm, ubuntu       air-ssh-access-fcvm            (fcvm-arm's instance only)
#   nextjs-ct, ejc3        air-ssh-access-nextjs-ejc3     (nextjs-dev-role; denied to every member's container by the
#                                                          owner's one pattern, *-ssh-access-nextjs-ejc3-*)
#
# Each token opens only the Access gate; the Mac still wants that account's own key (~/.ssh/to-air, made on the box by
# mac-ssh-client.tf as that account; its private half never leaves the box). The clients are mac-ssh-client.tf's installer
# with alias air (its Remote Login host key is pinned there, so the first apply installs them).
#
# The connector token's SECRET is administration-only, as every Mac's is, and scripts/mac-ssh-connector-setup.sh installs
# it on the Mac from a jumpbox, through ssh stdin. On the Mac it is a 0600 file of the account every client logs in as:
# what a client gains from it is a rogue connector for this one tunnel, which can take its traffic and break connections
# and nothing more (Access still gates every request, and every client pins the Mac's host key).

locals {
  air_ssh_hostname = "air-ssh.cc-games.dev"
  air_ssh_admins = [
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
    aws_iam_role.jumpbox_admin[0].arn,
  ]
  # One service token and one secret per client account. `reader` is the one role besides administration that may read it.
  air_ssh_clients = {
    fcvm        = { token_name = "air-ssh-fcvm-arm", secret = "air-ssh-access-fcvm", reader = aws_iam_role.dev_server.arn, who = "fcvm-arm ubuntu" }
    nextjs-ejc3 = { token_name = "air-ssh-nextjs-ejc3", secret = "air-ssh-access-nextjs-ejc3", reader = aws_iam_role.nextjs_dev.arn, who = "nextjs ejc3" }
  }
}

resource "cloudflare_zero_trust_access_service_token" "air_ssh" {
  for_each   = local.air_ssh_clients
  account_id = var.cloudflare_account_id
  name       = each.value.token_name
  duration   = "8760h"
  lifecycle {
    create_before_destroy = true
  }
}

# The clients' way in: their service tokens, and no person.
resource "cloudflare_zero_trust_access_policy" "air_ssh_clients" {
  account_id       = var.cloudflare_account_id
  name             = "EJ's MacBook Air SSH: clients' service tokens"
  decision         = "non_identity"
  session_duration = "12h"
  include          = [for k in sort(keys(local.air_ssh_clients)) : { service_token = { token_id = cloudflare_zero_trust_access_service_token.air_ssh[k].id } }]
}

resource "cloudflare_zero_trust_access_application" "air_ssh" {
  account_id       = var.cloudflare_account_id
  name             = "EJ's MacBook Air SSH"
  domain           = local.air_ssh_hostname
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
      id         = cloudflare_zero_trust_access_policy.air_ssh_clients.id
      precedence = 2
    },
  ]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "air_ssh" {
  account_id = var.cloudflare_account_id
  name       = "air-ssh"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "air_ssh" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.air_ssh.id
  config = {
    ingress = [
      {
        hostname = local.air_ssh_hostname
        service  = "ssh://127.0.0.1:22"
        # The connector itself refuses a request without a valid Access token for this application.
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.air_ssh.aud]
          }
        }
      },
      { service = "http_status:404" },
    ]
  }
}

# Overrides the *.cc-games.dev wildcard (which points at the nextjs-dev tunnel).
resource "cloudflare_dns_record" "air_ssh" {
  zone_id = var.cc_games_zone_id
  name    = local.air_ssh_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.air_ssh.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "EJ's MacBook Air SSH; Access-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.air_ssh,
    cloudflare_zero_trust_tunnel_cloudflared_config.air_ssh,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "air_ssh" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.air_ssh.id
}

resource "aws_secretsmanager_secret" "air_ssh_tunnel" {
  name                    = "air-ssh-tunnel-token"
  description             = "Connector token for EJ's MacBook Air SSH tunnel only (installed on that Mac by scripts/mac-ssh-connector-setup.sh); readable here by administration only"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = "air-ssh-tunnel-token" }
}

resource "aws_secretsmanager_secret_version" "air_ssh_tunnel" {
  secret_id     = aws_secretsmanager_secret.air_ssh_tunnel.id
  secret_string = data.cloudflare_zero_trust_tunnel_cloudflared_token.air_ssh.token
}

resource "aws_secretsmanager_secret_policy" "air_ssh_tunnel" {
  secret_arn = aws_secretsmanager_secret.air_ssh_tunnel.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.air_ssh_tunnel.arn
      Condition = {
        ArnNotEquals = { "aws:PrincipalArn" = local.air_ssh_admins }
      }
    }]
  })
}

resource "aws_secretsmanager_secret" "air_ssh_access" {
  for_each                = local.air_ssh_clients
  name                    = each.value.secret
  description             = "Cloudflare Access service token for ${each.value.who}'s SSH to EJ's MacBook Air (JSON: client_id, client_secret, hostname)"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = each.value.secret }
}

resource "aws_secretsmanager_secret_version" "air_ssh_access" {
  for_each  = local.air_ssh_clients
  secret_id = aws_secretsmanager_secret.air_ssh_access[each.key].id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.air_ssh[each.key].client_id
    client_secret = cloudflare_zero_trust_access_service_token.air_ssh[each.key].client_secret
    hostname      = local.air_ssh_hostname
  })
}

resource "aws_secretsmanager_secret_policy" "air_ssh_access" {
  for_each   = local.air_ssh_clients
  secret_arn = aws_secretsmanager_secret.air_ssh_access[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndItsClientCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.air_ssh_access[each.key].arn
      Condition = {
        ArnNotEquals = { "aws:PrincipalArn" = concat(local.air_ssh_admins, [each.value.reader]) }
      }
    }]
  })
}

# fcvm-arm's read of its token is a statement of assume-constellation-terraform (ReadTheMiniSshTokens in
# constellation-account.tf, pinned to fcvm-arm's instance), not a policy of its own: dev-server-role's inline policies are
# near IAM's 10,240-character total.

resource "aws_iam_role_policy" "nextjs_dev_air_ssh" {
  name = "read-air-ssh"
  role = aws_iam_role.nextjs_dev.id
  # Only once every member's session policy denies the owner's tokens (OwnerOnlySecrets, by name pattern): without this the
  # grant could land in the same apply before a member's policy update.
  depends_on = [aws_iam_policy.nextjs_ct_member]
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = [aws_secretsmanager_secret.air_ssh_access["nextjs-ejc3"].arn]
    }]
  })
}

output "air_ssh" {
  description = "EJ's MacBook Air SSH through Cloudflare (no credentials)"
  value = {
    hostname           = local.air_ssh_hostname
    tunnel_secret_name = aws_secretsmanager_secret.air_ssh_tunnel.name
    access_secrets     = { for k, s in aws_secretsmanager_secret.air_ssh_access : k => s.name }
  }
}

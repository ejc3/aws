# SSH to Colin's Windows PC through Cloudflare, built exactly like Colton's Mac (colton-mac-ssh.tf): a tunnel and Access in
# front of the PC's OpenSSH server, so a connection needs both Cloudflare Access and an SSH key the PC authorizes. No
# inbound port on the PC or in AWS, and no reverse tunnel (one is used once, to set the PC up: scripts/windows-pc-setup.sh).
#
#   colin-pc-ssh.cc-games.dev  -> OpenSSH Server (ssh://127.0.0.1:22) on Colin's PC
#
# Who gets in (owner, 2026-10-20): fcvm-arm's ubuntu and Colin's own account in its nextjs-ct container, by service
# token only, plus EJ in a browser through the browser-manager owner policy.
#
#   fcvm-arm, ubuntu       colin-pc-ssh-access-fcvm             (fcvm-arm's instance only)
#   nextjs-ct, colin       colin-pc-ssh-access-nextjs-colin     (nextjs-dev-role; denied to every other member's
#                                                                container, nextjs-ct-user-data.tf)
#
# The token opens only the Access gate; the PC still wants that account's own key (~/.ssh/to-colin-pc, made on the box
# by mac-ssh-client.tf as that account; its private half never leaves the box). The clients are mac-ssh-client.tf's
# installer with alias colin-pc.
#
# The connector token's SECRET is administration-only, as Colton's Mac's is, and scripts/windows-pc-setup.sh installs it
# on the PC from a jumpbox, through ssh stdin, into a file only SYSTEM and Administrators can read. The connector runs
# as a SYSTEM task at boot, so the PC is reachable before anyone logs in. A client that logs in as an administrator can
# still read that file: what that buys is a rogue connector for this one tunnel, which can take its traffic and break
# connections and nothing more: it cannot get in (Access still gates every request) and cannot pass for the PC (every
# client pins its OpenSSH host key).

locals {
  colin_pc_ssh_hostname = "colin-pc-ssh.cc-games.dev"
  colin_pc_ssh_admins = [
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
    aws_iam_role.jumpbox_admin[0].arn,
  ]
  # One service token and one secret per client account. `reader` is the one role besides administration that may read it.
  colin_pc_ssh_clients = {
    fcvm         = { token_name = "colin-pc-ssh-fcvm-arm", secret = "colin-pc-ssh-access-fcvm", reader = aws_iam_role.dev_server.arn, who = "fcvm-arm ubuntu" }
    nextjs-colin = { token_name = "colin-pc-ssh-nextjs-colin", secret = "colin-pc-ssh-access-nextjs-colin", reader = aws_iam_role.nextjs_dev.arn, who = "nextjs colin" }
  }
  # The PCs in mac-ssh-client.tf's map that serve Windows-MCP (scripts/windows-pc-setup.sh): a client account with one of
  # these aliases also gets Claude Code's entry for it (scripts/windows/windows-mcp-client-setup.sh).
  windows_mcp_pcs = ["colin-pc"]
}

resource "cloudflare_zero_trust_access_service_token" "colin_pc_ssh" {
  for_each   = local.colin_pc_ssh_clients
  account_id = var.cloudflare_account_id
  name       = each.value.token_name
  duration   = "8760h"
  lifecycle {
    create_before_destroy = true
  }
}

# The clients' way in: their service tokens, and no person.
resource "cloudflare_zero_trust_access_policy" "colin_pc_ssh_clients" {
  account_id       = var.cloudflare_account_id
  name             = "Colin's PC SSH: clients' service tokens"
  decision         = "non_identity"
  session_duration = "12h"
  include          = [for k in sort(keys(local.colin_pc_ssh_clients)) : { service_token = { token_id = cloudflare_zero_trust_access_service_token.colin_pc_ssh[k].id } }]
}

resource "cloudflare_zero_trust_access_application" "colin_pc_ssh" {
  account_id       = var.cloudflare_account_id
  name             = "Colin's PC SSH"
  domain           = local.colin_pc_ssh_hostname
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
      id         = cloudflare_zero_trust_access_policy.colin_pc_ssh_clients.id
      precedence = 2
    },
  ]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "colin_pc_ssh" {
  account_id = var.cloudflare_account_id
  name       = "colin-pc-ssh"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "colin_pc_ssh" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.colin_pc_ssh.id
  config = {
    ingress = [
      {
        hostname = local.colin_pc_ssh_hostname
        service  = "ssh://127.0.0.1:22"
        # The connector itself refuses a request without a valid Access token for this application.
        origin_request = {
          access = {
            required  = true
            team_name = "ejc3"
            aud_tag   = [cloudflare_zero_trust_access_application.colin_pc_ssh.aud]
          }
        }
      },
      { service = "http_status:404" },
    ]
  }
}

# Overrides the *.cc-games.dev wildcard (which points at the nextjs-dev tunnel).
resource "cloudflare_dns_record" "colin_pc_ssh" {
  zone_id = var.cc_games_zone_id
  name    = local.colin_pc_ssh_hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.colin_pc_ssh.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Colin's PC SSH; Access-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.colin_pc_ssh,
    cloudflare_zero_trust_tunnel_cloudflared_config.colin_pc_ssh,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "colin_pc_ssh" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.colin_pc_ssh.id
}

resource "aws_secretsmanager_secret" "colin_pc_ssh_tunnel" {
  name                    = "colin-pc-ssh-tunnel-token"
  description             = "Connector token for Colin's PC SSH tunnel only (installed on that PC by scripts/windows-pc-setup.sh); readable here by administration only"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = "colin-pc-ssh-tunnel-token" }
}

resource "aws_secretsmanager_secret_version" "colin_pc_ssh_tunnel" {
  secret_id     = aws_secretsmanager_secret.colin_pc_ssh_tunnel.id
  secret_string = data.cloudflare_zero_trust_tunnel_cloudflared_token.colin_pc_ssh.token
}

resource "aws_secretsmanager_secret_policy" "colin_pc_ssh_tunnel" {
  secret_arn = aws_secretsmanager_secret.colin_pc_ssh_tunnel.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.colin_pc_ssh_tunnel.arn
      Condition = {
        ArnNotEquals = { "aws:PrincipalArn" = local.colin_pc_ssh_admins }
      }
    }]
  })
}

resource "aws_secretsmanager_secret" "colin_pc_ssh_access" {
  for_each                = local.colin_pc_ssh_clients
  name                    = each.value.secret
  description             = "Cloudflare Access service token for ${each.value.who}'s SSH to Colin's PC (JSON: client_id, client_secret, hostname)"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = each.value.secret }
}

resource "aws_secretsmanager_secret_version" "colin_pc_ssh_access" {
  for_each  = local.colin_pc_ssh_clients
  secret_id = aws_secretsmanager_secret.colin_pc_ssh_access[each.key].id
  secret_string = jsonencode({
    client_id     = cloudflare_zero_trust_access_service_token.colin_pc_ssh[each.key].client_id
    client_secret = cloudflare_zero_trust_access_service_token.colin_pc_ssh[each.key].client_secret
    hostname      = local.colin_pc_ssh_hostname
  })
}

resource "aws_secretsmanager_secret_policy" "colin_pc_ssh_access" {
  for_each   = local.colin_pc_ssh_clients
  secret_arn = aws_secretsmanager_secret.colin_pc_ssh_access[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndItsClientCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.colin_pc_ssh_access[each.key].arn
      Condition = {
        ArnNotEquals = { "aws:PrincipalArn" = concat(local.colin_pc_ssh_admins, [each.value.reader]) }
      }
    }]
  })
}

resource "aws_iam_role_policy" "dev_server_colin_pc_ssh" {
  # Only while fcvm-arm exists: the grant names its instance.
  count = var.enable_firecracker_instance ? 1 : 0
  name  = "read-colin-pc-ssh"
  role  = aws_iam_role.dev_server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.colin_pc_ssh_access["fcvm"].arn
      # dev_server is fcvm-x86's role too: only credentials issued to fcvm-arm's own instance may read it.
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn } }
    }]
  })
}

resource "aws_iam_role_policy" "nextjs_dev_colin_pc_ssh" {
  name = "read-colin-pc-ssh"
  role = aws_iam_role.nextjs_dev.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.colin_pc_ssh_access["nextjs-colin"].arn
    }]
  })
}

output "colin_pc_ssh" {
  description = "Colin's PC SSH through Cloudflare (no credentials)"
  value = {
    hostname           = local.colin_pc_ssh_hostname
    tunnel_secret_name = aws_secretsmanager_secret.colin_pc_ssh_tunnel.name
    access_secrets     = { for k, s in aws_secretsmanager_secret.colin_pc_ssh_access : k => s.name }
  }
}

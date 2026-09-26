# macOS VMs on EJ's MacBook Pro, one per person, reachable through Cloudflare.
#
# Each VM is a Tart clone of ghcr.io/cirruslabs/macos-tahoe-base on the MacBook. It signs
# its own `admin` account in at boot, so it always has a desktop, and runs its OWN
# cloudflared connector for its own tunnel -- nothing depends on the VM's NAT address,
# and one VM's connector token cannot serve the other VM's hostnames.
#
# Per VM, two TCP routes:
#   <vm>-mac.cc-games.dev      -> the VM's Screen Sharing (localhost:5900)
#   <vm>-mac-ssh.cc-games.dev  -> the VM's sshd (localhost:22), SSH keys only
#
# Clients:
#   cloudflared access tcp --hostname <vm>-mac.cc-games.dev --url localhost:5901
#   open vnc://localhost:5901                       # sign in as admin
#   ssh -o ProxyCommand='cloudflared access ssh --hostname %h' admin@<vm>-mac-ssh.cc-games.dev
#
# Who gets in:
#   ejc3  -- EJ only (the browser-manager owner policy: Google or one-time PIN)
#   skevh -- GitHub members of dolphin-labs-hq, like *.dolphin-labs.dev
#
# The VM itself (Tart install, image, launchd agents, in-VM connector, SSH keys, the
# admin password below) is set up on the MacBook; see README "macOS VMs on the MacBook".

locals {
  mac_vms = merge(
    {
      ejc3 = {
        policy_id = cloudflare_zero_trust_access_policy.browser_manager_owner.id
        idps = concat(
          cloudflare_zero_trust_access_identity_provider.google[*].id,
          [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
        )
        auto_redirect = false
      }
    },
    # skevh rides on the dolphin GitHub identity provider, so it exists only with that zone.
    local.dolphin_enabled ? {
      skevh = {
        policy_id     = cloudflare_zero_trust_access_policy.mac_vm_dolphin[0].id
        idps          = [cloudflare_zero_trust_access_identity_provider.github[0].id]
        auto_redirect = true
      }
    } : {},
  )

  mac_vm_routes = merge([
    for vm, _ in local.mac_vms : {
      "${vm}-vnc" = { vm = vm, hostname = "${vm}-mac.cc-games.dev", service = "tcp://localhost:5900" }
      "${vm}-ssh" = { vm = vm, hostname = "${vm}-mac-ssh.cc-games.dev", service = "ssh://localhost:22" }
    }
  ]...)

  mac_vm_secret_readers = [
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
    aws_iam_role.jumpbox_admin[0].arn,
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*",
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AdministratorAccess_*",
  ]
}

resource "cloudflare_zero_trust_access_policy" "mac_vm_dolphin" {
  count = local.dolphin_enabled ? 1 : 0

  account_id       = var.cloudflare_account_id
  name             = "Mac VM (dolphin-labs-hq members)"
  decision         = "allow"
  session_duration = "24h"
  include = [
    {
      github_organization = {
        identity_provider_id = cloudflare_zero_trust_access_identity_provider.github[0].id
        name                 = var.dolphin_allowed_github_org
      }
    }
  ]
}

resource "cloudflare_zero_trust_access_application" "mac_vm" {
  for_each = local.mac_vm_routes

  account_id                = var.cloudflare_account_id
  name                      = "Mac VM ${each.key}"
  domain                    = each.value.hostname
  type                      = "self_hosted"
  session_duration          = "24h"
  allowed_idps              = local.mac_vms[each.value.vm].idps
  auto_redirect_to_identity = local.mac_vms[each.value.vm].auto_redirect

  # The policy attachment MUST be declared here; see cc_games_dev in cloudflare.tf.
  policies = [
    {
      id         = local.mac_vms[each.value.vm].policy_id
      precedence = 1
    },
  ]
}

resource "cloudflare_zero_trust_tunnel_cloudflared" "mac_vm" {
  for_each = local.mac_vms

  account_id = var.cloudflare_account_id
  name       = "mac-vm-${each.key}"
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "mac_vm" {
  for_each = local.mac_vms

  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.mac_vm[each.key].id
  config = {
    ingress = concat(
      [
        for route_key, r in local.mac_vm_routes : {
          hostname = r.hostname
          service  = r.service
          origin_request = {
            access = {
              required  = true
              team_name = "ejc3"
              aud_tag   = [cloudflare_zero_trust_access_application.mac_vm[route_key].aud]
            }
          }
        } if r.vm == each.key
      ],
      [{ service = "http_status:404" }],
    )
  }
  depends_on = [cloudflare_zero_trust_access_application.mac_vm]
}

# Overrides the *.cc-games.dev wildcard (which points at the nextjs-dev tunnel).
resource "cloudflare_dns_record" "mac_vm" {
  for_each = local.mac_vm_routes

  zone_id = var.cc_games_zone_id
  name    = each.value.hostname
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.mac_vm[each.value.vm].id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
  comment = "Mac VM ${each.key}; Access-gated"
  depends_on = [
    cloudflare_zero_trust_access_application.mac_vm,
    cloudflare_zero_trust_tunnel_cloudflared_config.mac_vm,
  ]
}

data "cloudflare_zero_trust_tunnel_cloudflared_token" "mac_vm" {
  for_each = local.mac_vms

  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.mac_vm[each.key].id
}

# Connector token only: it runs this one VM's tunnel and cannot administer the account.
resource "aws_secretsmanager_secret" "mac_vm_tunnel" {
  for_each = local.mac_vms

  name                    = "mac-vm-${each.key}-tunnel-token"
  description             = "Connector token for the ${each.key} Mac VM tunnel only"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = "mac-vm-${each.key}-tunnel-token" }
}

resource "aws_secretsmanager_secret_version" "mac_vm_tunnel" {
  for_each = local.mac_vms

  secret_id     = aws_secretsmanager_secret.mac_vm_tunnel[each.key].id
  secret_string = data.cloudflare_zero_trust_tunnel_cloudflared_token.mac_vm[each.key].token
}

# Replaces the image's well-known admin/admin. Alphanumeric so it survives any shell.
resource "random_password" "mac_vm_admin" {
  for_each = local.mac_vms

  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "mac_vm_admin" {
  for_each = local.mac_vms

  name                    = "mac-vm-${each.key}-admin-password"
  description             = "Password of the admin account inside the ${each.key} Mac VM (Screen Sharing sign-in)"
  recovery_window_in_days = 30
  tags                    = { Managed = "terraform", Name = "mac-vm-${each.key}-admin-password" }
}

resource "aws_secretsmanager_secret_version" "mac_vm_admin" {
  for_each = local.mac_vms

  secret_id     = aws_secretsmanager_secret.mac_vm_admin[each.key].id
  secret_string = random_password.mac_vm_admin[each.key].result
}

resource "aws_secretsmanager_secret_policy" "mac_vm" {
  for_each = {
    for pair in setproduct(keys(local.mac_vms), ["tunnel", "admin"]) :
    "${pair[0]}-${pair[1]}" => pair[1] == "tunnel" ? aws_secretsmanager_secret.mac_vm_tunnel[pair[0]].arn : aws_secretsmanager_secret.mac_vm_admin[pair[0]].arn
  }

  secret_arn = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = each.value
      Condition = {
        ArnNotLike = { "aws:PrincipalArn" = local.mac_vm_secret_readers }
      }
    }]
  })
}

output "mac_vms" {
  description = "Mac VM routes (no credentials)"
  value = {
    for vm, _ in local.mac_vms : vm => {
      vnc_hostname       = "${vm}-mac.cc-games.dev"
      ssh_hostname       = "${vm}-mac-ssh.cc-games.dev"
      tunnel_secret_name = aws_secretsmanager_secret.mac_vm_tunnel[vm].name
      admin_secret_name  = aws_secretsmanager_secret.mac_vm_admin[vm].name
    }
  }
}

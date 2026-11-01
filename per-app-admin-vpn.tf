# Server-side transport only. Intune assigns the iOS tunnel to Prompt separately;
# this creates no device-wide VPN profile and changes no existing SSH/ET aliases.
locals {
  admin_per_app_destinations = merge(
    var.enable_jumpbox ? { jumpbox = aws_eip.jumpbox[0].public_ip } : {},
    var.enable_jumpbox_2 ? { jumpbox2 = aws_eip.jumpbox_2[0].public_ip } : {},
    var.enable_firecracker_instance ? { arm = aws_eip.firecracker_dev_next[0].public_ip } : {},
    var.enable_x86_dev_instance ? { x86 = aws_eip.x86_dev[0].public_ip } : {},
    var.enable_nextjs_dev ? { nextjs = aws_eip.nextjs_dev[0].public_ip } : {},
  )
  admin_per_app_cidrs   = [for ip in values(local.admin_per_app_destinations) : "${ip}/32"]
  admin_per_app_traffic = "any(net.dst.ip[*] in {${join(" ", local.admin_per_app_cidrs)}})"
  admin_per_app_ssh_hostnames = {
    for key, ip in local.admin_per_app_destinations : key => "${key}-admin-ssh.cc-games.dev"
  }
}

# Inspect existing account settings without taking ownership of family settings.
data "cloudflare_zero_trust_device_settings" "admin_per_app" {
  account_id = var.cloudflare_account_id
}
data "cloudflare_zero_trust_gateway_policies" "admin_per_app" {
  account_id = var.cloudflare_account_id
  max_items  = 1000
}

resource "cloudflare_zero_trust_gateway_policy" "admin_per_app_allow" {
  account_id  = var.cloudflare_account_id
  name        = "Admin per-app SSH and Eternal Terminal"
  description = "Only the owner may use TCP 22/2022 to the five administration/development EIPs."
  action      = "allow"
  enabled     = true
  filters     = ["l4"]
  precedence  = 1000
  traffic     = "${local.admin_per_app_traffic} and net.protocol == \"tcp\" and net.dst.port in {22 2022}"
  identity    = "identity.email == ${jsonencode(local.browser_manager_owner)}"
}
resource "cloudflare_zero_trust_gateway_policy" "admin_per_app_block" {
  account_id  = var.cloudflare_account_id
  name        = "Deny all other access to admin per-app destinations"
  description = "Family enrollment alone grants no administration transport access; all other users and ports are blocked."
  action      = "block"
  enabled     = true
  filters     = ["l4"]
  precedence  = 1001
  traffic     = local.admin_per_app_traffic
}

# Mac uses cloudflared Access ProxyCommand per connection, without a Mac VPN.
# These exact applications override the existing family wildcard policy.
resource "cloudflare_zero_trust_access_policy" "admin_per_app_ssh" {
  account_id       = var.cloudflare_account_id
  name             = "Owner administration SSH only"
  decision         = "allow"
  session_duration = "24h"
  include          = [{ email = { email = local.browser_manager_owner } }]
}
resource "cloudflare_zero_trust_access_application" "admin_per_app_ssh" {
  for_each             = local.admin_per_app_ssh_hostnames
  account_id           = var.cloudflare_account_id
  name                 = "Administration SSH: ${each.key}"
  domain               = each.value
  type                 = "self_hosted"
  session_duration     = "24h"
  app_launcher_visible = false
  allowed_idps = concat(
    cloudflare_zero_trust_access_identity_provider.google[*].id,
    [cloudflare_zero_trust_access_identity_provider.onetimepin.id],
  )
  policies = [{ id = cloudflare_zero_trust_access_policy.admin_per_app_ssh.id, precedence = 1 }]
}

resource "random_bytes" "admin_per_app_tunnel" {
  length = 32
}
resource "cloudflare_zero_trust_tunnel_cloudflared" "admin_per_app" {
  account_id    = var.cloudflare_account_id
  name          = "admin-per-app"
  config_src    = "local"
  tunnel_secret = random_bytes.admin_per_app_tunnel.base64
  lifecycle { prevent_destroy = true }
}
resource "aws_secretsmanager_secret" "admin_per_app_tunnel" {
  name                    = "admin-per-app-tunnel"
  description             = "Dedicated administration connector credential; never granted to development hosts."
  recovery_window_in_days = 30
  lifecycle { prevent_destroy = true }
}
resource "aws_secretsmanager_secret_policy" "admin_per_app_tunnel" {
  secret_arn = aws_secretsmanager_secret.admin_per_app_tunnel.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Deny", Principal = "*", Action = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.admin_per_app_tunnel.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}
resource "aws_secretsmanager_secret_version" "admin_per_app_tunnel" {
  secret_id = aws_secretsmanager_secret.admin_per_app_tunnel.id
  secret_string = jsonencode({
    AccountTag   = var.cloudflare_account_id
    TunnelID     = cloudflare_zero_trust_tunnel_cloudflared.admin_per_app.id
    TunnelSecret = random_bytes.admin_per_app_tunnel.base64
  })
}

locals {
  admin_per_app_connector_setup = <<-SH
    set -eu
    set +x
    umask 077
    export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    work=$(mktemp -d)
    trap 'rm -rf "$work"' EXIT
    if ! id cloudflared-admin >/dev/null 2>&1; then
      useradd --system --user-group --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin cloudflared-admin
    fi
    test "$(id -u cloudflared-admin)" != 0
    install -d -o root -g cloudflared-admin -m 0750 /etc/cloudflared-admin
    install -d -o root -g root -m 0755 /usr/local/libexec
    candidate_binary=/usr/local/libexec/cloudflared-admin
    if ! echo '${local.cloudflared_sha256_linux_arm}  /usr/local/libexec/cloudflared-admin' | sha256sum -c --status; then
      curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' --tlsv1.2 --max-time 180 \
        'https://github.com/cloudflare/cloudflared/releases/download/${local.cloudflared_version}/cloudflared-linux-arm64' -o "$work/cloudflared"
      echo '${local.cloudflared_sha256_linux_arm}  '"$work/cloudflared" | sha256sum -c --status
      candidate_binary="$work/cloudflared"
    fi
    aws secretsmanager get-secret-value --region '${var.aws_region}' \
      --secret-id '${aws_secretsmanager_secret.admin_per_app_tunnel.arn}' --query SecretString --output text > "$work/credentials.json"
    python3 - "$work/credentials.json" <<'CREDS'
    import base64, json, sys
    value = json.load(open(sys.argv[1]))
    assert value['AccountTag'] == '${var.cloudflare_account_id}'
    assert value['TunnelID'] == '${cloudflare_zero_trust_tunnel_cloudflared.admin_per_app.id}'
    assert len(base64.b64decode(value['TunnelSecret'], validate=True)) == 32
    CREDS
    cat > "$work/config.yml" <<'CONFIG'
    ${jsonencode({
  tunnel           = cloudflare_zero_trust_tunnel_cloudflared.admin_per_app.id
  credentials-file = "/etc/cloudflared-admin/credentials.json"
  warp-routing     = { enabled = true }
  ingress = concat([
    for key, ip in local.admin_per_app_destinations : {
      hostname = local.admin_per_app_ssh_hostnames[key]
      service  = "ssh://${ip}:22"
    }
  ], [{ service = "http_status:404" }])
})}
    CONFIG
    cat > "$work/cloudflared-admin.service" <<'UNIT'
    [Unit]
    Description=Owner-only per-app administration transport
    Wants=network-online.target
    After=network-online.target

    [Service]
    Type=simple
    User=cloudflared-admin
    Group=cloudflared-admin
    Environment=HOME=/nonexistent
    UnsetEnvironment=AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_PROFILE AWS_SHARED_CREDENTIALS_FILE AWS_CONFIG_FILE AWS_CONTAINER_CREDENTIALS_RELATIVE_URI AWS_CONTAINER_CREDENTIALS_FULL_URI
    ExecStart=/usr/local/libexec/cloudflared-admin --no-autoupdate tunnel --config /etc/cloudflared-admin/config.yml --edge-ip-version 4 run
    Restart=on-failure
    RestartSec=5
    UMask=0077
    NoNewPrivileges=yes
    CapabilityBoundingSet=
    AmbientCapabilities=
    ProtectSystem=strict
    ProtectHome=yes
    PrivateTmp=yes
    PrivateDevices=yes
    ProtectKernelTunables=yes
    ProtectKernelModules=yes
    ProtectControlGroups=yes
    RestrictSUIDSGID=yes
    RestrictNamespaces=yes
    RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
    IPAddressDeny=169.254.169.254/32 fd00:ec2::254/128

    [Install]
    WantedBy=multi-user.target
    UNIT
    config_hash() {
      for path in "$@"; do test -r "$path" || return 1; done
      sha256sum "$@" | cut -d ' ' -f 1 | sha256sum | cut -d ' ' -f 1
    }
    proposed=$(config_hash "$work/credentials.json" "$work/config.yml" "$work/cloudflared-admin.service" "$candidate_binary")
    if systemctl is-active --quiet cloudflared-admin.service; then
      current=$(config_hash /etc/cloudflared-admin/credentials.json /etc/cloudflared-admin/config.yml /etc/systemd/system/cloudflared-admin.service /usr/local/libexec/cloudflared-admin) || current=missing
      if [ "$current" != "$proposed" ]; then
        echo 'ERROR: dedicated connector configuration changed; owner authorization is required before restarting its active unit.' >&2
        exit 1
      fi
    fi
    if [ "$candidate_binary" != /usr/local/libexec/cloudflared-admin ]; then
      install -o root -g root -m 0755 "$candidate_binary" /usr/local/libexec/cloudflared-admin
    fi
    install -o root -g cloudflared-admin -m 0640 "$work/credentials.json" /etc/cloudflared-admin/credentials.json
    install -o root -g cloudflared-admin -m 0640 "$work/config.yml" /etc/cloudflared-admin/config.yml
    /usr/local/libexec/cloudflared-admin tunnel --config /etc/cloudflared-admin/config.yml ingress validate
    install -o root -g root -m 0644 "$work/cloudflared-admin.service" /etc/systemd/system/cloudflared-admin.service
    systemd-analyze verify /etc/systemd/system/cloudflared-admin.service
    systemctl daemon-reload
    # Starts only the new dedicated unit; never restarts SSH, ET or an existing connector.
    systemctl enable --now cloudflared-admin.service
  SH
}

resource "aws_ssm_association" "admin_per_app_connector" {
  name                             = "AWS-RunShellScript"
  association_name                 = "admin-per-app-connector"
  wait_for_success_timeout_seconds = 300
  targets {
    key    = "tag:Name"
    values = ["jumpbox"]
  }
  parameters = {
    commands         = local.admin_per_app_connector_setup
    executionTimeout = "300"
  }
  depends_on = [
    aws_secretsmanager_secret_version.admin_per_app_tunnel,
    aws_secretsmanager_secret_policy.admin_per_app_tunnel,
    cloudflare_zero_trust_gateway_policy.admin_per_app_allow,
    cloudflare_zero_trust_gateway_policy.admin_per_app_block,
    cloudflare_zero_trust_access_application.admin_per_app_ssh,
  ]
}

resource "cloudflare_dns_record" "admin_per_app_ssh" {
  for_each = local.admin_per_app_ssh_hostnames
  zone_id  = var.cc_games_zone_id
  name     = each.value
  type     = "CNAME"
  content  = "${cloudflare_zero_trust_tunnel_cloudflared.admin_per_app.id}.cfargotunnel.com"
  proxied  = true
  ttl      = 1
  comment  = "Owner-only per-connection administration SSH"
  depends_on = [
    cloudflare_zero_trust_access_application.admin_per_app_ssh,
    aws_ssm_association.admin_per_app_connector,
  ]
}

output "admin_per_app_ssh_hostnames" {
  value = local.admin_per_app_ssh_hostnames
}

resource "cloudflare_zero_trust_tunnel_cloudflared_route" "admin_per_app" {
  for_each   = local.admin_per_app_destinations
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.admin_per_app.id
  network    = "${each.value}/32"
  comment    = "Owner per-app SSH/ET: ${each.key}"
  depends_on = [aws_ssm_association.admin_per_app_connector]

  lifecycle {
    precondition {
      condition     = data.cloudflare_zero_trust_device_settings.admin_per_app.gateway_proxy_enabled == true
      error_message = "Gateway TCP filtering must be enabled before advertising administration routes."
    }
    precondition {
      condition = length(data.cloudflare_zero_trust_gateway_policies.admin_per_app.result) < 1000 && alltrue([
        for rule in data.cloudflare_zero_trust_gateway_policies.admin_per_app.result :
        !rule.enabled || !contains(rule.filters, "l4") || rule.action != "allow" || rule.precedence > 1001 || rule.id == cloudflare_zero_trust_gateway_policy.admin_per_app_allow.id
      ])
      error_message = "An earlier broad Gateway allow rule could bypass the owner-only administration boundary."
    }
  }
}

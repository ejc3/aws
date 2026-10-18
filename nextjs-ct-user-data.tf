# nextjs-ct-user-data.tf
#
# Setup script of the containerized nextjs box (nextjs-ct.tf), published to S3 and applied at boot and by
# setup-sync whenever it changes. The host is an admin and routing layer that nobody works in:
#
#   ssh <user>@box      the host's sshd authenticates the person (their own authorized_keys) and its
#                       ForceCommand, nextjs-ct-enter, relays the session into their container
#   containers          one systemd-nspawn machine per account in local.nextjs_users (ejc3 included):
#                       their home bind-mounted, root inside, their own network namespace and AWS session
#   web                 `ndev` in a container publishes there; nextjs-ct-ndev-sync routes the hostname
#                       from the host's cloudflared to <container address>:<port>
#   AWS                 nextjs-ct-imds answers each container's 169.254.169.254 with a session of
#                       nextjs-dev-role narrowed by that person's session policy (below)
#
# Why systemd-nspawn: each container boots its own systemd, so the per-account units the old box ran
# (ndev@, claude-rc@, codex-rc@, codex-seed@, agent-session-sync@, the watchdog and update timers) run
# unchanged inside, as that account, with the container's own tmux server and lingering user manager. It
# ships with systemd (no daemon, no image registry, no snap), costs a few MB of RAM per container, and its
# user namespaces, ID-mapped bind mounts and veth links are the whole isolation story.
#
# Plain scripts live in scripts/nextjs-ct/ and are embedded base64-encoded, so they are never re-read as
# Terraform templates and the tests run the exact bytes the box runs. What only Terraform knows (accounts,
# zones, pins, session policies) goes into ONE file, /etc/nextjs-ct/config.json.

locals {
  # The accounts that existed on nextjs-dev keep their uid, so their homes copy over unchanged. An account
  # added to local.nextjs_users later gets the next free uid; its container address follows from it.
  nextjs_ct_uid_pins = { colton = 1001, connor = 1002, ejc3 = 1003, skevh = 1004, colin = 1005 }
  # The owner. Their container keeps the owner-only grants (the Dolphin apply role, the hop key, the
  # dev-box launchers) and is the only one that can reach the MacBook's reverse tunnel.
  nextjs_ct_owners = ["ejc3"]
  # Serving the live tunnels. FALSE until the cutover: with the same tunnel credentials on two boxes,
  # Cloudflare would split every hostname's traffic between them.
  nextjs_ct_serving = false

  # ---- per-container AWS sessions (nextjs-ct.tf explains the self-assume)
  # Denied to every container, the owner's too: what only the host does.
  nextjs_ct_deny_host_only = [
    {
      Sid    = "HostOnlyAgents"
      Effect = "Deny"
      Action = [
        "ssmmessages:*", "ec2messages:*", "ssm:UpdateInstance*", "ssm:*Association*", "ssm:Put*",
        "ssm:GetManifest", "ssm:GetDeployablePatchSnapshotForInstance", "ssm:*Document",
        "cloudwatch:PutMetricData", "ec2:CreateVolume", "ec2:AttachVolume", "ec2:DetachVolume", "ec2:DeleteVolume",
      ]
      Resource = "*"
    },
    {
      # The box's own role, assumed again WITHOUT a session policy, would shed every denial here. The owner
      # keeps sts:AssumeRole for other roles (the Dolphin apply role); nobody may assume this one.
      Sid      = "NoSessionWithoutThisPolicy"
      Effect   = "Deny"
      Action   = "sts:AssumeRole"
      Resource = local.nextjs_dev_role_arn
    },
    {
      # The tunnels' credentials (a container could otherwise run a connector for every hostname), the
      # setup scripts, and the session audit log.
      Sid    = "HostOnlyData"
      Effect = "Deny"
      Action = ["secretsmanager:GetSecretValue", "s3:GetObject", "s3:PutObject"]
      Resource = concat(
        [for s in data.aws_secretsmanager_secret.nextjs_connector : s.arn],
        ["${aws_s3_bucket.dev_scripts.arn}/*", "${aws_s3_bucket.security_config.arn}/sessions/*"],
      )
    },
  ]
  # Denied to everyone but the owner: other roles (the Dolphin apply role), the hop key and the Google
  # Docs token, and launching or driving the dev boxes (pbox, gbox, wbox), which reach their boxes with
  # the hop key anyway.
  nextjs_ct_deny_member = [
    {
      Sid      = "OwnerOnlyRoles"
      Effect   = "Deny"
      Action   = ["sts:AssumeRole", "iam:PassRole"]
      Resource = "*"
    },
    {
      Sid      = "OwnerOnlySecrets"
      Effect   = "Deny"
      Action   = "secretsmanager:GetSecretValue"
      Resource = concat([aws_secretsmanager_secret.dev_hop.arn], [for s in data.aws_secretsmanager_secret.nextjs_gdocs : s.arn])
    },
    {
      Sid    = "OwnerOnlyDevBoxes"
      Effect = "Deny"
      Action = [
        "ec2:RunInstances", "ec2:StartInstances", "ec2:StopInstances", "ec2:RebootInstances",
        "ec2:TerminateInstances", "ec2:CreateTags", "ec2:GetPasswordData", "ssm:SendCommand",
      ]
      Resource = "*"
    },
  ]
  nextjs_ct_allow_all = [{ Sid = "UpToTheRole", Effect = "Allow", Action = "*", Resource = "*" }]
  nextjs_ct_session_policies = {
    owner  = { Version = "2012-10-17", Statement = concat(local.nextjs_ct_allow_all, local.nextjs_ct_deny_host_only) }
    member = { Version = "2012-10-17", Statement = concat(local.nextjs_ct_allow_all, local.nextjs_ct_deny_host_only, local.nextjs_ct_deny_member) }
  }

  nextjs_ct_config = {
    role_arn         = local.nextjs_dev_role_arn
    serving          = local.nextjs_ct_serving
    zones            = local.nextjs_zone_tunnel
    ssh_hostnames    = local.dolphin_tunnel_id == null ? {} : { (local.dolphin_domain) = "ssh.${local.dolphin_domain}" }
    pinned_routes    = { for h, r in local.nextjs_pinned_routes : h => r if contains(keys(local.nextjs_zone_tunnel), r.zone) }
    session_policies = local.nextjs_ct_session_policies
    retired_keys     = local.nextjs_retired_user_keys
    claw_master      = { tag = local.claw_master_tag, sha256 = local.claw_master_sha256_aarch64, server = "${local.claw_master_server_ip}:${local.claw_master_server_port}" }
    tmux_scroll      = { tag = local.tmux_scroll_tag, sha256 = local.tmux_scroll_sha256_aarch64 }
    users = { for u in local.nextjs_users : u => {
      uid        = lookup(local.nextjs_ct_uid_pins, u, null)
      zone       = lookup(local.nextjs_user_zone, u, null)
      owner      = contains(local.nextjs_ct_owners, u)
      mac        = contains(local.nextjs_ct_owners, u)
      codex_seed = contains(local.nextjs_codex_seed_users, u)
      keys       = lookup(local.nextjs_user_keys, u, [])
      repos      = lookup(local.nextjs_user_repos, u, [])
    } }
  }

  # ejc3's MacBook aliases, inside ejc3's container only. The reverse tunnel's listener is on the host's
  # loopback; nextjs-ct-mac-tunnel relays it to that container's gateway, named nextjs-host there. The
  # private keys (~/.ssh/to-mac, ~/.ssh/to-macbook-desktop) are never installed by anything: they stay
  # where the owner put them.
  nextjs_ct_macbook_ssh = <<-MACBOOK
set -euo pipefail
U="$1"; S="/home/$U/.ssh"
as() { sudo -u "$U" -H "$@"; }
as install -d -m 700 "$S"
as tee "$S/config.d-macbook" >/dev/null <<'MACCFG'
# Generated by Terraform (nextjs-ct-user-data.tf). The MacBook through the reverse tunnel it keeps to this
# box; only this container can reach it.
# macbook: macOS Remote Login (full disk access; no Apple Events, no keychain).
# macbook-desktop: the Mac's desktop-session sshd on its own 127.0.0.1:22222, reached through macbook.
Host macbook
    HostName nextjs-host
    Port 2222
    User ejcampbell
    IdentityFile ~/.ssh/to-mac
    IdentitiesOnly yes
    HostKeyAlias ejs-macbook-pro-via-fcvm
    UserKnownHostsFile ~/.ssh/known_hosts.macbook
    StrictHostKeyChecking yes

Host macbook-desktop
    HostName 127.0.0.1
    Port 22222
    User ejcampbell
    ProxyJump macbook
    IdentityFile ~/.ssh/to-macbook-desktop
    IdentitiesOnly yes
    HostKeyAlias ejs-macbook-pro-via-fcvm
    UserKnownHostsFile ~/.ssh/known_hosts.macbook
    StrictHostKeyChecking yes
MACCFG
as tee "$S/known_hosts.macbook" >/dev/null <<'MACKEYS'
${local.mac_tunnel_host_keys}MACKEYS
as touch "$S/config"
if [ "$(head -n 1 "$S/config")" != "Include config.d-macbook" ]; then
  { echo "Include config.d-macbook"; grep -vxF "Include config.d-macbook" "$S/config" || true; } | as tee "$S/config.new" >/dev/null
  as mv -f "$S/config.new" "$S/config"
fi
chmod 600 "$S/config" "$S/config.d-macbook" "$S/known_hosts.macbook"
MACBOOK

  # Plain files: host tools, and the box's shared files every container mounts read-only.
  nextjs_ct_host_files = {
    "/usr/local/bin/setup-sync"           = "scripts/nextjs-ct/setup-sync"
    "/usr/local/sbin/nextjs-ct-health"    = "scripts/nextjs-ct/nextjs-ct-health"
    "/usr/local/sbin/nextjs-ct-enter"     = "scripts/nextjs-ct/host/nextjs-ct-enter"
    "/usr/local/sbin/nextjs-ct-imds"      = "scripts/nextjs-ct/host/nextjs-ct-imds"
    "/usr/local/sbin/nextjs-ct-ndev-sync" = "scripts/nextjs-ct/host/nextjs-ct-ndev-sync"
    "/usr/local/sbin/nextjs-ct-machine"   = "scripts/nextjs-ct/host/nextjs-ct-machine"
    "/usr/local/sbin/nextjs-ct-base"      = "scripts/nextjs-ct/host/nextjs-ct-base"
    "/usr/local/sbin/nextjs-ct-firewall"  = "scripts/nextjs-ct/host/nextjs-ct-firewall"
    "/usr/local/sbin/nextjs-ct-sshd"      = "scripts/nextjs-ct/host/nextjs-ct-sshd"
    "/usr/local/sbin/nextjs-ct-migrate"   = "scripts/nextjs-ct/host/nextjs-ct-migrate"
  }
  nextjs_ct_shared_files = merge(
    { for f in fileset(path.module, "scripts/nextjs-ct/ct/*") : "/var/lib/nextjs-ct/shared/ct/${basename(f)}" => f },
    { for f in fileset(path.module, "scripts/nextjs-ct/ct/units/*") : "/var/lib/nextjs-ct/shared/ct/units/${basename(f)}" => f },
  )
  # Rendered by Terraform from the snippets the other boxes run, so every box shares one definition.
  nextjs_ct_snippets = {
    "/var/lib/nextjs-ct/shared/config.json"                            = jsonencode(local.nextjs_ct_config)
    "/var/lib/nextjs-ct/shared/snippets/user-shell-env.sh"             = local.user_shell_env
    "/var/lib/nextjs-ct/shared/snippets/agent-session-sync-install.sh" = local.agent_session_sync_install
    "/var/lib/nextjs-ct/shared/snippets/codex-seed-install.sh"         = "${local.codex_seed_thread_install}\n${local.codex_restart_setup}"
    "/var/lib/nextjs-ct/shared/snippets/user-agents-seed-install.sh"   = local.user_agents_seed_install
    "/var/lib/nextjs-ct/shared/snippets/owner-tools.sh"                = "${local.pbox_setup}\n${local.gbox_setup}\n${local.wbox_setup}"
    "/var/lib/nextjs-ct/shared/snippets/macbook-ssh.sh"                = local.nextjs_ct_macbook_ssh
  }

  nextjs_ct_install_files = join("\n", concat(
    [for dest, src in local.nextjs_ct_host_files :
    "install -d \"$(dirname '${dest}')\" && printf '%s' '${base64encode(file("${path.module}/${src}"))}' | base64 -d > '${dest}.new' && chmod 755 '${dest}.new' && mv -f '${dest}.new' '${dest}'"],
    [for dest, src in local.nextjs_ct_shared_files :
    "install -d -m 755 \"$(dirname '${dest}')\" && printf '%s' '${base64encode(file("${path.module}/${src}"))}' | base64 -d > '${dest}.new' && chmod ${length(regexall("/units/|\\.rules$", src)) > 0 ? "644" : "755"} '${dest}.new' && mv -f '${dest}.new' '${dest}'"],
    [for dest, content in local.nextjs_ct_snippets :
    "install -d -m 755 \"$(dirname '${dest}')\" && printf '%s' '${base64encode(content)}' | base64 -d > '${dest}.new' && chmod 644 '${dest}.new' && mv -f '${dest}.new' '${dest}'"],
  ))

  nextjs_ct_user_data = <<-SCRIPT
#!/bin/bash
set -uxo pipefail
export DEBIAN_FRONTEND=noninteractive
# One setup at a time: the first boot's run and setup-sync's can otherwise overlap. setup-sync takes this
# lock itself and says so; a run started any other way waits for it.
if [ -z "$${NEXTJS_CT_SETUP_LOCK_HELD:-}" ]; then
  exec 9>/run/nextjs-ct-setup.lock
  flock -w 3600 9 || { echo "ERROR: another nextjs-ct setup still holds the lock"; exit 1; }
fi

# setup-sync first: if anything below fails, the timer is already there to retry the whole script.
# ---------------------------------------------------------------- files
# Written in place (the running containers mount this directory), then anything no longer published is
# removed, so a file deleted from scripts/nextjs-ct/ does not linger in the containers.
install -d -m 755 /var/lib/nextjs-ct /var/lib/nextjs-ct/shared /etc/nextjs-ct
${local.nextjs_ct_install_files}
install -m 600 /var/lib/nextjs-ct/shared/config.json /etc/nextjs-ct/config.json
cat > /var/lib/nextjs-ct/shared.manifest <<'MANIFEST'
${join("\n", concat(keys(local.nextjs_ct_shared_files), keys(local.nextjs_ct_snippets)))}
MANIFEST
find /var/lib/nextjs-ct/shared -type f | while IFS= read -r f; do
  grep -qxF -- "$f" /var/lib/nextjs-ct/shared.manifest || { echo "removing unpublished $f"; rm -f -- "$f"; }
done

# ---------------------------------------------------------------- setup-sync
cat > /etc/systemd/system/setup-sync.service <<'UNIT'
[Unit]
Description=Apply the published nextjs-ct setup script when it changes
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/setup-sync
UNIT
cat > /etc/systemd/system/setup-sync.timer <<'UNIT'
[Unit]
Description=Check for a newly published nextjs-ct setup script

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now setup-sync.timer >/dev/null 2>&1 || true

# ---------------------------------------------------------------- base packages
# Required: a failure stops the run, so setup-sync does not record a half-done setup and retries.
apt-get -o DPkg::Lock::Timeout=600 update || true
apt-get -o DPkg::Lock::Timeout=600 install -y curl git jq unzip python3 rsync systemd-container debootstrap nftables earlyoom \
  || { echo "ERROR: required packages did not install"; exit 1; }
if ! command -v aws >/dev/null 2>&1; then
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip \
    && (cd /tmp && unzip -qo awscliv2.zip && ./aws/install) \
    || { echo "ERROR: the AWS CLI did not install"; exit 1; }
  rm -rf /tmp/aws /tmp/awscliv2.zip
fi

# ---------------------------------------------------------------- ssm agent
# Ubuntu ships it as a snap; the IAM policy that lets it register is on nextjs-dev-role.
snap list amazon-ssm-agent >/dev/null 2>&1 \
  && systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service >/dev/null 2>&1 \
  || echo "WARNING: ssm agent not running"

# ---------------------------------------------------------------- cloudwatch agent
# mem_used_percent and swap_used_percent for the box's memory alarms (nextjs-alarms.tf), which point at this
# box from the cutover on. The role may publish to the CWAgent namespace only.
if ! systemctl is-active --quiet amazon-cloudwatch-agent 2>/dev/null; then
  CWA_TMP=$(mktemp /tmp/cwagent.XXXXXX.deb)
  if curl -fsSL --retry 3 "https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/arm64/latest/amazon-cloudwatch-agent.deb" -o "$CWA_TMP" \
     && dpkg -i "$CWA_TMP" >/dev/null; then
    mkdir -p /opt/aws/amazon-cloudwatch-agent/etc
    cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'CWACONF'
{
  "agent": { "metrics_collection_interval": 60 },
  "metrics": {
    "namespace": "CWAgent",
    "append_dimensions": { "InstanceId": "$${aws:InstanceId}" },
    "aggregation_dimensions": [["InstanceId"]],
    "metrics_collected": {
      "mem": { "measurement": ["mem_used_percent", "mem_available"], "metrics_collection_interval": 60 },
      "swap": { "measurement": ["swap_used_percent"], "metrics_collection_interval": 60 }
    }
  }
}
CWACONF
    /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 \
      -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json -s >/dev/null \
      || echo "WARNING: cloudwatch agent installed but did not start"
  else
    echo "WARNING: could not install the cloudwatch agent; memory stays invisible"
  fi
  rm -f "$CWA_TMP"
fi

# ---------------------------------------------------------------- memory
# A 4 GB swapfile absorbs compile spikes; earlyoom kills the biggest offender at 4% available memory so
# the box never pages itself unreachable (nextjs-dev, 2026-10-10). Each container is also capped at 75%
# of memory (systemd-nspawn@<user>.service), so one person's runaway dies inside their own container.
if [ -z "$(swapon --show=NAME --noheadings)" ]; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
cat > /etc/default/earlyoom <<'EARLYOOM'
# Managed by terraform (nextjs-ct-user-data.tf): do not edit by hand.
EARLYOOM_ARGS="-r 3600 -m 4,2 -s 100,100 -p --avoid '(^|/)(systemd|systemd-journal|systemd-nspawn|sshd|dbus-daemon|cloudflared|tmux|tmux-scroll|amazon-ssm-agen|ssm-agent-worke|snapd|earlyoom|amazon-cloudwat|nextjs-ct-imds)$' --prefer '^next-server|(^|/)(chrome|chromium|vitest)$'"
EARLYOOM
systemctl enable earlyoom.service >/dev/null 2>&1
systemctl restart earlyoom.service || echo "WARNING: earlyoom did not start"

# ---------------------------------------------------------------- kernel settings
# Forwarding for the containers' links (the firewall decides what is forwarded), and unprivileged user
# namespaces for Codex's bwrap sandbox inside the containers (Ubuntu restricts them through AppArmor).
cat > /etc/sysctl.d/60-nextjs-ct.conf <<'SYSCTL'
net.ipv4.ip_forward = 1
kernel.apparmor_restrict_unprivileged_userns = 0
SYSCTL
sysctl -q --system || true

# ---------------------------------------------------------------- cloudflared (pinned; started only when serving)
CF_VER="${local.cloudflared_version}"; CF_SHA="${local.cloudflared_sha256_linux_arm}"
if ! echo "$CF_SHA  /usr/local/bin/cloudflared" | sha256sum -c --quiet - 2>/dev/null; then
  curl -fsSL --retry 3 "https://github.com/cloudflare/cloudflared/releases/download/$CF_VER/cloudflared-linux-arm64" -o /tmp/cloudflared.new \
    && echo "$CF_SHA  /tmp/cloudflared.new" | sha256sum -c --quiet - \
    && install -m 755 /tmp/cloudflared.new /usr/local/bin/cloudflared \
    || { echo "ERROR: pinned cloudflared did not download or verify"; exit 1; }
  rm -f /tmp/cloudflared.new
fi
install -d -m 755 /etc/cloudflared
set +x
CREDS=$(aws secretsmanager get-secret-value --secret-id cloudflare-tunnel-credentials --region us-west-1 --query SecretString --output text) \
  && (umask 077; printf '%s' "$CREDS" > /etc/cloudflared/creds-${local.nextjs_domain}.json) \
  || echo "WARNING: could not read the cc-games tunnel credentials"
%{if local.dolphin_tunnel_id != null~}
CREDS=$(aws secretsmanager get-secret-value --secret-id cloudflare-dolphin-tunnel-credentials --region us-west-1 --query SecretString --output text) \
  && printf '%s' "$CREDS" | python3 -c 'import json,sys; d=json.load(sys.stdin); d.setdefault("AccountTag","${var.cloudflare_account_id}"); d.setdefault("TunnelID","${local.dolphin_tunnel_id}"); print(json.dumps(d))' \
     | (umask 077; cat > /etc/cloudflared/creds-${local.dolphin_domain}.json) \
  || echo "WARNING: could not read the dolphin-labs tunnel credentials"
%{endif~}
unset CREDS
set -x
chmod 600 /etc/cloudflared/creds-*.json 2>/dev/null || true
cat > /etc/systemd/system/cloudflared@.service <<'UNIT'
[Unit]
Description=Cloudflare Tunnel for %i
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/local/bin/cloudflared --config /etc/cloudflared/config-%i.yml --no-autoupdate tunnel run
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

# ---------------------------------------------------------------- host services
cat > /etc/systemd/system/nextjs-ct-firewall.service <<'UNIT'
[Unit]
Description=The containers' network boundary (nextjs-ct)
Before=machines.target network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/nextjs-ct-firewall

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/nextjs-ct-imds.service <<'UNIT'
[Unit]
Description=Each container's instance metadata: its own narrowed AWS session (nextjs-ct)
After=network-online.target nextjs-ct-firewall.service
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 -I /usr/local/sbin/nextjs-ct-imds
Restart=always
RestartSec=2
PrivateTmp=yes
ProtectSystem=strict
NoNewPrivileges=yes

[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/nextjs-ct-ndev-sync.service <<'UNIT'
[Unit]
Description=Route published projects to their containers (nextjs-ct)

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I /usr/local/sbin/nextjs-ct-ndev-sync
UNIT
cat > /etc/systemd/system/nextjs-ct-ndev-sync.timer <<'UNIT'
[Unit]
Description=Re-check published projects every minute (a deleted checkout loses its route)

[Timer]
OnBootSec=1min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
UNIT
cat > /etc/systemd/system/nextjs-ct-ndev-sync.path <<'UNIT'
[Unit]
Description=Publish a project as soon as ndev records it in a container

[Path]
%{for u in local.nextjs_users~}
PathChanged=/var/lib/nextjs-ct/ndev/${u}/instances
%{endfor~}

[Install]
WantedBy=paths.target
UNIT
systemctl daemon-reload
systemctl enable --now nextjs-ct-firewall.service >/dev/null 2>&1
systemctl restart nextjs-ct-firewall.service || { echo "ERROR: the containers' firewall did not load"; exit 1; }
systemctl enable nextjs-ct-imds.service nextjs-ct-ndev-sync.timer nextjs-ct-ndev-sync.path machines.target >/dev/null 2>&1
systemctl restart nextjs-ct-imds.service
systemctl start nextjs-ct-ndev-sync.timer nextjs-ct-ndev-sync.path

# ---------------------------------------------------------------- containers
/usr/local/sbin/nextjs-ct-base || { echo "ERROR: the container template did not build"; exit 1; }
CT_FAILED=""
%{for u in local.nextjs_users~}
/usr/local/sbin/nextjs-ct-machine ${u} || CT_FAILED="$CT_FAILED ${u}"
%{endfor~}
# The accounts now exist on the host: their firewall rules, sshd rules and credential server entries.
/usr/local/sbin/nextjs-ct-firewall
systemctl restart nextjs-ct-imds.service
/usr/local/sbin/nextjs-ct-sshd || CT_FAILED="$CT_FAILED sshd"
/usr/local/sbin/nextjs-ct-ndev-sync || CT_FAILED="$CT_FAILED ndev-sync"
%{if local.nextjs_ct_serving~}
%{for z, t in local.nextjs_zone_tunnel~}
systemctl enable --now cloudflared@${z}.service >/dev/null 2>&1 || CT_FAILED="$CT_FAILED cloudflared@${z}"
%{endfor~}
%{else~}
# Not serving yet: no connector may run here (the live box holds the tunnels until the cutover).
%{for z, t in local.nextjs_zone_tunnel~}
systemctl disable --now cloudflared@${z}.service >/dev/null 2>&1 || true
%{endfor~}
%{endif~}
[ -z "$CT_FAILED" ] || { echo "ERROR: not converged:$CT_FAILED"; exit 1; }

# ---------------------------------------------------------------- admin hops
# The hop key for the admin account (ubuntu) only: it reaches the other dev servers, never a jumpbox.
${local.dev_hop_setup}

# ---------------------------------------------------------------- done
# Healthy or not done: the exit status is what setup-sync (and the first boot) go by. The ETag of the
# script that ran is recorded here as well as by setup-sync, so the timer's first tick after the first
# boot does not run the same script again.
/usr/local/sbin/nextjs-ct-health || { echo "ERROR: nextjs-ct host is not healthy"; exit 1; }
if [ -n "$${NEXTJS_CT_ETAG:-}" ]; then
  mkdir -p /var/lib/nextjs-setup && printf '%s' "$NEXTJS_CT_ETAG" > /var/lib/nextjs-setup/applied-etag
fi
echo "nextjs-ct host ready"
SCRIPT
}

resource "aws_s3_object" "nextjs_ct_user_data" {
  count        = var.enable_nextjs_ct ? 1 : 0
  bucket       = aws_s3_bucket.dev_scripts.id
  key          = "user-data/nextjs-ct.sh"
  content      = local.nextjs_ct_user_data
  content_type = "text/x-shellscript"
  tags         = { Name = "nextjs-ct-user-data" }
}

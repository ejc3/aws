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
  # owner's boxes) and is the only one with `ssh mac` (local.nextjs_ct_mac_clients). Everyone else is a member (aws_iam_policy.nextjs_ct_member).
  nextjs_ct_owners = ["ejc3"]
  # Serving the live tunnels. TRUE since the cutover (2026-10-20): with the same tunnel credentials on two boxes,
  # Cloudflare would split every hostname's traffic between them.
  nextjs_ct_serving = true
  # The retired shared box (aws_instance.nextjs_dev), kept stopped as the rollback after the cutover. FALSE since
  # 2026-10-22 (owner: "you can terminate retired nextjs"): it is terminated, and its root volume is kept (it was never
  # delete_on_termination) with its AWS Backup snapshots. With it gone there is no rollback, so nextjs_ct_serving must stay
  # true (a precondition on the Elastic IP's association says so).
  nextjs_dev_retired_box = false

  # ---- per-container AWS sessions (nextjs-ct.tf explains the self-assume)
  # Denied to every container, the owner's too: what only the host does.
  nextjs_ct_deny_host_only = [
    {
      Sid    = "HostOnlyAgents"
      Effect = "Deny"
      Action = [
        "ssmmessages:*", "ec2messages:*", "ssm:UpdateInstance*", "ssm:*Association*", "ssm:Put*",
        "ssm:GetManifest", "ssm:GetDeployablePatchSnapshotForInstance", "ssm:*Document",
        "cloudwatch:PutMetricData",
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
  # ---- members: one managed session policy each (aws_iam_policy.nextjs_ct_member below). An inline session policy
  # is capped at 2,048 characters and a managed one at 6,144, and a member's denials name that member's own boxes.
  nextjs_ct_member_users = [for u in local.nextjs_users : u if !contains(local.nextjs_ct_owners, u)]

  # Denied to every member: other roles (the Dolphin apply role), the owner's secrets, and the box actions no
  # member's own box needs.
  nextjs_ct_deny_member = [
    {
      Sid      = "OwnerOnlyRoles"
      Effect   = "Deny"
      Action   = "sts:AssumeRole"
      Resource = "*"
    },
    {
      Sid    = "OwnerOnlySecrets"
      Effect = "Deny"
      Action = "secretsmanager:GetSecretValue"
      # The hop key, the Google Docs token, the owner's own wbox password and the owner's Cloudflare Access tokens
      # for the MacBook and for Colton's Mac (by name: a secret ARN ends in a random suffix).
      Resource = concat(
        [aws_secretsmanager_secret.dev_hop.arn, aws_secretsmanager_secret.wbox_admin.arn],
        [for s in data.aws_secretsmanager_secret.nextjs_gdocs : s.arn],
        ["arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:macbook-ssh-access-nextjs-ejc3-*"],
        ["arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:colton-mac-ssh-access-nextjs-ejc3-*"],
      )
    },
    {
      # The output of the owner's `wbox run` (wbox.tf: the role reads wbox-output/wbox/*). Members' wboxes do not use it.
      Sid      = "OwnerOnlyWboxOutput"
      Effect   = "Deny"
      Action   = ["s3:GetObject", "s3:ListBucket"]
      Resource = [aws_s3_bucket.wbox_output.arn, "${aws_s3_bucket.wbox_output.arn}/*"]
    },
    {
      Sid    = "NoMemberNeedsThese"
      Effect = "Deny"
      # CancelSpotInstanceRequests: pbox-flavor-types allows it account-wide for stale requests; a member could cancel
      # another member's or the owner's pending launch with it (a member's pbox only reports a stale request it cannot clear).
      Action   = ["ec2:GetPasswordData", "ssm:SendCommand", "ec2:CreateVolume", "ec2:DeleteVolume", "ec2:CancelSpotInstanceRequests"]
      Resource = "*"
    },
    # What nextjs-dev-fcvm-parity.tf gives the owner's container (fcvm's abilities), denied to every member again.
    {
      Sid      = "OwnerOnlyFcvmSecrets"
      Effect   = "Deny"
      Action   = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"]
      Resource = local.nextjs_dev_parity_secrets
    },
    {
      Sid      = "OwnerOnlyConstellationPointer"
      Effect   = "Deny"
      Action   = "ssm:GetParameter"
      Resource = aws_ssm_parameter.constellation.arn
    },
    {
      # Members keep Haiku 5.5 and opencode's DeepSeek, as before; every other model is the owner's.
      Sid         = "MemberModelsOnly"
      Effect      = "Deny"
      Action      = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"]
      NotResource = local.nextjs_ct_member_bedrock
    },
    {
      # The account is already subscribed to the models members use, so they need no Subscribe either.
      Sid      = "OwnerOnlyFcvmServices"
      Effect   = "Deny"
      Action   = ["bedrock-mantle:*", "aws-marketplace:Subscribe", "ses:*", "codeartifact:*", "sts:GetServiceBearerToken"]
      Resource = "*"
    },
    {
      # claw-master's log group and the dolphin-maps loop's bucket.
      Sid    = "OwnerOnlyFcvmData"
      Effect = "Deny"
      Action = ["logs:*", "s3:*"]
      Resource = [
        aws_cloudwatch_log_group.claw_master_server.arn, "${trimsuffix(aws_cloudwatch_log_group.claw_master_server.arn, ":*")}:*",
        aws_s3_bucket.dolphin_maps_loop.arn, "${aws_s3_bucket.dolphin_maps_loop.arn}/*",
      ]
    },
  ]

  # Each member's own three boxes (member-boxes.tf): wbox-<p>, pbox-<p>, gbox-<p>.
  nextjs_ct_member_box_all = { for p in local.member_box_people : p => {
    names     = [local.member_wbox_slots[p], local.member_pbox_slots[p], local.member_gbox_slots[p]]
    templates = [aws_launch_template.member_wbox[p].arn, aws_launch_template.member_pbox[p].arn, aws_launch_template.member_gbox[p].arn]
    volumes   = [aws_ebs_volume.member_wbox_data[p].arn, aws_ebs_volume.member_pbox_work[p].arn]
    role      = aws_iam_role.member_wbox[p].arn
    secrets   = [aws_secretsmanager_secret.member_box_ssh[p].arn, aws_secretsmanager_secret.member_wbox_admin[p].arn]
    # The private addresses this box reaches: the wbox (us-west-1) and the gbox (us-west-2, over the I/O box peering).
    # A member pbox is in Ohio with no route from here; it is reached at its public address, and the boundary there
    # is the member's own SSH key (denied to every other member above), not the network.
    ips = [local.member_wbox_ips[p], local.member_gbox_ips[p]]
  } }
  nextjs_ct_member_box     = { for p, b in local.nextjs_ct_member_box_all : p => b if contains(local.nextjs_users, p) }
  nextjs_ct_member_box_ips = flatten([for p, b in local.nextjs_ct_member_box_all : b.ips])
  nextjs_ct_ec2_launched   = ["arn:aws:ec2:*:*:instance/*", "arn:aws:ec2:*:*:volume/*", "arn:aws:ec2:*:*:network-interface/*"]

  # A member may act on their own boxes only. nextjs-dev-role's member-*-control grants admit every member's box; these
  # narrow a member's session to their own. Volumes are named by ARN, never only by an instance's tag: AttachVolume
  # grants combine with parallel-box-launch.tf's and dev-ebs.tf's.
  nextjs_ct_own_boxes = { for p, b in local.nextjs_ct_member_box : p => [
    {
      # Every other member's box key and wbox password, by ARN: a condition on secretsmanager:SecretId would compare
      # whatever the caller sent (a name or an ARN), the resource is the secret itself.
      Sid      = "OtherMembersBoxSecrets"
      Effect   = "Deny"
      Action   = "secretsmanager:GetSecretValue"
      Resource = flatten([for o, ob in local.nextjs_ct_member_box_all : ob.secrets if o != p])
    },
    {
      Sid       = "OwnBoxNamesOnly"
      Effect    = "Deny"
      Action    = ["ec2:RunInstances", "ec2:CreateTags"]
      Resource  = local.nextjs_ct_ec2_launched
      Condition = { StringNotEquals = { "aws:RequestTag/Name" = b.names } }
    },
    {
      # A Name is set only as the box is launched: no renaming someone else's box into one of these names.
      Sid       = "TagOnlyAtLaunch"
      Effect    = "Deny"
      Action    = "ec2:CreateTags"
      Resource  = local.nextjs_ct_ec2_launched
      Condition = { StringNotEquals = { "ec2:CreateAction" = "RunInstances" } }
    },
    {
      Sid       = "OwnLaunchTemplatesOnly"
      Effect    = "Deny"
      Action    = "ec2:RunInstances"
      Resource  = "arn:aws:ec2:*:*:instance/*"
      Condition = { ArnNotEquals = { "ec2:LaunchTemplate" = b.templates } }
    },
    {
      Sid       = "OwnBoxesOnly"
      Effect    = "Deny"
      Action    = ["ec2:TerminateInstances", "ec2:StartInstances", "ec2:StopInstances", "ec2:RebootInstances", "ec2:AttachVolume", "ec2:DetachVolume"]
      Resource  = "arn:aws:ec2:*:*:instance/*"
      Condition = { StringNotEquals = { "ec2:ResourceTag/Name" = b.names } }
    },
    {
      Sid         = "OwnVolumesOnly"
      Effect      = "Deny"
      Action      = ["ec2:AttachVolume", "ec2:DetachVolume"]
      NotResource = concat(b.volumes, ["arn:aws:ec2:*:*:instance/*"])
    },
    {
      Sid         = "OwnBoxRoleOnly"
      Effect      = "Deny"
      Action      = "iam:PassRole"
      NotResource = [b.role]
    },
  ] }
  # Each member's own Cloudflare Access token for their own machine (colton-mac-ssh.tf, colin-pc-ssh.tf): denied to every
  # other member, with or without boxes (ejc3's tokens are in OwnerOnlySecrets above), in one statement so a member's
  # session policy grows by an ARN per machine, not a statement. By name: a secret ARN ends in a random suffix.
  nextjs_ct_member_machine_tokens = {
    colton = "colton-mac-ssh-access-nextjs-colton"
    colin  = "colin-pc-ssh-access-nextjs-colin"
  }
  nextjs_ct_member_machine_ssh = { for u in local.nextjs_ct_member_users : u => [for s in [{
    Sid      = "MemberMachineTokensOwnOnly"
    Effect   = "Deny"
    Action   = "secretsmanager:GetSecretValue"
    Resource = [for owner, secret in local.nextjs_ct_member_machine_tokens : "arn:aws:secretsmanager:${var.aws_region}:${data.aws_caller_identity.current.account_id}:secret:${secret}-*" if owner != u]
  }] : s if length(s.Resource) > 0] }

  # A member with no boxes of their own (added to local.nextjs_users, not to local.member_box_people).
  nextjs_ct_no_boxes = [
    {
      Sid    = "NoBoxes"
      Effect = "Deny"
      Action = [
        "ec2:RunInstances", "ec2:CreateTags", "ec2:TerminateInstances", "ec2:StartInstances", "ec2:StopInstances",
        "ec2:RebootInstances", "ec2:AttachVolume", "ec2:DetachVolume", "iam:PassRole",
      ]
      Resource = "*"
    },
    {
      Sid      = "NoBoxSecrets"
      Effect   = "Deny"
      Action   = "secretsmanager:GetSecretValue"
      Resource = flatten([for o, ob in local.nextjs_ct_member_box_all : ob.secrets])
    },
  ]

  nextjs_ct_allow_all = [{ Sid = "UpToTheRole", Effect = "Allow", Action = "*", Resource = "*" }]
  # The inline session policy, the same for every account; a member's session also carries their managed policy.
  nextjs_ct_session_policy = { Version = "2012-10-17", Statement = concat(local.nextjs_ct_allow_all, local.nextjs_ct_deny_host_only) }

  # Terraform in every container, so dolphin-aws's scripts/apply.sh (applied from this box only) runs without a hand
  # install: the version main.tf requires and both jumpboxes run, and the sha256 of HashiCorp's linux_arm64 zip from
  # its SHA256SUMS, whose binary is byte for byte the jumpbox's /usr/local/lib/terraform-run-lock/terraform.
  # A Terraform bump changes this with main.tf, jumpbox2-user-data.tf and drift.yml (scripts/test-nextjs-ct-containers.py).
  nextjs_ct_terraform = { version = "1.10.3", sha256 = "b92d8245c67d4f7193245bc9d534048a63520302b6edb3c7764277b00c96ed31" }

  nextjs_ct_config = {
    role_arn            = local.nextjs_dev_role_arn
    serving             = local.nextjs_ct_serving
    zones               = local.nextjs_zone_tunnel
    ssh_hostnames       = local.dolphin_tunnel_id == null ? {} : { (local.dolphin_domain) = "ssh.${local.dolphin_domain}" }
    pinned_routes       = { for h, r in local.nextjs_pinned_routes : h => r if contains(keys(local.nextjs_zone_tunnel), r.zone) }
    session_policy      = local.nextjs_ct_session_policy
    session_policy_arns = { for u, pol in aws_iam_policy.nextjs_ct_member : u => [pol.arn] }
    member_box_ips      = local.nextjs_ct_member_box_ips
    io_box_ip           = local.io_box_private_ip
    retired_keys        = local.nextjs_retired_user_keys
    claw_master         = { tag = local.claw_master_tag, sha256 = local.claw_master_sha256_aarch64, server = "${local.claw_master_server_ip}:${local.claw_master_server_port}" }
    tmux_scroll         = { tag = local.tmux_scroll_tag, sha256 = local.tmux_scroll_sha256_aarch64 }
    terraform           = local.nextjs_ct_terraform
    # Eternal Terminal for the phone (Prompt connects with it): the vendored 7.x build nextjs-dev ran, pinned. The host's
    # 2022 (open in the security group, as on nextjs-dev) is forwarded to one container only, the owner's.
    eternal_terminal = { url = "https://github.com/ejc3/EternalTerminal/releases/download/binaries-7.x/et-aarch64.tar.gz", sha256 = "adb5e91ff25c0fea435971f8f06fd5f771cb33adff07e0bbf05c3fbc3a541d60", user = "ejc3", port = 2022 }
    users = { for u in local.nextjs_users : u => {
      uid        = lookup(local.nextjs_ct_uid_pins, u, null)
      zone       = lookup(local.nextjs_user_zone, u, null)
      owner      = contains(local.nextjs_ct_owners, u)
      codex_seed = contains(local.nextjs_codex_seed_users, u)
      keys       = lookup(local.nextjs_user_keys, u, [])
      repos      = lookup(local.nextjs_user_repos, u, [])
      box_ips    = try(local.nextjs_ct_member_box[u].ips, [])
      macs       = lookup(local.nextjs_ct_mac_clients, u, {})
      # Of those, the Windows PCs whose Windows-MCP the account's Claude Code gets (provision runs
      # windows-mcp-client-setup.sh as the account).
      windows_mcp = [for a in keys(lookup(local.nextjs_ct_mac_clients, u, {})) : a if contains(local.windows_mcp_pcs, a)]
    } }
  }

  # The Macs each account reaches, inside its own container only, the same as on nextjs-dev (mac-ssh-client.tf): alias
  # => that account's own Cloudflare Access service token, read through the container's own AWS session at connect
  # time. `mac` (the MacBook) is ejc3's alone; `colton-mac` is colton's and ejc3's (owner, 2026-10-19). Every other
  # member's session policy denies these tokens (above). No reverse tunnel and nothing listening on the box.
  nextjs_ct_mac_clients = {
    ejc3   = { mac = "macbook-ssh-access-nextjs-ejc3", colton-mac = "colton-mac-ssh-access-nextjs-ejc3" }
    colton = { colton-mac = "colton-mac-ssh-access-nextjs-colton" }
    colin  = { colin-pc = "colin-pc-ssh-access-nextjs-colin" }
  }
  # Run as root in the container by provision, once per alias: mac-ssh.sh <account> <alias> <secret>.
  nextjs_ct_mac_ssh = <<-MACSSH
set -euo pipefail
MAC_USER="$1"
MAC_ALIAS="$2"
MAC_SECRET="$3"
${local.mac_cloudflared_install}
# The shared install only warns (nextjs-dev's setup goes on without it); here a missing or wrong binary fails provision,
# so the container is not marked ready with an `ssh <alias>` that cannot connect.
echo "${local.cloudflared_sha256_linux_arm}  /home/$MAC_USER/.local/bin/cloudflared" | sha256sum -c - >/dev/null 2>&1 ||
  { echo "mac-ssh: cloudflared is missing or does not match its pin" >&2; exit 1; }
${local.mac_ssh_client_setup}
MACSSH

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
    # Windows-MCP's client side (colin-pc-ssh.tf): the bridge, its setup and mcp-remote's lockfile.
    # Listed, not globbed: fileset's {a,b} cannot hold a path separator.
    { for f in ["scripts/windows/windows-mcp-bridge", "scripts/windows/windows-mcp-client-setup.sh", "scripts/windows/mcp-remote/package.json", "scripts/windows/mcp-remote/package-lock.json"] : "/var/lib/nextjs-ct/shared/${trimprefix(f, "scripts/")}" => f },
  )
  # Rendered by Terraform from the snippets the other boxes run, so every box shares one definition.
  nextjs_ct_snippets = {
    "/var/lib/nextjs-ct/shared/config.json"                            = jsonencode(local.nextjs_ct_config)
    "/var/lib/nextjs-ct/shared/snippets/user-shell-env.sh"             = local.user_shell_env
    "/var/lib/nextjs-ct/shared/snippets/opencode-setup.sh"             = local.opencode_user_setup
    "/var/lib/nextjs-ct/shared/snippets/agent-session-sync-install.sh" = local.agent_session_sync_install
    "/var/lib/nextjs-ct/shared/snippets/codex-seed-install.sh"         = "${local.codex_seed_thread_install}\n${local.codex_restart_setup}"
    "/var/lib/nextjs-ct/shared/snippets/user-agents-seed-install.sh"   = local.user_agents_seed_install
    "/var/lib/nextjs-ct/shared/snippets/box-tools.sh"                  = "${local.pbox_setup}\n${local.gbox_setup}\n${local.wbox_setup}\n${local.wbox_member_setup}"
    "/var/lib/nextjs-ct/shared/snippets/mac-ssh.sh"                    = local.nextjs_ct_mac_ssh
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
# inotify instances: every container's user namespace is created by root (systemd-nspawn), so each instance made inside
# any container is charged to host root as well, against this one limit. Ubuntu's 128 was used up by five containers'
# Claude, Codex, dev servers and systemd (135 on 2026-10-22), and OpenCode 2's server then hung on EMFILE at start.
cat > /etc/sysctl.d/60-nextjs-ct.conf <<'SYSCTL'
net.ipv4.ip_forward = 1
kernel.apparmor_restrict_unprivileged_userns = 0
fs.inotify.max_user_instances = 8192
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
# The server also caps each container's connections in flight; these bound it as a whole.
TasksMax=256
MemoryMax=256M
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
# Reload the rules by running the script, never by restarting the unit: every container's systemd-nspawn@ unit
# Requires= it (nextjs-ct-machine), and restarting a required unit restarts all of them, ending every session.
# That is what a routine apply did on 2026-10-20 (13:54 and 14:04), since any change to this script re-runs setup.
# A rejected ruleset (nft -c fails) leaves the old rules loaded, so on failure the unit is stopped: every container
# Requires= it and stops with it, failing closed exactly as the old restart did, and setup-sync retries.
if ! { systemctl start nextjs-ct-firewall.service && /usr/local/sbin/nextjs-ct-firewall; }; then
  echo "ERROR: the containers' firewall did not load; stopping it, and with it every container"
  systemctl stop nextjs-ct-firewall.service
  exit 1
fi
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

# A member's managed session policy: up to nextjs-dev-role, minus the member denials, narrowed to their own boxes.
# Passed by nextjs-ct-imds with every session it assumes for that member's container; attached to nothing.
resource "aws_iam_policy" "nextjs_ct_member" {
  for_each    = toset(local.nextjs_ct_member_users)
  name        = "nextjs-ct-member-${each.key}"
  description = "SESSION policy only, attached to no principal: passed with ${each.key}'s nextjs-ct container session, it can only narrow nextjs-dev-role (Allow * intersects the role; every other statement denies)"
  tags        = { Purpose = "session-policy", AttachedTo = "none", Account = each.key }
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = concat(local.nextjs_ct_allow_all, local.nextjs_ct_deny_member, try(local.nextjs_ct_own_boxes[each.key], local.nextjs_ct_no_boxes), local.nextjs_ct_member_machine_ssh[each.key])
  })
}

resource "aws_s3_object" "nextjs_ct_user_data" {
  count        = var.enable_nextjs_ct ? 1 : 0
  bucket       = aws_s3_bucket.dev_scripts.id
  key          = "user-data/nextjs-ct.sh"
  content      = local.nextjs_ct_user_data
  content_type = "text/x-shellscript"
  tags         = { Name = "nextjs-ct-user-data" }
}

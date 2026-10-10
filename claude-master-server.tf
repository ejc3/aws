# claude-master-server.tf
#
# The shared claude-master server: ONE small box that holds the Claude subscription logins and
# routes inference for every other box, so no client box holds a login at all. Clients prove who
# they are with a client certificate this box issued (no password anywhere); the dev boxes reach it
# on a private address, a Mac with AWS access reaches it through an SSM port forward, and anything
# else can later come in over a Cloudflare tunnel. claude-master itself knows nothing of AWS: it is
# a generic binary driven by flags and files (github.com/ejc3/CLIProxyAPI, docs/claude-master.md).
# Everything cloud-specific -- this file, the IAM, the bootstrap, scripts/claude-master-enroll.sh --
# lives here.
#
# WHY A BOX AT ALL. A subscription login rotates on every refresh and the old access token dies at
# once, so copies of it on several boxes destroy each other within hours. One process owns each
# login; the rest talk to it.
#
# SIZE. t4g.micro (1GB). The Go proxy itself is small, but t4g.nano (0.5GB) was tried first and
# did not survive its own first boot: apt's post-install hooks (appstreamcli) were OOM-killed,
# cloud-final failed, and the SSM agent starved so no command was delivered. The swapfile is also
# created at the very top of the inline bootstrap, before any apt run, for the same reason.
#
# NETWORK. A fixed private address, because client certificates name it. The security group admits
# the proxy port from the dev fleet subnets alone and SSH from the two admin boxes alone. The box
# gets a public IPv4 only for OUTBOUND (api.anthropic.com, GitHub release downloads); nothing can
# connect in. It is reached for administration over SSM, or SSH from the jumpboxes.
#
# LOGINS ARE INTERACTIVE and belong to this box: `scripts/claude-master-login.sh --server` walks
# the four paste-a-code logins here. They are never copied from another box (a rotating login
# cannot be shared). The service stays idle until every profile below has a login, then
# `sudo claude-master-rollout` starts it (scripts/claude-master-login.sh --server does that).
#
# CERTIFICATES. The CA key never leaves /var/lib/claude-master/state. A client box makes its own
# key and a certificate request; scripts/claude-master-enroll.sh gets the request signed over SSM
# (an admin action) and installs the certificate. Certificates last 30 days.
#
# ROLLING RESTARTS. Envoy listens on the client address (and the tunnel's loopback port) and passes TCP
# through, unchanged, to one of two claude-master servers on this box, blue or green, each on its own
# port of the same address (the server certificate names the address clients dial; the ports are not
# in the security group). `sudo claude-master-rollout` starts the idle color, points Envoy's NEW
# connections at it (an endpoint file Envoy watches, replaced atomically), and stops the old color,
# which drains: it serves the connections it already has, each response closing its connection, so
# every client moves over on its next request without an error, and running requests get up to
# local.claude_master_drain_seconds. Nothing on the box restarts Envoy or a server on its own.
#
# TO ROLL THE BINARY FORWARD: publish a release of ejc3/CLIProxyAPI, change the tag and the sha256,
# apply (terraform_data.claude_master_server_converge installs the binary atomically), then
# `sudo claude-master-rollout`.

variable "enable_claude_master_server" {
  description = "Enable the shared claude-master server box"
  type        = bool
  default     = true
}

locals {
  claude_master_tag            = "claude-master-957ec56"
  claude_master_sha256_aarch64 = "850334a53ad430ab06325c4fe6636286fa8f04b6ca69e70abfbc9909693ccd75"

  # CloudWatch agent: receives the proxy's OTLP metrics on loopback and ships them (and the log file) to
  # CloudWatch. Pinned by version and sha256 like cloudflared; the versioned S3 path is the same file as
  # `latest` at the time of pinning (verified byte for byte).
  claude_master_cwagent_version      = "1.300073.2b1889"
  claude_master_cwagent_sha256_arm64 = "0d04b62f688f257aa35604f89b48f259cea5ed412985831d8d56838f43332169"
  claude_master_metrics_namespace    = "ClaudeMaster"
  claude_master_log_group            = "/claude-master/server"

  claude_master_server_ip   = "10.0.1.50" # in subnet_a; client certificates name this address
  claude_master_server_port = 8443        # Envoy; the servers behind it use the color ports below

  # Envoy in front of the servers. Pinned by version and sha256 (the official linux-aarch_64 binary).
  claude_master_envoy_version        = "1.39.1"
  claude_master_envoy_sha256_aarch64 = "8565ad0af4b1d1d3c986e5165c027add3073579182f398dd7f4d728d25e9ec62"
  # The two servers' ports: the client-facing one on the server address, the open one on loopback.
  claude_master_colors = {
    blue  = { port = 18443, open_port = 18444 }
    green = { port = 28443, open_port = 28444 }
  }
  # How long a stopping server lets running requests finish. Its unit's stop timeout is a minute longer.
  claude_master_drain_seconds = 600

  # Order is the fallback order. The first login is the one a fresh session prefers when quotas tie.
  claude_master_profiles = ["claude-connor", "claude-colton", "claude-colin", "claude-ejc3"]

  claude_master_admin_cidrs = concat(
    var.enable_jumpbox ? ["${aws_instance.jumpbox[0].private_ip}/32"] : [],
    var.enable_jumpbox_2 ? ["${aws_instance.jumpbox_2[0].private_ip}/32"] : [],
  )
}

locals {
  # The root volume holds the four subscription logins and the CA key that signs every client
  # certificate: the two things that are painful to recreate (three interactive logins; reissuing
  # every client). It joins the backup pool, and the DR copy, with the other persistent roots.
  claude_master_server_volume_arn = var.enable_claude_master_server ? "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:volume/${aws_instance.claude_master_server[0].root_block_device[0].volume_id}" : ""
}

resource "aws_security_group" "claude_master_server" {
  count       = var.enable_claude_master_server ? 1 : 0
  name_prefix = "claude-master-server-"
  description = "claude-master server: the proxy port from the dev fleet subnets, SSH from the admin boxes"
  vpc_id      = local.vpc_id

  ingress {
    description = "claude-master proxy (TLS, client certificate required)"
    from_port   = local.claude_master_server_port
    to_port     = local.claude_master_server_port
    protocol    = "tcp"
    cidr_blocks = [for s in local.dev_fleet_subnets : s.cidr_block]
  }

  ingress {
    description = "SSH from the admin boxes"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = local.claude_master_admin_cidrs
  }

  # IPv6 egress matters: see jumpbox2.tf. Nothing inbound is open to the internet.
  egress {
    description      = "all outbound"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = { Name = "claude-master-server" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_iam_role" "claude_master_server" {
  count = var.enable_claude_master_server ? 1 : 0
  name  = "claude-master-server-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = "claude-master-server-role" }
}

resource "aws_iam_role_policy_attachment" "claude_master_server_ssm" {
  count      = var.enable_claude_master_server ? 1 : 0
  role       = aws_iam_role.claude_master_server[0].name
  policy_arn = aws_iam_policy.ssm_managed_instance.arn
}

# Its bootstrap script and nothing else in the scripts bucket; the backup API key comes through
# aws_iam_policy.claude_master_backup_key_read (dev-ai-services.tf).
resource "aws_iam_role_policy" "claude_master_server_bootstrap" {
  count = var.enable_claude_master_server ? 1 : 0
  name  = "bootstrap-script"
  role  = aws_iam_role.claude_master_server[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadItsOwnBootstrapScript"
      Effect   = "Allow"
      Action   = "s3:GetObject"
      Resource = "${aws_s3_bucket.dev_scripts.arn}/user-data/claude-master-server.sh"
    }]
  })
}

# What the CloudWatch agent on the box may do, and nothing else: publish metrics in ONE namespace, and write
# to ONE log group that Terraform owns (so it needs no CreateLogGroup). No read access to anything.
resource "aws_iam_role_policy" "claude_master_server_telemetry" {
  count = var.enable_claude_master_server ? 1 : 0
  name  = "telemetry"
  role  = aws_iam_role.claude_master_server[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "PublishMetricsInOneNamespace"
        Effect   = "Allow"
        Action   = "cloudwatch:PutMetricData"
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = local.claude_master_metrics_namespace }
        }
      },
      {
        Sid    = "WriteItsOwnLogGroup"
        Effect = "Allow"
        Action = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = [
          aws_cloudwatch_log_group.claude_master_server.arn,
          "${aws_cloudwatch_log_group.claude_master_server.arn}:*",
        ]
      },
    ]
  })
}

# The proxy's own log (info level: what changed, plus a quota snapshot every five minutes). Redacted at the
# source; kept 90 days.
resource "aws_cloudwatch_log_group" "claude_master_server" {
  name              = local.claude_master_log_group
  retention_in_days = 90
  tags              = { Name = "claude-master-server" }
}

# Names for the incoming users' Anthropic accounts in the metrics (the `client_account` dimension), so the
# dashboards say who instead of acct-<8 hex>. claude-master reads them from /etc/claude-master/account-labels,
# which the server writes from this secret before every start (claude-master-account-labels below). Terraform
# creates only the container; the owner sets the value out of band, one `ACCOUNT_UUID=NAME` per line (UUID =
# oauthAccount.accountUuid in that user's ~/.claude.json; `claude-master account-key UUID` prints the
# acct-<8 hex> key an account shows as until it has a name). From a file, never on a command line:
#
#   aws secretsmanager put-secret-value --region us-west-1 --secret-id claude-master/account-labels \
#     --secret-string file://labels.txt && shred -u labels.txt
#
# then restart the server when its sessions can be interrupted (`sudo systemctl restart claude-master-server`
# over SSM): claude-master reads the file only when it starts. A value with any other kind of line is refused
# and the old file kept, because claude-master refuses to start on a bad line. Never write a UUID or a name
# into this repository.
resource "aws_secretsmanager_secret" "claude_master_account_labels" {
  count                   = var.enable_claude_master_server ? 1 : 0
  name                    = "claude-master/account-labels"
  description             = "ACCOUNT_UUID=NAME lines naming claude-master's incoming accounts in its metrics. Value set out of band; see claude-master-server.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "claude-master/account-labels", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_policy" "claude_master_account_labels" {
  count      = var.enable_claude_master_server ? 1 : 0
  secret_arn = aws_secretsmanager_secret.claude_master_account_labels[0].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheServerCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.claude_master_account_labels[0].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.claude_master_server[0].arn]) } }
    }]
  })
}

resource "aws_iam_role_policy" "claude_master_server_account_labels" {
  count = var.enable_claude_master_server ? 1 : 0
  name  = "account-labels"
  role  = aws_iam_role.claude_master_server[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadTheAccountLabels"
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.claude_master_account_labels[0].arn
    }]
  })
}

resource "aws_iam_instance_profile" "claude_master_server" {
  count = var.enable_claude_master_server ? 1 : 0
  name  = "claude-master-server-profile"
  role  = aws_iam_role.claude_master_server[0].name
}

locals {
  claude_master_server_user_data = <<SCRIPT
#!/bin/bash
set -uxo pipefail
export DEBIAN_FRONTEND=noninteractive

TAG='${local.claude_master_tag}'
SHA='${local.claude_master_sha256_aarch64}'
IP='${local.claude_master_server_ip}'
PORT='${local.claude_master_server_port}'
REGION='${var.aws_region}'
HOME_DIR=/var/lib/claude-master

# ---------------------------------------------------------------- base packages
apt-get update -y || true
apt-get install -y curl jq unzip || echo "WARNING: some base packages failed"

# ---------------------------------------------------------------- swap (also made earlier, inline)
if ! swapon --show=NAME --noheadings | grep -q .; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---------------------------------------------------------------- the service account
# Its own unix user: the logins and the CA key are readable by this account and root, not by the
# ubuntu account that people SSH in as.
id claude-master >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin claude-master
install -d -m 0700 -o claude-master -g claude-master "$HOME_DIR" "$HOME_DIR/state"

# ---------------------------------------------------------------- the pinned binary
current=$(sha256sum /usr/local/bin/claude-master 2>/dev/null | cut -d' ' -f1)
if [ "$current" != "$SHA" ]; then
  if curl -fsSL "https://github.com/ejc3/CLIProxyAPI/releases/download/$TAG/claude-master-linux-arm64" -o /tmp/claude-master.new \
     && echo "$SHA  /tmp/claude-master.new" | sha256sum -c - ; then
    install -m 0755 /tmp/claude-master.new /usr/local/bin/claude-master.new && mv -f /usr/local/bin/claude-master.new /usr/local/bin/claude-master
    echo "claude-master $TAG installed; a running server keeps its old binary until it is restarted"
  else
    echo "ERROR: claude-master $TAG did not download or did not match its sha256; leaving the installed binary alone" >&2
  fi
  rm -f /tmp/claude-master.new
fi

# ---------------------------------------------------------------- how the server starts
# The final paid backup (claude-master/backup-api-key) is read from Secrets Manager at start and
# handed over in the environment: never on a command line, never written to disk.
cat > /usr/local/bin/claude-master-serve <<'EOF'
#!/bin/bash
set -u
{ set +x; } 2>/dev/null   # the value below must never reach a trace
key=$(aws secretsmanager get-secret-value --region ${var.aws_region} --secret-id claude-master/backup-api-key --query SecretString --output text 2>/dev/null) || key=""
if [ -n "$key" ] && [ "$key" != "None" ]; then export CLAUDE_MASTER_BACKUP_API_KEY="$key"; else echo "claude-master-serve: no backup API key; subscriptions only" >&2; fi
unset key
# One of the two servers behind Envoy, by color: its own ports, log file and metrics instance. With no
# color it is the server from before Envoy (claude-master-server.service, until the first rollout moves
# it behind Envoy): the client port itself, and no balancer.
color=$${1:-legacy}
balancer="--balanced --drain-timeout ${local.claude_master_drain_seconds}s"
case "$color" in
${join("\n", [for c, v in local.claude_master_colors : "  ${c}) port=${v.port}; open=${v.open_port} ;;"])}
  legacy) port=${local.claude_master_server_port}; open=${local.claude_master_open_port}; balancer="" ;;
  *) echo "usage: claude-master-serve blue|green" >&2; exit 2 ;;
esac
exec /usr/local/bin/claude-master serve ${local.claude_master_profiles[0]}${join("", [for p in slice(local.claude_master_profiles, 1, length(local.claude_master_profiles)) : " --next-profile ${p}"])} \
  --listen ${local.claude_master_server_ip}:$port \
  --open-loopback 127.0.0.1:$open --state-dir /var/lib/claude-master/state \
  --log-level info --log-file /var/log/claude-master/server-$color.log --log-max-mb 20 --log-keep 5 --quota-log-interval 5m \
  --otlp-endpoint http://127.0.0.1:4318 --otlp-interval 60s --account-labels-file /etc/claude-master/account-labels \
  --instance claude-master-$color $balancer
EOF
chmod 0755 /usr/local/bin/claude-master-serve

# Names for the incoming users' Anthropic accounts in the dashboards, one `ACCOUNT_UUID=NAME` per line
# (`claude-master account-key UUID` shows the key an unnamed account gets). The file is written from the
# secret claude-master/account-labels before every start (below); until the secret has a value it holds
# only this explanation, and an existing file is kept.
mkdir -p /etc/claude-master
if [ ! -e /etc/claude-master/account-labels ]; then
  printf '%s\n' '# ACCOUNT_UUID=NAME, one per line, from the secret claude-master/account-labels at each start.' '# Without a line an account shows as acct-<8 hex of a hash>; the id itself is never exported.' > /etc/claude-master/account-labels
fi
chown root:claude-master /etc/claude-master/account-labels
chmod 0640 /etc/claude-master/account-labels

# Run by systemd as root before each start (ExecStartPre=-+ in the unit), because claude-master reads the
# file only when it starts. It never stops the start: without a value, when the secret cannot be read, or
# when a line is not `ACCOUNT_UUID=NAME` (on which claude-master would refuse to start), the file is kept.
cat > /usr/local/bin/claude-master-account-labels <<'EOF'
#!/bin/bash
set -u
{ set +x; } 2>/dev/null   # the names below stay out of traces
file=/etc/claude-master/account-labels
value=$(HOME=/root timeout 20 aws secretsmanager get-secret-value --region ${var.aws_region} --secret-id claude-master/account-labels --query SecretString --output text 2>/dev/null) || value=""
if [ -z "$value" ] || [ "$value" = "None" ]; then
  echo "claude-master-account-labels: claude-master/account-labels has no value or could not be read; keeping $file" >&2
  exit 0
fi
# claude-master's own rule: a blank line, a # comment, or ACCOUNT_UUID=NAME (8 to 64 hex digits and dashes).
if printf '%s\n' "$value" | grep -qvE '^[[:space:]]*(#.*)?$|^[[:space:]]*[0-9A-Fa-f-]{8,64}[[:space:]]*=[[:space:]]*[^[:space:]]'; then
  echo "claude-master-account-labels: claude-master/account-labels has a line that is not ACCOUNT_UUID=NAME; keeping $file" >&2
  exit 0
fi
tmp=$(mktemp /etc/claude-master/.account-labels.XXXXXX) || { echo "claude-master-account-labels: cannot write in /etc/claude-master; keeping $file" >&2; exit 0; }
if printf '%s\n' "$value" > "$tmp" && chown root:claude-master "$tmp" && chmod 0640 "$tmp" && mv -f "$tmp" "$file"; then
  echo "claude-master-account-labels: $file written with $(grep -cvE '^[[:space:]]*(#.*)?$' "$file") names" >&2
else
  rm -f "$tmp"
  echo "claude-master-account-labels: could not replace $file; keeping it" >&2
fi
exit 0
EOF
chmod 0755 /usr/local/bin/claude-master-account-labels

# The journal is small on this box: cap it, and trim what is there now. (No service is restarted: the cap
# applies the next time journald starts.)
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=200M\nMaxRetentionSec=30day\n' > /etc/systemd/journald.conf.d/claude-master.conf
journalctl --vacuum-size=200M >/dev/null 2>&1 || true

# One unit per color (claude-master-server@blue, @green). The unit of the active color is enabled at boot;
# claude-master-rollout moves that. A pre-Envoy box still runs claude-master-server.service until the
# first rollout moves it behind Envoy.
cat > /etc/systemd/system/claude-master-server@.service <<'EOF'
[Unit]
Description=claude-master server %i (shared Claude subscription pool, behind Envoy)
After=network-online.target
Wants=network-online.target
# Idle until every login exists: `scripts/claude-master-login.sh --server`, then start it.
${join("\n", [for p in local.claude_master_profiles : "ConditionPathExists=/var/lib/claude-master/.local/share/claude-master/profiles/${p}/current"])}

[Service]
User=claude-master
Group=claude-master
Environment=HOME=/var/lib/claude-master
# /var/log/claude-master, owned by the service account; the program rotates the file itself.
LogsDirectory=claude-master
LogsDirectoryMode=0750
# As root and outside the sandbox (+), and never fatal (-): the names from Secrets Manager.
ExecStartPre=-+/usr/local/bin/claude-master-account-labels
ExecStart=/usr/local/bin/claude-master-serve %i
Restart=on-failure
RestartSec=10
# A stop drains for up to the drain timeout; systemd must not cut it short.
TimeoutStopSec=${local.claude_master_drain_seconds + 60}
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/claude-master
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

# ---------------------------------------------------------------- Envoy (rolling restarts)
ENVOY_VERSION='${local.claude_master_envoy_version}'
ENVOY_SHA='${local.claude_master_envoy_sha256_aarch64}'
current=$(sha256sum /usr/local/bin/envoy 2>/dev/null | cut -d' ' -f1)
if [ "$current" != "$ENVOY_SHA" ]; then
  if curl -fsSL "https://github.com/envoyproxy/envoy/releases/download/v$ENVOY_VERSION/envoy-$ENVOY_VERSION-linux-aarch_64" -o /tmp/envoy.new \
     && echo "$ENVOY_SHA  /tmp/envoy.new" | sha256sum -c - ; then
    install -m 0755 /tmp/envoy.new /usr/local/bin/envoy.new && mv -f /usr/local/bin/envoy.new /usr/local/bin/envoy
    echo "envoy $ENVOY_VERSION installed; a running envoy keeps its old binary until it is restarted"
  else
    echo "ERROR: envoy $ENVOY_VERSION did not download or did not match its sha256; leaving the installed binary alone" >&2
  fi
  rm -f /tmp/envoy.new
fi
id envoy >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin envoy
install -d -m 0755 /etc/envoy /etc/envoy/eds

# TCP passthrough: Envoy never sees inside the TLS; client certificates are still checked by claude-master.
# The endpoints are files Envoy watches; claude-master-rollout replaces them (a rename, so atomically).
cat > /etc/envoy/envoy.yaml <<'EOF'
node: { id: claude-master, cluster: claude-master }
admin: { address: { socket_address: { address: 127.0.0.1, port_value: 9901 } } }
overload_manager:
  resource_monitors:
  - name: envoy.resource_monitors.global_downstream_max_connections
    typed_config:
      "@type": type.googleapis.com/envoy.extensions.resource_monitors.downstream_connections.v3.DownstreamConnectionsConfig
      max_active_downstream_connections: 10000
static_resources:
  listeners:
%{for name, addr in { cert = "${local.claude_master_server_ip}:${local.claude_master_server_port}", open = "127.0.0.1:${local.claude_master_open_port}" } ~}
  - name: ${name}
    address: { socket_address: { address: ${split(":", addr)[0]}, port_value: ${split(":", addr)[1]} } }
    filter_chains:
    - filters:
      - name: envoy.filters.network.tcp_proxy
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.tcp_proxy.v3.TcpProxy
          stat_prefix: ${name}
          cluster: ${name}
          idle_timeout: 3600s
%{endfor~}
  clusters:
%{for name in ["cert", "open"]~}
  - name: ${name}
    type: EDS
    connect_timeout: 2s
    eds_cluster_config:
      eds_config:
        resource_api_version: V3
        path_config_source:
          path: /etc/envoy/eds/${name}.yaml
          watched_directory: { path: /etc/envoy/eds }
%{endfor~}
EOF

cat > /usr/local/bin/claude-master-envoy-endpoints <<'EOF'
#!/bin/bash
# claude-master-envoy-endpoints COLOR: point Envoy's new connections at that color's two ports.
set -euo pipefail
case "$1" in
${join("\n", [for c, v in local.claude_master_colors : "  ${c}) port=${v.port}; open=${v.open_port} ;;"])}
  *) echo "usage: claude-master-envoy-endpoints blue|green" >&2; exit 2 ;;
esac
write() { # cluster address port
  tmp=$(mktemp /etc/envoy/eds/.$1.XXXXXX)
  printf '%s\n' 'resources:' '- "@type": type.googleapis.com/envoy.config.endpoint.v3.ClusterLoadAssignment' "  cluster_name: $1" \
    '  endpoints:' '  - lb_endpoints:' "    - endpoint: { address: { socket_address: { address: $2, port_value: $3 } } }" > "$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "/etc/envoy/eds/$1.yaml"
}
write cert ${local.claude_master_server_ip} "$port"
write open 127.0.0.1 "$open"
EOF
chmod 0755 /usr/local/bin/claude-master-envoy-endpoints

mkdir -p /etc/claude-master
[ -s /etc/claude-master/active-color ] || echo blue > /etc/claude-master/active-color
[ -e /etc/envoy/eds/cert.yaml ] && [ -e /etc/envoy/eds/open.yaml ] || /usr/local/bin/claude-master-envoy-endpoints "$(cat /etc/claude-master/active-color)"

cat > /etc/systemd/system/envoy.service <<'EOF'
[Unit]
Description=Envoy in front of the claude-master servers (TCP passthrough, rolling restarts)
After=network-online.target
Wants=network-online.target

[Service]
User=envoy
Group=envoy
ExecStart=/usr/local/bin/envoy -c /etc/envoy/envoy.yaml --log-level warn --disable-hot-restart
Restart=always
RestartSec=2
LimitNOFILE=65536
NoNewPrivileges=yes
ProtectSystem=strict
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
# A box still running the pre-Envoy server keeps it (it holds the client port) until claude-master-rollout
# moves it behind Envoy. Otherwise Envoy runs; the bootstrap starts it if it is stopped and never restarts it.
if systemctl is-active --quiet claude-master-server.service; then
  echo "claude-master-server.service (before Envoy) is running; run claude-master-rollout to move it behind Envoy"
else
  systemctl enable envoy.service >/dev/null 2>&1 || true
  systemctl is-active --quiet envoy.service || systemctl start envoy.service || true
fi

# Moves the pool to a fresh server process without a session noticing (see ROLLING RESTARTS above).
cat > /usr/local/bin/claude-master-rollout <<'EOF'
#!/bin/bash
set -uo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root: sudo claude-master-rollout" >&2; exit 2; }
IP=${local.claude_master_server_ip}
port_of() { case "$1" in ${join(" ", [for c, v in local.claude_master_colors : "${c}) echo ${v.port} ;;"])} esac; }
open_of() { case "$1" in ${join(" ", [for c, v in local.claude_master_colors : "${c}) echo ${v.open_port} ;;"])} esac; }
other() { [ "$1" = blue ] && echo green || echo blue; }
listening() { ss -ltnH | awk '{print $4}' | grep -qx "$1"; }

active=$(cat /etc/claude-master/active-color 2>/dev/null || echo blue)
legacy=0
systemctl is-active --quiet claude-master-server.service && legacy=1
if [ "$legacy" = 0 ] && systemctl is-active --quiet "claude-master-server@$active"; then
  next=$(other "$active")
else
  next=$active; active=none   # nothing behind Envoy is running yet: start the recorded color
fi

echo "starting claude-master-server@$next"
systemctl restart "claude-master-server@$next"
ready=0
for _ in $(seq 1 90); do
  if listening "$IP:$(port_of "$next")" && listening "127.0.0.1:$(open_of "$next")"; then ready=1; break; fi
  systemctl is-active --quiet "claude-master-server@$next" || break
  sleep 1
done
if [ "$ready" != 1 ]; then
  echo "claude-master-server@$next did not start listening; nothing was switched" >&2
  journalctl -u "claude-master-server@$next" -n 20 --no-pager >&2
  systemctl stop "claude-master-server@$next"
  exit 1
fi

/usr/local/bin/claude-master-envoy-endpoints "$next"
if [ "$legacy" = 1 ]; then
  # The pre-Envoy server holds the client port: it drains and exits, then Envoy takes the port over. If Envoy
  # does not come up, the pre-Envoy server is started again, so clients are never left with nothing.
  if ! /usr/local/bin/envoy --mode validate -c /etc/envoy/envoy.yaml >/dev/null 2>&1; then
    echo "Envoy's configuration does not validate; the pre-Envoy server keeps running" >&2
    systemctl stop "claude-master-server@$next"
    exit 1
  fi
  echo "stopping the pre-Envoy claude-master-server.service (it drains), then starting Envoy"
  systemctl stop claude-master-server.service
  systemctl start envoy.service
  up=0
  for _ in $(seq 1 20); do listening "$IP:${local.claude_master_server_port}" && listening "127.0.0.1:${local.claude_master_open_port}" && { up=1; break; }; sleep 0.5; done
  if [ "$up" != 1 ]; then
    echo "Envoy did not start listening; starting the pre-Envoy server again" >&2
    systemctl stop envoy.service
    systemctl start claude-master-server.service
    systemctl stop "claude-master-server@$next"
    exit 1
  fi
  systemctl disable claude-master-server.service >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/claude-master-server.service
  systemctl daemon-reload
  systemctl enable envoy.service >/dev/null 2>&1 || true
fi
systemctl is-active --quiet envoy.service || systemctl start envoy.service

switched=0
for _ in $(seq 1 20); do
  clusters=$(curl -fsS http://127.0.0.1:9901/clusters 2>/dev/null) || clusters=""
  if grep -q "^cert::$IP:$(port_of "$next")::" <<<"$clusters" && grep -q "^open::127.0.0.1:$(open_of "$next")::" <<<"$clusters"; then switched=1; break; fi
  sleep 0.5
done
if [ "$switched" != 1 ]; then
  # Envoy may already be sending some new connections to $next. Point it back at the color that is recorded as
  # active, so the record and Envoy agree and a retry does not restart the server that is taking traffic.
  if [ "$active" != none ]; then
    /usr/local/bin/claude-master-envoy-endpoints "$active"
    back=0
    for _ in $(seq 1 20); do
      clusters=$(curl -fsS http://127.0.0.1:9901/clusters 2>/dev/null) || clusters=""
      if grep -q "^cert::$IP:$(port_of "$active")::" <<<"$clusters" && grep -q "^open::127.0.0.1:$(open_of "$active")::" <<<"$clusters"; then back=1; break; fi
      sleep 0.5
    done
    if [ "$back" = 1 ]; then
      systemctl stop "claude-master-server@$next"
      echo "Envoy did not report $next's endpoints; pointed it back at $active and stopped $next" >&2
    else
      echo "Envoy reports neither $next nor $active; both are left running. Check: curl -s 127.0.0.1:9901/clusters" >&2
    fi
  else
    echo "Envoy did not report $next's endpoints; $next keeps running (nothing else is serving)" >&2
  fi
  exit 1
fi

echo "$next" > /etc/claude-master/active-color
systemctl enable "claude-master-server@$next" >/dev/null 2>&1 || true
echo "new connections go to $next"
if [ "$active" != none ]; then
  systemctl disable "claude-master-server@$active" >/dev/null 2>&1 || true
  systemctl stop --no-block "claude-master-server@$active"
  echo "claude-master-server@$active is draining (up to ${local.claude_master_drain_seconds}s); claude-master-status shows when it is done"
fi
EOF
chmod 0755 /usr/local/bin/claude-master-rollout

# ---------------------------------------------------------------- cloudflared (the tunnel for the Macs)
# Pinned by version and sha256. It dials OUT to Cloudflare and forwards to claude-master's open
# listener on loopback (claude-master-tunnel.tf); no inbound port is opened for it.
CFD_VERSION='${local.cloudflared_version}'
CFD_SHA='${local.cloudflared_sha256_linux_arm}'
current=$(sha256sum /usr/local/bin/cloudflared 2>/dev/null | cut -d' ' -f1)
if [ "$current" != "$CFD_SHA" ]; then
  if curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/$CFD_VERSION/cloudflared-linux-arm64" -o /tmp/cloudflared.new \
     && echo "$CFD_SHA  /tmp/cloudflared.new" | sha256sum -c - ; then
    install -m 0755 /tmp/cloudflared.new /usr/local/bin/cloudflared.new && mv -f /usr/local/bin/cloudflared.new /usr/local/bin/cloudflared
  else
    echo "ERROR: cloudflared $CFD_VERSION did not download or did not match its sha256; leaving the installed binary alone" >&2
  fi
  rm -f /tmp/cloudflared.new
fi

# The connector token comes from Secrets Manager into the environment (TUNNEL_TOKEN), never argv.
cat > /usr/local/bin/cloudflared-claude-master <<'EOF'
#!/bin/bash
set -u
{ set +x; } 2>/dev/null   # the token below must never reach a trace
TUNNEL_TOKEN=$(aws secretsmanager get-secret-value --region ${var.aws_region} --secret-id claude-master/tunnel-token --query SecretString --output text 2>/dev/null) || TUNNEL_TOKEN=""
[ -n "$TUNNEL_TOKEN" ] && [ "$TUNNEL_TOKEN" != "None" ] || { echo "cloudflared-claude-master: no tunnel token yet" >&2; exit 1; }
export TUNNEL_TOKEN
exec /usr/local/bin/cloudflared tunnel --no-autoupdate run
EOF
chmod 0755 /usr/local/bin/cloudflared-claude-master

cat > /etc/systemd/system/cloudflared-claude-master.service <<'EOF'
[Unit]
Description=Cloudflare tunnel for the claude-master open listener (Macs)
After=network-online.target
Wants=network-online.target

[Service]
User=claude-master
Group=claude-master
Environment=HOME=/var/lib/claude-master
ExecStart=/usr/local/bin/cloudflared-claude-master
Restart=always
RestartSec=10
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/claude-master
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable cloudflared-claude-master.service >/dev/null 2>&1 || true
# The tunnel is harmless to run before the server is: it leads to a loopback port nothing listens on
# yet. Starting it (not restarting a running one) is therefore safe on every convergence.
systemctl is-active --quiet cloudflared-claude-master.service || systemctl start cloudflared-claude-master.service || true

# ---------------------------------------------------------------- cloudwatch agent
# Receives the proxy's OTLP metrics on 127.0.0.1:4318 (the agent turns cumulative counters into deltas),
# publishes them in the ${local.claude_master_metrics_namespace} namespace, adds memory and swap (this box ran out of memory
# at its first size), and ships the proxy's log file to ${local.claude_master_log_group}.
CWA_VERSION='${local.claude_master_cwagent_version}'
CWA_SHA='${local.claude_master_cwagent_sha256_arm64}'
if ! dpkg-query -W -f='$${Version}' amazon-cloudwatch-agent 2>/dev/null | grep -q "^$CWA_VERSION"; then
  if curl -fsSL --retry 3 "https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/arm64/$CWA_VERSION/amazon-cloudwatch-agent.deb" -o /tmp/cwagent.deb \
     && echo "$CWA_SHA  /tmp/cwagent.deb" | sha256sum -c - >/dev/null; then
    dpkg -i /tmp/cwagent.deb >/dev/null 2>&1 || apt-get install -y -f >/dev/null 2>&1
  else
    echo "ERROR: cloudwatch agent $CWA_VERSION did not download or did not match its sha256; metrics and logs will not ship" >&2
  fi
  rm -f /tmp/cwagent.deb
fi
if [ -d /opt/aws/amazon-cloudwatch-agent/etc ]; then
  cat > /tmp/cwagent.json <<'CWACONF'
{
  "agent": { "metrics_collection_interval": 60, "run_as_user": "root", "omit_hostname": true },
  "metrics": {
    "namespace": "${local.claude_master_metrics_namespace}",
    "metrics_collected": {
      "otlp": { "http_endpoint": "127.0.0.1:4318" },
      "mem": { "measurement": ["mem_used_percent"] },
      "swap": { "measurement": ["swap_used_percent"] }
    }
  },
  "logs": { "logs_collected": { "files": { "collect_list": [
    { "file_path": "/var/log/claude-master/server*.log", "log_group_name": "${local.claude_master_log_group}", "log_stream_name": "{instance_id}" }
  ] } } }
}
CWACONF
  # Reload only when the configuration changed (or the agent is not running): a no-op convergence touches nothing.
  # Compared with OUR copy: the agent moves the file it is given into its own amazon-cloudwatch-agent.d/, so
  # its original path never holds the config again.
  # The copy is recorded only AFTER the agent accepted the new config: if translation or start fails, the next
  # convergence still sees a difference and tries again, instead of believing the old config is the new one.
  if ! cmp -s /tmp/cwagent.json /etc/claude-master/cwagent.json || ! systemctl is-active --quiet amazon-cloudwatch-agent; then
    cp /tmp/cwagent.json /tmp/cwagent.apply.json
    if /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 \
         -c file:/tmp/cwagent.apply.json -s >/dev/null 2>&1; then
      install -m 0644 /tmp/cwagent.json /etc/claude-master/cwagent.json
    else
      echo "WARNING: cloudwatch agent did not accept its configuration; it will be tried again at the next convergence" >&2
    fi
    rm -f /tmp/cwagent.apply.json
  fi
  rm -f /tmp/cwagent.json
fi

# ---------------------------------------------------------------- operator helpers
# A login is interactive (paste a code); run it as the service account so the profile is its own.
cat > /usr/local/bin/claude-master-login <<'EOF'
#!/bin/bash
[ $# -eq 1 ] || { echo "usage: claude-master-login PROFILE" >&2; exit 2; }
exec runuser -u claude-master -- env HOME=/var/lib/claude-master /usr/local/bin/claude-master login "$1"
EOF
chmod 0755 /usr/local/bin/claude-master-login

# Signs a client's certificate request (base64 on stdin) and prints the client certificate and then
# the CA certificate. scripts/claude-master-enroll.sh calls this over SSM; it is an admin action.
cat > /usr/local/bin/claude-master-sign <<'EOF'
#!/bin/bash
set -euo pipefail
days=$${1:-30}
tmp=$(mktemp -d); chmod 755 "$tmp"; trap 'rm -rf "$tmp"' EXIT
base64 -d > "$tmp/request.csr"; chmod 644 "$tmp/request.csr"
runuser -u claude-master -- env HOME=/var/lib/claude-master /usr/local/bin/claude-master issue --state-dir /var/lib/claude-master/state --request "$tmp/request.csr" --days "$days"
cat /var/lib/claude-master/state/ca.pem
EOF
chmod 0755 /usr/local/bin/claude-master-sign

# Which logins exist, whether the server is up, and the version it runs versus the one installed.
cat > /usr/local/bin/claude-master-status <<'EOF'
#!/bin/bash
for p in ${join(" ", local.claude_master_profiles)}; do
  if [ -d "/var/lib/claude-master/.local/share/claude-master/profiles/$p/current" ]; then echo "login $p: present"; else echo "login $p: MISSING"; fi
done
active=$(cat /etc/claude-master/active-color 2>/dev/null || echo none)
if systemctl is-active --quiet claude-master-server.service; then
  echo "server: active (before Envoy; sudo claude-master-rollout moves it behind Envoy)"
else
  echo "server: $(systemctl is-active "claude-master-server@$active") ($active)"
fi
clusters=$(curl -fsS http://127.0.0.1:9901/clusters 2>/dev/null)
for c in ${join(" ", keys(local.claude_master_colors))}; do
  pid=$(systemctl show -p MainPID --value "claude-master-server@$c")
  running=""; [ "$pid" != 0 ] && running=$(sha256sum "/proc/$pid/exe" 2>/dev/null | cut -c1-16)
  port=$(case $c in ${join(" ", [for c, v in local.claude_master_colors : "${c}) echo ${v.port} ;;"])} esac)
  conns=$(grep -E "^cert::${local.claude_master_server_ip}:$port::cx_active::" <<<"$clusters" | sed 's/.*:://')
  echo "  $c: $(systemctl is-active "claude-master-server@$c")$${running:+  running $running}$${conns:+  envoy connections $conns}"
done
echo "envoy: $(systemctl is-active envoy)   new connections go to: $(grep -o 'port_value: [0-9]*' /etc/envoy/eds/cert.yaml 2>/dev/null | grep -o '[0-9]*$')"
echo "cloudwatch agent: $(systemctl is-active amazon-cloudwatch-agent)   logs: $(ls -1 /var/log/claude-master 2>/dev/null | grep -E '^server.*log$' | tr '\n' ' ')"
if ss -ltn 2>/dev/null | grep -q "127.0.0.1:${local.claude_master_open_port} "; then
  echo "open listener: listening"
else
  echo "open listener: NOT listening (Envoy is stopped, or no server is running yet: sudo claude-master-rollout)"
fi
echo "installed: $(sha256sum /usr/local/bin/claude-master | cut -c1-16)  pinned: ${substr(local.claude_master_sha256_aarch64, 0, 16)}  ($TAG)"
EOF
sed -i "s/\$TAG/$TAG/" /usr/local/bin/claude-master-status
chmod 0755 /usr/local/bin/claude-master-status
SCRIPT
}

resource "aws_s3_object" "claude_master_server_user_data" {
  count        = var.enable_claude_master_server ? 1 : 0
  bucket       = aws_s3_bucket.dev_scripts.id
  key          = "user-data/claude-master-server.sh"
  content      = local.claude_master_server_user_data
  content_type = "text/x-shellscript"
  tags         = { Name = "claude-master-server-user-data" }
}

resource "aws_instance" "claude_master_server" {
  count                       = var.enable_claude_master_server ? 1 : 0
  ami                         = var.firecracker_ami # the same Ubuntu 24.04 ARM64 image as the fleet
  instance_type               = "t4g.micro"
  key_name                    = var.firecracker_key_name
  subnet_id                   = aws_subnet.subnet_a.id
  private_ip                  = local.claude_master_server_ip
  associate_public_ip_address = true # outbound only: the security group admits no one from outside
  vpc_security_group_ids      = [aws_security_group.claude_master_server[0].id]
  iam_instance_profile        = aws_iam_instance_profile.claude_master_server[0].name

  root_block_device {
    volume_size           = 20
    volume_type           = "gp3"
    delete_on_termination = false
    encrypted             = true
  }

  metadata_options {
    http_tokens = "required"
  }

  # Thin bootstrap, like every other box: the real script is the S3 object above, re-fetched and
  # re-run by hand to converge a running box. The AWS CLI has to be installed inline first.
  user_data = base64encode(<<-BOOTSTRAP
    #!/bin/bash
    export DEBIAN_FRONTEND=noninteractive
    # Swap BEFORE any apt run: a small box's first boot is the memory peak.
    if ! swapon --show=NAME --noheadings | grep -q .; then
      fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
      grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    apt-get update -y || true
    apt-get install -y unzip curl || true
    if ! command -v aws >/dev/null 2>&1; then
      curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip
      cd /tmp && unzip -qo awscliv2.zip && ./aws/install && rm -rf /tmp/aws /tmp/awscliv2.zip
    fi
    aws s3 cp s3://ejc3-dev-scripts/user-data/claude-master-server.sh /tmp/user_data.sh --region us-west-1
    chmod +x /tmp/user_data.sh && /tmp/user_data.sh
  BOOTSTRAP
  )

  # The bootstrap names the S3 object by a literal path, so Terraform needs the edge said aloud.
  depends_on = [aws_s3_object.claude_master_server_user_data]

  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      ami,
      user_data,
      user_data_base64,
    ]
  }

  tags = {
    Name    = "claude-master-server"
    Purpose = "Shared Claude subscription pool; clients authenticate with certificates"
  }
}

# CONVERGENCE. The instance ignores user_data changes, and cloud-init does not run per-instance
# user data again after a resize or a stop/start, so a script edit, a pin bump or a box whose first
# boot failed would change nothing on the running machine. This re-runs the bootstrap through SSM
# whenever the script, the helper or the instance changes. The bootstrap is idempotent and never
# restarts a running server or Envoy: a new binary is installed atomically and the running server keeps
# the old one until `sudo claude-master-rollout` (`claude-master-status` shows running versus pinned).
resource "terraform_data" "claude_master_server_converge" {
  count = var.enable_claude_master_server ? 1 : 0

  triggers_replace = {
    instance = aws_instance.claude_master_server[0].id
    type     = aws_instance.claude_master_server[0].instance_type
    script   = sha256(local.claude_master_server_user_data)
    helper   = filesha256("${path.module}/scripts/ssm-claude-master-server.sh")
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/scripts/ssm-claude-master-server.sh ${aws_instance.claude_master_server[0].id} ${var.aws_region}"
  }

  # The bootstrap starts cloudflared, which reads the connector token with the server's role: both
  # must exist first (claude-master-tunnel.tf), or its first start fails and waits for a retry.
  depends_on = [
    aws_s3_object.claude_master_server_user_data,
    aws_secretsmanager_secret_version.claude_master_tunnel_token,
    aws_iam_role_policy.claude_master_server_tunnel_token,
  ]
}

output "claude_master_server_address" {
  description = "Where client boxes point `claude-master connect --server`"
  value       = "${local.claude_master_server_ip}:${local.claude_master_server_port}"
}

# Alarms to the shared alert topic, like every other box: the instance itself, and the two signals that
# took nextjs-dev down (memory and swap; the CloudWatch agent publishes them, see below).
resource "aws_cloudwatch_metric_alarm" "claude_master_server_status" {
  count               = var.enable_claude_master_server ? 1 : 0
  alarm_name          = "claude-master-server-status-check"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "StatusCheckFailed"
  namespace           = "AWS/EC2"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "The claude-master server's instance status check failed: every box that uses the shared Claude subscriptions loses inference until it is back."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"

  dimensions = { InstanceId = aws_instance.claude_master_server[0].id }
}

# Memory and swap (the CloudWatch agent publishes them). The box ran out of memory at its first size.
# The agent adds a `host` dimension to host metrics unless told not to; the config sets omit_hostname so these
# two series have NO dimensions, which is what the alarms select. An alarm that selects dimensions the series
# does not have sees no data and, with notBreaching, stays green forever.
resource "aws_cloudwatch_metric_alarm" "claude_master_server_memory" {
  count               = var.enable_claude_master_server ? 1 : 0
  alarm_name          = "claude-master-server-memory-pressure"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "mem_used_percent"
  namespace           = local.claude_master_metrics_namespace
  period              = 300
  statistic           = "Average"
  threshold           = 85
  alarm_description   = "claude-master server memory above 85% on 1 GB. The proxy, cloudflared and the CloudWatch agent share it; the next step is swapping."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  # No data is not healthy: a dead agent must say so rather than leave the alarm green.
  treat_missing_data        = "missing"
  insufficient_data_actions = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "claude_master_server_swap" {
  count               = var.enable_claude_master_server ? 1 : 0
  alarm_name          = "claude-master-server-swapping"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "swap_used_percent"
  namespace           = local.claude_master_metrics_namespace
  period              = 300
  statistic           = "Average"
  threshold           = 50
  alarm_description   = "claude-master server is paging heavily; swap lives on the root volume."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  # No data is not healthy: a dead agent must say so rather than leave the alarm green.
  treat_missing_data        = "missing"
  insufficient_data_actions = [aws_sns_topic.cost_alerts.arn]
}

# Two things only a person can fix, found in the proxy's own log. The wording is the program's fixed text.
locals {
  claude_master_log_alarms = {
    CredentialRejected = {
      pattern     = "\"profile credential rejected by Anthropic\""
      description = "Anthropic rejected a subscription's login (401/403): it expired or was revoked. Redo it: scripts/claude-master-login.sh --server (the other subscriptions keep serving)."
    }
    NoAccountAvailable = {
      pattern     = "\"no inference account could be chosen\""
      description = "No subscription (and no API-key backup) could take a request: every one is rate limited, exhausted or its login failed. Clients are being refused."
    }
  }
}

resource "aws_cloudwatch_log_metric_filter" "claude_master_server" {
  for_each       = var.enable_claude_master_server ? local.claude_master_log_alarms : {}
  name           = "claude-master-${each.key}"
  log_group_name = aws_cloudwatch_log_group.claude_master_server.name
  pattern        = each.value.pattern
  metric_transformation {
    name          = each.key
    namespace     = "${local.claude_master_metrics_namespace}/Logs"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "claude_master_server_log" {
  for_each            = var.enable_claude_master_server ? local.claude_master_log_alarms : {}
  alarm_name          = "claude-master-${each.key}"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = each.key
  namespace           = "${local.claude_master_metrics_namespace}/Logs"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = each.value.description
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  depends_on          = [aws_cloudwatch_log_metric_filter.claude_master_server]
}

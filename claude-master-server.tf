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
# the three paste-a-code logins here. They are never copied from another box (a rotating login
# cannot be shared). The service stays idle until every profile below has a login, then
# `systemctl start claude-master-server`.
#
# CERTIFICATES. The CA key never leaves /var/lib/claude-master/state. A client box makes its own
# key and a certificate request; scripts/claude-master-enroll.sh gets the request signed over SSM
# (an admin action) and installs the certificate. Certificates last 30 days.
#
# TO ROLL THE BINARY FORWARD: publish a release of ejc3/CLIProxyAPI, change the tag and the sha256,
# apply, then re-run the bootstrap. The binary is swapped atomically but a running server keeps the
# old one until it is restarted -- a deliberate decision, like codex-restart.

variable "enable_claude_master_server" {
  description = "Enable the shared claude-master server box"
  type        = bool
  default     = true
}

locals {
  claude_master_tag            = "claude-master-66b3cee"
  claude_master_sha256_aarch64 = "d5459e55b54ebd6bc1b09e0858f19e493fb8f4d87be3347b415b0fe44aee0943"

  claude_master_server_ip   = "10.0.1.50" # in subnet_a; client certificates name this address
  claude_master_server_port = 8443

  # Order is the fallback order. The first login is the one a fresh session prefers when quotas tie.
  claude_master_profiles = ["claude-connor", "claude-ejc3", "claude-colton"]

  claude_master_admin_cidrs = concat(
    var.enable_jumpbox ? ["${aws_instance.jumpbox[0].private_ip}/32"] : [],
    var.enable_jumpbox_2 ? ["${aws_instance.jumpbox_2[0].private_ip}/32"] : [],
  )
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
exec /usr/local/bin/claude-master serve ${local.claude_master_profiles[0]}${join("", [for p in slice(local.claude_master_profiles, 1, length(local.claude_master_profiles)) : " --next-profile ${p}"])} \
  --listen ${local.claude_master_server_ip}:${local.claude_master_server_port} --state-dir /var/lib/claude-master/state
EOF
chmod 0755 /usr/local/bin/claude-master-serve

cat > /etc/systemd/system/claude-master-server.service <<'EOF'
[Unit]
Description=claude-master server (shared Claude subscription pool)
After=network-online.target
Wants=network-online.target
# Idle until every login exists: `scripts/claude-master-login.sh --server`, then start it.
${join("\n", [for p in local.claude_master_profiles : "ConditionPathExists=/var/lib/claude-master/.local/share/claude-master/profiles/${p}/current"])}

[Service]
User=claude-master
Group=claude-master
Environment=HOME=/var/lib/claude-master
ExecStart=/usr/local/bin/claude-master-serve
Restart=on-failure
RestartSec=10
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
systemctl enable claude-master-server.service >/dev/null 2>&1 || true

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
echo "server: $(systemctl is-active claude-master-server)"
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

output "claude_master_server_address" {
  description = "Where client boxes point `claude-master connect --server`"
  value       = "${local.claude_master_server_ip}:${local.claude_master_server_port}"
}

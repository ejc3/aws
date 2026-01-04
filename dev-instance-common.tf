# Common user_data scripts for development instances
# Shared between firecracker-dev.tf and x86-dev.tf

# Generate SSH key pair for dev servers to access runners
resource "tls_private_key" "dev_to_runner" {
  algorithm = "ED25519"
}

# Store private key in SSM (encrypted) for dev servers to fetch
resource "aws_ssm_parameter" "dev_ssh_private_key" {
  name  = "/dev-servers/runner-ssh-key"
  type  = "SecureString"
  value = tls_private_key.dev_to_runner.private_key_openssh
  tags = {
    Name = "dev-to-runner-ssh-key"
  }
}

# ============================================
# IAM Role for Dev Servers (restricted, not admin)
# ============================================

resource "aws_iam_role" "dev_server" {
  name = "dev-server-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  tags = { Name = "dev-server-role" }
}

resource "aws_iam_instance_profile" "dev_server" {
  name = "dev-server-profile"
  role = aws_iam_role.dev_server.name
}

# SSM managed instance (for dev server itself)
resource "aws_iam_role_policy_attachment" "dev_server_ssm" {
  role       = aws_iam_role.dev_server.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Dev server permissions
resource "aws_iam_role_policy" "dev_server" {
  name = "dev-server-policy"
  role = aws_iam_role.dev_server.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SSMSendCommandToRunners"
        Effect = "Allow"
        Action = "ssm:SendCommand"
        Resource = [
          "arn:aws:ssm:us-west-1::document/AWS-RunShellScript",
          "arn:aws:ec2:us-west-1:928413605543:instance/*"
        ]
        Condition = {
          StringEquals = {
            "ssm:resourceTag/Role" = "github-runner"
          }
        }
      },
      {
        Sid    = "SSMSendCommandToAMIBuilders"
        Effect = "Allow"
        Action = "ssm:SendCommand"
        Resource = [
          "arn:aws:ssm:us-west-1::document/AWS-RunShellScript",
          "arn:aws:ec2:us-west-1:928413605543:instance/*"
        ]
        Condition = {
          StringEquals = {
            "ssm:resourceTag/Name" = "ami-builder-temp"
          }
        }
      },
      {
        Sid      = "SSMSendCommandDocument"
        Effect   = "Allow"
        Action   = "ssm:SendCommand"
        Resource = "arn:aws:ssm:us-west-1::document/AWS-RunShellScript"
      },
      {
        Sid    = "SSMGetCommandResults"
        Effect = "Allow"
        Action = [
          "ssm:GetCommandInvocation",
          "ssm:ListCommandInvocations",
          "ssm:ListCommands"
        ]
        Resource = "*"
      },
      {
        Sid    = "SSMDescribeInstances"
        Effect = "Allow"
        Action = "ssm:DescribeInstanceInformation"
        Resource = "*"
      },
      {
        Sid    = "SSMGetRunnerSSHKey"
        Effect = "Allow"
        Action = "ssm:GetParameter"
        Resource = [
          aws_ssm_parameter.dev_ssh_private_key.arn,
          "arn:aws:ssm:us-west-1:928413605543:parameter/github-pat-ejc3"
        ]
      },
      {
        Sid    = "SecretsManagerGitHubPAT"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = "arn:aws:secretsmanager:us-west-1:928413605543:secret:github-pat-ejc3*"
      },
      {
        Sid    = "EC2ReadOnly"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:DescribeImages",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeVpcs",
          "ec2:DescribeSubnets",
          "ec2:DescribeVolumes",
          "ec2:DescribeSnapshots",
          "ec2:DescribeTags",
          "ec2:DescribeKeyPairs",
          "ec2:DescribeNetworkInterfaces"
        ]
        Resource = "*"
      },
      {
        Sid    = "EC2ManageRunners"
        Effect = "Allow"
        Action = [
          "ec2:StartInstances",
          "ec2:StopInstances",
          "ec2:TerminateInstances",
          "ec2:RebootInstances"
        ]
        Resource = "arn:aws:ec2:us-west-1:928413605543:instance/*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/Role" = "github-runner"
          }
        }
      }
    ]
  })
}

locals {
  # GitHub CLI authentication from Secrets Manager
  gh_auth_script = <<-SCRIPT
    # ============================================
    # GitHub CLI authentication (from Secrets Manager)
    # ============================================
    sudo -u ubuntu bash << 'GH_AUTH_SETUP'
    set -euxo pipefail

    # Fetch GitHub PAT from Secrets Manager
    GH_TOKEN=$(aws secretsmanager get-secret-value \
      --secret-id github-pat-ejc3 \
      --region us-west-1 \
      --query SecretString \
      --output text)

    # Configure gh CLI
    mkdir -p ~/.config/gh
    cat > ~/.config/gh/hosts.yml << EOF
github.com:
    users:
        ejc3:
            oauth_token: $GH_TOKEN
    oauth_token: $GH_TOKEN
    user: ejc3
EOF

    # Set up git credential helper
    gh auth setup-git
    GH_AUTH_SETUP
  SCRIPT

  # Claude Code Sync installation and initialization
  claude_sync_script = <<-SCRIPT
    # ============================================
    # Claude Code Sync (conversation history backup)
    # ============================================
    sudo -u ubuntu bash << 'CLAUDE_SYNC_SETUP'
    set -euxo pipefail

    # Clone and build from feature branch
    git clone -b feature/non-interactive-init https://github.com/ejc3/claude-code-sync.git ~/src/claude-code-sync
    cd ~/src/claude-code-sync
    ~/.cargo/bin/cargo install --path .

    # Create init config for non-interactive setup
    cat > ~/.claude-code-sync-init.toml << 'INITCFG'
repo_path = "~/claude-history-sync"
remote_url = "https://github.com/ejc3/claude-code-history.git"
clone = true
exclude_attachments = true
enable_lfs = true
INITCFG

    # Initialize (will clone the history repo)
    ~/.cargo/bin/claude-code-sync init || true
    CLAUDE_SYNC_SETUP
  SCRIPT

  # Ghostty terminal terminfo installation
  ghostty_terminfo_script = <<-SCRIPT
    # ============================================
    # Ghostty terminfo (for proper terminal support)
    # ============================================
    curl -sL https://raw.githubusercontent.com/ghostty-org/ghostty/main/src/terminfo/ghostty.terminfo | tic -x -
  SCRIPT

  # Unattended upgrades for automatic security updates
  unattended_upgrades_script = <<-SCRIPT
    # ============================================
    # Unattended Upgrades (automatic security updates)
    # ============================================
    apt-get install -y unattended-upgrades

    # Configure unattended-upgrades
    cat > /etc/apt/apt.conf.d/50unattended-upgrades << 'UNATTENDED'
Unattended-Upgrade::Allowed-Origins {
    "$${distro_id}:$${distro_codename}";
    "$${distro_id}:$${distro_codename}-security";
    "$${distro_id}ESMApps:$${distro_codename}-apps-security";
    "$${distro_id}ESM:$${distro_codename}-infra-security";
    "$${distro_id}:$${distro_codename}-updates";
};

// Remove unused kernel packages
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";

// Remove unused dependencies
Unattended-Upgrade::Remove-Unused-Dependencies "true";

// Auto-reboot if needed (at 3am)
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot-Time "03:00";
UNATTENDED

    # Enable automatic updates
    cat > /etc/apt/apt.conf.d/20auto-upgrades << 'AUTOUPGRADE'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::Download-Upgradeable-Packages "1";
APT::Periodic::AutocleanInterval "7";
AUTOUPGRADE

    # Enable and start the timer
    systemctl enable unattended-upgrades
    systemctl start unattended-upgrades
  SCRIPT
  # Claude Code Sync cron job (every 5 minutes, email on failure)
  claude_sync_cron_script = <<-SCRIPT
    # ============================================
    # Claude Code Sync cron job
    # ============================================

    # Create sync script
    cat > /home/ubuntu/.local/bin/claude-sync-cron.sh << 'SYNCSCRIPT'
#!/bin/bash
set -euo pipefail

LOGFILE="/tmp/claude-sync-cron.log"
HOSTNAME=$(hostname)
SNS_TOPIC="arn:aws:sns:us-west-1:928413605543:cost-alerts"

log() {
    echo "[$(date -Iseconds)] $1" >> "$LOGFILE"
}

notify_failure() {
    local msg="$1"
    log "FAILURE: $msg"
    aws sns publish \
        --topic-arn "$SNS_TOPIC" \
        --subject "claude-code-sync FAILED on $HOSTNAME" \
        --message "$msg" \
        --region us-west-1 || true
}

# Rotate log if > 1MB
if [ -f "$LOGFILE" ] && [ $(stat -f%z "$LOGFILE" 2>/dev/null || stat -c%s "$LOGFILE") -gt 1048576 ]; then
    mv "$LOGFILE" "$LOGFILE.old"
fi

log "Starting sync"

# Pull first
if ! /home/ubuntu/.cargo/bin/claude-code-sync pull >> "$LOGFILE" 2>&1; then
    notify_failure "Pull failed on $HOSTNAME. Check $LOGFILE for details."
    exit 1
fi

# Then push
if ! /home/ubuntu/.cargo/bin/claude-code-sync push >> "$LOGFILE" 2>&1; then
    notify_failure "Push failed on $HOSTNAME. Check $LOGFILE for details."
    exit 1
fi

log "Sync completed successfully"
SYNCSCRIPT
    chmod +x /home/ubuntu/.local/bin/claude-sync-cron.sh
    chown ubuntu:ubuntu /home/ubuntu/.local/bin/claude-sync-cron.sh

    # Install crontab for ubuntu user (every 5 minutes)
    sudo -u ubuntu bash -c 'crontab -l 2>/dev/null | grep -v claude-sync-cron || true; echo "*/5 * * * * /home/ubuntu/.local/bin/claude-sync-cron.sh"' | sudo -u ubuntu crontab -
  SCRIPT

  # Console logging for EC2 get-console-output debugging
  console_logging_script = <<-SCRIPT
    # ============================================
    # Console logging (for debugging via EC2 get-console-output)
    # ============================================
    # Forward important syslog messages to serial console
    cat >> /etc/rsyslog.d/50-console.conf << 'RSYSLOG'
# Log critical messages to serial console for EC2 get-console-output
*.emerg;*.alert;*.crit;*.err                    /dev/ttyS0
kern.*                                           /dev/ttyS0
RSYSLOG
    systemctl restart rsyslog || true

    # Enable kernel messages to console
    echo "kernel.printk = 7 4 1 7" >> /etc/sysctl.conf
    sysctl -w kernel.printk="7 4 1 7" || true
  SCRIPT

  # SSH key for accessing runners (fetched from SSM)
  runner_ssh_key_script = <<-SCRIPT
    # ============================================
    # SSH key for accessing GitHub runners
    # ============================================
    sudo -u ubuntu bash << 'RUNNER_SSH_SETUP'
    set -euxo pipefail
    mkdir -p ~/.ssh
    chmod 700 ~/.ssh

    # Fetch private key from SSM
    aws ssm get-parameter \
      --name /dev-servers/runner-ssh-key \
      --with-decryption \
      --region us-west-1 \
      --query Parameter.Value \
      --output text > ~/.ssh/runner_key
    chmod 600 ~/.ssh/runner_key

    # Add to SSH config for easy access
    cat >> ~/.ssh/config << 'SSHCONFIG'

# GitHub runners (use: ssh runner@<ip>)
Host runner-*
    User ubuntu
    IdentityFile ~/.ssh/runner_key
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
SSHCONFIG
    chmod 600 ~/.ssh/config
    RUNNER_SSH_SETUP
  SCRIPT

  # Combined script for GitHub auth + Claude Sync + Ghostty terminfo + Cron + Unattended Upgrades + Runner SSH + Console logging
  gh_and_claude_sync_script = join("\n", [
    local.console_logging_script,
    local.gh_auth_script,
    local.claude_sync_script,
    local.ghostty_terminfo_script,
    local.claude_sync_cron_script,
    local.unattended_upgrades_script,
    local.runner_ssh_key_script
  ])

  # Bootstrap script that fetches full user_data from SSM (to bypass 16KB limit)
  dev_bootstrap_script = <<-SCRIPT
    #!/bin/bash
    set -euxo pipefail
    # Fetch and execute full user_data from SSM
    PARAM_NAME=$1
    SCRIPT_CONTENT=$(aws ssm get-parameter --name "$PARAM_NAME" --region us-west-1 --query Parameter.Value --output text | base64 -d)
    echo "$SCRIPT_CONTENT" > /tmp/user_data.sh
    chmod +x /tmp/user_data.sh
    /tmp/user_data.sh
  SCRIPT
}

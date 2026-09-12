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
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })

  tags = { Name = "dev-server-role" }
}

resource "aws_iam_instance_profile" "dev_server" {
  name = "dev-server-profile"
  role = aws_iam_role.dev_server.name
}

# Preserve agent/session connectivity without account-wide Parameter Store reads.
resource "aws_iam_role_policy_attachment" "dev_server_ssm" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.ssm_managed_instance.arn

  lifecycle {
    create_before_destroy = true
  }
}

locals {
  # These explicit ENIs survive stop/start. Derive the allowlist from Terraform so
  # replacements and disabled-host gates cannot leave authority over stale ENIs.
  dev_server_ipv6_network_interface_arns = concat(
    aws_network_interface.firecracker_dev[*].arn,
    aws_network_interface.x86_dev[*].arn,
  )
}

# Dev server permissions
resource "aws_iam_role_policy" "dev_server" {
  name = "dev-server-policy"
  role = aws_iam_role.dev_server.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
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
        Sid      = "SSMSendCommandDocument"
        Effect   = "Allow"
        Action   = "ssm:SendCommand"
        Resource = "arn:aws:ssm:us-west-1::document/AWS-RunShellScript"
      },
      {
        # Run Command result/list APIs have no resource-level authorization. They
        # can expose admin-host command inputs/output even when SendCommand is
        # runner-scoped. Use the existing dev-to-runner SSH key for debug output.
        Sid    = "NeverReadAccountWideCommandOutput"
        Effect = "Deny"
        Action = [
          "ssm:GetCommandInvocation",
          "ssm:ListCommandInvocations",
          "ssm:ListCommands"
        ]
        Resource = "*"
      },
      {
        Sid      = "SSMDescribeInstances"
        Effect   = "Allow"
        Action   = "ssm:DescribeInstanceInformation"
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
        Resource = [
          "arn:aws:secretsmanager:us-west-1:928413605543:secret:github-pat-ejc3*",
          # Dev-server-to-dev-server hop key. Reaches only other dev boxes -- it is
          # deliberately not authorized on the jumpbox. See dev-hop-key.tf.
          aws_secretsmanager_secret.dev_hop.arn,
        ]
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
          "ec2:DescribeNetworkInterfaces",
          "ec2:DescribeNatGateways",
          "ec2:DescribeAddresses",
          "ec2:DescribeRouteTables",
          # Spot-market intel: price history, placement scores, and the instance-type
          # catalog. Read-only market data -- lets pbox/agents on a dev box reason about
          # capacity and cost BEFORE delegating a launch to the jumpbox, without widening
          # the actual launch permissions (still no ec2:RunInstances here).
          "ec2:DescribeSpotPriceHistory",
          "ec2:GetSpotPlacementScores",
          "ec2:DescribeSpotInstanceRequests",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeInstanceTypeOfferings",
          "ec2:DescribeAvailabilityZones"
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
      },
      {
        Sid    = "S3ReadDevScripts"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          "arn:aws:s3:::ejc3-dev-scripts",
          "arn:aws:s3:::ejc3-dev-scripts/*"
        ]
      },
      {
        Sid    = "SESSendEmail"
        Effect = "Allow"
        Action = [
          "ses:SendEmail",
          "ses:SendRawEmail"
        ]
        Resource = "*"
      },
      {
        Sid    = "CodeArtifact"
        Effect = "Allow"
        Action = [
          "codeartifact:GetAuthorizationToken",
          "codeartifact:GetRepositoryEndpoint",
          "codeartifact:ReadFromRepository",
          "codeartifact:PublishPackageVersion",
          "codeartifact:PutPackageMetadata",
          "codeartifact:DescribePackageVersion",
          "codeartifact:ListPackageVersions",
          "codeartifact:ListPackages",
          "codeartifact:DeletePackageVersions"
        ]
        Resource = "*"
      },
      {
        Sid      = "CodeArtifactToken"
        Effect   = "Allow"
        Action   = "sts:GetServiceBearerToken"
        Resource = "*"
        Condition = {
          StringEquals = {
            "sts:AWSServiceName" = "codeartifact.amazonaws.com"
          }
        }
      },
      {
        # Amazon Bedrock "Mantle" inference endpoint (OpenAI/Anthropic-compatible
        # API used by Claude Code). Region wildcard so it works in every Claude
        # region; account-pinned for least privilege. CreateInference runs the
        # request, CountTokens backs Claude's token counting, Get*/List* cover
        # model discovery (GET /v1/models) so Claude Code starts cleanly.
        Sid    = "BedrockMantleInference"
        Effect = "Allow"
        Action = [
          "bedrock-mantle:CreateInference",
          "bedrock-mantle:CountTokens",
          "bedrock-mantle:Get*",
          "bedrock-mantle:List*"
        ]
        Resource = "arn:aws:bedrock-mantle:*:928413605543:project/*"
      },
      {
        # First-use model subscription for both Bedrock inference paths (Mantle
        # for Claude Code, bedrock-runtime for opencode/tmux). Left unconditional
        # on purpose: the Marketplace auto-subscribe that Bedrock triggers on
        # first model use is not reliably tagged with a Bedrock aws:CalledVia, so
        # any CalledVia condition makes the subscribe intermittently fail. This
        # mirrors AWS's AmazonBedrockFullAccess managed policy, which grants these
        # Marketplace actions on "*" with no condition. Subscribe +
        # ViewSubscriptions only (no Unsubscribe).
        Sid    = "BedrockMarketplace"
        Effect = "Allow"
        Action = [
          "aws-marketplace:Subscribe",
          "aws-marketplace:ViewSubscriptions"
        ]
        Resource = "*"
      },
      {
        # Classic bedrock-runtime path (Invoke/Converse) used by opencode and
        # plain claude/tmux, separate from the Mantle endpoint above. Claude is
        # invoked via cross-region inference profiles (us.anthropic.*), which
        # require BOTH the inference-profile ARN and the underlying
        # foundation-model ARNs in every routed region — hence the region
        # wildcard. Foundation models are scoped to anthropic.* for least
        # privilege; inference profiles are account-pinned.
        Sid    = "BedrockRuntimeInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
          "bedrock:Converse",
          "bedrock:ConverseStream"
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/anthropic.*",
          "arn:aws:bedrock:*:928413605543:inference-profile/*"
        ]
      }
      ], length(local.dev_server_ipv6_network_interface_arns) > 0 ? [{
        Sid    = "EC2AssignIpv6Prefix"
        Effect = "Allow"
        Action = [
          "ec2:AssignIpv6Addresses",
          "ec2:UnassignIpv6Addresses"
        ]
        Resource = local.dev_server_ipv6_network_interface_arns
    }] : [])
  })
}

# Retire the unmanaged predecessor without deleting its role/profile or history.
# September 12, 2026: no instance, launch configuration, or launch-template version
# references its profile in any of the 17 enabled regions. IAM last used it on
# December 31, 2025. Keep the existing broad grants inert, including any later
# accidental attachment; the active metal role above is deliberately not this role.
resource "aws_iam_role_policy" "retired_legacy_dev_server" {
  name = "RetiredIdentityDenyAll"
  role = "aws-infrastructure-dev-instance-role"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "RetiredIdentityDenyAll"
      Effect   = "Deny"
      Action   = "*"
      Resource = "*"
    }]
  })

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = data.aws_caller_identity.current.account_id == "928413605543"
      error_message = "The legacy dev identity retirement belongs only to the main fleet account."
    }
  }
}

locals {
  # GitHub CLI authentication from Secrets Manager -- ONLY if not already logged in.
  #
  # THE BUG THIS FIXES: this used to unconditionally overwrite ~/.config/gh/hosts.yml
  # every time it ran -- which is every boot, AND every dev-selfupdate.sh
  # re-convergence. That silently destroyed any real personal `gh auth login` session
  # (the broad repo/workflow/gist-scoped token a human gets from the interactive device
  # flow) and replaced it with this single-repo sync PAT, with zero warning. Caught live:
  # running dev-selfupdate.sh to deploy an unrelated fix clobbered a working personal
  # login mid-session, made GitHub push access look broken when it never was, and cost a
  # long debugging detour to trace back to this line.
  #
  # THE FIX: only set up the PAT-based login if `gh` is not ALREADY authenticated. A
  # fresh box still gets a working `gh` for free (first boot, nobody has logged in yet).
  # Once a human runs `gh auth login` themselves -- on this box or any box -- that login
  # is never touched again, on this boot or any future one. claude-code-sync's own auth
  # no longer depends on this at all; see claude_sync_git_credential_script below.
  gh_auth_script = <<-SCRIPT
    # ============================================
    # GitHub CLI authentication (from Secrets Manager, skipped if already logged in)
    # ============================================
    sudo -u ubuntu bash << 'GH_AUTH_SETUP'
    set -euo pipefail

    if gh auth status >/dev/null 2>&1; then
      echo "gh already authenticated -- leaving the existing login alone"
    else
      # NOTE: deliberately NO -x for this branch. It expands a GitHub PAT, and xtrace
      # would echo `GH_TOKEN=github_pat_...` verbatim into the setup log.
      set +x
      GH_TOKEN=$(aws secretsmanager get-secret-value \
        --secret-id github-pat-ejc3 \
        --region us-west-1 \
        --query SecretString \
        --output text)

      mkdir -p ~/.config/gh
      cat > ~/.config/gh/hosts.yml << EOF
github.com:
    users:
        ejc3:
            oauth_token: $GH_TOKEN
    oauth_token: $GH_TOKEN
    user: ejc3
EOF

      gh auth setup-git
    fi
# NOTE: this terminator MUST stay at column 0. The heredoc above is `<< 'GH_AUTH_SETUP'`
# (no dash), so bash only accepts an unindented terminator -- and terraform's `<<-SCRIPT`
# strips the COMMON indentation of this block, which is 0 because the inner EOF body
# below is unindented. An indented terminator here silently swallows the entire rest of
# the setup script into `sudo -u ubuntu bash`, running it as ubuntu instead of root.
GH_AUTH_SETUP
  SCRIPT

  # Scoped git credential for claude-code-sync, INDEPENDENT of gh entirely.
  #
  # WHY THIS EXISTS SEPARATELY FROM gh_auth_script: claude-code-sync's automated cron
  # push/pull (claude_sync_cron_script) must always be able to reach
  # ejc3/claude-code-history, regardless of whatever `gh`'s login happens to be --
  # including nothing at all, since gh_auth_script above now leaves a personal login
  # alone rather than guaranteeing the PAT is in place. Scoping this to ONE exact URL via
  # git's own credential.<url>.helper, rather than gh's global hosts.yml/credential
  # helper, means it can never be clobbered by and never clobbers a personal `gh auth
  # login`, and never grants anything beyond that single repo. The token is fetched live
  # on every credential request, never written to disk, so a rotation takes effect
  # immediately with no re-run needed.
  #
  # claude-code-sync shells out to the real `git` binary (src/scm/git.rs), so it honors
  # this exactly the way plain `git push` does -- confirmed by reading its source.
  claude_sync_git_credential_script = <<-SCRIPT
    # ============================================
    # Scoped git credential for claude-code-sync (independent of gh auth)
    # ============================================
    sudo -u ubuntu bash << 'CCS_CRED_SETUP'
    set -euo pipefail
    git config --global credential."https://github.com/ejc3/claude-code-history.git".helper \
      '!f() { echo username=x-access-token; echo "password=$(aws secretsmanager get-secret-value --secret-id github-pat-ejc3 --region us-west-1 --query SecretString --output text)"; }; f'
CCS_CRED_SETUP
  SCRIPT

  # Claude Code Sync installation and initialization
  claude_sync_script = <<-SCRIPT
    # ============================================
    # Claude Code Sync (conversation history backup)
    # ============================================
    sudo -u ubuntu bash << 'CLAUDE_SYNC_SETUP'
    set -euxo pipefail

    # Clone and build from feature branch
    # Idempotent: this script now re-runs on every boot, and `git clone` into an
    # existing directory is a fatal error. Fetch into the existing checkout instead.
    if [ -d ~/src/claude-code-sync/.git ]; then
      cd ~/src/claude-code-sync
      git fetch origin feature/non-interactive-init
      git checkout -f feature/non-interactive-init
      git reset --hard origin/feature/non-interactive-init
    else
      git clone -b feature/non-interactive-init https://github.com/ejc3/claude-code-sync.git ~/src/claude-code-sync
      cd ~/src/claude-code-sync
    fi
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
# Terminator must stay at column 0 -- see the note on GH_AUTH_SETUP above.
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
    local.claude_sync_git_credential_script,
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

  # Persistent volume IDs for dev servers (manually swapped, not terraform-managed)
  # These are the root volumes we preserve across spot instance recreation
  arm_persistent_volume_arn = "arn:aws:ec2:us-west-1:928413605543:volume/vol-09e5c3cee32bb67dc"
  x86_persistent_volume_arn = "arn:aws:ec2:us-west-1:928413605543:volume/vol-071f114b67441e776"
}

# ============================================
# Backup Plan for Dev Server Persistent Volumes
# ============================================

resource "aws_backup_plan" "dev_servers" {
  name = "dev-servers-backup"

  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.ejc3_backup.name
    schedule          = "cron(0 6 * * ? *)"
    start_window      = 60
    completion_window = 120
    lifecycle {
      delete_after = 7
    }
  }

  rule {
    rule_name         = "weekly"
    target_vault_name = aws_backup_vault.ejc3_backup.name
    schedule          = "cron(0 6 ? * SUN *)"
    start_window      = 60
    completion_window = 120
    lifecycle {
      delete_after = 30
    }
    copy_action {
      destination_vault_arn = aws_backup_vault.ejc3_backup_dr.arn
      lifecycle {
        delete_after = 30
      }
    }
  }

  rule {
    rule_name         = "monthly"
    target_vault_name = aws_backup_vault.ejc3_backup.name
    schedule          = "cron(0 6 1 * ? *)"
    start_window      = 60
    completion_window = 300
    lifecycle {
      cold_storage_after = 30
      delete_after       = 365
    }
    copy_action {
      destination_vault_arn = aws_backup_vault.ejc3_backup_dr.arn
      lifecycle {
        cold_storage_after = 30
        delete_after       = 365
      }
    }
    copy_action {
      destination_vault_arn = aws_backup_vault.staging.arn
      lifecycle {
        cold_storage_after = 30
        delete_after       = 365
      }
    }
  }

  tags = { Name = "dev-servers-backup" }
}

resource "aws_backup_selection" "dev_servers" {
  name         = "dev-server-persistent-volumes"
  plan_id      = local.backup_recovery_cutover_enabled ? aws_backup_plan.processing["dev"].id : aws_backup_plan.dev_servers.id
  iam_role_arn = "arn:aws:iam::928413605543:role/AWSBackupDefaultServiceRole"

  # compact() drops the Next.js entry when that box is disabled, rather than passing an
  # empty string that AWS Backup rejects as a malformed ARN.
  resources = compact([
    local.arm_persistent_volume_arn,
    local.x86_persistent_volume_arn,
    local.nextjs_root_volume_arn,
  ])
  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = !local.backup_recovery_cutover_enabled || local.backup_recovery_cleanup_enabled
      error_message = "Verify temporary-copy cleanup before switching the dev backup selection."
    }
  }
}

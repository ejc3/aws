# Common user_data scripts for development instances
# Shared between firecracker-dev.tf and x86-dev.tf

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
  # Ghostty terminal terminfo installation
  ghostty_terminfo_script = <<-SCRIPT
    # ============================================
    # Ghostty terminfo (for proper terminal support)
    # ============================================
    curl -sL https://raw.githubusercontent.com/ghostty-org/ghostty/main/src/terminfo/ghostty.terminfo | tic -x -
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

  # Combined script for GitHub auth + Claude Sync + Ghostty terminfo + Cron
  gh_and_claude_sync_script = join("\n", [
    local.gh_auth_script,
    local.claude_sync_script,
    local.ghostty_terminfo_script,
    local.claude_sync_cron_script
  ])
}

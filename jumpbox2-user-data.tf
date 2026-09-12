# jumpbox2-user-data.tf
#
# Full boot-time bootstrap for jumpbox-2 (jumpbox2.tf). Everything the original jumpbox has
# by hand, this box gets from `terraform apply` alone: shell, tmux + Eternal Terminal,
# AWS CLI, terraform itself, git/gh authenticated as ejc3, Claude Code and Codex with
# remote control, and the fcvm-ec2 key needed to reach the rest of the fleet.
#
# Runs as root (cloud-init's default), matching every other box's user_data convention --
# per-user steps drop privileges explicitly with `sudo -u ubuntu`.

locals {
  jumpbox_2_user_data = <<-SCRIPT
#!/bin/bash
set -uxo pipefail
export DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------- base packages
apt-get update -y || true
apt-get install -y curl git build-essential zsh jq unzip tar || echo "WARNING: some base packages failed"

# ---------------------------------------------------------------- swap
# t4g.micro is 1GB RAM. terraform alone can use several hundred MB planning against this
# repo's ~155 resources; that has to coexist with the AWS CLI, git, and potentially an
# agent, all at once. A 2GB swapfile turns a would-be OOM kill into a slow moment, the
# same reasoning used on nextjs-dev.
if ! swapon --show=NAME --noheadings | grep -q .; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---------------------------------------------------------------- aws cli v2
# No awscli apt package on Ubuntu 24.04 -- the official installer is the only reliable
# route (observed repeatedly across every box in this repo).
if ! command -v aws >/dev/null 2>&1; then
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip
  (cd /tmp && unzip -qo awscliv2.zip && ./aws/install && rm -rf /tmp/aws /tmp/awscliv2.zip)
fi

# ---------------------------------------------------------------- terraform
# Pinned to the version the original jumpbox actually runs day to day (`terraform
# version` there reports 1.10.3), so the two admin boxes behave identically against the
# same state rather than drifting on a provider-behavior edge case between versions.
TF_VERSION="1.10.3"
if ! command -v terraform >/dev/null 2>&1 || ! terraform version 2>/dev/null | head -1 | grep -q "$TF_VERSION"; then
  TARCH=$(dpkg --print-architecture)
  curl -fsSL "https://releases.hashicorp.com/terraform/$TF_VERSION/terraform_$${TF_VERSION}_linux_$${TARCH}.zip" -o /tmp/tf.zip \
    && unzip -qo /tmp/tf.zip -d /tmp \
    && install -m 755 /tmp/terraform /usr/local/bin/terraform \
    && rm -f /tmp/tf.zip /tmp/terraform \
    || echo "WARNING: terraform install failed"
fi

# ---------------------------------------------------------------- tmux (prebuilt, not compiled)
# The metal boxes' et_setup/tmux compile from source, which is fine on 64 vCPU and wrong
# here -- this is the same prebuilt-binary approach already proven on nextjs-dev.
if ! command -v tmux >/dev/null 2>&1 || ! /usr/local/bin/tmux -V 2>/dev/null | grep -q "3\.[6-9]"; then
  TARCH=$(uname -m)
  if curl -fsSL --retry 3 "https://github.com/ejc3/tmux/releases/download/binaries-3.x/tmux-$TARCH.tar.gz" -o /tmp/tmux.tgz && [ -s /tmp/tmux.tgz ]; then
    tar xzf /tmp/tmux.tgz -C /tmp && install -m 755 /tmp/tmux /usr/local/bin/tmux && rm -f /tmp/tmux.tgz /tmp/tmux
  else
    apt-get install -y tmux || true
  fi
fi

# ---------------------------------------------------------------- eternal terminal (prebuilt)
if ! /usr/bin/etserver --version 2>/dev/null | grep -q "7\."; then
  apt-get install -y libprotobuf32t64 libsodium23 >/dev/null 2>&1 || \
    apt-get install -y libprotobuf32t64 >/dev/null 2>&1 || \
    echo "WARNING: could not install ET runtime deps"
  EARCH=$(uname -m)
  if curl -fsSL --retry 3 "https://github.com/ejc3/EternalTerminal/releases/download/binaries-7.x/et-$EARCH.tar.gz" -o /tmp/et.tgz && [ -s /tmp/et.tgz ]; then
    if tar xzf /tmp/et.tgz -C /tmp && [ -s /tmp/etserver ]; then
      # Check OUTPUT, not exit status: this build prints its version then aborts non-zero.
      chmod +x /tmp/et /tmp/etserver /tmp/etterminal 2>/dev/null
      if /tmp/etserver --version 2>&1 </dev/null | grep -q "7\."; then
        install -m 755 /tmp/et /tmp/etserver /tmp/etterminal /usr/bin/
      else
        echo "WARNING: downloaded etserver did not report a 7.x version; leaving ET uninstalled"
      fi
    fi
    rm -f /tmp/et.tgz /tmp/et /tmp/etserver /tmp/etterminal
  else
    echo "WARNING: could not fetch Eternal Terminal binary"
  fi
fi
if [ -x /usr/bin/etserver ]; then
cat > /etc/systemd/system/etserver.service <<'UNIT'
[Unit]
Description=Eternal Terminal Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/etserver --port 2022
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now etserver.service 2>/dev/null || true
fi

# ---------------------------------------------------------------- shell env (ubuntu only)
# zsh, starship, atuin, fzf, t-claude/nosync-wrap, hollowed tmux.conf -- the SAME body
# every other box runs, so there is one definition of "our shell" and nothing to keep in
# sync by hand across five boxes.
sudo -u ubuntu bash << 'USERSHELL' || echo "WARNING: shell env setup had errors"
${local.user_shell_env}
USERSHELL
chsh -s /usr/bin/zsh ubuntu 2>/dev/null || true

# ---------------------------------------------------------------- github cli + auth
if ! command -v gh >/dev/null 2>&1; then
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update || true
  apt-get install -y gh || echo "WARNING: gh install failed"
fi
${local.gh_auth_script}

# ---------------------------------------------------------------- this repo
# Cloned authenticated as ubuntu via the credential helper gh_auth_script just configured,
# so it is immediately usable for `terraform plan`/`apply` without a manual clone step.
if [ ! -d /home/ubuntu/aws ]; then
  sudo -u ubuntu git clone https://github.com/ejc3/aws.git /home/ubuntu/aws \
    || echo "WARNING: could not clone ejc3/aws"
fi

# ---------------------------------------------------------------- node.js (general CLI tooling)
# Keep the existing tooling runtime; native Claude below does not depend on npm.
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi

# ---------------------------------------------------------------- claude code
# Match the dev hosts: one user-owned native install that can update without sudo.
# Never use `command -v claude` as the native guard: a stale /usr/bin npm copy satisfies
# it. This only installs the program; personal login and history remain untouched.
sudo -u ubuntu -H bash -c 'set -o pipefail; [ -x "$HOME/.local/bin/claude" ] || curl -fsSL https://claude.ai/install.sh | bash' >/dev/null 2>&1 \
  || echo "WARNING: claude native install failed"

# Ensure interactive shells prefer the same native binary as remote-control sessions.
sudo -u ubuntu -H bash -c 'grep -q "HOME/.local/bin" ~/.zshrc 2>/dev/null || printf "%s\\n" "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> ~/.zshrc'

# Remove the obsolete global copy only after the native binary actually runs as ubuntu.
# Do not run npm lifecycle scripts, alter personal credentials, or restart any sessions.
if sudo -u ubuntu -H /home/ubuntu/.local/bin/claude --version >/dev/null 2>&1; then
  npm uninstall -g --ignore-scripts @anthropic-ai/claude-code >/dev/null 2>&1 \
    || echo "WARNING: legacy npm claude removal failed"
else
  echo "WARNING: native claude unavailable -- keeping the npm copy"
fi

# ---------------------------------------------------------------- codex, with remote control
# Same setup as the metal boxes and nextjs-dev: standalone install, config.toml, and the
# codex-rc@ systemd unit. The unit only ENABLES once ~/.codex/auth.json exists -- `codex
# login --device-auth` is the other manual step, same reason as claude above.
${local.codex_remote_control}

# ---------------------------------------------------------------- fcvm-ec2 key
# The key every box in this fleet trusts for inbound SSH, restored from the Secrets
# Manager backup (fcvm-ec2-key-backup.tf) so this box can reach the rest of the fleet
# exactly the way the original jumpbox does. NOT the dev-hop key: that key is deliberately
# absent from every jumpbox by design (dev-hop-key.tf) -- dev servers reach each other
# with it, but no jumpbox holds it, and that must stay true for jumpbox-2 too.
FCVM_KEY=$(aws secretsmanager get-secret-value --secret-id fcvm-ec2-ssh-key \
  --region us-west-1 --query SecretString --output text 2>/dev/null)
if [ -n "$FCVM_KEY" ]; then
  install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
  printf '%s\n' "$FCVM_KEY" > /home/ubuntu/.ssh/fcvm-ec2
  chmod 600 /home/ubuntu/.ssh/fcvm-ec2
  chown ubuntu:ubuntu /home/ubuntu/.ssh/fcvm-ec2
  echo "fcvm-ec2 key restored"
else
  echo "WARNING: fcvm-ec2-ssh-key secret is empty or unreadable -- this box cannot reach the rest of the fleet until it is populated (see fcvm-ec2-key-backup.tf)"
fi

echo "jumpbox-2 ready"
SCRIPT
}

resource "aws_s3_object" "jumpbox_2_user_data" {
  count        = var.enable_jumpbox_2 ? 1 : 0
  bucket       = aws_s3_bucket.dev_scripts.id
  key          = "user-data/jumpbox2.sh"
  content      = local.jumpbox_2_user_data
  content_type = "text/x-shellscript"
  tags         = { Name = "jumpbox-2-user-data" }
}

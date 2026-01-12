# Dev Server User Data Scripts (stored in SSM to bypass 16KB limit)
# These scripts configure ARM and x86 dev instances from scratch

locals {
  # NVMe btrfs setup - runs on every boot via systemd
  # Formats NVMe as btrfs and mounts at /mnt/fcvm-btrfs (for CoW reflinks)
  nvme_btrfs_setup = <<-NVME
# Install btrfs-progs
apt-get install -y btrfs-progs

# Create systemd service for NVMe setup on every boot
cat > /etc/systemd/system/nvme-btrfs.service << 'SVC'
[Unit]
Description=Format and mount NVMe as btrfs
After=local-fs.target
Before=multi-user.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/nvme-btrfs-setup.sh
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SVC

# Create the setup script
cat > /usr/local/bin/nvme-btrfs-setup.sh << 'SCRIPT'
#!/bin/bash
set -euo pipefail

# Find NVMe device that's NOT the root disk
ROOT_DEV=$$(lsblk -no PKNAME $$(findmnt -no SOURCE /) 2>/dev/null | head -1)
NVME_DEV=$$(lsblk -dn -o NAME,TYPE | awk '$$2=="disk" && /^nvme/ {print $$1}' | grep -v "^$${ROOT_DEV}$$" | head -1)

if [ -z "$$NVME_DEV" ]; then
    echo "No NVMe instance storage found, skipping"
    exit 0
fi

echo "Setting up NVMe as btrfs: /dev/$$NVME_DEV"

# Format as btrfs (always - NVMe is ephemeral)
mkfs.btrfs -f /dev/$$NVME_DEV

# Mount
mkdir -p /mnt/fcvm-btrfs
mount /dev/$$NVME_DEV /mnt/fcvm-btrfs
chmod 1777 /mnt/fcvm-btrfs

# Create directory structure for fcvm
mkdir -p /mnt/fcvm-btrfs/{kernels,rootfs,initrd,state,snapshots,vm-disks,cache,image-cache}
mkdir -p /mnt/fcvm-btrfs/{containers,cargo-target}
chown -R ubuntu:ubuntu /mnt/fcvm-btrfs

# Symlink podman containers to NVMe
CONTAINERS_DIR="/home/ubuntu/.local/share/containers"
if [ ! -L "$$CONTAINERS_DIR" ]; then
    rm -rf "$$CONTAINERS_DIR"
    mkdir -p /home/ubuntu/.local/share
    ln -sf /mnt/fcvm-btrfs/containers "$$CONTAINERS_DIR"
    chown -R ubuntu:ubuntu /home/ubuntu/.local
fi

echo "NVMe btrfs setup complete: /mnt/fcvm-btrfs"
SCRIPT

chmod +x /usr/local/bin/nvme-btrfs-setup.sh
systemctl daemon-reload
systemctl enable nvme-btrfs.service

# Run it now too (for first boot)
/usr/local/bin/nvme-btrfs-setup.sh || true
NVME

  # ARM dev server (c7gd.metal) full setup script
  arm_user_data = <<-SCRIPT
#!/bin/bash
set -euxo pipefail

# System packages
apt-get update && apt-get upgrade -y
apt-get install -y \
  zsh curl wget git jq build-essential software-properties-common \
  podman uidmap slirp4netns fuse-overlayfs containernetworking-plugins \
  nftables iproute2 dnsmasq cmake ninja-build pkg-config autoconf libtool \
  fuse3 libfuse3-dev protobuf-compiler libprotobuf-dev libsodium-dev \
  libcurl4-openssl-dev libutempter-dev unzip zip flex bison libssl-dev \
  libelf-dev bc dwarves

# NVMe btrfs setup (scratch space for builds, VMs, containers)
${local.nvme_btrfs_setup}

# Eternal Terminal
git clone --recurse-submodules --depth 1 https://github.com/MisterTea/EternalTerminal.git /tmp/et
cd /tmp/et && mkdir build && cd build && cmake .. && make -j$(nproc) && make install
rm -rf /tmp/et
cat > /etc/systemd/system/et.service << 'EOF'
[Unit]
Description=Eternal Terminal Server
After=network.target
[Service]
Type=simple
ExecStart=/usr/bin/etserver
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable et.service && systemctl start et.service

# GitHub CLI
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
apt-get update && apt-get install -y gh

# Node.js 22.x
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs

# Rust
sudo -u ubuntu bash -c 'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y'

# Firecracker ARM64
FIRECRACKER_VERSION="v1.13.1"
wget -q -O /tmp/fc.tgz "https://github.com/firecracker-microvm/firecracker/releases/download/$${FIRECRACKER_VERSION}/firecracker-$${FIRECRACKER_VERSION}-aarch64.tgz"
tar -xzf /tmp/fc.tgz -C /tmp/
mv /tmp/release-$${FIRECRACKER_VERSION}-aarch64/firecracker-$${FIRECRACKER_VERSION}-aarch64 /usr/local/bin/firecracker
chmod +x /usr/local/bin/firecracker && rm -rf /tmp/fc.tgz /tmp/release-*

# Podman rootless
echo "ubuntu:100000:65536" >> /etc/subuid
echo "ubuntu:100000:65536" >> /etc/subgid
sysctl -w kernel.unprivileged_userns_clone=1
echo "kernel.unprivileged_userns_clone=1" >> /etc/sysctl.conf

# Shell setup
sudo -u ubuntu bash << 'SHELL'
set -e
mkdir -p ~/.local/bin ~/.config ~/.zsh
curl -sS https://starship.rs/install.sh | sh -s -- -y -b ~/.local/bin
cat > ~/.config/starship.toml << 'TOML'
format = "$directory$git_branch$git_status$character"
add_newline = false
[directory]
truncation_length = 3
[git_branch]
format = "[$branch]($style) "
[character]
success_symbol = "[❯](green)"
error_symbol = "[❯](red)"
TOML
git clone --depth 1 https://github.com/junegunn/fzf.git ~/.fzf
~/.fzf/install --all --no-bash --no-fish
curl --proto '=https' --tlsv1.2 -sSf https://setup.atuin.sh | bash
git clone https://github.com/zsh-users/zsh-autosuggestions ~/.zsh/zsh-autosuggestions
git clone https://github.com/zsh-users/zsh-syntax-highlighting ~/.zsh/zsh-syntax-highlighting
cat > ~/.zshrc << 'ZSH'
export PATH="$HOME/.local/bin:$HOME/.atuin/bin:$HOME/.cargo/bin:$PATH"
# Use NVMe for cargo builds if available
[ -d /mnt/fcvm-btrfs/cargo-target ] && export CARGO_TARGET_DIR=/mnt/fcvm-btrfs/cargo-target
HISTFILE=~/.zsh_history; HISTSIZE=100000; SAVEHIST=100000
setopt SHARE_HISTORY HIST_IGNORE_DUPS HIST_IGNORE_SPACE
autoload -Uz compinit && compinit
eval "$(starship init zsh)"
[ -f ~/.fzf.zsh ] && source ~/.fzf.zsh
command -v atuin >/dev/null && eval "$(atuin init zsh)"
. "$HOME/.atuin/bin/env"
[ -f ~/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh ] && source ~/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh
[ -f ~/.zsh/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh ] && source ~/.zsh/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh
alias ll="ls -la" gs="git status" gd="git diff"
ZSH
SHELL
chsh -s /usr/bin/zsh ubuntu

# Claude Code
sudo -u ubuntu bash -c 'npm install -g @anthropic-ai/claude-code'

${local.gh_and_claude_sync_script}

echo "ARM dev instance ready!"
SCRIPT

  # x86 dev server (c5d.metal) full setup script
  x86_user_data = <<-SCRIPT
#!/bin/bash
set -euxo pipefail

# System packages
apt-get update && apt-get upgrade -y
apt-get install -y \
  zsh curl wget git jq build-essential software-properties-common \
  podman uidmap slirp4netns fuse-overlayfs containernetworking-plugins \
  nftables iproute2 dnsmasq cmake ninja-build pkg-config autoconf libtool \
  fuse3 libfuse3-dev protobuf-compiler libprotobuf-dev libsodium-dev \
  libcurl4-openssl-dev libutempter-dev libssl-dev unzip zip

# NVMe btrfs setup (scratch space for builds, VMs, containers)
${local.nvme_btrfs_setup}

# Eternal Terminal
git clone --recurse-submodules --depth 1 https://github.com/MisterTea/EternalTerminal.git /tmp/et
cd /tmp/et && mkdir build && cd build && cmake .. && make -j$(nproc) && make install
rm -rf /tmp/et
cat > /etc/systemd/system/et.service << 'EOF'
[Unit]
Description=Eternal Terminal Server
After=network.target
[Service]
Type=simple
ExecStart=/usr/bin/etserver
Restart=on-failure
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable et.service && systemctl start et.service

# GitHub CLI
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
apt-get update && apt-get install -y gh

# Node.js 22.x
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs

# Rust
sudo -u ubuntu bash -c 'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y'

# Firecracker x86
FIRECRACKER_VERSION="v1.13.1"
wget -q -O /tmp/fc.tgz "https://github.com/firecracker-microvm/firecracker/releases/download/$${FIRECRACKER_VERSION}/firecracker-$${FIRECRACKER_VERSION}-x86_64.tgz"
tar -xzf /tmp/fc.tgz -C /tmp/
mv /tmp/release-$${FIRECRACKER_VERSION}-x86_64/firecracker-$${FIRECRACKER_VERSION}-x86_64 /usr/local/bin/firecracker
chmod +x /usr/local/bin/firecracker && rm -rf /tmp/fc.tgz /tmp/release-*

# Podman rootless
echo "ubuntu:100000:65536" >> /etc/subuid
echo "ubuntu:100000:65536" >> /etc/subgid
sysctl -w kernel.unprivileged_userns_clone=1
echo "kernel.unprivileged_userns_clone=1" >> /etc/sysctl.conf

# Shell setup
sudo -u ubuntu bash << 'SHELL'
set -e
mkdir -p ~/.local/bin ~/.config ~/.zsh
curl -sS https://starship.rs/install.sh | sh -s -- -y -b ~/.local/bin
cat > ~/.config/starship.toml << 'TOML'
format = "$directory$git_branch$git_status$character"
add_newline = false
[directory]
truncation_length = 3
[git_branch]
format = "[$branch]($style) "
[character]
success_symbol = "[❯](green)"
error_symbol = "[❯](red)"
TOML
git clone --depth 1 https://github.com/junegunn/fzf.git ~/.fzf
~/.fzf/install --all --no-bash --no-fish
curl --proto '=https' --tlsv1.2 -sSf https://setup.atuin.sh | bash
git clone https://github.com/zsh-users/zsh-autosuggestions ~/.zsh/zsh-autosuggestions
git clone https://github.com/zsh-users/zsh-syntax-highlighting ~/.zsh/zsh-syntax-highlighting
cat > ~/.zshrc << 'ZSH'
export PATH="$HOME/.local/bin:$HOME/.atuin/bin:$HOME/.cargo/bin:$PATH"
# Use NVMe for cargo builds if available
[ -d /mnt/fcvm-btrfs/cargo-target ] && export CARGO_TARGET_DIR=/mnt/fcvm-btrfs/cargo-target
HISTFILE=~/.zsh_history; HISTSIZE=100000; SAVEHIST=100000
setopt SHARE_HISTORY HIST_IGNORE_DUPS HIST_IGNORE_SPACE
autoload -Uz compinit && compinit
eval "$(starship init zsh)"
[ -f ~/.fzf.zsh ] && source ~/.fzf.zsh
command -v atuin >/dev/null && eval "$(atuin init zsh)"
. "$HOME/.atuin/bin/env"
[ -f ~/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh ] && source ~/.zsh/zsh-autosuggestions/zsh-autosuggestions.zsh
[ -f ~/.zsh/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh ] && source ~/.zsh/zsh-syntax-highlighting/zsh-syntax-highlighting.zsh
alias ll="ls -la" gs="git status" gd="git diff"
ZSH
SHELL
chsh -s /usr/bin/zsh ubuntu

# Claude Code
sudo -u ubuntu bash -c 'npm install -g @anthropic-ai/claude-code'

${local.gh_and_claude_sync_script}

echo "x86 dev instance ready!"
SCRIPT
}

# S3 bucket for dev server user_data scripts (SSM has 8KB limit)
resource "aws_s3_bucket" "dev_scripts" {
  bucket = "ejc3-dev-scripts"
  tags   = { Name = "dev-scripts" }
}

resource "aws_s3_bucket_versioning" "dev_scripts" {
  bucket = aws_s3_bucket.dev_scripts.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_object" "arm_user_data" {
  count        = var.enable_firecracker_instance ? 1 : 0
  bucket       = aws_s3_bucket.dev_scripts.id
  key          = "user-data/arm.sh"
  content      = local.arm_user_data
  content_type = "text/x-shellscript"
  tags         = { Name = "arm-dev-user-data" }
}

resource "aws_s3_object" "x86_user_data" {
  count        = var.enable_x86_dev_instance ? 1 : 0
  bucket       = aws_s3_bucket.dev_scripts.id
  key          = "user-data/x86.sh"
  content      = local.x86_user_data
  content_type = "text/x-shellscript"
  tags         = { Name = "x86-dev-user-data" }
}

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

# Pick the ephemeral instance-store NVMe disks to format.
#
# SAFETY: this script runs mkfs on whatever it selects, on EVERY boot. Selecting by
# "every nvme except the root device" was unsafe: if ROOT_DEV ever resolved empty, the
# exclusion became `grep -v "^$"` (which excludes nothing) and mkfs would have wiped the
# root EBS volume -- the persistent, delete_on_termination=false disk. So:
#   1. select POSITIVELY on the instance-store model, so an EBS disk can never match, and
#   2. fail closed if the root disk cannot be identified.
ROOT_DEV=$(lsblk -no PKNAME $(findmnt -no SOURCE /) 2>/dev/null | head -1)
if [ -z "$${ROOT_DEV}" ]; then
    echo "FATAL: cannot determine the root disk; refusing to format anything" >&2
    exit 1
fi
NVME_DEVS=$(lsblk -dn -o NAME,TYPE,MODEL \
    | awk '$2=="disk" && /Instance Storage/ {print $1}' \
    | grep -v "^$${ROOT_DEV}$")
NVME_COUNT=$(echo "$NVME_DEVS" | wc -w)

if [ "$NVME_COUNT" -eq 0 ]; then
    echo "No NVMe instance storage found, skipping"
    exit 0
fi

# Already set up (this script also runs at boot, and self-update re-runs it later):
# mkfs on a mounted device fails, and reformatting would throw away the cache for no
# reason. The instance store is ephemeral, so a mount that already exists is correct.
if mountpoint -q /mnt/fcvm-btrfs; then
    echo "/mnt/fcvm-btrfs already mounted; leaving it alone"
    exit 0
fi

if [ "$NVME_COUNT" -ge 2 ]; then
    # RAID0 multiple NVMe drives for maximum throughput
    NVME_PATHS=$(echo "$NVME_DEVS" | sed 's|^|/dev/|' | tr '\n' ' ')
    echo "Setting up btrfs RAID0 across $NVME_COUNT NVMe drives: $NVME_PATHS"
    mkfs.btrfs -f -d raid0 -m raid0 $NVME_PATHS
    mkdir -p /mnt/fcvm-btrfs
    mount $(echo "$NVME_PATHS" | awk '{print $1}') /mnt/fcvm-btrfs
else
    NVME_DEV=$(echo "$NVME_DEVS" | head -1)
    echo "Setting up NVMe as btrfs: /dev/$NVME_DEV"
    mkfs.btrfs -f /dev/$NVME_DEV
    mkdir -p /mnt/fcvm-btrfs
    mount /dev/$NVME_DEV /mnt/fcvm-btrfs
fi
chmod 1777 /mnt/fcvm-btrfs

# Create directory structure for fcvm
mkdir -p /mnt/fcvm-btrfs/{kernels,rootfs,initrd,state,snapshots,vm-disks,cache,image-cache}
mkdir -p /mnt/fcvm-btrfs/{containers,cargo-target}
chown -R ubuntu:ubuntu /mnt/fcvm-btrfs

# Symlink podman containers to NVMe
CONTAINERS_DIR="/home/ubuntu/.local/share/containers"
if [ ! -L "$CONTAINERS_DIR" ]; then
    rm -rf "$CONTAINERS_DIR"
    mkdir -p /home/ubuntu/.local/share
    ln -sf /mnt/fcvm-btrfs/containers "$CONTAINERS_DIR"
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

  # Eternal Terminal (pinned tag, built from source = single source of truth).
  # The cleanup lines below remove a *manually* apt/PPA-installed et: nothing in
  # this repo or the base AMI installs it, but it can persist on the boxes'
  # persistent root volumes. That packaged et.service also binds :2022 and races
  # our etserver.service, crash-looping one on "address already in use".
  et_setup = <<-ET
apt-get remove -y et || true
add-apt-repository -r -y ppa:jgmath2000/et || true
systemctl disable --now et.service || true
rm -f /etc/systemd/system/et.service /usr/lib/systemd/system/et.service
if /usr/bin/etserver --version 2>/dev/null | grep -q "7.0.0"; then
  echo "Eternal Terminal already at 7.0.0, skipping rebuild"
else
git clone --recurse-submodules --depth 1 --branch et-v7.0.0 https://github.com/MisterTea/EternalTerminal.git /tmp/et
cd /tmp/et && mkdir build && cd build && cmake .. && make -j$(nproc)
cp et etserver etterminal /usr/bin/
rm -rf /tmp/et
cat > /etc/systemd/system/etserver.service << 'EOF'
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
EOF
systemctl daemon-reload && systemctl enable etserver.service && systemctl start etserver.service
fi
ET

  # gh CLI + Node.js 22 + Rust (identical on both arches)
  dev_langs = <<-DEVLANGS
# GitHub CLI
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" > /etc/apt/sources.list.d/github-cli.list
apt-get update || true
apt-get install -y gh || echo "WARNING: gh install failed; continuing"

# Node.js 22.x
curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
apt-get install -y nodejs

# Rust
sudo -u ubuntu bash -c 'curl --proto "=https" --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y'
DEVLANGS

  # Rootless podman + kernel sysctls (userns, AppArmor, writeback tuning)
  podman_sysctl_setup = <<-PODMANSYS
# Podman rootless (idempotent: user_data re-runs on persistent-volume recreation)
grep -qxF "ubuntu:100000:65536" /etc/subuid || echo "ubuntu:100000:65536" >> /etc/subuid
grep -qxF "ubuntu:100000:65536" /etc/subgid || echo "ubuntu:100000:65536" >> /etc/subgid
# Kernel sysctls: write a drop-in (overwrite = idempotent) and apply immediately.
# Disable AppArmor restriction on unprivileged user namespaces (required for rootless podman/fcvm).
# Raise dirty_ratio to prevent writeback throttling during concurrent snapshot creation:
# default 20% makes the kernel throttle all writers once dirty pages exceed 20% of RAM, which
# stalls simultaneous VM snapshots (CI) for 100+ seconds; at 80% most complete at memory speed.
cat > /etc/sysctl.d/99-fcvm.conf << 'SYSCTL'
kernel.unprivileged_userns_clone=1
kernel.apparmor_restrict_unprivileged_userns=0
vm.dirty_ratio=80
vm.dirty_background_ratio=50
SYSCTL
# Non-fatal: `sysctl -p` exits non-zero if ANY key is missing, and these boxes boot a
# custom -nested-dsb kernel that lacks kernel.unprivileged_userns_clone and
# kernel.apparmor_restrict_unprivileged_userns (both Ubuntu-stock-kernel keys). Under
# set -e that aborted the whole setup script before the shell/gh/self-update sections.
# The keys that DO exist are still applied; the rest are reported and skipped.
sysctl -p /etc/sysctl.d/99-fcvm.conf \
  || echo "WARNING: some sysctl keys are unavailable on this kernel; applied the rest and continuing"
PODMANSYS

  # Interactive shell setup: starship, fzf, atuin, zsh plugins, .zshrc (identical on both arches)
  shell_setup = <<-SHELLSETUP
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
[ -d ~/.fzf ] || git clone --depth 1 https://github.com/junegunn/fzf.git ~/.fzf
[ -f ~/.fzf.zsh ] || ~/.fzf/install --all --no-bash --no-fish
curl --proto '=https' --tlsv1.2 -sSf https://setup.atuin.sh | bash
[ -d ~/.zsh/zsh-autosuggestions ] || git clone https://github.com/zsh-users/zsh-autosuggestions ~/.zsh/zsh-autosuggestions
[ -d ~/.zsh/zsh-syntax-highlighting ] || git clone https://github.com/zsh-users/zsh-syntax-highlighting ~/.zsh/zsh-syntax-highlighting
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
[ -f ~/.config/t-claude.zsh ] && source ~/.config/t-claude.zsh
ZSH
cat > ~/.tmux.conf << 'TMUXCONF'
# Goal: scrolling behaves as if tmux were not running -- a swipe in Panic Prompt (iOS)
# scrolls the terminal's OWN scrollback. No copy-mode, no key chords.
#
# THE MECHANISM (deterministic, 4/4 across burst and paced output):
#   default config    -> tmux emits ESC[?1049h, terminal enters the ALTERNATE screen
#   with smcup@ below -> no ESC[?1049h, tmux stays on the PRIMARY screen
#
# The alternate screen has no scrollback by definition, so anything scrolling out of the
# pane is discarded by the terminal and there is nothing for a swipe to reach. Staying on
# the primary screen means output lands in the terminal's own scrollback, as with no tmux.
#
# CORRECTION: an earlier version of this file claimed "0 of 36 scrolled-off lines ever
# reached the terminal". That was a capture-flush artifact of a single test run. Re-measured
# it is 36/36 in BOTH configs -- the bytes always arrive; what differs is whether the
# terminal RETAINS them, which is decided solely by the alternate screen. Same conclusion,
# but do not trust the old byte-count reasoning.
#
# Verified separately: Prompt on iOS implements xterm mouse "click-only" (Panic's own docs),
# so it sends no wheel events and `mouse on` can never scroll here. Full-screen apps inside
# tmux (vim/htop) still work and their alt-screen does not leak outward.
#
# KNOWN WART: reattaching redraws the visible screen, so roughly one screenful (measured up
# to ~3x for some rows) is re-emitted into scrollback per attach. Inherent to primary-screen
# operation; the tradeoff for having any scrollback at all.

# THE KEY LINES. Native scrollback needs BOTH:
#  1. smcup@/rmcup@ -- stay on the primary screen (the alt screen has no scrollback).
#  2. status off -- keep the pane the FULL terminal height. With a status line the pane
#     is one row short, so tmux scrolls inside a DECSTBM region (ESC[1;H-1r), and lines
#     scrolled out of a restricted region are DISCARDED by the terminal, not saved.
#     Byte-capture verified: status on emits ESC[1;23r on every scroll; status off emits
#     only a full-screen region at startup and scrolls with bare linefeeds, identical to
#     a bare shell. Splits break this too -- single pane only. Never set status-position
#     top: a top status shifts the region top off row 0, which kills scrollback in every
#     emulator (xterm/VTE feed scrollback only when the region top is row 0, full width).
set -ga terminal-overrides ',*:smcup@:rmcup@'
set -g status off
# NOTE: these three settings are necessary but NOT sufficient for Claude Code. Claude
# wraps its repaints in synchronized-output mode (CSI ?2026h/l); tmux buffers grid
# updates during sync and emits only a viewport redraw, so scrolled lines never reach
# native scrollback. t-claude launches claude under `nosync-wrap`, which strips those
# sequences. Plain shell output needs only the three settings above.
# 3. indn@ -- scroll with bare linefeeds ONLY, never CSI S. tmux batches multi-line
#    scrolls into the indn capability (ESC[nS) when the terminal advertises it, and
#    Prompt on iOS saves LF-scrolled lines to its scrollback but discards CSI-S-scrolled
#    ones (side-by-side verified: seq output scrolled by LF survives a swipe; Claude's
#    Ink commits scrolled by CSI S vanish). Disabling indn forces a plain LF loop.
set -as terminal-overrides ',*:indn@'

# Mouse OFF, and PINNED deliberately: `mouse on` makes tmux capture wheel events into
# copy-mode and steal them from the terminal. tmux 3.8 changes the DEFAULT to on, so
# leaving it unset would silently break native scrolling on a future upgrade.
set -g mouse off

# Lowered from 50000: tmux history costs ~310 bytes/line plus ~27 bytes per used cell and
# has no byte cap (tmux#4859), so 50k dense lines is ~120MB per pane. The terminal owns
# scrollback now, so this is only a copy-mode fallback (Ctrl-b [ , arrows, q).
# To extract a full pane regardless: tmux capture-pane -J -S - -p > file.txt
set -g history-limit 10000

set -g default-terminal "tmux-256color"
set -as terminal-features ",xterm-256color:RGB"
set -g set-clipboard on
set -sg escape-time 10
set -g focus-events on
TMUXCONF
mkdir -p ~/.config
# t-claude and nosync-wrap now live in their own repo: github.com/ejc3/t-claude
# Fetch them here instead of vendoring. Non-fatal (guarded, and set -e is on): a failed
# fetch leaves the ~/.zshrc source line a no-op and t-claude falls back to bare claude.
# Verify non-empty before installing so a truncated download never replaces a good file.
TCRAW="https://raw.githubusercontent.com/ejc3/t-claude/main"
if curl -fsSL --retry 3 "$TCRAW/t-claude.zsh" -o /tmp/t-claude.zsh && [ -s /tmp/t-claude.zsh ]; then
  mv /tmp/t-claude.zsh ~/.config/t-claude.zsh
else
  echo "WARNING: could not fetch t-claude.zsh from $TCRAW (keeping any existing copy)"
fi
if curl -fsSL --retry 3 "$TCRAW/nosync-wrap" -o /tmp/nosync-wrap && [ -s /tmp/nosync-wrap ]; then
  sudo install -m 755 /tmp/nosync-wrap /usr/local/bin/nosync-wrap && rm -f /tmp/nosync-wrap
else
  echo "WARNING: could not fetch nosync-wrap from $TCRAW (native scrollback may be off)"
fi
SHELL
chsh -s /usr/bin/zsh ubuntu
SHELLSETUP

  # ARM dev server (c7gd.metal) full setup script
  arm_user_data = <<-SCRIPT
#!/bin/bash
set -euxo pipefail

# System packages
# Non-fatal on purpose: a stale mirror index 404s on unrelated packages (snapd has done
# this) and, under set -e, that aborted the entire setup before the config sections ran.
# Retry once with a refreshed index, then continue regardless -- an OS upgrade is not a
# prerequisite for converging dotfiles and services.
apt-get update || true
apt-get upgrade -y || { echo "WARNING: apt upgrade failed (likely a stale mirror); retrying once"; apt-get update || true; apt-get upgrade -y || echo "WARNING: apt upgrade still failing; continuing"; }
apt-get install -y \
  zsh curl wget git jq build-essential software-properties-common \
  podman uidmap slirp4netns fuse-overlayfs containernetworking-plugins \
  nftables iproute2 dnsmasq cmake ninja-build pkg-config autoconf libtool \
  fuse3 libfuse3-dev protobuf-compiler libprotobuf-dev libsodium-dev \
  libcurl4-openssl-dev libutempter-dev unzip zip flex bison libssl-dev \
  libelf-dev bc dwarves nfs-kernel-server

# AWS CLI v2 (apt package not available on Ubuntu 24.04)
curl "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/awscliv2.zip
cd /tmp && unzip -o awscliv2.zip && ./aws/install && rm -rf aws awscliv2.zip

# Enable user_allow_other in fuse.conf (required for FUSE tests)
sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf

# NVMe btrfs setup (scratch space for builds, VMs, containers)
${local.nvme_btrfs_setup}

# Eternal Terminal (isolated so a build failure can't abort the rest of setup; SSH :22 remains)
${local.bin_update}
# Try the prebuilt binary from ejc3/EternalTerminal CI first; compile from source only
# if no release asset is available for this arch.
/usr/local/bin/dev-bin-update.sh || true
if ! /usr/bin/etserver --version 2>/dev/null | grep -q "7\."; then
( ${local.et_setup} ) || echo "WARNING: Eternal Terminal setup failed; continuing (SSH :22 unaffected)"
fi

${local.dev_langs}

# Firecracker ARM64
FIRECRACKER_VERSION="v1.13.1"
wget -q -O /tmp/fc.tgz "https://github.com/firecracker-microvm/firecracker/releases/download/$${FIRECRACKER_VERSION}/firecracker-$${FIRECRACKER_VERSION}-aarch64.tgz"
tar -xzf /tmp/fc.tgz -C /tmp/
mv /tmp/release-$${FIRECRACKER_VERSION}-aarch64/firecracker-$${FIRECRACKER_VERSION}-aarch64 /usr/local/bin/firecracker
chmod +x /usr/local/bin/firecracker && rm -rf /tmp/fc.tgz /tmp/release-*

${local.podman_sysctl_setup}

# Shell setup
${local.shell_setup}

# Claude Code
# Global npm installs write to /usr/lib/node_modules, which is root-owned -- running
# this as the ubuntu user fails with EACCES. Install as root; ubuntu only needs to RUN it.
npm install -g @anthropic-ai/claude-code

${local.gh_and_claude_sync_script}

${local.pbox_setup}

${local.selfupdate_setup}

echo "ARM dev instance ready!"
SCRIPT

  # x86 dev server (c5d.metal) full setup script
  x86_user_data = <<-SCRIPT
#!/bin/bash
set -euxo pipefail

# System packages
# Non-fatal on purpose: a stale mirror index 404s on unrelated packages (snapd has done
# this) and, under set -e, that aborted the entire setup before the config sections ran.
# Retry once with a refreshed index, then continue regardless -- an OS upgrade is not a
# prerequisite for converging dotfiles and services.
apt-get update || true
apt-get upgrade -y || { echo "WARNING: apt upgrade failed (likely a stale mirror); retrying once"; apt-get update || true; apt-get upgrade -y || echo "WARNING: apt upgrade still failing; continuing"; }
apt-get install -y \
  zsh curl wget git jq build-essential software-properties-common \
  podman uidmap slirp4netns fuse-overlayfs containernetworking-plugins \
  nftables iproute2 dnsmasq cmake ninja-build pkg-config autoconf libtool \
  fuse3 libfuse3-dev protobuf-compiler libprotobuf-dev libsodium-dev \
  libcurl4-openssl-dev libutempter-dev libssl-dev unzip zip nfs-kernel-server

# AWS CLI v2 (apt package not available on Ubuntu 24.04)
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
cd /tmp && unzip -o awscliv2.zip && ./aws/install && rm -rf aws awscliv2.zip

# Enable user_allow_other in fuse.conf (required for FUSE tests)
sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf

# NVMe btrfs setup (scratch space for builds, VMs, containers)
${local.nvme_btrfs_setup}

# Eternal Terminal (isolated so a build failure can't abort the rest of setup; SSH :22 remains)
${local.bin_update}
# Try the prebuilt binary from ejc3/EternalTerminal CI first; compile from source only
# if no release asset is available for this arch.
/usr/local/bin/dev-bin-update.sh || true
if ! /usr/bin/etserver --version 2>/dev/null | grep -q "7\."; then
( ${local.et_setup} ) || echo "WARNING: Eternal Terminal setup failed; continuing (SSH :22 unaffected)"
fi

${local.dev_langs}

# Firecracker x86
FIRECRACKER_VERSION="v1.13.1"
wget -q -O /tmp/fc.tgz "https://github.com/firecracker-microvm/firecracker/releases/download/$${FIRECRACKER_VERSION}/firecracker-$${FIRECRACKER_VERSION}-x86_64.tgz"
tar -xzf /tmp/fc.tgz -C /tmp/
mv /tmp/release-$${FIRECRACKER_VERSION}-x86_64/firecracker-$${FIRECRACKER_VERSION}-x86_64 /usr/local/bin/firecracker
chmod +x /usr/local/bin/firecracker && rm -rf /tmp/fc.tgz /tmp/release-*

${local.podman_sysctl_setup}

# Shell setup
${local.shell_setup}

# Claude Code
# Global npm installs write to /usr/lib/node_modules, which is root-owned -- running
# this as the ubuntu user fails with EACCES. Install as root; ubuntu only needs to RUN it.
npm install -g @anthropic-ai/claude-code

${local.gh_and_claude_sync_script}

${local.pbox_setup}

${local.selfupdate_setup}

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

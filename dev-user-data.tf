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

# Podman's system services use storage under /mnt/fcvm-btrfs. default.target can
# start them in parallel with nvme-btrfs.service, so make the storage dependency
# explicit on every unit that opens the store. A dependency on the mount path is
# insufficient because the setup service performs the mount itself (there is no
# corresponding systemd .mount unit).
for UNIT in \
    podman.service \
    podman-clean-transient.service \
    podman-restart.service \
    podman-auto-update.service; do
    install -d -m 0755 "/etc/systemd/system/$UNIT.d"
    cat > "/etc/systemd/system/$UNIT.d/10-nvme-btrfs.conf" << 'PODMAN_ORDER'
[Unit]
Requires=nvme-btrfs.service
After=nvme-btrfs.service
PODMAN_ORDER
done

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

# When this update first converges on an existing box, the old boot dependency
# graph may already have let these units race and fail. Retry only failed units
# now that the mount is ready; future boots are ordered by the drop-ins above.
for UNIT in \
    podman.service \
    podman-clean-transient.service \
    podman-restart.service \
    podman-auto-update.service; do
    if systemctl is-failed --quiet "$UNIT"; then
        systemctl restart "$UNIT" || echo "WARNING: failed to restart $UNIT"
    fi
done
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
# --- diagnostics for a wedged box (see dev-diagnostics.tf) ---
# panic_on_oops: on 2026-08-08 a kernel BUG in the nested-KVM path killed an fc_vcpu
# thread "with irqs disabled" and the box then hung INDEFINITELY -- 25 minutes of dead
# dev box before a human noticed, with no self-recovery. Turning that oops into a panic
# makes the kernel reboot instead (the cmdline already carries panic=-1, reboot
# immediately). A reboot does NOT clear the EC2 console ring buffer -- only stop/start
# does -- so the trace survives for the capture Lambda to archive. Trade accepted: an
# oops that would NOT have wedged the box now costs a reboot, which on a dev box beats
# an indefinite hang that costs the whole box.
kernel.panic_on_oops=1
# Log D-state pileups (the 2026-08-07 shape: 103 uninterruptible tasks) to the console
# so they reach the same capture path. Deliberately WARN, not panic: heavy Firecracker
# and NFS I/O can legitimately block a task, and a spurious reboot mid-build is worse
# than a log line. 300s is far past any legitimate block on these boxes.
kernel.hung_task_timeout_secs=300
kernel.hung_task_warnings=10
# Let an operator on the serial console dump tasks/memory or force a crash on a box
# that is too far gone to accept SSH. Inert unless deliberately used.
kernel.sysrq=1
SYSCTL
# Keep the journal across reboots. Without this the journal lives in /run (tmpfs) and
# every reboot -- including the automatic one a panic now triggers -- discards the last
# thing the box logged before it died.
mkdir -p /var/log/journal
if ! grep -q '^Storage=persistent' /etc/systemd/journald.conf 2>/dev/null; then
  sed -i 's/^#\?Storage=.*/Storage=persistent/' /etc/systemd/journald.conf
  grep -q '^Storage=persistent' /etc/systemd/journald.conf || echo 'Storage=persistent' >> /etc/systemd/journald.conf
  systemd-tmpfiles --create --prefix /var/log/journal >/dev/null 2>&1 || true
  systemctl restart systemd-journald 2>/dev/null || true
fi

# Non-fatal: `sysctl -p` exits non-zero if ANY key is missing, and these boxes boot a
# custom -nested-dsb kernel that lacks kernel.unprivileged_userns_clone and
# kernel.apparmor_restrict_unprivileged_userns (both Ubuntu-stock-kernel keys). Under
# set -e that aborted the whole setup script before the shell/gh/self-update sections.
# The keys that DO exist are still applied; the rest are reported and skipped.
sysctl -p /etc/sysctl.d/99-fcvm.conf \
  || echo "WARNING: some sysctl keys are unavailable on this kernel; applied the rest and continuing"
PODMANSYS

  # Interactive shell setup: starship, fzf, atuin, zsh plugins, .zshrc (identical on both arches)
  # Per-user interactive shell environment: starship, fzf, atuin, zsh plugins, .zshrc,
  # .tmux.conf and t-claude. Deliberately user-AGNOSTIC -- every path is ~-relative, so
  # the same body sets up whichever account runs it. dev boxes run it for ubuntu; the
  # Next.js box runs it for every account (ubuntu, ej, colton, connor) so each kid gets
  # the same shell, history and t-claude without duplicating any of it here.
  user_shell_env = <<-USERENV
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
# Remote control on EVERY session, not just the managed one. agents-start passes
# --remote-control explicitly, so without this the first session a person starts -- the one
# they log in with -- is the only one that is NOT reachable, and t-claude will not repair it
# later: a live claude is selected, never relaunched. t-claude merges TCLAUDE_ARGS into each
# launch and explicit arguments win, so agents-start does not end up passing it twice.
export TCLAUDE_ARGS="--remote-control"
[ -f ~/.config/t-claude.zsh ] && source ~/.config/t-claude.zsh
ZSH
cat > ~/.tmux.conf << 'TMUXCONF'
# Native (swipe/wheel) scrollback in Panic Prompt on iOS -- smcup@/rmcup@, status off,
# indn@, mouse off, history-limit -- is NOT set here. t-claude (github.com/ejc3/t-claude)
# asserts those itself at runtime on every invocation, scoped to the session it manages,
# so it works from a bare host with nothing but zsh and tmux and cannot go stale the way a
# static file here could. See t-claude.zsh's APPLY_SCROLLBACK_SETTINGS for the mechanism,
# measurements and the tmux option-scope research behind each choice. A tmux opened WITHOUT
# ever running t-claude on this server will not have native scrollback -- run `t-claude`
# once and every session on the server picks it up (they are global-within-server session
# options), or scroll with the copy-mode fallback (Ctrl-b [, arrows, q).

set -g default-terminal "tmux-256color"
set -as terminal-features ",xterm-256color:RGB"
set -g set-clipboard on
set -sg escape-time 10
set -g focus-events on
# Let the inner program's title and notifications reach the outer terminal (ghostty/cmux),
# so a rename inside Claude renames the cmux tab. Claude sets its title with OSC 0; tmux only
# forwards a title to the client when set-titles is on, and the string is just the pane title
# so the tab shows exactly what the program set. Measured through nosync-wrap: set-titles off
# -> 0 title escapes reach the client; on -> the client receives ESC]0;<claude title>BEL.
# allow-passthrough lets OSC 9 / OSC 777 desktop-notification escapes through; monitor-bell
# surfaces Claude's attention bell.
set -g set-titles on
set -g set-titles-string "#{pane_title}"
set -g allow-passthrough on
set -g monitor-bell on
set -g bell-action any
set -g visual-bell off
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
  # `|| true`: on multi-user boxes the non-admin accounts have no general sudo, and the
  # binary is installed once as root anyway -- a failure here must not abort their setup.
  sudo install -m 755 /tmp/nosync-wrap /usr/local/bin/nosync-wrap 2>/dev/null || true
  rm -f /tmp/nosync-wrap
else
  echo "WARNING: could not fetch nosync-wrap from $TCRAW (native scrollback may be off)"
fi
USERENV

  shell_setup = <<-SHELLSETUP
sudo -u ubuntu bash << 'SHELL'
${local.user_shell_env}
SHELL
chsh -s /usr/bin/zsh ubuntu

# Dev-server-to-dev-server hops. Also DELETES ~/.ssh/fcvm-ec2 from this box: that is the
# jumpbox's key, nothing here used it (runner access goes via ~/.ssh/runner_key), and its
# presence meant anything able to read the file could SSH to the jumpbox -- where the AWS
# admin session lives -- from a box that runs agents with --dangerously-skip-permissions.
${local.dev_hop_setup}

# Private NFSv4 automount for the shared us-west-2 NVMe scratch box.
${local.io_box_client_setup}

${local.codex_remote_control}
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

# Host IPv6. IPv6 forwarding is on for the routed Firecracker VMs, and with forwarding on,
# networkd ignores router advertisements, so its DHCPv6 client never starts and the host has
# no IPv6 address of its own even though the ENI has one (ipv6_address_count in
# firecracker-dev.tf). cloud-init renders DHCPv4 only unless the ENI had an address at the
# instance's first boot, and it never re-renders on reboot. This drop-in requests DHCPv6 and
# accepts RAs on the primary interface, and netplan's generator reads it on every boot.
# Applying it live reconfigures the interface, so that only happens while the box is still
# booting (the bootstrap and dev-selfupdate run then); a later manual run leaves it for the
# next boot.
IPV6_IF=$(ip -o -4 route show default | awk '{print $5; exit}')
if [ -n "$IPV6_IF" ]; then
  IPV6_NETPLAN=$(printf 'network:\n  version: 2\n  ethernets:\n    %s:\n      dhcp6: true\n      accept-ra: true' "$IPV6_IF")
  if [ "$(cat /etc/netplan/60-fcvm-ipv6.yaml 2>/dev/null)" != "$IPV6_NETPLAN" ]; then
    printf '%s\n' "$IPV6_NETPLAN" > /etc/netplan/60-fcvm-ipv6.yaml
    chmod 600 /etc/netplan/60-fcvm-ipv6.yaml
    netplan generate || echo "WARNING: netplan generate failed for 60-fcvm-ipv6.yaml"
    case "$(systemctl is-system-running 2>/dev/null || true)" in
      initializing|starting) netplan apply || echo "WARNING: netplan apply failed; host IPv6 comes up on the next boot" ;;
      *) echo "host IPv6: netplan drop-in written; it takes effect on the next boot" ;;
    esac
  fi
fi

# Shell setup
${local.shell_setup}

# Claude Code -- the NATIVE installer, deliberately not npm.
#
# npm was the wrong channel. On 2026-08-26 the registry's "latest" was 2.1.241 while the
# native channel was already on 2.1.246, so npm installs were shipping a STALE claude. And
# because a global npm install lands in root-owned /usr/lib/node_modules, the ubuntu user
# cannot update it: every session start retried an auto-update, failed with install_failed,
# and put "Auto-update failed - Run claude doctor" in the TUI of all 12 sessions.
#
# Worse, the box ended up with BOTH installs and they disagreed about which claude you got:
# systemd sessions resolved the native 2.1.246 (its unit PATH puts ~/.local/bin first) while
# interactive shells resolved the npm 2.1.245, because ~/.local/bin was not on the login
# PATH. Same command, two different binaries, depending on how you arrived.
#
# The native installer writes under ~/.local, so it runs AS ubuntu with no sudo and can
# keep itself current afterwards.
# Test for the NATIVE binary specifically, never `command -v claude`: on a box that still
# has the npm copy, command -v succeeds, the native install is skipped, and the uninstall
# below then leaves the box with NO claude at all.
sudo -u ubuntu -H bash -c '[ -x "$HOME/.local/bin/claude" ] || curl -fsSL https://claude.ai/install.sh | bash' >/dev/null 2>&1 \
  || echo "WARNING: claude native install failed"

# ~/.local/bin has to PRECEDE /usr/bin, or a leftover npm copy keeps winning interactively.
sudo -u ubuntu -H bash -c 'grep -q "HOME/.local/bin" ~/.zshrc 2>/dev/null || printf "%s\n" "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> ~/.zshrc'

# Exactly one claude. Removing the root-owned npm copy is what stops the two installs from
# disagreeing; the native one above is already in place before this runs.
if sudo -u ubuntu -H test -x /home/ubuntu/.local/bin/claude; then
  npm uninstall -g @anthropic-ai/claude-code >/dev/null 2>&1 || true
else
  echo "WARNING: native claude missing -- keeping the npm copy rather than leaving no claude"
fi

${local.gh_and_claude_sync_script}

${local.metal_claude_remote_control}

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

# Claude Code -- the NATIVE installer, deliberately not npm.
#
# npm was the wrong channel. On 2026-08-26 the registry's "latest" was 2.1.241 while the
# native channel was already on 2.1.246, so npm installs were shipping a STALE claude. And
# because a global npm install lands in root-owned /usr/lib/node_modules, the ubuntu user
# cannot update it: every session start retried an auto-update, failed with install_failed,
# and put "Auto-update failed - Run claude doctor" in the TUI of all 12 sessions.
#
# Worse, the box ended up with BOTH installs and they disagreed about which claude you got:
# systemd sessions resolved the native 2.1.246 (its unit PATH puts ~/.local/bin first) while
# interactive shells resolved the npm 2.1.245, because ~/.local/bin was not on the login
# PATH. Same command, two different binaries, depending on how you arrived.
#
# The native installer writes under ~/.local, so it runs AS ubuntu with no sudo and can
# keep itself current afterwards.
# Test for the NATIVE binary specifically, never `command -v claude`: on a box that still
# has the npm copy, command -v succeeds, the native install is skipped, and the uninstall
# below then leaves the box with NO claude at all.
sudo -u ubuntu -H bash -c '[ -x "$HOME/.local/bin/claude" ] || curl -fsSL https://claude.ai/install.sh | bash' >/dev/null 2>&1 \
  || echo "WARNING: claude native install failed"

# ~/.local/bin has to PRECEDE /usr/bin, or a leftover npm copy keeps winning interactively.
sudo -u ubuntu -H bash -c 'grep -q "HOME/.local/bin" ~/.zshrc 2>/dev/null || printf "%s\n" "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> ~/.zshrc'

# Exactly one claude. Removing the root-owned npm copy is what stops the two installs from
# disagreeing; the native one above is already in place before this runs.
if sudo -u ubuntu -H test -x /home/ubuntu/.local/bin/claude; then
  npm uninstall -g @anthropic-ai/claude-code >/dev/null 2>&1 || true
else
  echo "WARNING: native claude missing -- keeping the npm copy rather than leaving no claude"
fi

${local.gh_and_claude_sync_script}

${local.metal_claude_remote_control}

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

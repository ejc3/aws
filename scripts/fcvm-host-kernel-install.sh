#!/bin/bash
# Install fcvm's arm64 host kernel (ejc3/fcvm#659: 7.3.3) on an fcvm-metal-arm dev box, and
# select it for the next boot. It does not reboot: the box is shared, so the owner picks the
# reboot. Idempotent: `make install-host-kernel` only selects the kernel when its package is
# already installed.
#
# Usage: fcvm-host-kernel-install.sh <fcvm commit>
set -euo pipefail
COMMIT=$1
[[ $COMMIT =~ ^[0-9a-f]{40}$ ]] || { echo "need a full fcvm commit, got '$COMMIT'" >&2; exit 2; }
[ "$(uname -m)" = aarch64 ] || { echo "arm64 only, this is $(uname -m)" >&2; exit 2; }
# A checkout of our own on the scratch disk, at the pinned commit; never the user's ~/fcvm.
SRC=/mnt/fcvm-btrfs/fcvm-host-kernel/$COMMIT
if [ ! -d "$SRC/.git" ]; then
  sudo -u ubuntu mkdir -p "$(dirname "$SRC")"
  sudo -u ubuntu git clone --quiet https://github.com/ejc3/fcvm.git "$SRC.tmp.$$"
  sudo -u ubuntu git -C "$SRC.tmp.$$" checkout --quiet --detach "$COMMIT"
  mv "$SRC.tmp.$$" "$SRC"
fi
[ "$(sudo -u ubuntu git -C "$SRC" rev-parse HEAD)" = "$COMMIT" ] || { echo "$SRC is not at $COMMIT" >&2; exit 1; }
# make install-host-kernel builds as the user and installs through sudo (the deb and GRUB).
sudo -u ubuntu -i bash -c "cd '$SRC' && make install-host-kernel"
want=$(awk '/^\[kernel_profiles.nested.arm64.host_kernel\]/{s=1} s && /^kernel_version/{gsub(/"/,"",$3); print $3; exit}' "$SRC/rootfs-config.toml")
dpkg -l | awk '{print $2}' | grep -q "^linux-image-${want}-fcvm-" || { echo "no linux-image-${want}-fcvm-* installed" >&2; exit 1; }
echo "installed linux-image-${want}-fcvm-*; selected for the next boot; running $(uname -r)"

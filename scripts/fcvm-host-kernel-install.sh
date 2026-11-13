#!/bin/bash
# Install the pinned fcvm ARM host kernel and verify its selection for the
# next boot. The running kernel and the operator's FCVM config stay in place.
set -euo pipefail

verify_kernel_selection() {
  local kernel=$1 boot=$2 grub_default=$3 selected status grub_env
  local prefix='GRUB_DEFAULT="Advanced options for Ubuntu>Ubuntu, with Linux '
  selected=$(grep '^GRUB_DEFAULT=' "$grub_default") || return 1
  [[ $selected == "$prefix$kernel"'"' ]] || return 1

  status=$(dpkg-query -W -f='${db:Status-Status}\n' "linux-image-$kernel") || return 1
  [[ $status == installed && -s "$boot/vmlinuz-$kernel" && -s "$boot/initrd.img-$kernel" ]] || return 1

  # The fcvm installer can warn and succeed if update-grub fails. Check both
  # the effective generated default and the entry it names, not just the input.
  grub-script-check "$boot/grub/grub.cfg" || return 1
  grep -Fx "   set default=\"${selected#GRUB_DEFAULT=\"}" "$boot/grub/grub.cfg" >/dev/null || return 1
  grep -E "^[[:space:]]*submenu 'Advanced options for Ubuntu'" "$boot/grub/grub.cfg" >/dev/null || return 1
  grep -E "^[[:space:]]*menuentry 'Ubuntu, with Linux ${kernel//./\\.}' " "$boot/grub/grub.cfg" >/dev/null || return 1
  grub_env=$(grub-editenv "$boot/grub/grubenv" list) || return 1
  if grep -E '^next_entry=.+' <<<"$grub_env" >/dev/null; then
    return 1
  fi
  printf '%s\n' "$kernel"
}

main() (
  local commit=${1:-} scratch want package selected
  [[ $commit =~ ^[0-9a-f]{40}$ ]] || { echo 'need a full lowercase fcvm commit' >&2; return 2; }
  [[ $(uname -m) == aarch64 ]] || { echo 'arm64 only' >&2; return 2; }
  [[ $(findmnt -n -o FSTYPE --mountpoint /mnt/fcvm-btrfs) == btrfs ]] ||
    { echo 'FCVM scratch must be mounted on /mnt/fcvm-btrfs' >&2; return 2; }

  # Always use a clean checkout and isolate the generated FCVM config:
  # upstream "make build" writes it with --force before installing the kernel.
  scratch=$(mktemp -d /mnt/fcvm-btrfs/host-kernel.XXXXXXXX)
  trap 'rm -rf -- "$scratch"' EXIT
  # Keep the root-written install log outside the user-owned directories.
  chmod 0711 "$scratch"
  install -d -o ubuntu -m 0700 "$scratch/src" "$scratch/config"
  sudo -u ubuntu git init --quiet "$scratch/src"
  sudo -u ubuntu git -C "$scratch/src" remote add origin https://github.com/ejc3/fcvm.git
  sudo -u ubuntu git -C "$scratch/src" fetch --quiet --depth=1 origin "$commit"
  sudo -u ubuntu git -C "$scratch/src" checkout --quiet --detach FETCH_HEAD
  [[ $(sudo -u ubuntu git -C "$scratch/src" rev-parse HEAD) == "$commit" ]] ||
    { echo 'fcvm checkout does not match its pin' >&2; return 1; }

  want=$(sudo -u ubuntu awk '/^\[kernel_profiles.nested.arm64.host_kernel\]/{s=1; next}
              s && /^\[/{exit} s && /^kernel_version/{gsub(/"/,"",$3); print $3; exit}' \
             "$scratch/src/rootfs-config.toml")
  [[ $want =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo 'missing pinned host kernel version' >&2; return 1; }
  sudo -u ubuntu -i env "FCVM_CONFIG_DIR=$scratch/config" make -C "$scratch/src" install-host-kernel |
    tee "$scratch/install.log"

  # Both fresh and idempotent pinned upstream installs report the exact image
  # package, which includes the patch hash. Fail closed if that contract changes.
  package=$(awk '/Host kernel (installed|already installed):/ {print $NF}' "$scratch/install.log")
  [[ $package == "linux-image-$want-fcvm-"* && ${package#"linux-image-$want-fcvm-"} =~ ^[0-9a-f]{12}$ ]] ||
    { echo 'cannot identify the exact fcvm kernel package from the install' >&2; return 1; }
  selected=$(verify_kernel_selection "${package#linux-image-}" /boot /etc/default/grub) ||
    { echo "cannot verify an installed fcvm $want kernel is selected in generated GRUB" >&2; return 1; }
  printf 'installed and selected for the next boot: %s; running: %s\n' "$selected" "$(uname -r)"
)

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
  main "$@"
fi

#!/bin/bash
# Expand the live ext4 root and add swap alongside the original active file.
set -euo pipefail
export LC_ALL=C
umask 077

fail() { echo "nextjs-storage: $*" >&2; exit 1; }
volume=$1
gib=$2
extra=/swapfile-extra
extra_bytes=$((12 * 1024 ** 3))
page=$(getconf PAGESIZE)
active_bytes() {
  swapon --show=NAME,SIZE --bytes --noheadings --raw | awk -v path="$1" '$1 == path {print $2}'
}

# A fresh boot's shared setup owns the first 4 GiB. Never replace or disable it.
cloud-init status --wait >/dev/null
exec 9>/run/nextjs-storage.lock
flock -w 120 9
[[ $(active_bytes /swapfile) == $((4 * 1024 ** 3 - page)) ]] || fail "original 4 GiB swapfile is not active"
findmnt --fstab -nr -t swap -S /swapfile >/dev/null || fail "original swapfile is missing from fstab"

root=$(findmnt -nro SOURCE /)
[[ $(findmnt -nro FSTYPE /) == ext4 && $(lsblk -dnro TYPE "$root") == part ]] || fail "expected an ext4 root partition"
disk="/dev/$(lsblk -dnro PKNAME "$root")"
part=$(lsblk -dnro PARTN "$root")
[[ $(lsblk -dnro SERIAL "$disk") == "${volume//-/}" ]] || fail "root disk does not match the Terraform EBS volume"
disk_bytes=$(blockdev --getsize64 "$disk")
(( disk_bytes >= gib * 1024 ** 3 )) || fail "kernel does not see the requested EBS size"

# The root is the last physical partition, after Ubuntu's EFI and boot partitions.
# Linux reports partition start in 512-byte sectors, regardless of logical block size.
start=$(lsblk -dnro START "$root")
if (( disk_bytes - start * 512 - $(blockdev --getsize64 "$root") > 1024 ** 2 )); then
  growpart "$disk" "$part"
  udevadm settle
fi
part_bytes=$(blockdev --getsize64 "$root")
(( disk_bytes - start * 512 - part_bytes <= 1024 ** 2 )) || fail "root partition did not expand"
resize2fs "$root"

if [[ ! -e $extra && ! -L $extra ]]; then
  (( $(df -B1 --output=avail / | tail -1) >= extra_bytes + 1024 ** 3 )) || fail "insufficient root space for supplemental swap"
  # ext4 supports unnamed temporary files. A crash releases the allocation until the
  # completed swapfile is linked into place, and link will never replace an existing file.
  python3 - "$extra" "$extra_bytes" <<'PYTHON'
import os
import subprocess
import sys

path, size = sys.argv[1:]
directory, basename = os.path.split(path)
dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
fd = os.open(directory, os.O_TMPFILE | os.O_RDWR, 0o600)
os.posix_fallocate(fd, 0, int(size))
ref = f"/proc/self/fd/{fd}"
subprocess.run(["mkswap", "-L", "nextjs-extra", ref], pass_fds=(fd,), check=True)
os.fsync(fd)
os.link(ref, basename, dst_dir_fd=dir_fd, follow_symlinks=True)
os.fsync(dir_fd)
PYTHON
fi
[[ -f $extra && ! -L $extra && $(stat -c '%u:%a:%s:%h' "$extra") == "0:600:$extra_bytes:1" ]] || fail "unexpected supplemental swapfile"
[[ $(blkid -p -s LABEL -o value "$extra") == nextjs-extra && $(blkid -p -s TYPE -o value "$extra") == swap ]] || fail "invalid supplemental swap header"
if [[ -z $(active_bytes "$extra") ]]; then swapon "$extra"; fi
[[ $(active_bytes "$extra") == $((extra_bytes - page)) ]] || fail "supplemental swap is not active"

if ! awk -v path="$extra" '$1==path {found=1} END {exit !found}' /etc/fstab; then
  printf '\n%s none swap sw 0 0\n' "$extra" >> /etc/fstab
fi
awk -v path="$extra" '$1==path {n++; if ($3=="swap" && $4=="sw") valid++} END {exit !(n==1 && valid==1)}' /etc/fstab || fail "unexpected supplemental fstab entry"
df -hT /
swapon --show

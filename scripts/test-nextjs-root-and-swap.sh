#!/bin/bash
# Run the real convergence against temporary files and a simulated EBS/ext4 device.
set -euo pipefail
source_script=$(realpath "$(dirname "$0")/nextjs-root-and-swap.sh")
sandbox=$(mktemp -d)
trap 'rm -rf "$sandbox"' EXIT
cd "$sandbox"
export sandbox
sed -e "s|/swapfile|$sandbox/swapfile|g" -e "s|/etc/fstab|$sandbox/fstab|g" \
    -e "s|/run/nextjs-storage.lock|$sandbox/lock|g" \
    -e 's/12 \* 1024 \*\* 3/8 * 1024 ** 2/' "$source_script" > converge
# Fail before touching disk if the production size expression changed.
grep -Fxq "extra_bytes=\$((8 * 1024 ** 2))" converge
printf '%s/swapfile none swap sw 0 0\n' "$sandbox" > fstab
printf '%s/swapfile %d\n' "$sandbox" "$((4 * 1024**3 - $(getconf PAGESIZE)))" > active
printf '%s\n' "$((699 * 1024**3))" > part-size
: > actions

cloud-init() { [[ "$*" == 'status --wait' ]]; }
findmnt() {
  case "$*" in
    '-nro SOURCE /') echo /dev/mock1 ;;
    '-nro FSTYPE /') echo ext4 ;;
    "--fstab -nr -t swap -S $sandbox/swapfile") grep -Fqx "$sandbox/swapfile none swap sw 0 0" fstab ;;
    *) return 1 ;;
  esac
}
lsblk() {
  case "$2:$3" in
    TYPE:/dev/mock1) echo part ;; PKNAME:/dev/mock1) echo mock ;;
    PARTN:/dev/mock1) echo 1 ;; START:/dev/mock1) echo 2048 ;;
    SERIAL:/dev/mock) echo vol123 ;;
    *) return 1 ;;
  esac
}
blockdev() {
  case "$2" in
    /dev/mock) echo "$((900 * 1024**3))" ;;
    /dev/mock1) cat part-size ;;
    *) return 1 ;;
  esac
}
growpart() {
  [[ "$*" == '/dev/mock 1' ]] || return 1
  echo growpart >>actions
  echo "$((900 * 1024**3 - 2048 * 512))" > part-size
}
udevadm() { [[ "$*" == settle ]]; }
resize2fs() { [[ "$1" == /dev/mock1 ]] && echo resize2fs >>actions; }
df() { echo "$((50 * 1024**3))"; }
stat() {
  if [[ "$2" == '%u:'* ]]; then command stat -c "${2/#%u/0}" "$3"; else command stat "$@"; fi
}
swapon() {
  if [[ "$1" == --show* ]]; then cat active; return; fi
  [[ "$1" == "$sandbox/swapfile-extra" ]] || return 1
  printf '%s %d\n' "$1" "$(( $(stat -c %s "$1") - $(getconf PAGESIZE) ))" >>active
  echo swapon >>actions
}
export -f cloud-init findmnt lsblk blockdev growpart udevadm resize2fs df stat swapon

# Wrong EBS serial must fail before growing or formatting anything.
if bash converge vol-unrelated 900 >log 2>&1; then exit 1; fi
grep -q 'does not match' log
[[ ! -s actions && ! -e swapfile-extra ]]

# An existing unrecognized file stays byte-for-byte unchanged and never becomes swap.
printf 'unrelated file' > swapfile-extra
chmod 600 swapfile-extra
truncate -s 8M swapfile-extra
before=$(sha256sum swapfile-extra)
if bash converge vol-123 900 >log 2>&1; then exit 1; fi
grep -q 'invalid supplemental swap header' log
[[ $(sha256sum swapfile-extra) == "$before" ]]
if grep -qx swapon actions; then exit 1; fi
rm swapfile-extra

# Both files are active and persistent; replay preserves the existing inode and signature.
bash converge vol-123 900 >log
[[ $(blkid -p -s TYPE -o value swapfile-extra) == swap ]]
[[ $(stat -c %s swapfile-extra) == $((8 * 1024 ** 2)) ]]
before=$(command stat -c '%i:%s:%Y' swapfile-extra)
bash converge vol-123 900 >>log
[[ $(command stat -c '%i:%s:%Y' swapfile-extra) == "$before" ]]
for op in growpart swapon; do [[ $(grep -cx "$op" actions) == 1 ]]; done
[[ $(wc -l < active) == 2 ]]
for file in swapfile swapfile-extra; do [[ $(grep -Fxc "$sandbox/$file none swap sw 0 0" fstab) == 1 ]]; done
echo 'PASS: disk identity, foreign file protection, online growth, persistent additive swap and replay'

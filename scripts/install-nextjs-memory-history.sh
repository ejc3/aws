#!/bin/bash
# Run as root with the checked-in executable and units in one directory.
set -euo pipefail
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 077
src=${1:?directory containing the verified memory recorder files}
[[ $(id -u) -eq 0 ]] || { echo "nextjs-memory-history: installer requires root" >&2; exit 1; }
exec 9>/run/nextjs-memory-history-install.lock
flock -w 15 9

publish() {
    local mode=$1 source=$2 target=$3 tmp
    if [[ -f "$target" && ! -L "$target" ]] && cmp -s -- "$source" "$target" \
        && [[ $(stat -c '%u:%g:%a' -- "$target") == "0:0:$mode" ]]; then
        return
    fi
    tmp=$(mktemp "$target.new.XXXXXXXX")
    if ! install -o root -g root -m "$mode" -- "$source" "$tmp" || ! mv -fT -- "$tmp" "$target"; then
        rm -f -- "$tmp"
        echo "nextjs-memory-history: could not publish $target" >&2
        return 1
    fi
}

publish 755 "$src/nextjs-memory-history.py" /usr/local/sbin/nextjs-memory-history
publish 644 "$src/nextjs-memory-history.service" /etc/systemd/system/nextjs-memory-history.service
publish 644 "$src/nextjs-memory-history.timer" /etc/systemd/system/nextjs-memory-history.timer

systemctl daemon-reload
systemctl enable --now nextjs-memory-history.timer
systemctl is-enabled --quiet nextjs-memory-history.timer
systemctl is-active --quiet nextjs-memory-history.timer

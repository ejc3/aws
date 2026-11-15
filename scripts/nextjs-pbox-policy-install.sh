#!/bin/bash
# Run by State Manager inside the Next.js host, not on the jumpbox or inside a guest.
set -euo pipefail
umask 077
directory=${1:?directory containing the checked policy and worker}
shift
test "$#" -gt 0
[[ $(id -u) == 0 ]]
exec 9>/run/nextjs-ct-setup.lock
flock -w 120 9

# Check the expected mounted home before entering each guest. Use the kernel mount
# table so host root never follows a guest-controlled path.
ready() {
    local account=$1 leader
    test "$(/usr/bin/machinectl show "$account" -p State --value)" = running || return 1
    leader=$(/usr/bin/machinectl show "$account" -p Leader --value) || return 1
    [[ "$leader" =~ ^[1-9][0-9]*$ && "$leader" != 1 ]] || return 1
    awk -v home="/home/$account" '$5 == home {found = 1} END {exit !found}' "/proc/$leader/mountinfo"
}

policy=$(base64 -w0 "$directory/nextjs-pbox-policy.md")
rc=0
for account in "$@"; do
    if ! [[ "$account" =~ ^[a-z][a-z0-9_-]*$ ]] || ! ready "$account"; then
        printf 'Expected Next.js guest or home is not ready: %s\n' "$account" >&2
        rc=1
        continue
    fi
    for mode in claude codex; do
        user=root
        [[ "$mode" != codex ]] || user=$account
        /usr/bin/systemd-run --machine="$account" --wait --pipe --collect --quiet --service-type=exec \
            --property=RuntimeMaxSec=20s --uid="$user" \
            /usr/bin/python3 -I - "$mode" "$account" "$policy" < "$directory/nextjs-pbox-policy.py" || rc=1
    done
done
exit "$rc"

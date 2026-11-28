# Run on fcvm-arm as ubuntu with antigravity_tunnel_down from the fleet installer.
# $1 is the UTF-16LE-encoded PowerShell dispatcher. $2 and $3 contain the installer and guide for stdin.
set -eu
errors=$(mktemp)
trap 'rm -f -- "$errors"' EXIT
if printf '%s\n%s\n' "$2" "$3" | timeout 540 ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o ForwardAgent=no \
    colin-pc "powershell -NoProfile -NonInteractive -EncodedCommand $1" 2> "$errors"; then
  cat "$errors" >&2
  exit 0
else
  rc=$?
fi
if [[ $rc = 255 ]] && grep -Fxq 'websocket: bad handshake' "$errors" && antigravity_tunnel_down colin-pc; then
  echo 'colin-pc deferred: Cloudflare reports its tunnel offline (1033); retry next hour.'
  exit 0
fi
cat "$errors" >&2
exit "$rc"

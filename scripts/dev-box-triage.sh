#!/usr/bin/env bash
# Triage the shared dev box: is this us, or is it Anthropic/Cloudflare?
#
# WHY THIS EXISTS: on 2026-08-16 the box "broke" four separate ways in one evening, and every
# wrong turn came from measuring a proxy for health instead of health itself. This script is
# the set of checks that actually distinguished cause from symptom, in the order that answers
# the question fastest -- upstream first, because three of the four were not our fault.
#
#   * A 302 on a preview URL is the Cloudflare Access SSO gate, served at the EDGE. It is
#     returned whether or not cloudflared is running and whether or not the origin exists.
#     Half an hour was spent reporting "tunnels healthy" from that signal while BOTH tunnels
#     were down. The tunnel's own status comes from the Cloudflare API, and only the
#     cloudflare-tunnel-token secret can read it (the api/account tokens return 401).
#
#   * Remote Control failing with "Session creation failed" is usually THEIRS, not ours. A
#     packet capture showed the body is {"type":"overloaded_error","message":"Overloaded"}
#     with a 503, during a published Anthropic incident. Restarting a session in response
#     destroys a live conversation and cannot help, so this script never restarts anything.
#
#   * Memory is the box's real constraint and CloudWatch does not collect it by default. When
#     RAM runs out the kernel pages to /swapfile, which lives on the root volume, and swap-in
#     reads pin the gp3 volume at its 125 MB/s ceiling. With the disk saturated, sshd cannot
#     complete a handshake -- the box looks hung while the instance status check says "ok".
#     The tell is VolumeReadBytes at the ceiling with VolumeTotalWriteTime near zero.
#
#   * IPv6 egress was missing from the security group for three weeks. It only bit endpoints
#     that are genuinely dual-stack (secretsmanager), so most things worked and a few hung for
#     60-90s in SYN-SENT. Any "sometimes slow, sometimes fine" AWS call deserves a v6 check.
#
# READ-ONLY BY DESIGN. It restarts nothing, kills nothing, and reboots nothing. Ask a human
# before any of that -- during an upstream incident those actions only lose work.
#
# Usage: scripts/dev-box-triage.sh [--host IP] [--quiet]

set -uo pipefail

HOST="${DEV_BOX_HOST:-54.241.72.29}"
KEY="${DEV_BOX_KEY:-$HOME/.ssh/fcvm-ec2}"
REGION="${AWS_REGION:-us-west-1}"
INSTANCE="${DEV_BOX_INSTANCE:-i-0709826a574337214}"
VOLUME="${DEV_BOX_VOLUME:-vol-0c9a32118a5f99240}"
ACCOUNT="${CF_ACCOUNT_ID:-12ea67fb7ced068de03f35c22688e436}"
CC_GAMES_TUNNEL=60234535-279b-4b20-bbc3-7fd353abb7f6
DOLPHIN_TUNNEL=92ad014f-2aa1-4a6e-b700-fa6bd3f35663
USERS=(colton connor ejc3 skevh)

while [ $# -gt 0 ]; do
  case "$1" in
    --host) HOST="$2"; shift 2 ;;
    --quiet) QUIET=1; shift ;;
    *) shift ;;
  esac
done

say() { printf '%s\n' "$*"; }
hdr() { printf '\n== %s ==\n' "$*"; }
ssh_box() { timeout 30 ssh -i "$KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=10 "ubuntu@$HOST" "$@" 2>/dev/null; }

verdict_upstream=0
verdict_local=0

# ---------------------------------------------------------------------------- upstream first
# Cheapest check, and most likely answer. If Anthropic is in an incident, stop here: the
# session errors, the /rc failures and the 503s are theirs, and nothing local will fix them.
hdr "Anthropic status"
# The JSON is PIPED in, not heredoc'd next to the program: `python3 - <<PROG <<<"$json"` has
# two stdin redirections and the last wins, so python would execute the JSON and die on
# `null`. That crash exits 1 -- which an "incident?" check reads as "yes, incident". A broken
# status check must never be able to impersonate a detected outage, so parse failure is its
# own exit code and is reported as UNKNOWN.
status_json=$(curl -s --max-time 15 https://status.claude.com/api/v2/summary.json 2>/dev/null)
if [ -n "$status_json" ]; then
  printf '%s' "$status_json" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except Exception as e:
    print("  could not parse the status feed: %s" % str(e)[:60]); sys.exit(2)
print("  overall: %s" % (d.get("status") or {}).get("description", "?"))
active = [i for i in d.get("incidents", []) if i.get("status") != "resolved"]
for i in active[:5]:
    print("  INCIDENT: %s [%s] %s" % (i.get("name"), i.get("status"), (i.get("updated_at") or "")[:19]))
    names = ", ".join(c.get("name", "?") for c in i.get("components", []))
    if names:
        print("            affects: %s" % names)
if not active:
    print("  no active incidents")
sys.exit(1 if active else 0)
'
  case "$?" in
    1) verdict_upstream=1 ;;
    2) say "  status feed unreadable -- UNKNOWN, not healthy" ;;
  esac
else
  say "  could not reach the status page -- UNKNOWN, not healthy"
fi

# ------------------------------------------------------------------------------ real tunnels
# NOT a curl of the hostname: that returns 302 from Access even when the tunnel is dead.
hdr "Cloudflare tunnels (authoritative)"
# The token stays out of xtrace (`bash -x` on this script) and off curl's command line, which
# any local user can read from /proc/<pid>/cmdline. Tracing is off while the token is held,
# the header reaches curl on stdin from the printf builtin, and the caller's tracing returns.
case $- in *x*) cf_xtrace=1 ;; *) cf_xtrace=0 ;; esac
{ set +x; } 2>/dev/null
CF_TOKEN=$(aws secretsmanager get-secret-value --secret-id cloudflare-tunnel-token \
             --region "$REGION" --query SecretString --output text 2>/dev/null)
if [ -n "$CF_TOKEN" ]; then
  for pair in "cc-games:$CC_GAMES_TUNNEL" "dolphin-labs:$DOLPHIN_TUNNEL"; do
    name="${pair%%:*}"; tid="${pair##*:}"
    body=$(printf 'Authorization: Bearer %s\n' "$CF_TOKEN" | curl -s --max-time 15 -H @- \
      "https://api.cloudflare.com/client/v4/accounts/$ACCOUNT/cfd_tunnel/$tid" 2>/dev/null)
    line=$(python3 - <<PY
import json
try:
    r = json.loads('''$body''').get("result") or {}
    print("%-13s status=%-8s connections=%d" % ("$name", r.get("status"), len(r.get("connections") or [])))
except Exception:
    print("%-13s unreadable" % "$name")
PY
)
    say "  $line"
    case "$line" in *"status=down"*|*"connections=0"*) verdict_local=1 ;; esac
  done
else
  say "  no cloudflare-tunnel-token (the api/account tokens cannot read tunnel status)"
fi
unset CF_TOKEN
if [ "$cf_xtrace" = 1 ]; then set -x; fi

# -------------------------------------------------------------------------------- box vitals
hdr "Box vitals"
if ssh_box true; then
  ssh_box 'bash -s' <<'REMOTE' | sed 's/^/  /'
free -m | awk 'NR==2{printf "memory: %s/%s MB used, %s available\n", $3, $2, $7}
              NR==3{printf "swap:   %s/%s MB used%s\n", $3, $2, ($3>0 ? "   <-- paging; swapfile is on the root volume" : "")}'
df -h / | awk 'NR==2{printf "disk:   %s used of %s (%s free)\n", $5, $2, $4}'
for z in cc-games.dev dolphin-labs.dev; do
  printf "cloudflared@%-17s %s\n" "$z" "$(systemctl is-active cloudflared@$z)"
done
for u in colton connor ejc3 skevh; do
  p=$(grep -oP '(?<=^PORT=).*' /var/lib/ndev/$u.env 2>/dev/null)
  [ -n "$p" ] || continue
  printf "ndev@%-7s %-8s origin=%s\n" "$u" "$(systemctl is-active ndev@$u)" \
    "$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:$p/ 2>/dev/null)"
done
REMOTE
else
  say "  SSH DID NOT ANSWER."
  say "  Before assuming the box is down: a saturated disk starves sshd's handshake while the"
  say "  instance status check still reads ok. Check the EBS numbers below, then use SSM"
  say "  (aws ssm start-session) rather than rebooting."
  verdict_local=1
fi

# ------------------------------------------------------------------------- the starvation tell
hdr "Root volume (the swap-thrash signature)"
end=$(date -u +%Y-%m-%dT%H:%M:%SZ); start=$(date -u -d '20 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
for m in VolumeReadBytes VolumeQueueLength; do
  vals=$(aws cloudwatch get-metric-statistics --region "$REGION" --namespace AWS/EBS \
    --metric-name "$m" --dimensions Name=VolumeId,Value="$VOLUME" \
    --start-time "$start" --end-time "$end" --period 300 \
    --statistics "$( [ "$m" = VolumeReadBytes ] && echo Sum || echo Average )" \
    --query 'sort_by(Datapoints,&Timestamp)[-3:].[Timestamp,'"$( [ "$m" = VolumeReadBytes ] && echo Sum || echo Average )"']' \
    --output text 2>/dev/null)
  say "  $m:"
  [ -n "$vals" ] && printf '%s\n' "$vals" | awk -v m="$m" '{
    if (m == "VolumeReadBytes") printf "    %s  %.1f MB/min\n", substr($1,12,5), $2/1048576/5
    else printf "    %s  %.2f\n", substr($1,12,5), $2 }'
done
say "  (~125 MB/s sustained with writes near zero = paging, not a runaway process)"

# ----------------------------------------------------------------------------- remote control
hdr "Claude Remote Control"
if ssh_box true; then
  for u in "${USERS[@]}"; do
    st=$(ssh_box "sudo -u $u -H env HOME=/home/$u tmux capture-pane -p -t \$(sudo -u $u -H env HOME=/home/$u tmux list-windows -a -F '#{window_id}' 2>/dev/null | head -1) 2>/dev/null | grep -oE '/rc [a-z]+' | tail -1")
    printf '  %-7s %s\n' "$u" "${st:-no session}"
  done
  say "  /rc failed during an Anthropic incident is THEIRS. Do not restart the unit:"
  say "  the conversation dies and the 503 does not care. It re-arms itself."
fi

# ------------------------------------------------------------------------------------ verdict
hdr "Verdict"
if [ "$verdict_upstream" = 1 ]; then
  say "  UPSTREAM incident is active. Expect session errors, /rc failures and 503s."
  say "  Wait it out. Do not restart services or reboot -- you will only lose work."
elif [ "$verdict_local" = 1 ]; then
  say "  Something local looks wrong (tunnel down, or SSH not answering)."
  say "  Work the vitals above before touching anything, and ask before restarting."
else
  say "  No upstream incident and nothing obviously local. If a user reports a problem,"
  say "  trust them and dig further -- do not close it on these checks alone."
fi

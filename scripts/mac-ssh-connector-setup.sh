#!/usr/bin/env bash
#
# mac-ssh-connector-setup -- put a kid's Mac on its Cloudflare route (colton-mac-ssh.tf): the pinned cloudflared, the
# connector LaunchAgent with its token, and the clients' public keys. Run on a jumpbox as an AWS administrator.
#
#   scripts/mac-ssh-connector-setup.sh colton-mac                       over the Mac's Cloudflare route (a re-run)
#   scripts/mac-ssh-connector-setup.sh colton-mac --bootstrap-port 2224 over a temporary reverse tunnel the Mac keeps
#                                                                       to fcvm-arm's 127.0.0.1:2224 (the first run)
#
# It reaches the Mac from fcvm-arm as `ssh colton-mac` (mac-ssh-client.tf), which is fcvm-arm's own key and Host key pin;
# --bootstrap-port only points that alias at 127.0.0.1:PORT with no proxy command, for the first run, when the
# connector is not there yet. On the Mac it installs, in the user's home only:
#   * ~/.local/bin/cloudflared, pinned by version and sha256 (the pins in scripts/claw-master-mac-setup.sh; a test keeps
#     them equal) and verified ON the Mac
#   * ~/.cloudflared/<alias>-ssh.token (0600): the connector token, from the administration-only secret, through ssh
#     STDIN only: never an argument, never written on fcvm-arm or here
#   * the LaunchAgent com.ccgames.<alias>-ssh.cloudflared: `cloudflared tunnel --no-autoupdate run --token-file ...`,
#     started at login and restarted by launchd when it drops
#   * a managed block in ~/.ssh/authorized_keys with each client's ~/.ssh/to-<alias>.pub, read from the clients' boxes
#     (fcvm-arm over ssh, nextjs-dev over SSM Run Command); every other line is kept, and a line elsewhere for one of
#     those keys (the bootstrap tunnel's) is folded into the block
# Re-running is safe: it re-verifies cloudflared, rewrites the files and reloads the agent. Reloading the agent ends the
# sessions that came through it, so the first run should come over the bootstrap tunnel.
set -euo pipefail

REGION=${AWS_REGION:-us-west-1}
FCVM=${FCVM_HOST:-184.72.40.255} # fcvm-metal-arm's Elastic IP
KEY=${FCVM_KEY:-$HOME/.ssh/fcvm-ec2}

# Pins: keep equal to scripts/claw-master-mac-setup.sh (scripts/test-mac-ssh-client.py enforces it).
CFD_VERSION=2026.9.3
CFD_SHA256_DARWIN_ARM64_TGZ=587c2cfb1c230fe36c7fa7727da78be459dae028cabe8c001291999350f07095
CFD_SHA256_DARWIN_ARM64_BIN=5472c1a01c84bc31b3021056a73b4e5774ddddefc572124ea8fdf6c340639f32

usage() { echo "usage: mac-ssh-connector-setup.sh ALIAS [--bootstrap-port PORT]   (ALIAS: colton-mac)" >&2; exit 2; }
ALIAS=${1:-}
shift || true
BOOTSTRAP_PORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bootstrap-port) [ $# -ge 2 ] || usage; BOOTSTRAP_PORT=$2; shift 2 ;;
    *) usage ;;
  esac
done
[ -z "$BOOTSTRAP_PORT" ] || [[ $BOOTSTRAP_PORT =~ ^[0-9]{4,5}$ ]] || usage

# Per Mac: its connector-token secret and its clients (box:account), matching colton-mac-ssh.tf and mac-ssh-client.tf.
case "$ALIAS" in
  colton-mac)
    TUNNEL_SECRET=colton-mac-ssh-tunnel-token
    CLIENTS="fcvm:ubuntu nextjs:colton nextjs:ejc3"
    ;;
  *) usage ;;
esac
LABEL="com.ccgames.$ALIAS-ssh.cloudflared"
TOKEN_FILE=".cloudflared/$ALIAS-ssh.token" # relative to the Mac user's home

MAC_SSH="$ALIAS"
if [ -n "$BOOTSTRAP_PORT" ]; then
  MAC_SSH="-o ProxyCommand=none -o HostName=127.0.0.1 -o Port=$BOOTSTRAP_PORT $ALIAS"
fi
on_fcvm() { ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$KEY" "ubuntu@$FCVM" "$@"; }
# Run something on the Mac: jumpbox -> fcvm-arm -> the Mac. stdin is passed through.
on_mac() { on_fcvm "ssh -o BatchMode=yes -o ConnectTimeout=10 $MAC_SSH $*"; }

PUBKEY_RE='^ssh-ed25519 AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{48}( .*)?$'

echo "1/5 reaching $ALIAS from fcvm-arm"
[ "$(on_mac "'uname -sm'")" = "Darwin arm64" ] || { echo "$ALIAS is not reachable, or is not a macOS arm64 Mac" >&2; exit 1; }

echo "2/5 the clients' public keys"
KEYS=""
NEXTJS_USERS=""
for client in $CLIENTS; do
  box=${client%%:*} user=${client#*:}
  case "$box" in
    fcvm) [ "$user" = ubuntu ] || { echo "only ubuntu on fcvm-arm" >&2; exit 1; }
          pub=$(on_fcvm "cat ~/.ssh/to-$ALIAS.pub" 2>/dev/null || true)
          [[ $pub =~ $PUBKEY_RE ]] || { echo "fcvm-arm ubuntu has no ~/.ssh/to-$ALIAS.pub yet (mac-ssh-client.tf makes it)" >&2; exit 1; }
          KEYS+="$(printf '%s' "$pub" | cut -d' ' -f1-2) fcvm-arm ubuntu -> $ALIAS"$'\n' ;;
    nextjs) NEXTJS_USERS+=" $user" ;;
    *) echo "unknown box $box" >&2; exit 1 ;;
  esac
done
if [ -n "$NEXTJS_USERS" ]; then
  NEXTJS_ID=$(aws ec2 describe-instances --region "$REGION" --filters Name=tag:Name,Values=nextjs-dev Name=instance-state-name,Values=running \
    --query 'Reservations[0].Instances[0].InstanceId' --output text)
  [ -n "$NEXTJS_ID" ] && [ "$NEXTJS_ID" != None ] || { echo "nextjs-dev is not running" >&2; exit 1; }
  CMD=""
  for user in $NEXTJS_USERS; do
    [[ $user =~ ^[a-z][a-z0-9]*$ ]] || { echo "bad account $user" >&2; exit 1; }
    CMD+="printf '$user '; cat /home/$user/.ssh/to-$ALIAS.pub 2>/dev/null || echo MISSING; "
  done
  CMD_ID=$(aws ssm send-command --region "$REGION" --instance-ids "$NEXTJS_ID" --document-name AWS-RunShellScript \
    --parameters "$(python3 -c 'import json,sys; print(json.dumps({"commands": [sys.argv[1]]}))' "$CMD")" \
    --query Command.CommandId --output text)
  aws ssm wait command-executed --region "$REGION" --command-id "$CMD_ID" --instance-id "$NEXTJS_ID" || true
  OUT=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$NEXTJS_ID" \
    --query StandardOutputContent --output text)
  for user in $NEXTJS_USERS; do
    pub=$(printf '%s\n' "$OUT" | awk -v u="$user" '$1 == u { $1 = ""; sub(/^ /, ""); print; exit }')
    [[ $pub =~ $PUBKEY_RE ]] || { echo "nextjs-dev $user has no ~/.ssh/to-$ALIAS.pub yet (setup-sync makes it within 10 minutes of the apply)" >&2; exit 1; }
    KEYS+="$(printf '%s' "$pub" | cut -d' ' -f1-2) nextjs-dev $user -> $ALIAS"$'\n'
  done
fi
printf '%s' "$KEYS" | sed 's/^/   /'

echo "3/5 cloudflared $CFD_VERSION (verified on the Mac)"
on_mac bash -s <<REMOTE
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
mkdir -p "\$HOME/.local/bin"
tmp=\$(mktemp -d) && trap 'rm -rf "\$tmp"' EXIT
sum() { shasum -a 256 "\$1" | cut -d' ' -f1; }
# Judged by the hash of the installed EXECUTABLE, never by the version it reports.
if [ "\$(sum "\$HOME/.local/bin/cloudflared" 2>/dev/null || true)" != "$CFD_SHA256_DARWIN_ARM64_BIN" ]; then
  curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/$CFD_VERSION/cloudflared-darwin-arm64.tgz" -o "\$tmp/cfd.tgz"
  [ "\$(sum "\$tmp/cfd.tgz")" = "$CFD_SHA256_DARWIN_ARM64_TGZ" ] || { echo "cloudflared did not match its pinned sha256" >&2; exit 1; }
  tar -xzf "\$tmp/cfd.tgz" -C "\$tmp" cloudflared && chmod 755 "\$tmp/cloudflared"
  [ "\$(sum "\$tmp/cloudflared")" = "$CFD_SHA256_DARWIN_ARM64_BIN" ] || { echo "the cloudflared executable did not match its pinned sha256" >&2; exit 1; }
  mv -f "\$tmp/cloudflared" "\$HOME/.local/bin/cloudflared"
fi
"\$HOME/.local/bin/cloudflared" --version | head -1
REMOTE

echo "4/5 the connector token (from Secrets Manager, through ssh stdin only)"
{ set +x; } 2>/dev/null
aws secretsmanager get-secret-value --region "$REGION" --secret-id "$TUNNEL_SECRET" --query SecretString --output text |
  on_mac "'umask 077; mkdir -p \$HOME/.cloudflared && cat > \$HOME/$TOKEN_FILE.new && [ -s \$HOME/$TOKEN_FILE.new ] && mv -f \$HOME/$TOKEN_FILE.new \$HOME/$TOKEN_FILE && chmod 600 \$HOME/$TOKEN_FILE'"

echo "5/5 the clients' keys and the connector LaunchAgent"
on_mac bash -s <<REMOTE
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
BEGIN='# >>> $ALIAS clients (managed by aws scripts/mac-ssh-connector-setup.sh) >>>'
END='# <<< $ALIAS clients <<<'
AK="\$HOME/.ssh/authorized_keys"
mkdir -p "\$HOME/.ssh" && chmod 700 "\$HOME/.ssh"
touch "\$AK"
cat > "\$AK.keys" <<'KEYS'
$KEYS
KEYS
# Keep every line except our previous block (only a complete one) and any other line for one of these keys.
if grep -qxF "\$END" "\$AK"; then whole=1; else whole=0; fi
awk -v b="\$BEGIN" -v e="\$END" -v whole="\$whole" -v keys="\$AK.keys" '
  BEGIN { while ((getline line < keys) > 0) { split(line, f, " "); if (f[2] != "") managed[f[2]] = 1 } }
  whole && \$0 == b { inblock = 1; next }
  inblock { if (\$0 == e) inblock = 0; next }
  { for (i = 1; i <= NF; i++) if (\$i in managed && \$0 !~ /^#/) next; print }
' "\$AK" > "\$AK.new"
{ echo "\$BEGIN"; grep -v '^\$' "\$AK.keys"; echo "\$END"; } >> "\$AK.new"
echo "authorized_keys: \$(grep -c '^ssh-' "\$AK.keys") managed keys"
rm -f "\$AK.keys"
chmod 600 "\$AK.new" && mv -f "\$AK.new" "\$AK"

PLIST="\$HOME/Library/LaunchAgents/$LABEL.plist"
mkdir -p "\$HOME/Library/LaunchAgents" "\$HOME/Library/Logs"
cat > "\$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>\$HOME/.local/bin/cloudflared</string><string>tunnel</string><string>--no-autoupdate</string>
    <string>run</string><string>--token-file</string><string>\$HOME/$TOKEN_FILE</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardErrorPath</key><string>\$HOME/Library/Logs/$ALIAS-ssh-cloudflared.log</string>
</dict></plist>
PL
chmod 644 "\$PLIST"
plutil -lint "\$PLIST" >/dev/null
uid=\$(id -u)
launchctl bootout "gui/\$uid/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/\$uid" "\$PLIST" && echo "connector agent loaded"
sleep 5
echo "agent: \$(launchctl print "gui/\$uid/$LABEL" 2>/dev/null | awk '/state =/{print \$3; exit}')"
tail -3 "\$HOME/Library/Logs/$ALIAS-ssh-cloudflared.log" 2>/dev/null | cut -c1-160 || true
REMOTE
echo "done. From each client: ssh $ALIAS true"

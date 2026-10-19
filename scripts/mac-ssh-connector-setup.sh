#!/usr/bin/env bash
#
# mac-ssh-connector-setup -- put a Mac on its Cloudflare route (macbook-ssh.tf for `mac`, colton-mac-ssh.tf for
# `colton-mac`): the pinned cloudflared, the connector LaunchAgent with its token, the clients' public keys and a
# keys-only Remote Login. Run on a jumpbox as an AWS administrator.
#
#   scripts/mac-ssh-connector-setup.sh mac                              EJ's MacBook, over its own route
#   scripts/mac-ssh-connector-setup.sh colton-mac                       over the Mac's Cloudflare route (a re-run)
#   scripts/mac-ssh-connector-setup.sh colton-mac --bootstrap-port 2224 over a temporary reverse tunnel the Mac keeps
#                                                                       to fcvm-arm's 127.0.0.1:2224 (the first run)
#
# It reaches the Mac from fcvm-arm as `ssh <alias>` (mac-ssh-client.tf), which is fcvm-arm's own key and Host key pin;
# --bootstrap-port only points that alias at 127.0.0.1:PORT with no proxy command, for the first run, when the
# connector is not there yet. First it checks that Remote Login refuses password and keyboard-interactive logins,
# installing the MacBook's keys-only drop-in (/etc/ssh/sshd_config.d/050-keys-only.conf) when sudo allows it without a
# password, and stopping with the command to type at the Mac when it does not. Then, in the user's home only:
#   * ~/.local/bin/cloudflared, pinned by version and sha256 (the pins in scripts/claw-master-mac-setup.sh; a test keeps
#     them equal) and verified ON the Mac
#   * ~/.cloudflared/<alias>-ssh.token (0600): the connector token, from its administration-only secret, through ssh
#     STDIN only: never an argument, never written on fcvm-arm or here. On the Mac it belongs to the account every
#     client logs in as, so any client can read it: a connector run with it can only take this tunnel's traffic and
#     break connections, never get in (Access still gates every request) nor pass for the Mac (clients pin its host key)
#   * the LaunchAgent com.ccgames.<alias>-ssh.cloudflared: `cloudflared tunnel --no-autoupdate run --token-file ...`,
#     started at login and restarted by launchd when it drops
#   * a managed block in ~/.ssh/authorized_keys with each client's ~/.ssh/to-<alias>.pub, read from the clients' boxes
#     (fcvm-arm over ssh, each nextjs-ct account's own file over its own SSM Run Command) and kept only if it is exactly
#     one `ssh-ed25519 <blob>`; every other line is kept, and a line elsewhere for one of those keys (the bootstrap
#     tunnel's) is folded into the block
# Re-running is safe: a file whose content is already right is left alone, and the connector is reloaded only when its
# LaunchAgent or its token changed or it is not loaded. A reload ends every session through the connector, this one too
# when it came that way, so it runs detached and still completes after the session drops. On a Mac already set up the
# same way (the MacBook, whose connector was made by hand with these names) a run changes nothing that matters: the
# clients' keys move into the managed block, the same keys.
set -euo pipefail

REGION=${AWS_REGION:-us-west-1}
FCVM=${FCVM_HOST:-184.72.40.255} # fcvm-metal-arm's Elastic IP
KEY=${FCVM_KEY:-$HOME/.ssh/fcvm-ec2}

# Pins: keep equal to scripts/claw-master-mac-setup.sh (scripts/test-mac-ssh-client.py enforces it).
CFD_VERSION=2026.9.3
CFD_SHA256_DARWIN_ARM64_TGZ=587c2cfb1c230fe36c7fa7727da78be459dae028cabe8c001291999350f07095
CFD_SHA256_DARWIN_ARM64_BIN=5472c1a01c84bc31b3021056a73b4e5774ddddefc572124ea8fdf6c340639f32

usage() { echo "usage: mac-ssh-connector-setup.sh ALIAS [--bootstrap-port PORT]   (ALIAS: mac, colton-mac)" >&2; exit 2; }
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

# Per Mac: its connector-token secret, its clients (box:account) and the names of its connector's files, matching
# macbook-ssh.tf / colton-mac-ssh.tf and mac-ssh-client.tf. The clients are exactly the accounts Terraform gives that
# Mac's alias and service token. The MacBook's names are the ones its connector was set up with by hand (README,
# "MacBook: Rebuilding from nothing"), so running this against `mac` rewrites them unchanged and reloads nothing.
case "$ALIAS" in
  mac)
    TUNNEL_SECRET=macbook-ssh-tunnel-token
    CLIENTS="fcvm:ubuntu nextjs:ejc3"
    LABEL=com.ejcampbell.macbook-ssh.cloudflared
    TOKEN_FILE=.cloudflared/macbook-ssh.token # relative to the Mac user's home, as LOG_FILE is
    LOG_FILE=Library/Logs/macbook-ssh/cloudflared.log
    ;;
  colton-mac)
    TUNNEL_SECRET=colton-mac-ssh-tunnel-token
    CLIENTS="fcvm:ubuntu nextjs:colton nextjs:ejc3"
    LABEL=com.ccgames.colton-mac-ssh.cloudflared
    TOKEN_FILE=.cloudflared/colton-mac-ssh.token
    LOG_FILE=Library/Logs/colton-mac-ssh-cloudflared.log
    ;;
  *) usage ;;
esac

MAC_SSH="$ALIAS"
if [ -n "$BOOTSTRAP_PORT" ]; then
  MAC_SSH="-o ProxyCommand=none -o HostName=127.0.0.1 -o Port=$BOOTSTRAP_PORT $ALIAS"
fi
on_fcvm() { ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$KEY" "ubuntu@$FCVM" "$@"; }
# Run something on the Mac: jumpbox -> fcvm-arm -> the Mac. stdin is passed through.
on_mac() { on_fcvm "ssh -o BatchMode=yes -o ConnectTimeout=10 $MAC_SSH $*"; }

# A client's public key as exactly `ssh-ed25519 <blob>`, or nothing. The text comes from another box, so it is refused
# outright if it holds more than one line (extra keys, a line that ends the heredoc below, terminal escapes); only the
# type and blob are kept, and the comment written on the Mac is this script's own.
clean_key() {
  local raw=$1 key
  case "$raw" in *$'\n'* | *$'\r'*) return 1 ;; esac
  key=$(printf '%s' "$raw" | cut -d' ' -f1-2)
  [[ $key =~ ^ssh-ed25519\ AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{48}$ ]] || return 1
  printf '%s' "$key"
}

# One nextjs-ct account's key, read from that account's own file in its own Run Command, so one account's file can
# never stand in for another's.
nextjs_key() {
  local user=$1 cmd_id
  [[ $user =~ ^[a-z][a-z0-9]*$ ]] || return 1
  cmd_id=$(aws ssm send-command --region "$REGION" --instance-ids "$NEXTJS_ID" --document-name AWS-RunShellScript \
    --parameters "$(python3 -c 'import json,sys; print(json.dumps({"commands": [sys.argv[1]]}))' "cat /home/$user/.ssh/to-$ALIAS.pub")" \
    --query Command.CommandId --output text)
  aws ssm wait command-executed --region "$REGION" --command-id "$cmd_id" --instance-id "$NEXTJS_ID" >/dev/null 2>&1 || true
  aws ssm get-command-invocation --region "$REGION" --command-id "$cmd_id" --instance-id "$NEXTJS_ID" \
    --query StandardOutputContent --output text
}

echo "1/6 reaching $ALIAS from fcvm-arm"
[ "$(on_mac "'uname -sm'")" = "Darwin arm64" ] || { echo "$ALIAS is not reachable, or is not a macOS arm64 Mac" >&2; exit 1; }

echo "2/6 Remote Login takes keys only"
# What Remote Login offers a client with no key, asked on the Mac itself: exactly "publickey" passes. Otherwise this
# installs the MacBook's keys-only drop-in (README "MacBook: Rebuilding from nothing", step 1) if sudo needs no password,
# and stops with the command to type at the Mac if it does. macOS starts sshd per connection, so the next check sees it.
on_mac bash -s <<'REMOTE'
set -uo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
offered() {
  ssh -v -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive -o NumberOfPasswordPrompts=0 \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o ConnectTimeout=5 -p 22 "$(id -un)@127.0.0.1" true 2>&1 </dev/null |
    awk -F': ' '/Authentications that can continue/ { print $NF; exit }' | tr -d '\r[:space:]'
}
methods=$(offered)
if [ "$methods" != publickey ]; then
  echo "Remote Login offers: ${methods:-nothing (no answer)}; installing /etc/ssh/sshd_config.d/050-keys-only.conf"
  if ! printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' | sudo -n /usr/bin/tee /etc/ssh/sshd_config.d/050-keys-only.conf >/dev/null 2>&1; then
    echo "sudo needs a password here. At the Mac, in Terminal, run:" >&2
    echo "  printf 'PasswordAuthentication no\\nKbdInteractiveAuthentication no\\n' | sudo tee /etc/ssh/sshd_config.d/050-keys-only.conf" >&2
    echo "then run this script again." >&2
    exit 1
  fi
  methods=$(offered)
fi
[ "$methods" = publickey ] || { echo "Remote Login still offers: ${methods:-nothing}" >&2; exit 1; }
echo "Remote Login: publickey only"
REMOTE

echo "3/6 the clients' public keys"
KEYS=""
NEXTJS_ID=""
for client in $CLIENTS; do
  box=${client%%:*} user=${client#*:}
  case "$box" in
    fcvm)
      [ "$user" = ubuntu ] || { echo "only ubuntu on fcvm-arm" >&2; exit 1; }
      key=$(clean_key "$(on_fcvm "cat ~/.ssh/to-$ALIAS.pub" 2>/dev/null || true)") ||
        { echo "fcvm-arm ubuntu has no single valid ~/.ssh/to-$ALIAS.pub (mac-ssh-client.tf makes it)" >&2; exit 1; }
      KEYS+="$key fcvm-arm ubuntu -> $ALIAS"$'\n'
      ;;
    nextjs)
      if [ -z "$NEXTJS_ID" ]; then
        # The containerized box (nextjs-ct.tf), found by its Box tag, which it keeps through the cutover (its Name
        # becomes nextjs-dev then). Each account's container home is the host's /home/<account>.
        NEXTJS_ID=$(aws ec2 describe-instances --region "$REGION" --filters Name=tag:Box,Values=nextjs-ct Name=instance-state-name,Values=running \
          --query 'Reservations[0].Instances[0].InstanceId' --output text)
        [ -n "$NEXTJS_ID" ] && [ "$NEXTJS_ID" != None ] || { echo "the nextjs-ct box is not running" >&2; exit 1; }
      fi
      key=$(clean_key "$(nextjs_key "$user" || true)") ||
        { echo "nextjs-ct $user has no single valid ~/.ssh/to-$ALIAS.pub (the container's provision makes it at setup)" >&2; exit 1; }
      KEYS+="$key nextjs $user -> $ALIAS"$'\n'
      ;;
    *) echo "unknown box $box" >&2; exit 1 ;;
  esac
done
printf '%s' "$KEYS" | sed 's/^/   /'

echo "4/6 cloudflared $CFD_VERSION (verified on the Mac)"
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

echo "5/6 the connector token (from Secrets Manager, through ssh stdin only)"
{ set +x; } 2>/dev/null
# Replaced only when it differs; a new token leaves a flag so step 6 restarts the connector (it reads the file at start).
aws secretsmanager get-secret-value --region "$REGION" --secret-id "$TUNNEL_SECRET" --query SecretString --output text |
  on_mac "'umask 077; t=\$HOME/$TOKEN_FILE; mkdir -p \$HOME/.cloudflared && cat > \$t.new && [ -s \$t.new ] || { rm -f \$t.new; exit 1; }; if cmp -s \$t.new \$t; then rm -f \$t.new; echo token unchanged; else mv -f \$t.new \$t && touch \$t.changed && echo token installed; fi; chmod 600 \$t'"

echo "6/6 the clients' keys and the connector LaunchAgent"
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
if cmp -s "\$AK.new" "\$AK"; then rm -f "\$AK.new"; else mv -f "\$AK.new" "\$AK"; fi
chmod 600 "\$AK"

PLIST="\$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="\$HOME/$LOG_FILE"
mkdir -p "\$HOME/Library/LaunchAgents" "\$(dirname "\$LOG")"
cat > "\$PLIST.new" <<PL
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
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>\$LOG</string>
  <key>StandardErrorPath</key><string>\$LOG</string>
</dict></plist>
PL
plutil -lint "\$PLIST.new" >/dev/null
uid=\$(id -u)
changed=""
if ! cmp -s "\$PLIST.new" "\$PLIST"; then changed="its LaunchAgent changed"; fi
[ ! -e "\$HOME/$TOKEN_FILE.changed" ] || changed="\${changed:+\$changed, }its token changed"
launchctl print "gui/\$uid/$LABEL" >/dev/null 2>&1 || changed="\${changed:+\$changed, }it is not loaded"
chmod 644 "\$PLIST.new" && mv -f "\$PLIST.new" "\$PLIST"
if [ -n "\$changed" ]; then
  # Reloading ends every session through this connector, this one too when it came that way: run it detached so the
  # bootstrap still happens after the session is cut.
  nohup /bin/sh -c "sleep 2; launchctl bootout gui/\$uid/$LABEL 2>/dev/null; launchctl bootstrap gui/\$uid '\$PLIST' && rm -f '\$HOME/$TOKEN_FILE.changed'" >/dev/null 2>&1 &
  echo "connector: reloading in the background (\$changed); sessions through it will drop"
else
  echo "connector: unchanged and loaded; not reloaded"
  echo "agent: \$(launchctl print "gui/\$uid/$LABEL" 2>/dev/null | awk '/state =/{print \$3; exit}')"
fi
REMOTE
echo "done. From each client: ssh $ALIAS true"

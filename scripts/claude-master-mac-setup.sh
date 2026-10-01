#!/usr/bin/env bash
#
# claude-master-mac-setup -- set a Mac up to use the shared Claude subscriptions through the Cloudflare
# tunnel (claude-master-tunnel.tf), reaching it over the reverse tunnel the Mac keeps open to
# fcvm-metal-arm (mac-reverse-tunnels.tf: `ssh mac` / `ssh macbook` from there).
#
#   scripts/claude-master-mac-setup.sh macbook        run on a jumpbox as an AWS administrator
#   scripts/claude-master-mac-setup.sh mac
#
# ADDITIVE AND OPT-IN. It installs into the user's home only (~/.local/bin, ~/.config/claude-master,
# ~/Library/LaunchAgents) and leaves the Mac's own `claude`, its login and its settings alone: a
# plain `claude` still uses that Mac's own subscription. Only the new `claude-pool` command routes
# inference through the shared pool.
#   * claude-master and cloudflared, each pinned by version and sha256 and verified ON the Mac
#   * ~/.config/claude-master/cloudflare.env (0600): the Access service token. It travels from
#     Secrets Manager through ssh STDIN only: never an argument, never written on fcvm or here.
#   * ~/.config/claude-master/ca.pem: the server's PUBLIC CA certificate, once the server has one
#   * a LaunchAgent that keeps `cloudflared access tcp` running on 127.0.0.1:8444 (reads the token
#     from the 0600 file, not from the plist)
#   * ~/.local/bin/claude-pool: `claude` with inference from the pool; arguments pass straight through
# Re-running is safe: it re-verifies the binaries, rewrites the files and reloads the agent.
set -euo pipefail

REGION=${AWS_REGION:-us-west-1}
FCVM=${FCVM_HOST:-184.72.40.255}                       # fcvm-metal-arm's Elastic IP
KEY=${FCVM_KEY:-$HOME/.ssh/fcvm-ec2}

# Pins. Keep CM_TAG equal to claude_master_tag in claude-master-server.tf (a test enforces it) and
# bump the sha256 values together with it.
CM_TAG=claude-master-e8704e4
CM_SHA256_DARWIN_ARM64=2584c0f24e9e2aab346d7b02bc1ebca46d7848daf6477f5ba53606fdb8ec72a9
CFD_VERSION=2026.9.3
CFD_SHA256_DARWIN_ARM64_TGZ=587c2cfb1c230fe36c7fa7727da78be459dae028cabe8c001291999350f07095

ALIAS=${1:-}
[[ $ALIAS =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { echo "usage: claude-master-mac-setup.sh ALIAS   (mac or macbook: an ssh alias on fcvm-arm)" >&2; exit 2; }

# Run something on the Mac: jumpbox -> fcvm -> (reverse tunnel) -> Mac. stdin is passed through.
on_mac() { ssh -o BatchMode=yes -o ConnectTimeout=10 -i "$KEY" "ubuntu@$FCVM" "ssh -o BatchMode=yes -o ConnectTimeout=10 $ALIAS $*"; }

echo "1/6 reaching $ALIAS through fcvm"
on_mac "'echo \$(hostname) \$(uname -sm)'" || { echo "$ALIAS is not connected: its reverse tunnel to fcvm is down (the Mac must be online and running its tunnel)" >&2; exit 1; }
[ "$(on_mac "'uname -sm'")" = "Darwin arm64" ] || { echo "this script installs the macOS arm64 builds" >&2; exit 1; }

echo "2/6 installing claude-master $CM_TAG and cloudflared $CFD_VERSION (verified on the Mac)"
on_mac bash -s <<REMOTE
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
mkdir -p "\$HOME/.local/bin" "\$HOME/.config/claude-master" && chmod 700 "\$HOME/.config/claude-master"
tmp=\$(mktemp -d) && trap 'rm -rf "\$tmp"' EXIT
sum() { shasum -a 256 "\$1" | cut -d' ' -f1; }
if [ "\$(sum "\$HOME/.local/bin/claude-master" 2>/dev/null || true)" != "$CM_SHA256_DARWIN_ARM64" ]; then
  curl -fsSL "https://github.com/ejc3/CLIProxyAPI/releases/download/$CM_TAG/claude-master-darwin-arm64" -o "\$tmp/cm"
  [ "\$(sum "\$tmp/cm")" = "$CM_SHA256_DARWIN_ARM64" ] || { echo "claude-master did not match its pinned sha256" >&2; exit 1; }
  chmod 755 "\$tmp/cm" && mv -f "\$tmp/cm" "\$HOME/.local/bin/claude-master"
fi
if ! "\$HOME/.local/bin/cloudflared" --version 2>/dev/null | grep -q "version $CFD_VERSION"; then
  curl -fsSL "https://github.com/cloudflare/cloudflared/releases/download/$CFD_VERSION/cloudflared-darwin-arm64.tgz" -o "\$tmp/cfd.tgz"
  [ "\$(sum "\$tmp/cfd.tgz")" = "$CFD_SHA256_DARWIN_ARM64_TGZ" ] || { echo "cloudflared did not match its pinned sha256" >&2; exit 1; }
  tar -xzf "\$tmp/cfd.tgz" -C "\$tmp" cloudflared && chmod 755 "\$tmp/cloudflared" && mv -f "\$tmp/cloudflared" "\$HOME/.local/bin/cloudflared"
fi
"\$HOME/.local/bin/cloudflared" --version | head -1
shasum -a 256 "\$HOME/.local/bin/claude-master" | cut -c1-16
REMOTE

echo "3/6 the Access service token (from Secrets Manager, through ssh stdin only)"
{ set +x; } 2>/dev/null
SECRET=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id claude-master/mac-access --query SecretString --output text)
HOSTNAME_=$(printf '%s' "$SECRET" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hostname"])')
PORT=$(printf '%s' "$SECRET" | python3 -c 'import json,sys; print(json.load(sys.stdin)["local_port"])')
printf '%s' "$SECRET" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("TUNNEL_SERVICE_TOKEN_ID=" + d["client_id"])
print("TUNNEL_SERVICE_TOKEN_SECRET=" + d["client_secret"])
print("CLAUDE_MASTER_TUNNEL_HOSTNAME=" + d["hostname"])
print("CLAUDE_MASTER_TUNNEL_PORT=%s" % d["local_port"])' |
  on_mac "'umask 077; cat > \$HOME/.config/claude-master/cloudflare.env.new && mv -f \$HOME/.config/claude-master/cloudflare.env.new \$HOME/.config/claude-master/cloudflare.env && chmod 600 \$HOME/.config/claude-master/cloudflare.env'"
unset SECRET

echo "4/6 the server's public CA certificate (only exists once the server has started)"
SERVER_ID=$(aws ec2 describe-instances --region "$REGION" --filters Name=tag:Name,Values=claude-master-server Name=instance-state-name,Values=running --query 'Reservations[0].Instances[0].InstanceId' --output text)
CMD_ID=$(aws ssm send-command --region "$REGION" --instance-ids "$SERVER_ID" --document-name AWS-RunShellScript --parameters 'commands=["cat /var/lib/claude-master/state/ca.pem 2>/dev/null; true"]' --query Command.CommandId --output text)
aws ssm wait command-executed --region "$REGION" --command-id "$CMD_ID" --instance-id "$SERVER_ID"
CA=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$SERVER_ID" --query StandardOutputContent --output text)
if printf '%s\n' "$CA" | grep -q 'BEGIN CERTIFICATE'; then
  printf '%s\n' "$CA" | on_mac "'cat > \$HOME/.config/claude-master/ca.pem && chmod 644 \$HOME/.config/claude-master/ca.pem'"
  echo "   installed ca.pem"
else
  echo "   the server has not created its CA yet (it does that on its first start, after the three logins); re-run this script then"
fi

echo "5/6 the claude-pool command and the tunnel LaunchAgent"
on_mac bash -s <<REMOTE
set -euo pipefail
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
D="\$HOME/.config/claude-master"
cat > "\$HOME/.local/bin/claude-pool" <<'POOL'
#!/bin/bash
# claude-pool: Claude Code with inference from the shared pool of subscriptions. Your own login is
# still how you sign in; only inference is shared. A plain \`claude\` is unchanged.
set -euo pipefail
D="\$HOME/.config/claude-master"
. "\$D/cloudflare.env"
if [ ! -f "\$D/ca.pem" ]; then
  echo "claude-pool: \$D/ca.pem is missing: the shared server has not published its CA certificate yet" >&2
  exit 1
fi
if ! launchctl print "gui/\$(id -u)/dev.cc-games.claude-master-tunnel" >/dev/null 2>&1; then
  echo "claude-pool: the tunnel agent is not loaded (launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/dev.cc-games.claude-master-tunnel.plist)" >&2
  exit 1
fi
exec "\$HOME/.local/bin/claude-master" connect --open "127.0.0.1:\$CLAUDE_MASTER_TUNNEL_PORT" --ca "\$D/ca.pem" -- "\$@"
POOL
chmod 755 "\$HOME/.local/bin/claude-pool"

cat > "\$HOME/.local/bin/claude-master-tunnel" <<'TUNNEL'
#!/bin/bash
# Runs the local end of the Cloudflare tunnel. The token comes from a 0600 file, never an argument.
set -euo pipefail
# launchd appends to its log file forever. Rotate it here, at every (re)start: keep the last 5 MB
# worth as .1 and start a fresh one. cp-then-truncate because launchd holds the file open.
LOG="\$HOME/Library/Logs/claude-master-tunnel.log"
if [ -f "\$LOG" ] && [ "\$(stat -f %z "\$LOG" 2>/dev/null || echo 0)" -gt 5242880 ]; then
  cp "\$LOG" "\$LOG.1" && : > "\$LOG"
fi
set -a; . "\$HOME/.config/claude-master/cloudflare.env"; set +a
exec "\$HOME/.local/bin/cloudflared" access tcp --hostname "\$CLAUDE_MASTER_TUNNEL_HOSTNAME" --url "127.0.0.1:\$CLAUDE_MASTER_TUNNEL_PORT"
TUNNEL
chmod 755 "\$HOME/.local/bin/claude-master-tunnel"

PLIST="\$HOME/Library/LaunchAgents/dev.cc-games.claude-master-tunnel.plist"
mkdir -p "\$HOME/Library/LaunchAgents" "\$HOME/Library/Logs"
cat > "\$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.cc-games.claude-master-tunnel</string>
  <key>ProgramArguments</key><array><string>\$HOME/.local/bin/claude-master-tunnel</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>StandardErrorPath</key><string>\$HOME/Library/Logs/claude-master-tunnel.log</string>
  <key>StandardOutPath</key><string>\$HOME/Library/Logs/claude-master-tunnel.log</string>
</dict></plist>
PL
chmod 644 "\$PLIST"
plutil -lint "\$PLIST" >/dev/null
uid=\$(id -u)
launchctl bootout "gui/\$uid/dev.cc-games.claude-master-tunnel" 2>/dev/null || true
launchctl bootstrap "gui/\$uid" "\$PLIST" && echo "tunnel agent loaded"
REMOTE

echo "6/6 checking"
on_mac bash -s <<'REMOTE'
export PATH=/usr/bin:/bin:/usr/sbin:/sbin
sleep 3
echo "agent: $(launchctl print gui/$(id -u)/dev.cc-games.claude-master-tunnel 2>/dev/null | awk '/state =/{print $3; exit}')"
echo "listening on 127.0.0.1:8444: $(lsof -nP -iTCP:8444 -sTCP:LISTEN 2>/dev/null | awk 'NR==2{print $1}')"
ls -l "$HOME/.config/claude-master" | awk 'NR>1{print $1, $NF}'
tail -3 "$HOME/Library/Logs/claude-master-tunnel.log" 2>/dev/null | cut -c1-160
REMOTE
echo "done. On the Mac: claude-pool   (needs the shared server running and ca.pem installed)"

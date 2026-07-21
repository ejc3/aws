# dev-selfupdate.tf
#
# Keeps the dev boxes converged on the codified setup without recreating them.
#
# THE PROBLEM: user_data only executes when an instance is CREATED. These boxes are
# long-lived (persistent root volumes on spot), so every terraform change to the setup
# script sat unapplied until someone hand-patched the running box -- which is drift by
# another name.
#
# THE FIX: two units.
#   dev-selfupdate  - on every boot, fetch the published script from S3 and re-apply it
#                     if its checksum changed. The login banner reports what happened.
#   et-update       - weekly, install prebuilt binaries built by ejc3 CI instead of
#                     compiling on the box. Driven by a table: Eternal Terminal from
#                     ejc3/EternalTerminal, and tmux from ejc3/tmux (Ubuntu 24.04 ships
#                     3.4, which predates pane-scrollbars).
#
# JUMPBOX IS DELIBERATELY EXCLUDED: it is the machine we manage everything else from, so
# it does not get a service that re-runs a downloaded script as root. Dev boxes only.
#
# ---------------------------------------------------------------------------------
# FORMATTING RULE FOR THIS FILE -- the heredoc bodies below start at COLUMN 0.
#
# Terraform's `<<-EOT` strips the COMMON leading whitespace of the block, so if every
# line were indented the strip would remove it -- but the moment ANY line sits at column
# 0 (such as a heredoc terminator) the common indent becomes 0 and NOTHING is stripped.
# The bodies would then render indented, and a file whose first line is `    #!/bin/bash`
# has no valid shebang: the kernel silently falls back to /bin/sh, and `set -o pipefail`
# dies with "Illegal option". Keeping everything at column 0 makes the render exact.
# ---------------------------------------------------------------------------------

locals {
  # ---------------------------------------------------------------------------
  # Prebuilt-binary refresh, driven by a table so ET and tmux share one code path
  # rather than duplicating an updater per tool.
  #
  # Every failure path must leave a WORKING binary in place: the download is executed
  # once to prove it runs BEFORE the current one is replaced, and a service that fails
  # to restart is rolled back. ET is how we log in, so this matters; SSH on :22 is
  # unaffected regardless.
  # ---------------------------------------------------------------------------
  bin_update = <<-EOT
cat > /usr/local/bin/dev-bin-update.sh <<'BINUPD'
#!/bin/bash
set -uo pipefail
ARCH=$(uname -m)

# repo|release-tag|asset-prefix|binaries|install-dir|service-to-restart|version-cmd
# tmux installs to /usr/local/bin so it shadows the distro package in PATH without
# fighting dpkg -- removing the tarball reverts cleanly to Ubuntu's 3.4.
TABLE='ejc3/EternalTerminal|binaries-7.x|et|et etserver etterminal|/usr/bin|etserver.service|/usr/bin/etserver --version
ejc3/tmux|binaries-3.x|tmux|tmux|/usr/local/bin||/usr/local/bin/tmux -V'

echo "$TABLE" | while IFS='|' read -r repo tag prefix bins dir svc vercmd; do
  [ -n "$repo" ] || continue
  url="https://github.com/$repo/releases/download/$tag/$prefix-$ARCH.tar.gz"
  state="/var/lib/dev-bin-update/$prefix"
  mkdir -p "$state"
  tmp=$(mktemp -d)

  if ! curl -fsSL --retry 3 -o "$tmp/a.tar.gz" "$url"; then
    echo "bin-update[$prefix]: no asset at $url (keeping current)"; rm -rf "$tmp"; continue
  fi

  sum=$(sha256sum "$tmp/a.tar.gz" | awk '{print $1}')
  if [ "$sum" = "$(cat "$state/sha256" 2>/dev/null)" ] && $vercmd >/dev/null 2>&1; then
    echo "bin-update[$prefix]: already current"; rm -rf "$tmp"; continue
  fi

  if ! tar xzf "$tmp/a.tar.gz" -C "$tmp"; then
    echo "bin-update[$prefix]: bad tarball"; rm -rf "$tmp"; continue
  fi

  # Prove the downloaded binary executes on THIS box before touching the live one.
  ok=1
  for b in $bins; do
    chmod +x "$tmp/$b" 2>/dev/null
    "$tmp/$b" -V >/dev/null 2>&1 || "$tmp/$b" --version >/dev/null 2>&1 || ok=0
  done
  if [ "$ok" -ne 1 ]; then
    echo "bin-update[$prefix]: downloaded binary will not execute; keeping current"
    rm -rf "$tmp"; continue
  fi

  [ -n "$svc" ] && systemctl stop "$svc" 2>/dev/null
  mkdir -p "$dir"
  for b in $bins; do
    # rename-then-install: overwriting a RUNNING binary in place fails "Text file busy"
    mv "$dir/$b" "$dir/$b.prev" 2>/dev/null || true
    install -m 755 "$tmp/$b" "$dir/$b"
  done

  if [ -z "$svc" ] || systemctl start "$svc"; then
    echo "$sum" > "$state/sha256"
    echo "bin-update[$prefix]: installed $($vercmd 2>&1 | head -1)"
  else
    echo "bin-update[$prefix]: service failed to start -- rolling back"
    for b in $bins; do mv "$dir/$b.prev" "$dir/$b" 2>/dev/null || true; done
    systemctl start "$svc" 2>/dev/null
  fi
  rm -rf "$tmp"
done
BINUPD
chmod +x /usr/local/bin/dev-bin-update.sh

# Keep the old path working; it is referenced by existing units and muscle memory.
ln -sf /usr/local/bin/dev-bin-update.sh /usr/local/bin/et-update.sh

cat > /etc/systemd/system/et-update.service <<'SVC'
[Unit]
Description=Install prebuilt dev binaries (Eternal Terminal, tmux) from ejc3 releases
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/dev-bin-update.sh
SVC

cat > /etc/systemd/system/et-update.timer <<'TMR'
[Unit]
Description=Weekly prebuilt dev-binary refresh

[Timer]
OnCalendar=Mon 07:00
Persistent=true
RandomizedDelaySec=30m

[Install]
WantedBy=timers.target
TMR

systemctl daemon-reload
systemctl enable et-update.timer >/dev/null 2>&1 || true
systemctl start et-update.timer >/dev/null 2>&1 || true
  EOT

  # ---------------------------------------------------------------------------
  # `pbox` -- "I want it" / "I don't" for the on-demand 192-core Graviton box.
  #
  # Dev boxes deliberately hold a restricted IAM role with NO ec2:RunInstances, and
  # terraform state lives on the jumpbox. So this does not launch anything itself: it
  # delegates the terraform half to the jumpbox over SSH, then drops you onto the new
  # machine. Keeps dev-box permissions unchanged.
  # ---------------------------------------------------------------------------
  pbox_setup = <<-EOT
cat > /usr/local/bin/pbox <<'PBOX'
#!/bin/bash
# On-demand 192-core Graviton box (c8g.48xlarge, ~$3.14/hr spot).
#
#   pbox up      launch it and ssh over        ("I want it")
#   pbox down    terminate it                  ("I don't")
#   pbox ssh     reconnect to a running box
#   pbox status  running? cost? disk?
#
# Work lives on /mnt/work -- a persistent 100GB volume that survives every up/down and
# every spot interruption. The root disk does not; treat it as scratch.
#
# An idle watchdog terminates the box automatically after 30 min below 5% CPU, because
# it costs roughly 50x a dev box per hour. Losing it costs nothing: /mnt/work persists.
set -euo pipefail
JUMPBOX=10.0.1.72
KEY="$HOME/.ssh/fcvm-ec2"
JB() { ssh -i "$KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no "ubuntu@$JUMPBOX" "cd ~/aws && ./scripts/parallel-box.sh $*"; }

case "$${1:-status}" in
  up|want)
    JB up
    IP="$(JB ip)"
    [ -n "$IP" ] || { echo "box did not come up; try: pbox status" >&2; exit 1; }
    echo "Connecting to $IP (work disk at /mnt/work)..."
    exec ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP"
    ;;
  down|dont|stop)
    JB down
    ;;
  ssh)
    IP="$(JB ip)"
    [ -n "$IP" ] || { echo "Box is down. Run: pbox up" >&2; exit 1; }
    exec ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP"
    ;;
  status|*)
    JB status
    ;;
esac
PBOX
chmod +x /usr/local/bin/pbox
  EOT

  # ---------------------------------------------------------------------------
  # Boot-time convergence on the published setup script.
  # ---------------------------------------------------------------------------
  selfupdate_setup = <<-EOT
cat > /usr/local/bin/dev-selfupdate.sh <<'SELFUPD'
#!/bin/bash
set -uo pipefail
case "$(uname -m)" in
  aarch64) KEY=arm.sh ;;
  x86_64)  KEY=x86.sh ;;
  *) exit 0 ;;
esac
STATE=/var/lib/dev-selfupdate
LOG=/var/log/dev-selfupdate.log
mkdir -p "$STATE"
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT

if ! aws s3 cp "s3://ejc3-dev-scripts/user-data/$KEY" "$TMP" --region us-west-1 >/dev/null 2>&1; then
  echo "$(date -Is) fetch failed" >> "$LOG"
  exit 0
fi

NEW=$(sha256sum "$TMP" | awk '{print $1}')
OLD=$(cat "$STATE/sha256" 2>/dev/null || echo none)

if [ "$NEW" = "$OLD" ]; then
  printf 'current %s\n' "$${NEW:0:12}" > "$STATE/status"
  exit 0
fi

echo "$(date -Is) applying $NEW (was $OLD)" >> "$LOG"
if bash "$TMP" >> "$LOG" 2>&1; then
  echo "$NEW" > "$STATE/sha256"
  printf 'UPDATED %s -> %s on %s\n' "$${OLD:0:12}" "$${NEW:0:12}" "$(date -Is)" > "$STATE/status"
else
  printf 'FAILED applying %s on %s -- see /var/log/dev-selfupdate.log\n' "$${NEW:0:12}" "$(date -Is)" > "$STATE/status"
fi
SELFUPD
chmod +x /usr/local/bin/dev-selfupdate.sh

cat > /etc/systemd/system/dev-selfupdate.service <<'SVC'
[Unit]
Description=Re-apply the codified dev-server setup published to S3
After=network-online.target cloud-init.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
TimeoutStartSec=3600
ExecStart=/usr/local/bin/dev-selfupdate.sh

[Install]
WantedBy=multi-user.target
SVC

# Report the result at login. Stays silent on an ordinary no-change boot so the
# banner only speaks up when something actually happened.
cat > /etc/update-motd.d/99-dev-selfupdate <<'MOTD'
#!/bin/sh
S=/var/lib/dev-selfupdate/status
[ -r "$S" ] || exit 0
case "$(cat "$S")" in
  UPDATED*) printf '\n  \033[1;32m*\033[0m dev config %s\n' "$(cat "$S")" ;;
  FAILED*)  printf '\n  \033[1;31m!\033[0m dev config %s\n' "$(cat "$S")" ;;
esac
MOTD
chmod +x /etc/update-motd.d/99-dev-selfupdate

systemctl daemon-reload
systemctl enable dev-selfupdate.service >/dev/null 2>&1 || true

# Seed the checksum with what we are running RIGHT NOW, so the next boot does not
# pointlessly re-apply the very script that just ran.
mkdir -p /var/lib/dev-selfupdate
aws s3 cp "s3://ejc3-dev-scripts/user-data/$(uname -m | sed 's/aarch64/arm/;s/x86_64/x86/').sh" - \
  --region us-west-1 2>/dev/null | sha256sum | awk '{print $1}' \
  > /var/lib/dev-selfupdate/sha256 || true
  EOT
}

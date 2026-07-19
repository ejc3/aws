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
#   et-update       - weekly, install prebuilt Eternal Terminal binaries built by
#                     ejc3/EternalTerminal CI instead of compiling from source on the box.
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
  # Weekly Eternal Terminal binary refresh.
  #
  # ET is how we log in, so every failure path here must leave a WORKING etserver:
  # the download is verified by running it before install, and a failed start rolls
  # back to the previous binary. SSH on :22 is unaffected regardless.
  # ---------------------------------------------------------------------------
  et_binary_update = <<-EOT
cat > /usr/local/bin/et-update.sh <<'ETUPD'
#!/bin/bash
set -uo pipefail
REPO=ejc3/EternalTerminal
TAG=binaries-7.x
ARCH=$(uname -m)
URL="https://github.com/$REPO/releases/download/$TAG/et-$ARCH.tar.gz"
STATE=/var/lib/et-update
mkdir -p "$STATE"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

curl -fsSL --retry 3 -o "$TMP/et.tar.gz" "$URL" || {
  echo "et-update: no release asset at $URL (keeping current build)"; exit 0; }

NEW_SUM=$(sha256sum "$TMP/et.tar.gz" | awk '{print $1}')
if [ "$NEW_SUM" = "$(cat "$STATE/sha256" 2>/dev/null)" ] && /usr/bin/etserver --version >/dev/null 2>&1; then
  echo "et-update: already current"; exit 0
fi

tar xzf "$TMP/et.tar.gz" -C "$TMP" || { echo "et-update: bad tarball"; exit 0; }
chmod +x "$TMP"/et "$TMP"/etserver "$TMP"/etterminal
# Prove the downloaded binary runs on THIS box before touching the working one.
"$TMP/etserver" --version >/dev/null 2>&1 || {
  echo "et-update: downloaded etserver will not execute; keeping current"; exit 0; }

systemctl stop etserver.service
for b in et etserver etterminal; do
  # rename-then-copy: overwriting a running binary in place fails with "Text file busy"
  mv "/usr/bin/$b" "/usr/bin/$b.prev" 2>/dev/null || true
  install -m 755 "$TMP/$b" "/usr/bin/$b"
done

if systemctl start etserver.service; then
  echo "$NEW_SUM" > "$STATE/sha256"
  /usr/bin/etserver --version > "$STATE/version" 2>&1
  echo "et-update: installed $(cat "$STATE/version")"
else
  echo "et-update: new etserver failed to start -- rolling back"
  for b in et etserver etterminal; do
    mv "/usr/bin/$b.prev" "/usr/bin/$b" 2>/dev/null || true
  done
  systemctl start etserver.service
fi
ETUPD
chmod +x /usr/local/bin/et-update.sh

cat > /etc/systemd/system/et-update.service <<'SVC'
[Unit]
Description=Install prebuilt Eternal Terminal binaries from ejc3/EternalTerminal
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/et-update.sh
SVC

cat > /etc/systemd/system/et-update.timer <<'TMR'
[Unit]
Description=Weekly Eternal Terminal binary refresh

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

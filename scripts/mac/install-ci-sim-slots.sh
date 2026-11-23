#!/bin/sh
# Invoked inside the CI VM as root. A staging prefix also permits a disposable install test
# (with CI_LAUNCH_AGENTS, CI_CLEAN_USER and LAUNCHCTL pointing at stand-ins).
# Installs the simulator test slots and the hourly disk cleaner (ci-disk-clean, ejc3/aws#1021). Idempotent:
# the cleaner's LaunchAgent is reloaded only when its plist changed or it is not loaded.
set -eu
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
prefix=${1:-/usr/local}
lib=$prefix/libexec/mini-simulator
link=$prefix/bin/xcodebuild
target=$lib/sim-slot-shim
agents=${CI_LAUNCH_AGENTS:-/Library/LaunchAgents}
user=${CI_CLEAN_USER:-admin}
launchctl=${LAUNCHCTL:-/bin/launchctl}
label=com.ejcampbell.ci-disk-clean
case "$user" in ''|-*|*[!A-Za-z0-9_.-]*) echo "Invalid user $user" >&2; exit 1 ;; esac
if [ -e "$link" ] || [ -L "$link" ]; then
  [ "$(readlink "$link")" = "$target" ] || { echo "Refusing to replace unmanaged $link" >&2; exit 1; }
fi
for dir in "$prefix/bin" "$prefix/libexec" "$lib"; do
  [ -d "$dir" ] || install -d -m 755 "$dir"
done
stage=$(mktemp -d "$lib/.install.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
for source in bin/mac-build ci/sim-slot-shim ci/ci-disk-clean; do
  file=${source##*/}
  install -m 755 "$src/$source" "$stage/$file"
  mv -f "$stage/$file" "$lib/$file"
done
if [ ! -L "$link" ]; then
  ln -s "$target" "$link" || [ "$(readlink "$link")" = "$target" ]
fi
echo "Installed simulator test slots at $link"

# The cleaner runs in the user's own login session, where its simulators and their CoreSimulator service live,
# hourly and at load, at the lowest CPU and I/O priority.
uid=$(id -u "$user")
home=$(eval "printf '%s' ~$user")
[ -d "$agents" ] || install -d -m 755 "$agents"
plist=$agents/$label.plist
cat > "$stage/plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array><string>$lib/ci-disk-clean</string></array>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>3600</integer>
  <key>ProcessType</key><string>Background</string>
  <key>Nice</key><integer>20</integer>
  <key>LowPriorityIO</key><true/>
  <key>LowPriorityBackgroundIO</key><true/>
  <key>StandardOutPath</key><string>$home/Library/Logs/ci-disk-clean.log</string>
  <key>StandardErrorPath</key><string>$home/Library/Logs/ci-disk-clean.log</string>
</dict></plist>
PLIST
if cmp -s "$stage/plist" "$plist" && "$launchctl" print "gui/$uid/$label" >/dev/null 2>&1; then
  echo "Disk cleaner unchanged and loaded"
  exit 0
fi
install -m 644 "$stage/plist" "$stage/plist.new"
mv -f "$stage/plist.new" "$plist"
if "$launchctl" print "gui/$uid" >/dev/null 2>&1; then
  "$launchctl" bootout "gui/$uid/$label" >/dev/null 2>&1 || true
  # bootout returns before a running instance has exited, and bootstrap fails while it is still listed.
  tries=0
  while "$launchctl" print "gui/$uid/$label" >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "$tries" -le 60 ] || { echo "The old disk cleaner did not unload within 30 seconds" >&2; exit 1; }
    sleep 0.5
  done
  "$launchctl" bootstrap "gui/$uid" "$plist"
  echo "Disk cleaner loaded for $user"
else
  echo "Disk cleaner installed; it loads at $user's next login"
fi

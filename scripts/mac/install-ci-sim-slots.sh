#!/bin/sh
# Invoked inside the CI VM as root. A staging prefix also permits a disposable install test
# (with CI_LAUNCH_AGENTS, CI_LAUNCH_DAEMONS, CI_SWIFT_TEST_DIR, CI_CLEAN_USER and LAUNCHCTL pointing at stand-ins).
# Installs the simulator test slots, the one swift-test slot (/usr/local/bin/swift, ejc3/aws#1075), the hourly disk
# cleaner (ci-disk-clean, ejc3/aws#1021) and the 5-minute coreaudiod check (ci-coreaudio-check, ejc3/aws#1068). Idempotent: each job is reloaded only when its plist changed
# or it is not loaded.
set -eu
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
prefix=${1:-/usr/local}
lib=$prefix/libexec/mini-simulator
link=$prefix/bin/xcodebuild
target=$lib/sim-slot-shim
swift_link=$prefix/bin/swift
swift_target=$lib/swift-test-shim
swift_dir=${CI_SWIFT_TEST_DIR:-/Users/Shared/swift-test}
agents=${CI_LAUNCH_AGENTS:-/Library/LaunchAgents}
daemons=${CI_LAUNCH_DAEMONS:-/Library/LaunchDaemons}
user=${CI_CLEAN_USER:-admin}
launchctl=${LAUNCHCTL:-/bin/launchctl}
label=com.ejcampbell.ci-disk-clean
audio_label=com.ejcampbell.ci-coreaudio-check
case "$user" in ''|-*|*[!A-Za-z0-9_.-]*) echo "Invalid user $user" >&2; exit 1 ;; esac
managed_link() {
  if [ -e "$1" ] || [ -L "$1" ]; then
    [ "$(readlink "$1")" = "$2" ] || { echo "Refusing to replace unmanaged $1" >&2; exit 1; }
  fi
}
managed_link "$link" "$target"
managed_link "$swift_link" "$swift_target"
for dir in "$prefix/bin" "$prefix/libexec" "$lib"; do
  [ -d "$dir" ] || install -d -m 755 "$dir"
done
stage=$(mktemp -d "$lib/.install.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
for source in bin/mac-build ci/sim-slot-shim ci/swift-test-shim ci/ci-disk-clean ci/ci-coreaudio-check; do
  file=${source##*/}
  install -m 755 "$src/$source" "$stage/$file"
  mv -f "$stage/$file" "$lib/$file"
done
if [ ! -L "$link" ]; then
  ln -s "$target" "$link" || [ "$(readlink "$link")" = "$target" ]
fi
echo "Installed simulator test slots at $link"
if [ ! -L "$swift_link" ]; then
  ln -s "$swift_target" "$swift_link" || [ "$(readlink "$swift_link")" = "$swift_target" ]
fi
# The slot's lock file lives in one directory for the whole VM, whatever a job sets HOME to, owned by the CI user.
# /Users/Shared is writable by everyone, so a link planted there is refused rather than followed.
[ ! -L "$swift_dir" ] || { echo "Refusing the symlink at $swift_dir" >&2; exit 1; }
[ -d "$swift_dir" ] || install -d -m 700 "$swift_dir"
chown -h "$user" "$swift_dir"
chmod 700 "$swift_dir"
echo "Installed the swift test slot at $swift_link ($swift_dir)"

# The coreaudiod check runs as root in the system domain: restarting coreaudiod needs root, and a hung coreaudiod
# hangs a root client's request as it does the runners'. Standard priority, not Background: throttled, a slow probe
# under a busy VM could look like a hang.
[ -d "$daemons" ] || install -d -m 755 "$daemons"
audio_plist=$daemons/$audio_label.plist
cat > "$stage/audio.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$audio_label</string>
  <key>ProgramArguments</key><array><string>$lib/ci-coreaudio-check</string></array>
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>300</integer>
  <key>ProcessType</key><string>Standard</string>
  <key>StandardOutPath</key><string>/Library/Logs/ci-coreaudio-check.log</string>
  <key>StandardErrorPath</key><string>/Library/Logs/ci-coreaudio-check.log</string>
</dict></plist>
PLIST
if cmp -s "$stage/audio.plist" "$audio_plist" && "$launchctl" print "system/$audio_label" >/dev/null 2>&1; then
  echo "coreaudiod check unchanged and loaded"
else
  install -m 644 "$stage/audio.plist" "$stage/audio.plist.new"
  mv -f "$stage/audio.plist.new" "$audio_plist"
  "$launchctl" bootout "system/$audio_label" >/dev/null 2>&1 || true
  tries=0
  while "$launchctl" print "system/$audio_label" >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "$tries" -le 60 ] || { echo "The old coreaudiod check did not unload within 30 seconds" >&2; exit 1; }
    sleep 0.5
  done
  "$launchctl" bootstrap system "$audio_plist"
  echo "coreaudiod check loaded"
fi

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

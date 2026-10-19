#!/bin/sh
# Runs ON the Mac, from scripts/mac/install.zsh: the pinned Peekaboo, app and CLI, and the LaunchAgent that keeps the app
# running in the logged-in session. Idempotent: a Mac already on the pin changes nothing.
#
#   ~/Applications/Peekaboo.app                           the app (it holds Screen Recording and Accessibility, and
#                                                         serves the Bridge socket the CLI talks to)
#   ~/Library/Application Support/Peekaboo/cli-<version>  the CLI release for that version
#   ~/Library/Application Support/Peekaboo/cli-current    -> the CLI matching the INSTALLED app (the peekaboo wrapper
#                                                         runs cli-current/peekaboo), so app and CLI never disagree
#   ~/Library/LaunchAgents/com.ccgames.peekaboo.plist     `open -g -a ~/Applications/Peekaboo.app` at login
#
# Every download is checked against its pinned sha256 and its code signature (Team ID) before it is installed.
# A RUNNING Peekaboo of another version is left alone (it would be replaced under a running process) unless the first
# argument is --update-running: then it is quit, swapped and reopened through its LaunchAgent. Updates from one
# signed release to another keep the Mac's privacy grants (same identifier and Team ID). The grants themselves (Screen
# Recording, Accessibility) are a one-time step at the Mac: README, "MacBook: The agent tools".
set -eu

VERSION=4.9.5
TEAM=FWJYW4S8P8 # Developer ID Application: OpenClaw Foundation (the release's SIGNING-MANIFEST.json)
APP_ZIP_SHA256=d176301b0ef4b695c11cbdb8c0bdbbc71f47f866f76c734ddad8343ab5f93e38
CLI_TGZ_SHA256=2f435c736459a3d30279835038fc7e67028875be837f467c87521ea14da3a9ee
BASE=https://github.com/openclaw/Peekaboo/releases/download/v$VERSION

update_running=0
[ "${1:-}" = --update-running ] && update_running=1
apps=$HOME/Applications
app=$apps/Peekaboo.app
support="$HOME/Library/Application Support/Peekaboo"
backup=$HOME/.local/state/mac-setup/backups
label=com.ccgames.peekaboo
plist=$HOME/Library/LaunchAgents/$label.plist
uid=$(id -u)
mkdir -p "$apps" "$support" "$backup" "$HOME/Library/LaunchAgents"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT

sum() { shasum -a 256 "$1" | cut -d' ' -f1; }
team() { codesign -dv "$1" 2>&1 | sed -n 's/^TeamIdentifier=//p'; }
app_version() { defaults read "$1/Contents/Info" CFBundleShortVersionString 2>/dev/null || true; }
fetch() { # URL SHA256 FILE
  curl -fsSL --retry 3 "$1" -o "$3"
  [ "$(sum "$3")" = "$2" ] || { echo "peekaboo: $(basename "$1") did not match its pinned sha256" >&2; exit 1; }
}

# The CLI for the pinned version.
cli=$support/cli-$VERSION
if [ "$(cat "$cli/.release-sha256" 2>/dev/null || true)" = "$CLI_TGZ_SHA256" ] && [ -x "$cli/peekaboo" ] &&
  [ "$(team "$cli/peekaboo")" = "$TEAM" ] && codesign --verify --strict "$cli/peekaboo" 2>/dev/null; then
  echo "peekaboo: CLI $VERSION already installed"
else
  fetch "$BASE/peekaboo-macos-arm64.tar.gz" "$CLI_TGZ_SHA256" "$tmp/cli.tgz"
  tar -xzf "$tmp/cli.tgz" -C "$tmp"
  [ "$(cat "$tmp/peekaboo-macos-arm64/VERSION")" = "$VERSION" ] || { echo "peekaboo: the CLI release is not $VERSION" >&2; exit 1; }
  [ "$(team "$tmp/peekaboo-macos-arm64/peekaboo")" = "$TEAM" ] || { echo "peekaboo: the CLI is not signed by $TEAM" >&2; exit 1; }
  codesign --verify --strict "$tmp/peekaboo-macos-arm64/peekaboo" || { echo "peekaboo: the CLI's signature does not verify" >&2; exit 1; }
  echo "$CLI_TGZ_SHA256" > "$tmp/peekaboo-macos-arm64/.release-sha256"
  rm -rf "$support/.cli-$VERSION.new" "$support/.cli-$VERSION.old"
  mv "$tmp/peekaboo-macos-arm64" "$support/.cli-$VERSION.new"
  [ ! -e "$cli" ] || mv "$cli" "$support/.cli-$VERSION.old"
  mv "$support/.cli-$VERSION.new" "$cli"
  rm -rf "$support/.cli-$VERSION.old"
  echo "peekaboo: installed CLI $VERSION"
fi

# The app.
running() { pgrep -f "^$app/Contents/MacOS/Peekaboo" >/dev/null 2>&1; }
have=$(app_version "$app")
if [ "$have" = "$VERSION" ] && [ "$(team "$app")" = "$TEAM" ] && codesign --verify --deep --strict "$app" 2>/dev/null; then
  echo "peekaboo: app $VERSION already installed"
elif running && [ "$update_running" -eq 0 ]; then
  echo "peekaboo: app ${have:-?} is running; it stays until Peekaboo is quit at the Mac, or rerun with --update-peekaboo" \
       "(quits it, installs $VERSION and reopens it)" >&2
else
  fetch "$BASE/Peekaboo-$VERSION.app.zip" "$APP_ZIP_SHA256" "$tmp/app.zip"
  mkdir "$tmp/app"
  ditto -x -k "$tmp/app.zip" "$tmp/app"
  [ "$(app_version "$tmp/app/Peekaboo.app")" = "$VERSION" ] || { echo "peekaboo: the app release is not $VERSION" >&2; exit 1; }
  [ "$(team "$tmp/app/Peekaboo.app")" = "$TEAM" ] || { echo "peekaboo: the app is not signed by $TEAM" >&2; exit 1; }
  codesign --verify --deep --strict "$tmp/app/Peekaboo.app" || { echo "peekaboo: the app's signature does not verify" >&2; exit 1; }
  was_running=0
  if running; then
    was_running=1
    pkill -f "^$app/Contents/MacOS/Peekaboo" || true
    n=0
    while running && [ "$n" -lt 20 ]; do sleep 1; n=$((n + 1)); done
    running && { echo "peekaboo: the running app did not quit; nothing replaced" >&2; exit 1; }
  fi
  if [ -e "$app" ]; then
    rm -rf "$backup/Peekaboo.app"
    mv "$app" "$backup/Peekaboo.app"
  fi
  mv "$tmp/app/Peekaboo.app" "$app"
  echo "peekaboo: installed app $VERSION (was ${have:-none}; the old one is in $backup)"
  # Start the new app whenever it is not running, whether it was quit for this swap or before it (the documented update
  # path is to quit Peekaboo first, and the already-loaded one-shot LaunchAgent will not run again until the next login).
  running || launchctl kickstart "gui/$uid/$label" >/dev/null 2>&1 || open -g -a "$app" || true
fi

# cli-current follows the app that is actually installed.
have=$(app_version "$app")
if [ -n "$have" ] && [ -x "$support/cli-$have/peekaboo" ]; then
  want=cli-$have
else
  want=cli-$VERSION
  [ -z "$have" ] || echo "peekaboo: WARNING: no CLI for app $have; cli-current -> $want" >&2
fi
if [ "$(readlink "$support/cli-current" 2>/dev/null || true)" != "$want" ]; then
  ln -sfn "$want" "$support/cli-current"
  echo "peekaboo: cli-current -> $want"
fi

# The LaunchAgent (an earlier hand-made one had another label: retire it; booting out a finished `open` stops nothing).
old=$HOME/Library/LaunchAgents/com.ejcampbell.peekaboo.plist
if [ -e "$old" ]; then
  launchctl bootout "gui/$uid/com.ejcampbell.peekaboo" >/dev/null 2>&1 || true
  mv -f "$old" "$backup/com.ejcampbell.peekaboo.plist"
  echo "peekaboo: retired the LaunchAgent com.ejcampbell.peekaboo (kept in $backup)"
fi
cat > "$tmp/agent.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key>
	<string>$label</string>
	<key>LimitLoadToSessionType</key>
	<string>Aqua</string>
	<key>ProcessType</key>
	<string>Interactive</string>
	<key>ProgramArguments</key>
	<array>
		<string>/usr/bin/open</string>
		<string>-g</string>
		<string>-a</string>
		<string>$app</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
</dict>
</plist>
PLIST
if ! cmp -s "$tmp/agent.plist" "$plist"; then
  install -m 644 "$tmp/agent.plist" "$plist"
  launchctl bootout "gui/$uid/$label" >/dev/null 2>&1 || true
  # Loading it runs `open -g -a`: it starts Peekaboo if it is not running, and does nothing to a running one.
  { launchctl bootstrap "gui/$uid" "$plist" || { echo "peekaboo: could not load the LaunchAgent $label (is anyone logged in at the Mac?)" >&2; exit 1; }; } && echo "peekaboo: LaunchAgent $label loaded"
elif ! launchctl print "gui/$uid/$label" >/dev/null 2>&1; then
  { launchctl bootstrap "gui/$uid" "$plist" || { echo "peekaboo: could not load the LaunchAgent $label (is anyone logged in at the Mac?)" >&2; exit 1; }; } && echo "peekaboo: LaunchAgent $label loaded"
fi
echo "peekaboo: app $(app_version "$app"), $(readlink "$support/cli-current")." \
     "Permissions are granted once at the Mac; check with: peekaboo permissions status"

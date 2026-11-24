#!/bin/zsh
# Runs ON the Mac, from scripts/mac-setup.sh, with the unpacked bin/ directory as $1. Idempotent:
# rerunning puts every file back. Never restarts, kills or relaunches anything, except a running Peekaboo of an
# older version when the second argument is --update-peekaboo (scripts/mac/peekaboo-install.sh).
set -euo pipefail
src=${1:?usage: install.zsh STAGING_DIR [--build-only|--update-peekaboo]}
mode=${2:-}
bin=$HOME/.local/bin
state=$HOME/.local/state
backup=$state/mac-setup/backups
mkdir -p "$bin" "$backup"
chmod 700 "$state/mac-setup" "$backup"

[ "$(uname -sm)" = "Darwin arm64" ] || { echo "mac-setup: this is for an Apple silicon Mac (Darwin arm64)" >&2; exit 1; }

tools=(mac-build mac-build-shim)
if [[ $mode = --build-only ]]; then
  mkdir -p -m 700 "$HOME/agent-work"
  chmod 700 "$HOME/agent-work"
  tools+=(mac-agents-md)
else
  tools+=(mac-gui mac-lease peekaboo)
  # A hand-made peekaboo wrapper predates this; keep the first copy we replace.
  if [ -f "$bin/peekaboo" ] && ! grep -q 'ejc3/aws scripts/mac-setup.sh' "$bin/peekaboo" && [ ! -e "$backup/peekaboo" ]; then
    cp -p "$bin/peekaboo" "$backup/peekaboo"
  fi
fi

for f in "${tools[@]}"; do
  install -m 755 "$src/bin/$f" "$bin/.$f.new"
  mv -f "$bin/.$f.new" "$bin/$f"
done
# The build cap's shims: ~/.local/bin is first on PATH in every SSH session (~/.zshenv).
for t in swift xcodebuild xcrun; do
  if [ -e "$bin/$t" ] && [ ! -L "$bin/$t" ]; then
    echo "mac-setup: $bin/$t exists and is not our shim; leaving it" >&2
    continue
  fi
  ln -sfn mac-build-shim "$bin/$t"
done

# ~/.local/bin first on PATH in every zsh, non-interactive SSH sessions included: a managed block at the
# end of ~/.zshenv, whatever else the file does to PATH (otherwise /usr/bin/swift would bypass the cap).
/bin/sh "$src/set-zshenv.sh" "$HOME/.zshenv" "$mode"
for t in swift xcodebuild xcrun; do
  got=$(env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin /bin/zsh -c "command -v $t")
  [ "$got" = "$bin/$t" ] || { echo "mac-setup: a non-interactive zsh finds $t at $got, not $bin/$t" >&2; exit 1; }
done

if [[ $mode = --build-only ]]; then
  # The iOS app toolchain on the host (pinned, under ~/agent-tools), and the machine's AGENTS.md: the
  # template from this repository and what is on the machine now, rewritten hourly by a LaunchAgent so
  # the attached devices it lists stay current.
  /bin/zsh "$src/ios-toolchain.zsh"
  share=$HOME/.local/share/mac-setup
  mkdir -p "$share"
  install -m 644 "$src/mini-AGENTS.md" "$share/.mini-AGENTS.md.new" && mv -f "$share/.mini-AGENTS.md.new" "$share/mini-AGENTS.md"
  agent=$HOME/Library/LaunchAgents/com.ejc3.mac-agents-md.plist
  mkdir -p "${agent:h}"
  cat > "$agent.new" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.ejc3.mac-agents-md</string>
  <key>ProgramArguments</key><array><string>$bin/mac-agents-md</string></array>
  <key>StartInterval</key><integer>3600</integer>
  <key>RunAtLoad</key><true/>
</dict></plist>
PLIST
  mv -f "$agent.new" "$agent"
  # Loaded once; a later setup changes only the file, read at the next login or load.
  launchctl print "gui/$(id -u)/com.ejc3.mac-agents-md" >/dev/null 2>&1 \
    || launchctl bootstrap "gui/$(id -u)" "$agent" 2>/dev/null \
    || echo "mac-setup: the AGENTS.md LaunchAgent is not loaded (no GUI session); it loads at the next login" >&2
  "$bin/mac-agents-md"
  echo "mac-setup: ~/AGENTS.md written (and hourly); the iOS toolchain is in ~/agent-tools (source ~/agent-tools/env.zsh)"
  echo "mac-setup: build tools ready in $bin; create each workspace with mktemp -d ~/agent-work/task.XXXXXXXX"
  "$bin/mac-build" --status
  exit 0
fi

# Builds use Xcode, not the Command Line Tools (xcodebuild refuses to run under the CLT, and SwiftPM
# then misses the platform SDKs). Only switches when Xcode is installed and the CLT is selected.
if [ -d /Applications/Xcode.app/Contents/Developer ] && [ "$(xcode-select -p)" != /Applications/Xcode.app/Contents/Developer ]; then
  if sudo -n true 2>/dev/null; then
    sudo -n xcode-select -s /Applications/Xcode.app/Contents/Developer
    echo "mac-setup: xcode-select now points at /Applications/Xcode.app"
  else
    echo "mac-setup: WARNING: xcode-select points at $(xcode-select -p) and sudo needs a password; run: sudo xcode-select -s /Applications/Xcode.app" >&2
  fi
fi

# Xcode's DerivedData on the disk ~/.config/mac-setup/deriveddata names, when that file exists and the disk is
# mounted; Xcode's default is ~/Library/Developer/Xcode/DerivedData, on the internal disk. It is a user default,
# so it applies to Xcode and to every xcodebuild run without -derivedDataPath.
dd_file=$HOME/.config/mac-setup/deriveddata
if [ -r "$dd_file" ]; then
  dd=$(<"$dd_file")
  if [ -n "$dd" ] && [ -d "${dd:h}" ] && mkdir -p "$dd"; then
    if [ "$(defaults read com.apple.dt.Xcode IDECustomDerivedDataLocation 2>/dev/null)" != "$dd" ]; then
      defaults write com.apple.dt.Xcode IDECustomDerivedDataLocation "$dd"
      echo "mac-setup: Xcode's DerivedData now goes to $dd"
    fi
  else
    echo "mac-setup: WARNING: $dd_file names $dd, whose disk is not mounted; DerivedData left where it is" >&2
  fi
fi

# Only the MacBook alias receives the mac-artifact-clean installer and payload.
if [[ -f $src/install-artifact-cleaner.py ]]; then
  /usr/bin/python3 "$src/install-artifact-cleaner.py" "$src"
fi

# The pinned Peekaboo app and CLI, and its LaunchAgent (the wrapper above runs its CLI).
if [ "$mode" = --update-peekaboo ]; then
  /bin/sh "$src/peekaboo-install.sh" --update-running
else
  /bin/sh "$src/peekaboo-install.sh"
fi

echo "mac-setup: installed mac-gui, mac-lease, mac-build (+ swift/xcodebuild/xcrun shims), peekaboo into $bin"
"$bin/mac-lease" status | sed 's/^/mac-setup: desktop lease: /'
"$bin/mac-build" --status | sed 's/^/mac-setup: build /'

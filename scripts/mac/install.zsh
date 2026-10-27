#!/bin/zsh
# Runs ON the Mac, from scripts/mac-setup.sh, with the unpacked bin/ directory as $1. Idempotent:
# rerunning puts every file back. Never restarts, kills or relaunches anything, except a running Peekaboo of an
# older version when the second argument is --update-peekaboo (scripts/mac/peekaboo-install.sh).
set -euo pipefail
src=${1:?usage: install.zsh STAGING_DIR [--update-peekaboo]}
update_peekaboo=${2:-}
bin=$HOME/.local/bin
state=$HOME/.local/state
backup=$state/mac-setup/backups
mkdir -p "$bin" "$backup"
chmod 700 "$state/mac-setup" "$backup"

[ "$(uname -sm)" = "Darwin arm64" ] || { echo "mac-setup: this is for the MacBook (Darwin arm64)" >&2; exit 1; }

# A hand-made peekaboo wrapper predates this; keep the first copy we replace.
if [ -f "$bin/peekaboo" ] && ! grep -q 'ejc3/aws scripts/mac-setup.sh' "$bin/peekaboo" && [ ! -e "$backup/peekaboo" ]; then
  cp -p "$bin/peekaboo" "$backup/peekaboo"
fi

for f in mac-gui mac-lease mac-build mac-build-shim peekaboo; do
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

# ~/.local/bin first on PATH in every zsh, non-interactive SSH sessions included: a managed block at the
# end of ~/.zshenv, whatever else the file does to PATH (otherwise /usr/bin/swift would bypass the cap).
/bin/sh "$src/set-zshenv.sh" "$HOME/.zshenv"
for t in swift xcodebuild xcrun; do
  got=$(env -i HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin /bin/zsh -c "command -v $t")
  [ "$got" = "$bin/$t" ] || { echo "mac-setup: a non-interactive zsh finds $t at $got, not $bin/$t" >&2; exit 1; }
done

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

# The pinned Peekaboo app and CLI, and its LaunchAgent (the wrapper above runs its CLI).
if [ "$update_peekaboo" = --update-peekaboo ]; then
  /bin/sh "$src/peekaboo-install.sh" --update-running
else
  /bin/sh "$src/peekaboo-install.sh"
fi

echo "mac-setup: installed mac-gui, mac-lease, mac-build (+ swift/xcodebuild/xcrun shims), peekaboo into $bin"
"$bin/mac-lease" status | sed 's/^/mac-setup: desktop lease: /'
"$bin/mac-build" --status | sed 's/^/mac-setup: build /'

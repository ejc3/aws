#!/bin/zsh
# Runs ON the MacBook, from scripts/mac-setup.sh, with the unpacked bin/ directory as $1. Idempotent:
# rerunning puts every file back. Never restarts, kills or relaunches anything.
set -euo pipefail
src=${1:?usage: install.zsh STAGING_DIR}
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

# ~/.local/bin first on PATH for noninteractive SSH sessions (it already is on the live Mac).
if ! grep -q '\.local/bin' "$HOME/.zshenv" 2>/dev/null; then
  printf '\n# Make local tools available to noninteractive SSH sessions (ejc3/aws scripts/mac-setup.sh).\ntypeset -U path\npath=("$HOME/.local/bin" $path)\nexport PATH\n' >> "$HOME/.zshenv"
fi

echo "mac-setup: installed mac-gui, mac-lease, mac-build (+ swift/xcodebuild/xcrun shims), peekaboo into $bin"
"$bin/mac-lease" status | sed 's/^/mac-setup: desktop lease: /'
"$bin/mac-build" --status | sed 's/^/mac-setup: build /'

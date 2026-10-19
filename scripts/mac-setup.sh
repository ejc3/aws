#!/usr/bin/env bash
#
# mac-setup -- install a Mac's agent tools from this repository, so the Mac side is reproducible:
#
#   mac-gui      run a command in the logged-in GUI session (the login keychain: codesign, archive, export)
#   mac-lease    the desktop lease: who is driving the screen right now (advisory)
#   mac-build    at most two heavy builds at once; swift/xcodebuild/xcrun shims route builds through it
#   peekaboo     the Peekaboo CLI through the app's Bridge, waiting while another session holds the lease
#   Peekaboo     the pinned app and CLI release (scripts/mac/peekaboo-install.sh) and the LaunchAgent that opens it
#
# and point xcode-select at Xcode. Run it from a box where `ssh <alias>` works (fcvm-arm as ubuntu; nextjs-dev as ejc3
# for mac, as colton or ejc3 for colton-mac), from a checkout of this repository:
#
#   scripts/mac-setup.sh [SSH_ALIAS] [--update-peekaboo]      default alias: mac
#
# Idempotent; it replaces files and never restarts, kills or relaunches anything, except that --update-peekaboo quits a
# running Peekaboo of another version, installs the pinned one and reopens it. The macOS privacy grants Peekaboo needs
# (Screen Recording, Accessibility) are a one-time step at the Mac. README, "The MacBook: remote access and its scratch
# drive", has the routes and the rules these tools serve.
set -euo pipefail
here=$(cd "$(dirname "$0")/mac" && pwd)
usage() { echo "usage: scripts/mac-setup.sh [SSH_ALIAS] [--update-peekaboo]" >&2; exit 2; }
alias_=mac
update=""
for a in "$@"; do
  case "$a" in
    --update-peekaboo) update=--update-peekaboo ;;
    -*) usage ;;
    *) alias_=$a ;;
  esac
done
[[ $alias_ =~ ^[a-z][a-z0-9-]{0,30}$ ]] || usage
for f in install.zsh set-zshenv.sh peekaboo-install.sh bin/mac-gui bin/mac-lease bin/mac-build bin/mac-build-shim bin/peekaboo; do
  [ -f "$here/$f" ] || { echo "mac-setup: missing $here/$f" >&2; exit 1; }
done
# One SSH session: the files as a tar stream on stdin, unpacked into a private temporary directory.
tar -C "$here" -cf - install.zsh set-zshenv.sh peekaboo-install.sh bin | ssh -o BatchMode=yes -o ConnectTimeout=20 "$alias_" \
  '/bin/zsh -c '\''d=$(mktemp -d) && tar -C "$d" -xf - && /bin/zsh "$d/install.zsh" "$d" '"$update"'; rc=$?; rm -rf "$d"; exit $rc'\'''

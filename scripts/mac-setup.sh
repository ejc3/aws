#!/usr/bin/env bash
#
# mac-setup -- install the MacBook's agent tools from this repository, so the Mac side is reproducible:
#
#   mac-gui      run a command in the logged-in GUI session (the login keychain: codesign, archive, export)
#   mac-lease    the desktop lease: who is driving the screen right now (advisory)
#   mac-build    at most two heavy builds at once; swift/xcodebuild/xcrun shims route builds through it
#   peekaboo     the Peekaboo CLI through the app's Bridge, waiting while another session holds the lease
#
# and point xcode-select at Xcode. Run it from a box where `ssh mac` works (fcvm-arm as ubuntu, nextjs-dev
# as ejc3), from a checkout of this repository:
#
#   scripts/mac-setup.sh [SSH_ALIAS]      default: mac
#
# Idempotent; it replaces files and never restarts, kills or relaunches anything on the Mac. README,
# "The MacBook: remote access and its scratch drive", has the routes and the rules these tools serve.
set -euo pipefail
here=$(cd "$(dirname "$0")/mac" && pwd)
alias_=${1:-mac}
[[ $alias_ =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { echo "usage: scripts/mac-setup.sh [SSH_ALIAS]" >&2; exit 2; }
for f in install.zsh bin/mac-gui bin/mac-lease bin/mac-build bin/mac-build-shim bin/peekaboo; do
  [ -f "$here/$f" ] || { echo "mac-setup: missing $here/$f" >&2; exit 1; }
done
# One SSH session: the files as a tar stream on stdin, unpacked into a private temporary directory.
tar -C "$here" -cf - install.zsh bin | ssh -o BatchMode=yes -o ConnectTimeout=20 "$alias_" \
  '/bin/zsh -c '\''d=$(mktemp -d) && tar -C "$d" -xf - && /bin/zsh "$d/install.zsh" "$d"; rc=$?; rm -rf "$d"; exit $rc'\'''

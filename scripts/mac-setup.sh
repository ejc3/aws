#!/usr/bin/env bash
#
# mac-setup -- install a Mac's agent tools from this repository, so the Mac side is reproducible:
#
#   mac-gui      run a command in the logged-in GUI session (the login keychain: codesign, archive, export)
#   mac-lease    the desktop lease: who is driving the screen right now (advisory)
#   mac-build    at most two heavy builds at once; swift/xcodebuild/xcrun shims route builds through it
#   peekaboo     the Peekaboo CLI through the app's Bridge, waiting while another session holds the lease
#   mac-artifact-clean  old Xcode artifacts under pressure on the MacBook Scratch volume; hourly (mac only)
#   Peekaboo     the pinned app and CLI release (scripts/mac/peekaboo-install.sh) and the LaunchAgent that opens it
#
# and point xcode-select at Xcode. Run it from a box where `ssh <alias>` works (fcvm-arm as ubuntu; nextjs-dev as ejc3
# for mac, as colton or ejc3 for colton-mac), from a checkout of this repository:
#
#   scripts/mac-setup.sh [SSH_ALIAS] [--update-peekaboo]      default alias: mac
#   scripts/mac-setup.sh mac --artifact-cleaner-only     install only the MacBook Scratch cleaner
#   scripts/mac-setup.sh mini      the mini: private workspaces, the shared build slots, the iOS app toolchain on the
#                                  host (scripts/mac/ios-toolchain.zsh) and its ~/AGENTS.md (mac-agents-md, hourly)
#
# Idempotent; it replaces files and never restarts, kills or relaunches anything, except that --update-peekaboo quits a
# running Peekaboo of another version, installs the pinned one and reopens it. The macOS privacy grants Peekaboo needs
# (Screen Recording, Accessibility) are a one-time step at the Mac. README, "The MacBook: remote access and its scratch
# drive", has the routes and the rules these tools serve.
set -euo pipefail
here=$(cd "$(dirname "$0")/mac" && pwd)
usage() { echo "usage: scripts/mac-setup.sh [SSH_ALIAS] [--update-peekaboo | --artifact-cleaner-only]" >&2; exit 2; }
alias_=mac
update=""
cleaner_only=""
for a in "$@"; do
  case "$a" in
    --update-peekaboo) update=--update-peekaboo ;;
    --artifact-cleaner-only) cleaner_only=1 ;;
    -*) usage ;;
    *) alias_=$a ;;
  esac
done
[[ $alias_ =~ ^[a-z][a-z0-9-]{0,30}$ ]] || usage
mode=$update
entry='/bin/zsh "$d/install.zsh"'
files=(install.zsh set-zshenv.sh bin/mac-build bin/mac-build-shim)
if [[ -n $cleaner_only ]]; then
  [[ $alias_ = mac && -z $update ]] || usage
  files=(bin/mac-build)
  entry='/usr/bin/python3 "$d/install-artifact-cleaner.py"'
elif [[ $alias_ = mini ]]; then
  [[ -z $update ]] || usage
  mode=--build-only
  files+=(ios-toolchain.zsh mini-AGENTS.md bin/mac-agents-md)
else
  files+=(peekaboo-install.sh bin/mac-gui bin/mac-lease bin/peekaboo bin/mac-agents-md mac-AGENTS.md)
fi
if [[ $alias_ = mac ]]; then
  files+=(install-artifact-cleaner.py bin/mac-artifact-clean)
fi
for f in "${files[@]}"; do
  [ -f "$here/$f" ] || { echo "mac-setup: missing $here/$f" >&2; exit 1; }
done
# One SSH session: the files as a tar stream on stdin, unpacked into a private temporary directory.
tar -C "$here" -cf - "${files[@]}" | ssh -o BatchMode=yes -o ConnectTimeout=20 "$alias_" \
  '/bin/zsh -c '\''d=$(mktemp -d /tmp/mac-setup.XXXXXXXX) && tar -C "$d" -xf - && '"$entry"' "$d" '"$mode"'; rc=$?; rm -rf "$d"; exit $rc'\'''

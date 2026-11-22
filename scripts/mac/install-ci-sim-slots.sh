#!/bin/sh
# Invoked inside the CI VM as root. A staging prefix also permits a disposable install test.
set -eu
src=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
prefix=${1:-/usr/local}
lib=$prefix/libexec/mini-simulator
link=$prefix/bin/xcodebuild
target=$lib/sim-slot-shim
if [ -e "$link" ] || [ -L "$link" ]; then
  [ "$(readlink "$link")" = "$target" ] || { echo "Refusing to replace unmanaged $link" >&2; exit 1; }
fi
for dir in "$prefix/bin" "$prefix/libexec" "$lib"; do
  [ -d "$dir" ] || install -d -m 755 "$dir"
done
stage=$(mktemp -d "$lib/.install.XXXXXXXX")
trap 'rm -rf -- "$stage"' EXIT
for source in bin/mac-build ci/sim-slot-shim; do
  file=${source##*/}
  install -m 755 "$src/$source" "$stage/$file"
  mv -f "$stage/$file" "$lib/$file"
done
if [ ! -L "$link" ]; then
  ln -s "$target" "$link" || [ "$(readlink "$link")" = "$target" ]
fi
echo "Installed simulator test slots at $link"

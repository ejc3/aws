#!/bin/zsh
# Runs ON the mini, from install.zsh --build-only: the toolchain a React Native iOS app needs to
# build on the host (not only in the CI VMs), pinned and checked, under ~/agent-tools:
#
#   node     Node.js LTS (the official tarball, SHA-256 pinned)
#   pnpm     pnpm, installed by that Node's npm at a pinned version
#   xcodegen XcodeGen (the release zip, SHA-256 pinned)
#   ruby     Ruby 3.3 from conda-forge, through a pinned micromamba (SHA-256 pinned); the system
#            Ruby (2.6) is too old for CocoaPods' dependencies
#   pod      CocoaPods, a pinned gem in that Ruby
#   tmux     tmux 3.x from conda-forge through the same micromamba (owner, 2026-11-28: "install tmux on
#            the machine"); install.zsh links it into ~/.local/bin so every shell finds it
#
# and ~/agent-tools/env.zsh, which a build shell sources to put them first on PATH. Idempotent: a
# tool already at its pinned version is left alone. Nothing outside ~/agent-tools is changed, and
# nothing needs sudo.
set -euo pipefail
root=$HOME/agent-tools
mkdir -p -m 700 "$root"

node_version=22.23.5
node_sha256=e7131995759b61a0170b646010d2ad35a5d37b4cb0d471f3e8317eac6f742f3d
pnpm_version=12.15.0
xcodegen_version=2.48.0
xcodegen_sha256=620c9f0dc10849f94728ab5df52d225ebab8187439dd6fee1925e59e80dff892
micromamba_version=2.10.0-0
micromamba_sha256=c547639f5caf742236641c362746abb4a3505774753ecd08fe341643670c4c01
ruby_spec='ruby>=3.3,<3.4'
cocoapods_version=1.17.0
tmux_spec='tmux>=3.5,<4'

tmp=$(mktemp -d "${TMPDIR:-/tmp}/ios-toolchain.XXXXXX")
trap 'rm -rf "$tmp"' EXIT

# Downloads $1 to $2 and refuses it unless its SHA-256 is $3.
fetch() {
  curl -fsSL --retry 3 -o "$2" "$1"
  local got
  got=$(shasum -a 256 "$2" | cut -d' ' -f1)
  [ "$got" = "$3" ] || { echo "ios-toolchain: $1 has SHA-256 $got, not the pinned $3" >&2; exit 1; }
}

if [ "$("$root/node/bin/node" -v 2>/dev/null)" != "v$node_version" ]; then
  fetch "https://nodejs.org/dist/v$node_version/node-v$node_version-darwin-arm64.tar.gz" "$tmp/node.tgz" "$node_sha256"
  rm -rf "$root/node.new" && mkdir -p "$root/node.new"
  tar -xzf "$tmp/node.tgz" -C "$root/node.new" --strip-components=1
  rm -rf "$root/node" && mv "$root/node.new" "$root/node"
fi
export PATH="$root/node/bin:$PATH"

if [ "$("$root/pnpm/bin/pnpm" -v 2>/dev/null)" != "$pnpm_version" ]; then
  npm install -g --prefix "$root/pnpm" "pnpm@$pnpm_version" >/dev/null
fi

if [ "$("$root/xcodegen/bin/xcodegen" --version 2>/dev/null | tail -1)" != "Version: $xcodegen_version" ]; then
  fetch "https://github.com/yonaskolb/XcodeGen/releases/download/$xcodegen_version/xcodegen.zip" "$tmp/xcodegen.zip" "$xcodegen_sha256"
  rm -rf "$root/xcodegen.new" && mkdir -p "$root/xcodegen.new"
  unzip -q "$tmp/xcodegen.zip" -d "$root/xcodegen.new"
  rm -rf "$root/xcodegen" && mv "$root/xcodegen.new/xcodegen" "$root/xcodegen" && rm -rf "$root/xcodegen.new"
fi

if [ "$("$root/micromamba" --version 2>/dev/null)" != "${micromamba_version%-*}" ]; then
  fetch "https://github.com/mamba-org/micromamba-releases/releases/download/$micromamba_version/micromamba-osx-arm64" "$tmp/micromamba" "$micromamba_sha256"
  install -m 755 "$tmp/micromamba" "$root/micromamba"
fi
if ! "$root/ruby/bin/ruby" -e 'exit(RUBY_VERSION.start_with?("3.3") ? 0 : 1)' 2>/dev/null; then
  MAMBA_ROOT_PREFIX="$root/mamba" "$root/micromamba" create -y -q -p "$root/ruby" -c conda-forge "$ruby_spec" >/dev/null
fi
if ! "$root/tmux/bin/tmux" -V 2>/dev/null | grep -q '^tmux 3\.'; then
  MAMBA_ROOT_PREFIX="$root/mamba" "$root/micromamba" create -y -q -p "$root/tmux" -c conda-forge "$tmux_spec" >/dev/null
fi
gems=$root/ruby/share/rubygems
if [ "$(GEM_HOME=$gems GEM_PATH=$gems "$root/ruby/bin/ruby" "$gems/bin/pod" --version 2>/dev/null)" != "$cocoapods_version" ]; then
  GEM_HOME=$gems GEM_PATH=$gems "$root/ruby/bin/gem" install cocoapods -v "$cocoapods_version" --no-document >/dev/null
fi

cat > "$root/env.zsh.new" <<EOF
# The mini's iOS app toolchain (ejc3/aws scripts/mac/ios-toolchain.zsh). Source it in a build shell.
export PATH="$root/node/bin:$root/pnpm/bin:$root/xcodegen/bin:$gems/bin:$root/ruby/bin:\$PATH"
export GEM_HOME="$gems" GEM_PATH="$gems"
export DEVELOPER_DIR="\${DEVELOPER_DIR:-\$HOME/Applications/Xcode.app/Contents/Developer}"
export LANG=en_US.UTF-8
EOF
mv -f "$root/env.zsh.new" "$root/env.zsh"

source "$root/env.zsh"
echo "ios-toolchain: node $(node -v), pnpm $(pnpm -v), $(xcodegen --version | tail -1 | tr 'V' 'v'), ruby $(ruby -e 'print RUBY_VERSION'), pod $(pod --version), $("$root/tmux/bin/tmux" -V)"

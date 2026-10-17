#!/bin/bash
# The imagine-runner image's toolchains (runner-app-image.tf), run as root on the builder by
# runner-app/image-build.sh, after bootstrap.sh's own install (the runner, its packages, the
# `runner` user and the tool cache). The versions are runner_app_images' pins, exported by the
# builder's user data.
#
# Each toolchain goes where ejc3/imagine's CI setup looks before it downloads one, so a job finds
# it and skips that download. A job that does not find what it asks for (an image built before a
# version changed) installs it as on Canonical's image: an image that falls behind is only slower.
set -euo pipefail

TOOL_CACHE=${TOOL_CACHE:-/opt/hostedtoolcache}  # bootstrap.sh's; jobs see it as RUNNER_TOOL_CACHE
TOOL_ARCH=x64                                   # the tool cache's name for x86_64 (Node's os.arch())
HEX_MIRROR=${HEX_MIRROR:-https://builds.hex.pm}
NODE_DIR=${NODE_DIR:-/opt/node}

get() { curl -fsSL --retry 3 --retry-all-errors "$@"; }

# The listing's sha256 (its fourth field) of one build, or nothing.
listed_sha256() { awk -v ref="$2" '$1 == ref { print $4; exit }' "$1"; }

install_packages() {
  # pkg-config for the engine's native build; docker and buildx for the e2e image and the libSQL
  # server. The runner joins the docker group, so a job reaches the socket without sudo.
  apt-get install -y -q --no-install-recommends pkg-config xz-utils docker.io docker-buildx
  usermod -aG docker runner
  systemctl enable docker.service docker.socket
  systemctl start docker.service
}

install_rust() {
  # rustup in the runner's home, as a job's own setup would put it, with the toolchain
  # rust-toolchain.toml names: its components and the WebAssembly target.
  sudo -u runner -H bash -c 'curl --proto "=https" --tlsv1.2 -fsSL https://sh.rustup.rs \
    | sh -s -- -y --profile minimal --default-toolchain none --no-modify-path'
  sudo -u runner -H /home/runner/.cargo/bin/rustup toolchain install "$RUST_TOOLCHAIN" \
    --profile minimal --component "$RUST_COMPONENTS" --target "$RUST_TARGETS"
  sudo -u runner -H /home/runner/.cargo/bin/rustup default "$RUST_TOOLCHAIN"
}

install_node() {
  # The newest Node of the pinned major, checked against nodejs.org's SHASUMS256.txt, and pnpm.
  local version name
  version=$(get https://nodejs.org/dist/index.json \
    | jq -r --arg major "v$NODE_MAJOR." '[.[] | select(.version | startswith($major))][0].version')
  [[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "no Node $NODE_MAJOR release listed" >&2; return 1; }
  name=node-$version-linux-x64.tar.gz
  get -o "/tmp/$name" "https://nodejs.org/dist/$version/$name"
  get "https://nodejs.org/dist/$version/SHASUMS256.txt" | grep " $name\$" | (cd /tmp && sha256sum -c -)
  mkdir -p "$NODE_DIR"
  tar -xzf "/tmp/$name" -C "$NODE_DIR" --strip-components=1
  rm -f "/tmp/$name"
  PATH="$NODE_DIR/bin:$PATH" npm install -g --no-fund --no-audit "pnpm@$PNPM_VERSION"
}

install_beam() {
  # Erlang/OTP and Elixir from builds.hex.pm, each checked against the sha256 its listing gives,
  # laid out exactly as erlef/setup-beam caches them (tool-cache cacheDir(dir, tool, versionSpec)):
  # otp/ubuntu-24.04/OTP-<v>/x64 and elixir/<v>-otp-<major>/x64, each with its `.complete`
  # marker. setup-beam picks Elixir's build for the newest OTP major it has, from this one down
  # by two, and so does this.
  local listing sha dir major m ref=""
  listing=$(mktemp)
  get -o "$listing" "$HEX_MIRROR/builds/otp/amd64/ubuntu-24.04/builds.txt"
  sha=$(listed_sha256 "$listing" "OTP-$OTP_VERSION")
  [ -n "$sha" ] || { echo "OTP-$OTP_VERSION is not listed for ubuntu-24.04" >&2; return 1; }
  get -o /tmp/otp.tar.gz "$HEX_MIRROR/builds/otp/amd64/ubuntu-24.04/OTP-$OTP_VERSION.tar.gz"
  echo "$sha  /tmp/otp.tar.gz" | sha256sum -c -
  dir=$TOOL_CACHE/otp/ubuntu-24.04/OTP-$OTP_VERSION/$TOOL_ARCH
  rm -rf "$dir" "$dir.complete"
  mkdir -p "$dir"
  tar -xzf /tmp/otp.tar.gz -C "$dir" --strip-components=1
  touch "$dir.complete"
  rm -f /tmp/otp.tar.gz

  get -o "$listing" "$HEX_MIRROR/builds/elixir/builds.txt"
  major=${OTP_VERSION%%.*}
  for m in "$major" "$((major - 1))" "$((major - 2))"; do
    sha=$(listed_sha256 "$listing" "v$ELIXIR_VERSION-otp-$m")
    if [ -n "$sha" ]; then ref=v$ELIXIR_VERSION-otp-$m; break; fi
  done
  [ -n "$ref" ] || { echo "no Elixir $ELIXIR_VERSION build for OTP $major" >&2; return 1; }
  get -o /tmp/elixir.zip "$HEX_MIRROR/builds/elixir/$ref.zip"
  echo "$sha  /tmp/elixir.zip" | sha256sum -c -
  dir=$TOOL_CACHE/elixir/${ref#v}/$TOOL_ARCH
  rm -rf "$dir" "$dir.complete"
  mkdir -p "$dir"
  unzip -q /tmp/elixir.zip -d "$dir"
  touch "$dir.complete"
  rm -f /tmp/elixir.zip "$listing"
  chown -R runner:runner "$TOOL_CACHE"
}

install_playwright_deps() {
  # Chromium's system libraries, as `playwright install --with-deps chromium` installs them. The
  # browser itself stays in the job's own cache, keyed by Playwright's version.
  PATH="$NODE_DIR/bin:$PATH" npx -y "playwright@$PLAYWRIGHT_VERSION" install-deps chromium
}

pull_images() {
  # The libSQL server image the e2e job runs, so its `docker run` starts without a pull.
  docker pull "$LIBSQL_IMAGE"
}

main() {
  : "${RUST_TOOLCHAIN:?}" "${RUST_COMPONENTS:?}" "${RUST_TARGETS:?}" "${NODE_MAJOR:?}" "${PNPM_VERSION:?}"
  : "${OTP_VERSION:?}" "${ELIXIR_VERSION:?}" "${PLAYWRIGHT_VERSION:?}" "${LIBSQL_IMAGE:?}"
  install_packages
  install_rust
  install_node
  install_beam
  install_playwright_deps
  pull_images
  # Where bootstrap.sh finds this image's toolchains for a job's PATH.
  mkdir -p /etc/github-app-runner
  printf '%s\n' /home/runner/.cargo/bin "$NODE_DIR/bin" > /etc/github-app-runner/path
}

# The tests source this file for one function at a time.
if [ "${IMAGE_SCRIPT_FUNCTIONS_ONLY:-}" != 1 ]; then
  main
fi

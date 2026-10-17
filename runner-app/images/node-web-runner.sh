#!/bin/bash
# The Node web runner images' toolchains (runner-app-image.tf: dolphin-labs-runner, dolphin-films-runner,
# dolphin-maps-runner, colton-games-runner), run as root on the builder by runner-app/image-build.sh, after
# bootstrap.sh's own install (the runner, its packages, the `runner` user and the tool cache). The
# versions are runner_app_images' pins, exported by the builder's user data:
#
#   NODE_MAJOR           required; the newest Node of it, in the tool cache where actions/setup-node finds it
#   PYTHON_VERSIONS      optional, comma-separated minors (3.11,3.12); the newest build of each from
#                        actions/python-versions, in the tool cache where actions/setup-python finds it
#   PLAYWRIGHT_VERSION   optional, with PLAYWRIGHT_BROWSERS (comma-separated: chromium,webkit); those
#                        browsers' system libraries, and the browsers in the runner's ~/.cache/ms-playwright
#   APT_PACKAGES         optional, comma-separated Ubuntu packages a job installs with apt-get
#
# Each goes where the repo's own setup step looks before it downloads, so a job finds it and skips
# that download. A job that does not find what it asks for (an image built before a version changed)
# installs it as on Canonical's image: an image that falls behind is only slower, never different.
set -euo pipefail

TOOL_CACHE=${TOOL_CACHE:-/opt/hostedtoolcache}  # bootstrap.sh's; jobs see it as RUNNER_TOOL_CACHE
TOOL_ARCH=x64                                   # the tool cache's name for x86_64 (Node's os.arch())
NODE_MIRROR=${NODE_MIRROR:-https://nodejs.org/dist}
PYTHON_MANIFEST=${PYTHON_MANIFEST:-https://raw.githubusercontent.com/actions/python-versions/main/versions-manifest.json}
UBUNTU_RELEASE=${UBUNTU_RELEASE:-24.04}
RUNNER_HOME=${RUNNER_HOME:-/home/runner}

get() { curl -fsSL --retry 3 --retry-all-errors "$@"; }

# "a,b" -> "a b", for the pins that are lists (a pin cannot hold a space).
words() { printf '%s' "${1//,/ }"; }

install_node() {
  # The newest Node of the pinned major, checked against nodejs.org's SHASUMS256.txt, laid out as
  # @actions/tool-cache cacheDir('node', version) writes it: node/<version>/x64 and x64.complete.
  # setup-node looks there first (tc.find('node', <its node-version>)) unless check-latest is set.
  local version name dir
  version=$(get "$NODE_MIRROR/index.json" \
    | jq -r --arg major "v$NODE_MAJOR." '[.[] | select(.version | startswith($major))][0].version')
  [[ $version =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "no Node $NODE_MAJOR release listed" >&2; return 1; }
  name=node-$version-linux-x64.tar.gz
  get -o "$TMPDIR/$name" "$NODE_MIRROR/$version/$name"
  get "$NODE_MIRROR/$version/SHASUMS256.txt" | grep " $name\$" | (cd "$TMPDIR" && sha256sum -c -)
  dir=$TOOL_CACHE/node/${version#v}/$TOOL_ARCH
  rm -rf "$dir" "$dir.complete"
  mkdir -p "$dir"
  tar -xzf "$TMPDIR/$name" -C "$dir" --strip-components=1
  touch "$dir.complete"
  rm -f "$TMPDIR/$name"
  NODE_BIN=$dir/bin
}

install_python() {
  # The newest stable build of each pinned minor for this Ubuntu release, from the manifest
  # actions/setup-python itself downloads from, installed by that build's own setup.sh into
  # Python/<version>/x64 (with x64.complete), where setup-python's tc.find looks first.
  local minor version url manifest
  manifest=$(mktemp)
  get -o "$manifest" "$PYTHON_MANIFEST"
  for minor in $(words "$PYTHON_VERSIONS"); do
    read -r version url < <(jq -r --arg minor "$minor." --arg release "$UBUNTU_RELEASE" --arg arch "$TOOL_ARCH" '
      [.[] | select(.stable and (.version | startswith($minor)))
           | {version, file: ([.files[] | select(.platform == "linux" and .platform_version == $release
                                                 and .arch == $arch)][0])}
           | select(.file)][0] | "\(.version) \(.file.download_url)"' "$manifest")
    [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "no Python $minor build for $UBUNTU_RELEASE" >&2; return 1; }
    rm -rf "$TMPDIR/python-$version"
    mkdir -p "$TMPDIR/python-$version"
    get "$url" | tar -xz -C "$TMPDIR/python-$version"
    (cd "$TMPDIR/python-$version" && env -u AGENT_TOOLSDIRECTORY RUNNER_TOOL_CACHE="$TOOL_CACHE" bash ./setup.sh)
    rm -rf "$TMPDIR/python-$version"
  done
  rm -f "$manifest"
}

install_apt_packages() {
  # shellcheck disable=SC2046 # one word per package
  apt-get install -y -q --no-install-recommends $(words "$APT_PACKAGES")
}

install_playwright() {
  # The browsers' system libraries, as `playwright install --with-deps` installs them, and the
  # browsers themselves in the runner's cache: Playwright keeps each browser under its revision,
  # so a job whose Playwright wants another revision downloads it as usual.
  local browsers
  browsers=$(words "$PLAYWRIGHT_BROWSERS")
  # shellcheck disable=SC2086 # one word per browser
  PATH="$NODE_BIN:$PATH" npx -y "playwright@$PLAYWRIGHT_VERSION" install-deps $browsers
  # shellcheck disable=SC2086
  sudo -u runner -H env PATH="$NODE_BIN:$PATH" npx -y "playwright@$PLAYWRIGHT_VERSION" install $browsers
}

main() {
  : "${NODE_MAJOR:?}"
  if [ -n "${PLAYWRIGHT_VERSION:-}" ] || [ -n "${PLAYWRIGHT_BROWSERS:-}" ]; then
    : "${PLAYWRIGHT_VERSION:?set with PLAYWRIGHT_BROWSERS}" "${PLAYWRIGHT_BROWSERS:?set with PLAYWRIGHT_VERSION}"
  fi
  TMPDIR=${TMPDIR:-/tmp}
  [ -z "${APT_PACKAGES:-}" ] || install_apt_packages
  install_node
  [ -z "${PYTHON_VERSIONS:-}" ] || install_python
  [ -z "${PLAYWRIGHT_VERSION:-}" ] || install_playwright
  chown -R runner:runner "$TOOL_CACHE"
}

# The tests source this file for one function at a time.
if [ "${IMAGE_SCRIPT_FUNCTIONS_ONLY:-}" != 1 ]; then
  main
fi

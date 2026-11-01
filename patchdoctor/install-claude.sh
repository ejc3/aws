#!/usr/bin/env bash
set -euo pipefail
: "${CLAUDE_VERSION:?reviewed Claude Code version required}"
: "${CLAUDE_SHA256:?reviewed linux-x64 checksum required}"
[[ "$CLAUDE_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || exit 1
[[ "$CLAUDE_SHA256" =~ ^[a-f0-9]{64}$ ]] || exit 1
[[ $(uname -m) == x86_64 ]] || exit 1
tmp=$(mktemp)
trap 'rm -f "$tmp"' EXIT
curl --fail --silent --show-error --proto '=https' --tlsv1.2 --max-time 180 \
  "https://downloads.claude.ai/claude-code-releases/$CLAUDE_VERSION/linux-x64/claude" -o "$tmp"
printf '%s  %s\n' "$CLAUDE_SHA256" "$tmp" | sha256sum --check --status
destination=${1:?absolute installation path required}
[[ "$destination" == /opt/patchdoctor/bin/claude ]] || exit 1
install -d -m 0755 "$(dirname "$destination")"
install -m 0755 "$tmp" "$destination"
if ! id doctor >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash doctor
fi
install -d -o doctor -g doctor -m 0700 /tmp/patchdoctor

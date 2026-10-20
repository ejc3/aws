#!/usr/bin/env bash
#
# windows-mcp-client-setup ALIAS -- give this account's Claude Code the Windows-MCP server of a Windows PC it reaches as
# `ssh ALIAS` (mac-ssh-client.tf; the PC set up by scripts/windows-pc-setup.sh). Run as the account itself, on fcvm-arm
# or in a nextjs-ct container. Needs node and npm.
#
#   scripts/windows/windows-mcp-client-setup.sh colin-pc
#
#   * ~/.local/share/windows-mcp-bridge: mcp-remote from the checked-in package-lock.json (`npm ci`: every package at
#     its locked version and integrity hash)
#   * ~/.local/bin/windows-mcp-bridge: the stdio MCP server Claude starts (scripts/windows/windows-mcp-bridge)
#   * `claude mcp add -s user ALIAS -- ~/.local/bin/windows-mcp-bridge ALIAS`, unless an entry by that name exists
# The bridge reads the PC's bearer key over ssh on first use; nothing secret passes through this script.
# Re-running is safe: it reinstalls the same locked packages and leaves an existing Claude entry alone.
set -euo pipefail

ALIAS=${1:-}
[[ $ALIAS =~ ^[a-z][a-z0-9-]{0,30}$ ]] || { echo "usage: windows-mcp-client-setup.sh ALIAS" >&2; exit 2; }
HERE=$(cd "$(dirname "$0")" && pwd)
BRIDGE_HOME="$HOME/.local/share/windows-mcp-bridge"
# Claude Code's native install is in ~/.local/bin, which a provisioning `sudo -u` does not put on PATH.
CLAUDE=$(command -v claude || echo "$HOME/.local/bin/claude")
[ -x "$CLAUDE" ] || { echo "windows-mcp-client-setup: no claude for this account" >&2; exit 1; }

echo "1/3 mcp-remote from the lockfile"
mkdir -p "$BRIDGE_HOME" "$HOME/.local/bin"
cp "$HERE/mcp-remote/package.json" "$HERE/mcp-remote/package-lock.json" "$BRIDGE_HOME/"
(cd "$BRIDGE_HOME" && npm ci --no-fund --no-audit --omit=dev --ignore-scripts >/dev/null)
echo "   mcp-remote $(node -p "require('$BRIDGE_HOME/node_modules/mcp-remote/package.json').version")"

echo "2/3 the bridge"
install -m 755 "$HERE/windows-mcp-bridge" "$HOME/.local/bin/windows-mcp-bridge"

echo "3/3 Claude Code's entry '$ALIAS'"
if "$CLAUDE" mcp get "$ALIAS" >/dev/null 2>&1; then
  echo "   already there; left alone (claude mcp remove $ALIAS -s user, then re-run, to replace it)"
else
  "$CLAUDE" mcp add -s user "$ALIAS" -- "$HOME/.local/bin/windows-mcp-bridge" "$ALIAS"
fi
"$CLAUDE" mcp get "$ALIAS" 2>&1 | grep -E 'Status|Issue' || true

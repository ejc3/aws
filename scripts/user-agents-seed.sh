#!/bin/bash
# user-agents-seed [USER...]
#
# Gives an account that has no user-level agent instructions a starting file:
#   ~/.codex/AGENTS.md   the real file (what Codex reads), a copy of scripts/user-agents.md
#   ~/.claude/CLAUDE.md  a symlink to it (what Claude Code reads), so both agents read one file
#
# It only ever CREATES. If either path already exists (a file, a symlink, even a dangling one)
# the account is left exactly as it is: nothing is overwritten, appended to or re-linked. A
# re-run is therefore a no-op, and an edit the account's owner makes later is never undone.
#
# As root, `user-agents-seed USER...` re-runs itself as each named account, so nothing is ever
# written into a home directory as root. As anyone else, with no arguments, it seeds the calling
# account's $HOME.
set -u
SRC=${USER_AGENTS_SRC:-/usr/local/share/user-agents/AGENTS.md}

if [ "$(id -u)" = 0 ]; then
  [ "$#" -gt 0 ] || { echo "usage: user-agents-seed USER..." >&2; exit 2; }
  rc=0
  for u in "$@"; do
    home=$(getent passwd "$u" | cut -d: -f6)
    if [ -z "$home" ] || [ ! -d "$home" ]; then
      echo "user-agents-seed: no account or home directory for $u, skipping"
      continue
    fi
    runuser -u "$u" -- env HOME="$home" USER_AGENTS_SRC="$SRC" "$0" || rc=1
  done
  exit "$rc"
fi

[ "$#" -eq 0 ] || { echo "user-agents-seed: naming accounts needs root" >&2; exit 2; }
me=$(id -un)
agents="$HOME/.codex/AGENTS.md"
claude="$HOME/.claude/CLAUDE.md"

for f in "$agents" "$claude"; do
  if [ -e "$f" ] || [ -L "$f" ]; then
    echo "user-agents-seed: $me already has $f, leaving the account alone"
    exit 0
  fi
done

[ -s "$SRC" ] || { echo "user-agents-seed: $SRC is missing or empty" >&2; exit 1; }
mkdir -p -m 700 "$HOME/.codex" "$HOME/.claude" || exit 1
tmp=$(mktemp "$HOME/.codex/.AGENTS.md.XXXXXX") || exit 1
trap 'rm -f "$tmp"' EXIT
cat "$SRC" > "$tmp" && chmod 644 "$tmp" || exit 1

# ln refuses a name that exists, so a file that appeared since the check above is kept.
ln "$tmp" "$agents" || { echo "user-agents-seed: $me: could not create $agents" >&2; exit 1; }
ln -s ../.codex/AGENTS.md "$claude" || { echo "user-agents-seed: $me: could not create $claude" >&2; exit 1; }
echo "user-agents-seed: $me: created $agents and $claude -> ../.codex/AGENTS.md"

#!/usr/bin/env bash
# pr-watch-install.sh: install the owner's PR and issue watch (scripts/pr-watch/).
#
#   scripts/pr-watch-install.sh --watcher
#       On the main jumpbox, as ubuntu: the /pr-watch skill (~/.claude/skills/pr-watch) and a SessionStart hook in
#       ~/.claude/settings.json that has every interactive Claude session keep a /pr-watch job (the heartbeat in
#       claim.sh decides which one works). Only this box runs the watch.
#
# The dev boxes' side of the rule is part of their instructions, which come from ejc3/agents-md (agents-md.tf).
# Idempotent: a re-run replaces what it installed and leaves everything else in settings.json alone.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
mode=""
while [ $# -gt 0 ]; do
  case "$1" in
    --watcher) mode=watcher ;;
    *) echo "usage: $0 --watcher" >&2; exit 2 ;;
  esac
  shift
done

case "$mode" in
watcher)
  mkdir -p ~/.claude/skills/pr-watch ~/.local/share/pr-watch
  install -m 0644 "$here/pr-watch/SKILL.md" ~/.claude/skills/pr-watch/SKILL.md
  install -m 0755 "$here/pr-watch/session-start.sh" ~/.local/share/pr-watch/session-start.sh
  install -m 0755 "$here/pr-watch/claim.sh" ~/.local/share/pr-watch/claim.sh
  install -m 0755 "$here/pr-watch/alert-poll.py" ~/.local/share/pr-watch/alert-poll.py
  install -m 0755 "$here/pr-watch/tf-targets.py" ~/.local/share/pr-watch/tf-targets.py
  # Write through a symlinked settings.json rather than replacing the link.
  settings=$(readlink -f ~/.claude/settings.json 2>/dev/null || echo ~/.claude/settings.json)
  [ -s "$settings" ] || echo '{}' >"$settings"
  tmp=$(mktemp)
  trap 'rm -f "$tmp"' EXIT
  # Drop only our own hook entries (a hook without a command, such as a prompt hook, is kept), then add ours once.
  jq --arg cmd "bash $HOME/.local/share/pr-watch/session-start.sh" '
    .hooks.SessionStart = ([(.hooks.SessionStart // [])[]
        | .hooks = [.hooks[]? | select(((.command // "") | test("pr-watch/session-start")) | not)]
        | select((.hooks | length) > 0)]
      + [{hooks: [{type: "command", command: $cmd, timeout: 10}]}])' "$settings" >"$tmp"
  cat "$tmp" >"$settings"
  echo "installed the pr-watch skill and SessionStart hook for $(id -un)"
  ;;
*) echo "usage: $0 --watcher" >&2; exit 2 ;;
esac

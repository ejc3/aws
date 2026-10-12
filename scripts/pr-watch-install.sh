#!/usr/bin/env bash
# pr-watch-install.sh: install the owner's PR and issue watch (scripts/pr-watch/).
#
#   scripts/pr-watch-install.sh --watcher
#       On the main jumpbox, as ubuntu: the /pr-watch skill (~/.claude/skills/pr-watch) and a SessionStart hook in
#       ~/.claude/settings.json that has exactly one live Claude session schedule it. Only this box runs the watch.
#   scripts/pr-watch-install.sh --dev-rule --ssh HOST [--as USER]
#       On a dev box: the rule in scripts/pr-watch/dev-box-rule.md, as a marked block in USER's ~/.claude/CLAUDE.md
#       (draft while working; the jumpbox's watch reviews and merges). Over SSH from the jumpbox.
#
# Both are idempotent: a re-run replaces what it installed and leaves everything else in the files alone.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
mode="" host="" as=""
while [ $# -gt 0 ]; do
  case "$1" in
    --watcher) mode=watcher ;;
    --dev-rule) mode=dev-rule ;;
    --ssh) host=$2; shift ;;
    --as) as=$2; shift ;;
    *) echo "usage: $0 --watcher | --dev-rule --ssh HOST [--as USER]" >&2; exit 2 ;;
  esac
  shift
done

begin='<!-- BEGIN pr-watch (ejc3/aws scripts/pr-watch-install.sh) -->'
end='<!-- END pr-watch -->'

case "$mode" in
watcher)
  mkdir -p ~/.claude/skills/pr-watch ~/.local/share/pr-watch
  install -m 0644 "$here/pr-watch/SKILL.md" ~/.claude/skills/pr-watch/SKILL.md
  install -m 0755 "$here/pr-watch/session-start.sh" ~/.local/share/pr-watch/session-start.sh
  settings=~/.claude/settings.json
  [ -f "$settings" ] || echo '{}' >"$settings"
  tmp=$(mktemp)
  jq --arg cmd "bash $HOME/.local/share/pr-watch/session-start.sh" '
    .hooks.SessionStart = ([(.hooks.SessionStart // [])[] | select(([.hooks[]?.command] | map(test("pr-watch")) | any) | not)]
      + [{hooks: [{type: "command", command: $cmd, timeout: 10}]}])' "$settings" >"$tmp"
  mv "$tmp" "$settings"
  echo "installed the pr-watch skill and SessionStart hook for $(id -un)"
  ;;
dev-rule)
  [ -n "$host" ] || { echo "--dev-rule needs --ssh HOST" >&2; exit 2; }
  block=$(printf '%s\n%s\n%s\n' "$begin" "$(cat "$here/pr-watch/dev-box-rule.md")" "$end" | base64 -w0)
  run=""; [ -n "$as" ] && run="sudo -u $as -H"
  # The remote side edits the file the account's Claude reads (following a symlink to ~/.codex/AGENTS.md).
  ssh -o BatchMode=yes -i ~/.ssh/fcvm-ec2 "$host" "$run bash -s" <<REMOTE
set -euo pipefail
f=\$(readlink -f ~/.claude/CLAUDE.md 2>/dev/null || echo ~/.claude/CLAUDE.md)
mkdir -p "\$(dirname "\$f")"; touch "\$f"
python3 - "\$f" "$block" <<'PY'
import base64, re, sys
path, block = sys.argv[1], base64.b64decode(sys.argv[2]).decode()
text = open(path).read()
pattern = re.compile(re.escape("$begin") + r".*?" + re.escape("$end") + r"\n?", re.S)
text = pattern.sub("", text).rstrip("\n")
open(path, "w").write((text + "\n\n" if text else "") + block)
PY
echo "installed the pr-watch rule in \$f for \$(id -un) on \$(hostname)"
REMOTE
  ;;
*) echo "usage: $0 --watcher | --dev-rule --ssh HOST [--as USER]" >&2; exit 2 ;;
esac

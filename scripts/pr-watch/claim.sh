#!/usr/bin/env bash
# claim.sh: the PR watch's heartbeat, so exactly one Claude session on the box runs it (scripts/pr-watch/SKILL.md).
#
#   claim.sh claim    at the start of a /pr-watch run, and again between items. Prints "owner" (this session holds the
#                     watch: go on) or "other" (another session ran it in the last 45 minutes: delete this session's
#                     /pr-watch job and stop). Under flock, so sessions racing each other get one owner.
#   claim.sh status   prints "fresh" or "stale" (the SessionStart hook's question: has any session run it lately?).
#
# A session is named by its messaging socket (CLAUDE_CODE_MESSAGING_SOCKET, one per Claude process), falling back to the
# parent process id. Nothing about a session's liveness is guessed: a watch that stops running (its session ended, its
# 7-day job expired, nobody ever prompted it) simply stops refreshing, and after 45 minutes another session takes over.
set -euo pipefail
state="$HOME/.local/state/pr-watch"
mkdir -p "$state"
beat="$state/heartbeat"
stale_after=${PR_WATCH_STALE_SECONDS:-2700}
me=${CLAUDE_CODE_MESSAGING_SOCKET:-ppid-$PPID}
now=$(date +%s)

fresh_owner() { # prints the owner if the heartbeat is fresh
  local t o
  read -r t o <"$beat" 2>/dev/null || return 1
  [[ "$t" =~ ^[0-9]+$ ]] && ((now - t < stale_after)) && printf '%s' "$o"
}

case "${1:-}" in
status)
  if fresh_owner >/dev/null; then echo fresh; else echo stale; fi
  ;;
claim)
  exec 9>"$state/lock"
  flock -w 30 9
  owner=$(fresh_owner || true)
  if [[ -n "$owner" && "$owner" != "$me" ]]; then
    echo other
    exit 0
  fi
  printf '%s %s\n' "$now" "$me" >"$beat.tmp" && mv "$beat.tmp" "$beat"
  echo owner
  ;;
*)
  echo "usage: $0 claim|status" >&2
  exit 2
  ;;
esac

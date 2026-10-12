#!/usr/bin/env bash
# SessionStart hook: have exactly one live Claude session on this host schedule the PR and issue watch
# (ejc3/aws scripts/pr-watch/SKILL.md, installed by scripts/pr-watch-install.sh --watcher).
# survives restarts only if a new session schedules it again; several sessions run in this repository at once, and
# each scheduling its own would review the same pull requests in parallel. The owner is recorded as the Claude process
# id in ~/.local/state/pr-watch/owner; while that process is alive, other sessions are told nothing. Installed on the
# main jumpbox only. Prints context for the model; changes nothing else.
set -uo pipefail
cat >/dev/null

state="$HOME/.local/state/pr-watch"
mkdir -p "$state"
owner_file="$state/owner"
me=$PPID # the claude process that runs this hook

owner=$(cat "$owner_file" 2>/dev/null || true)
if [ -n "$owner" ] && [ "$owner" != "$me" ] && kill -0 "$owner" 2>/dev/null \
  && tr '\0' ' ' <"/proc/$owner/cmdline" 2>/dev/null | grep -q claude; then
  exit 0
fi
printf '%s\n' "$me" >"$owner_file.tmp" && mv "$owner_file.tmp" "$owner_file"

jq -n '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext:
  "This session owns the standing PR and issue watch (ejc3/aws scripts/pr-watch/SKILL.md). Check CronList; if no job runs /pr-watch, create one with CronCreate (cron \"7,37 * * * *\", prompt \"/pr-watch\", recurring) and tell the owner it runs for up to 7 days or until this session ends. Do not create a second one."}}'

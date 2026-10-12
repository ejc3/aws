#!/usr/bin/env bash
# SessionStart hook for the PR and issue watch (ejc3/aws scripts/pr-watch/SKILL.md, installed by
# scripts/pr-watch-install.sh --watcher on the main jumpbox only). A CronCreate job lives only as long as its session,
# so when no session has run the watch for 45 minutes (claim.sh status: stale) this asks the new session to schedule
# it. It records nothing: a session owns the watch only once a /pr-watch run claims the heartbeat, so a session that
# is never prompted (remote control, claude -p) cannot hold it. Prints context for the model; changes nothing.
set -uo pipefail
cat >/dev/null
here=$(cd "$(dirname "$0")" && pwd)
[ "$(bash "$here/claim.sh" status 2>/dev/null)" = stale ] || exit 0
jq -n '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext:
  "The standing PR and issue watch (ejc3/aws scripts/pr-watch/SKILL.md) has not run for 45 minutes. If this is an interactive session: check CronList; if no job runs /pr-watch, create one with CronCreate (cron \"7,37 * * * *\", prompt \"/pr-watch\", recurring), run /pr-watch once now so it claims the watch, and tell the owner it runs for up to 7 days or until this session ends. A run that finds another session owns the watch deletes its own job."}}'

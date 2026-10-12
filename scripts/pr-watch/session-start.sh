#!/usr/bin/env bash
# SessionStart hook for the PR and issue watch (ejc3/aws scripts/pr-watch/SKILL.md, installed by
# scripts/pr-watch-install.sh --watcher on the main jumpbox only). A CronCreate job lives only as long as its session,
# and nothing fires when an owning session ends, so every interactive session keeps a /pr-watch job of its own: the
# one holding the heartbeat (claim.sh) does the work, the others answer "standby" in one line and take over once the
# heartbeat is 45 minutes stale. This hook records nothing (a session that is never prompted claims nothing); it only
# reminds the session to keep its job. Prints context for the model; changes nothing.
set -uo pipefail
cat >/dev/null
jq -n '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext:
  "Standing PR and issue watch (ejc3/aws scripts/pr-watch/SKILL.md). If this is an interactive session: check CronList; if no job runs /pr-watch, create one with CronCreate (cron \"7,37 * * * *\", prompt \"/pr-watch\", recurring). Each run first claims the heartbeat; when another session holds it, the run answers \"standby\" in one line and stops, so a spare job costs one check and takes over if the owner goes away. Do not create a second job in this session."}}'

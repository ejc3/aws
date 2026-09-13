#!/usr/bin/env bash
# Publish a non-secret "is main applied?" status for the dev boxes (see applied-status.tf).
#
#   scripts/publish-applied-status.sh <terraform-plan-exit-code> <plan-log>
#
# Called by .claude/hooks/verify-consistent.sh after its `terraform plan -detailed-exitcode`.
# Publishes only from a clean checkout of origin/main, so the status always describes main.
# Only resource addresses and actions are taken from the plan log, never attribute values.
set -uo pipefail

rc="${1:?terraform plan exit code}"
log="${2:?plan log path}"
param="${APPLIED_STATUS_PARAM:-/aws-infra/applied-status}"
region="${AWS_REGION:-us-west-1}"

root=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
cd "$root" || exit 0
head=$(git rev-parse HEAD 2>/dev/null) || exit 0
main=$(git rev-parse origin/main 2>/dev/null) || exit 0
if [ "$head" != "$main" ] || [ -n "$(git status --porcelain)" ]; then
  echo "applied-status: not publishing; the checkout is not a clean origin/main"
  exit 0
fi

case "$rc" in
  0) plan=clean ;;
  2) plan=pending ;;
  *) plan=error ;;
esac

json=$(python3 - "$log" "$plan" "$main" "$(git log -1 --format=%s "$main")" <<'PY'
import datetime, json, re, sys

log, plan, sha, subject = sys.argv[1:5]
header = re.compile(r"^  # (\S+) (?:\(deposed object \S+\) )?(?:will be|must be) (.+)$")
pending = []
try:
    with open(log, errors="replace") as fh:
        for line in fh:
            match = header.match(line.rstrip("\n"))
            if match:
                pending.append(f"{match.group(1)} {match.group(2)}"[:200])
except OSError:
    pass
doc = {"main": sha, "subject": subject[:120], "plan": plan, "pending_count": len(pending),
       "pending": [],
       "checked_at": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
# A Standard parameter holds 4 KB. Keep the full count, list as many addresses as fit.
for item in pending:
    trial = dict(doc, pending=doc["pending"] + [item])
    if len(json.dumps(trial, separators=(",", ":"))) > 3900:
        break
    doc = trial
print(json.dumps(doc, separators=(",", ":")))
PY
) || exit 0

aws ssm put-parameter --region "$region" --name "$param" --type String --overwrite \
  --value "$json" >/dev/null \
  && echo "applied-status: published plan=$plan for ${main:0:8}"

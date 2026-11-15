#!/usr/bin/env bash
# Run a full origin/main Terraform plan on a jumpbox and publish its non-secret result.
# The caller initializes the checkout first. --if-idle skips instead of waiting for locks.
set -uo pipefail

region=us-west-1
parameter=/infra/applied-status
wait=10m
unset TF_RUN_LOCK_NOWAIT TF_RUN_LOCK_FILE TF_RUN_LOCK_REAL TF_RUN_LOCK_DEFAULT_REAL
if [ "$#" -eq 1 ] && [ "$1" = --if-idle ]; then
  export TF_RUN_LOCK_NOWAIT=1
  wait=0s
elif [ "$#" -ne 0 ]; then
  echo "Usage: $0 [--if-idle]" >&2
  exit 64
fi

skip() { echo "applied-status: $*" >&2; exit 75; }
fail() { echo "applied-status: $*" >&2; exit 1; }

# A credentialed Mac can write the parameter, but it cannot publish through this
# script. Require the real local EC2 identity and the same EC2 session in STS.
# A traced caller would otherwise log the token assignment and the header.
set +x
metadata=http://169.254.169.254/latest
token=$(curl -fsS --noproxy '*' --connect-timeout 1 --max-time 2 -X PUT \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 30' "$metadata/api/token" 2>/dev/null) ||
  skip "not on an EC2 administration jumpbox"
identity=$(printf '%s\n' "X-aws-ec2-metadata-token: $token" |
  curl -fsS --noproxy '*' --connect-timeout 1 --max-time 2 -H @- \
    "$metadata/dynamic/instance-identity/document" 2>/dev/null) ||
  skip "could not verify the local EC2 instance"
unset token
instance=$(jq -er '.instanceId | select(test("^i-[0-9a-f]{8}([0-9a-f]{9})?$"))' <<<"$identity" 2>/dev/null) ||
  skip "invalid local EC2 instance ID"
account=$(jq -er '.accountId | select(test("^[0-9]{12}$"))' <<<"$identity" 2>/dev/null) ||
  skip "invalid local EC2 account ID"
caller=$(aws sts get-caller-identity --region "$region" --query Arn --output text \
  --cli-connect-timeout 5 --cli-read-timeout 10) || fail "could not verify the AWS caller"
[ "$caller" = "arn:aws:sts::$account:assumed-role/jumpbox-admin-role/$instance" ] ||
  skip "requires the local jumpbox-admin-role instance profile"

terraform=$(type -P terraform) || fail "Terraform is not installed"
head -n 2 "$terraform" | grep -q '^[[:blank:]]*# TERRAFORM-RUN-LOCK-WRAPPER ' ||
  fail "Terraform is not using the installed jumpbox run-lock wrapper"

root=$(git rev-parse --show-toplevel) || fail "not in the Terraform checkout"
cd "$root" || fail "could not enter the Terraform checkout"
head_sha=$(git rev-parse HEAD) || fail "could not resolve the checkout commit"
main_exact() {
  # A failed fetch does not establish that an old local ref is still main.
  git fetch -q origin '+refs/heads/main:refs/remotes/origin/main' &&
    [ "$head_sha" = "$(git rev-parse HEAD)" ] &&
    [ "$head_sha" = "$(git rev-parse refs/remotes/origin/main)" ] &&
    [ -z "$(git status --porcelain)" ]
}
main_exact || skip "the checkout could not be verified as clean origin/main"
# The repository workflow initializes a clean worktree with the checked-in backend.
# Do not let ambient command flags turn this into a targeted or no-refresh plan.
unset TF_CLI_ARGS TF_CLI_ARGS_plan TF_DATA_DIR
export TF_WORKSPACE=default AWS_PAGER=""
# An uninitialized hook checkout, or a checkout initialized for another state,
# is not a failed production plan and must not replace the last known status.
if ! jq -e --arg region "$region" '
  .backend | .type == "s3" and .config.bucket == "ejc3-terraform-state"
  and .config.key == "aws-infrastructure/terraform.tfstate"
  and .config.region == $region and .config.dynamodb_table == "ejc3-terraform-locks"
' .terraform/terraform.tfstate >/dev/null 2>&1; then
  skip "initialize this checkout with the checked-in production backend first"
fi

umask 077
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/aws-applied-status"
mkdir -p -- "$state_dir" || fail "could not create the diagnostic directory"
chmod 0700 -- "$state_dir" || fail "could not protect the diagnostic directory"
plan_log=$(mktemp "$state_dir/.plan.XXXXXX") || fail "could not open a private plan log"
diag_tmp=""
trap 'rm -f -- "$plan_log" ${diag_tmp:+"$diag_tmp"}' EXIT
# A direct Terraform invocation or a surviving provider can still use this
# host's resources after the wrapper lock is free. Linux comm truncates the
# provider names to "terraform-provi". The wrapper and backend still lock.
if pgrep -x 'terraform|terraform-provi' >/dev/null; then
  skip "Terraform or a provider is already running on this jumpbox"
fi

# A full plan usually takes 10–15 minutes. Leave time for a 10-minute
# backend-lock wait in explicit mode and for Terraform to unwind cancellation.
timeout --signal=TERM 29m "$terraform" plan -detailed-exitcode -lock=true \
  -lock-timeout="$wait" -input=false -no-color >"$plan_log" 2>&1
rc=$?
if [ "$rc" -eq 75 ]; then
  skip "another Terraform run holds this jumpbox's run lock"
fi
# DynamoDB's conditional Put proves contention even if the previous owner
# released the state lock before Terraform could read out its details.
if [ "$rc" -eq 1 ] &&
   grep -q '^Error: Error acquiring the state lock' "$plan_log" &&
   grep -q 'operation error DynamoDB: PutItem' "$plan_log" &&
   grep -q 'ConditionalCheckFailedException:' "$plan_log"; then
  skip "another Terraform run holds the shared backend lock"
fi

# Retain only the latest run's tail, with the exact commit, result and time. The
# raw run log is local only; neither it nor its diagnostics go into the parameter.
checked_at=$(date -u +'%Y-%m-%dT%H:%M:%SZ')
diagnostic="$state_dir/last-plan.log"
diag_tmp=$(mktemp "$state_dir/.last.XXXXXX") || fail "could not open the local diagnostic"
{
  printf 'main=%s checked_at=%s terraform_exit=%s\n' "$head_sha" "$checked_at" "$rc"
  tail -c 4194304 -- "$plan_log"
} >"$diag_tmp" || fail "could not save the local diagnostic"
mv -f -- "$diag_tmp" "$diagnostic" || fail "could not save the local diagnostic"
echo "applied-status: Terraform exit $rc; local diagnostic: $diagnostic" >&2
main_exact || skip "origin/main moved or the checkout changed during the plan"

case "$rc" in
  0) plan=clean ;;
  2) plan=pending ;;
  *) plan=error ;;
esac

json=$(python3 - "$plan_log" "$plan" "$head_sha" "$(git log -1 --format=%s "$head_sha")" "$checked_at" <<'PY'
import json
import re
import sys

log, plan, sha, subject, checked_at = sys.argv[1:6]
# Only the actionable plan section counts. Drift notes and data reads describe
# observations, not resources Terraform needs to change.
key = r'\[(?:[0-9]+|"(?:\\.|[^"\\])*")\]'
part = rf"[\w-]+(?:{key})?"
address = rf"(?:module\.{part}\.)*(?:(?:data|ephemeral)\.)?[\w-]+\.{part}"
actions = {
    "will be created": "created",
    "will be updated in-place": "updated in-place",
    "must be replaced": "replaced",
    "will be destroyed": "destroyed",
    "will be imported": "imported",
    "will no longer be managed by Terraform, but will not be destroyed": "removed from state",
}
header = re.compile(rf"^  # ({address}) (?:\(deposed object \w+\) )?(.+)$")
pending = []
with open(log, errors="replace") as fh:
    in_plan = False
    for line in fh:
        if line.rstrip() == "Terraform will perform the following actions:":
            in_plan = True
        if in_plan and line.startswith("Plan: "):
            break
        if not in_plan or plan != "pending":
            continue
        match = header.match(line.rstrip("\n"))
        if match and match.group(2) in actions:
            pending.append(f"{match.group(1)} {actions[match.group(2)]}"[:200])

doc = {"main": sha, "subject": subject[:120], "plan": plan, "pending_count": len(pending),
       "pending": [], "checked_at": checked_at}
# A Standard parameter holds 4 KB. Keep the full count, list as many as fit.
for item in pending:
    trial = dict(doc, pending=doc["pending"] + [item])
    if len(json.dumps(trial, separators=(",", ":"))) > 3900:
        break
    doc = trial
print(json.dumps(doc, separators=(",", ":")))
PY
) || fail "could not summarize the plan"

aws ssm put-parameter --region "$region" --name "$parameter" --type String --overwrite \
  --cli-connect-timeout 5 --cli-read-timeout 10 --value "$json" >/dev/null ||
  fail "could not publish the Terraform status"
echo "applied-status: published plan=$plan for ${head_sha:0:8}"
exit "$rc"

#!/bin/bash
# ssm-claude-master-server.sh <instance-id> <region>
#
# Converges the claude-master server box: fetches its bootstrap script from S3 and runs it, through
# SSM Run Command, waits for a final status, prints the output and fails unless it succeeded. Called
# by terraform_data.claude_master_server_converge (claude-master-server.tf).
#
# WHY THIS EXISTS. The instance ignores user_data changes and cloud-init does not run per-instance
# user data again after a resize or a stop/start, so editing the script, bumping the pinned binary
# or recovering a box whose first boot failed changed nothing on the running machine. This is the
# one explicit convergence step. The bootstrap is idempotent and never restarts a running server:
# a new binary is swapped in atomically and a running server keeps the old one until it is
# restarted, which is the owner's decision.
set -euo pipefail
iid=$1 region=$2

# A new or rebuilt box reaches "running" before its SSM agent registers.
for _ in $(seq 1 60); do
  ping=$(aws ssm describe-instance-information --region "$region" \
    --filters "Key=InstanceIds,Values=$iid" --query 'InstanceInformationList[0].PingStatus' --output text)
  [ "$ping" = Online ] && break
  sleep 10
done
if [ "$ping" != Online ]; then
  echo "ssm-claude-master-server: $iid is not registered with SSM after 10 minutes" >&2
  exit 1
fi

# `cloud-init status --wait` first, so this never races the first boot's own run of the script.
remote='cloud-init status --wait >/dev/null 2>&1 || true; swapon --show=NAME --noheadings | grep -q . || { fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile; }; aws s3 cp s3://ejc3-dev-scripts/user-data/claude-master-server.sh /tmp/claude-master-server.sh --region '"$region"' && chmod 755 /tmp/claude-master-server.sh && NEEDRESTART_MODE=a /tmp/claude-master-server.sh; rc=$?; rm -f /tmp/claude-master-server.sh; claude-master-status; exit $rc'

params=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": ["900"]}))' "$remote")
cid=$(aws ssm send-command --region "$region" --instance-ids "$iid" \
  --document-name AWS-RunShellScript --comment "claude-master server bootstrap (claude-master-server.tf)" \
  --parameters "$params" --query Command.CommandId --output text)
echo "ssm-claude-master-server: $iid command $cid"

status=Pending
for _ in $(seq 1 100); do
  sleep 10
  status=$(aws ssm get-command-invocation --region "$region" --command-id "$cid" \
    --instance-id "$iid" --query Status --output text 2>/dev/null || echo Pending)
  case "$status" in Pending|InProgress|Delayed|Cancelling) continue ;; *) break ;; esac
done

aws ssm get-command-invocation --region "$region" --command-id "$cid" --instance-id "$iid" --output json |
  python3 -c 'import json, sys; d = json.load(sys.stdin)
sys.stdout.write(d.get("StandardOutputContent", "")[-3000:])
sys.stderr.write(d.get("StandardErrorContent", "")[-1500:])'
if [ "$status" != Success ]; then
  echo "ssm-claude-master-server: $iid finished $status" >&2
  exit 1
fi

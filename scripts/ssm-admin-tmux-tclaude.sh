#!/bin/bash
# ssm-admin-tmux-tclaude.sh <instance-id> <region>
#
# Runs scripts/admin-tmux-tclaude.sh on <instance-id> as ubuntu through SSM Run Command, waits
# for a final status, prints its output, and fails unless it succeeded. Called by
# terraform_data.admin_tmux_tclaude (tmux-scroll.tf) with TMUX_SCROLL_TAG, TMUX_SCROLL_SHA256
# and TCLAUDE_REF in the environment. None of those is a secret.
set -euo pipefail
iid=$1 region=$2
: "${TMUX_SCROLL_TAG:?}" "${TMUX_SCROLL_SHA256:?}" "${TCLAUDE_REF:?}"
here=$(cd "$(dirname "$0")" && pwd)

# A just-created or rebuilt box reaches "running" before its SSM agent registers. Wait for it.
for _ in $(seq 1 60); do
  ping=$(aws ssm describe-instance-information --region "$region" \
    --filters "Key=InstanceIds,Values=$iid" --query 'InstanceInformationList[0].PingStatus' --output text)
  [ "$ping" = Online ] && break
  sleep 10
done
if [ "$ping" != Online ]; then
  echo "ssm-admin-tmux-tclaude: $iid is not registered with SSM after 10 minutes" >&2
  exit 1
fi

# The installer travels base64-encoded, so no quoting in it can break the SSM command line.
# `cloud-init status --wait` first: on a new box, user_data's shell setup also writes
# ~/.config/t-claude.zsh (from t-claude main), and it must not land after the pinned copy.
payload=$(base64 -w0 "$here/admin-tmux-tclaude.sh")
remote="cloud-init status --wait >/dev/null 2>&1 || true; \
echo $payload | base64 -d > /tmp/admin-tmux-tclaude.sh && chmod 755 /tmp/admin-tmux-tclaude.sh && \
runuser -u ubuntu -- env HOME=/home/ubuntu PATH=/usr/local/bin:/usr/bin:/bin \
TMUX_SCROLL_TAG=$TMUX_SCROLL_TAG TMUX_SCROLL_SHA256=$TMUX_SCROLL_SHA256 TCLAUDE_REF=$TCLAUDE_REF \
bash /tmp/admin-tmux-tclaude.sh; rc=\$?; rm -f /tmp/admin-tmux-tclaude.sh; exit \$rc"

params=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": ["1800"]}))' "$remote")
cid=$(aws ssm send-command --region "$region" --instance-ids "$iid" \
  --document-name AWS-RunShellScript --comment "tmux-scroll + t-claude (tmux-scroll.tf)" \
  --parameters "$params" --query Command.CommandId --output text)
echo "ssm-admin-tmux-tclaude: $iid command $cid"

# Poll to a final status for the whole execution window. The CLI's own waiter gives up after
# about 100 seconds, well inside what the command is allowed to run.
status=Pending
for _ in $(seq 1 190); do
  sleep 10
  status=$(aws ssm get-command-invocation --region "$region" --command-id "$cid" \
    --instance-id "$iid" --query Status --output text 2>/dev/null || echo Pending)
  case "$status" in Pending|InProgress|Delayed|Cancelling) continue ;; *) break ;; esac
done

aws ssm get-command-invocation --region "$region" --command-id "$cid" --instance-id "$iid" --output json |
  python3 -c 'import json, sys; d = json.load(sys.stdin)
sys.stdout.write(d.get("StandardOutputContent", ""))
sys.stderr.write(d.get("StandardErrorContent", ""))'
if [ "$status" != Success ]; then
  echo "ssm-admin-tmux-tclaude: $iid finished $status" >&2
  exit 1
fi

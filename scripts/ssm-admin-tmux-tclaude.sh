#!/bin/bash
# ssm-admin-tmux-tclaude.sh <instance-id> <region>
#
# Runs scripts/admin-tmux-tclaude.sh on <instance-id> as ubuntu through SSM Run Command, waits
# for it, prints its output, and fails unless it succeeded. Called by
# terraform_data.admin_tmux_tclaude (tmux-scroll.tf) with TMUX_SCROLL_TAG, TMUX_SCROLL_SHA256
# and TCLAUDE_REF in the environment. None of those is a secret.
set -euo pipefail
iid=$1 region=$2
: "${TMUX_SCROLL_TAG:?}" "${TMUX_SCROLL_SHA256:?}" "${TCLAUDE_REF:?}"
here=$(cd "$(dirname "$0")" && pwd)

# The installer travels base64-encoded, so no quoting in it can break the SSM command line.
payload=$(base64 -w0 "$here/admin-tmux-tclaude.sh")
remote="echo $payload | base64 -d > /tmp/admin-tmux-tclaude.sh && chmod 755 /tmp/admin-tmux-tclaude.sh && \
runuser -u ubuntu -- env HOME=/home/ubuntu PATH=/usr/local/bin:/usr/bin:/bin \
TMUX_SCROLL_TAG=$TMUX_SCROLL_TAG TMUX_SCROLL_SHA256=$TMUX_SCROLL_SHA256 TCLAUDE_REF=$TCLAUDE_REF \
bash /tmp/admin-tmux-tclaude.sh; rc=\$?; rm -f /tmp/admin-tmux-tclaude.sh; exit \$rc"

params=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": ["600"]}))' "$remote")
cid=$(aws ssm send-command --region "$region" --instance-ids "$iid" \
  --document-name AWS-RunShellScript --comment "tmux-scroll + t-claude (tmux-scroll.tf)" \
  --parameters "$params" --query Command.CommandId --output text)
echo "ssm-admin-tmux-tclaude: $iid command $cid"

# The waiter exits non-zero on a failed command too; the status check below reports why.
aws ssm wait command-executed --region "$region" --command-id "$cid" --instance-id "$iid" || true
read -r status out err < <(aws ssm get-command-invocation --region "$region" --command-id "$cid" \
  --instance-id "$iid" --output json |
  python3 -c 'import json, sys, base64; d = json.load(sys.stdin); print(d["Status"], *(base64.b64encode(d.get(k, "").encode()).decode() or "-" for k in ("StandardOutputContent", "StandardErrorContent")))')
[ "$out" != - ] && echo "$out" | base64 -d
[ "$err" != - ] && echo "$err" | base64 -d >&2
if [ "$status" != Success ]; then
  echo "ssm-admin-tmux-tclaude: $iid finished $status" >&2
  exit 1
fi

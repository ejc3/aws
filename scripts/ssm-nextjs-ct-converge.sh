#!/bin/bash
# ssm-nextjs-ct-converge.sh <instance-id> <region> <serving true|false>
#
# Applies the published nextjs-ct setup on the box NOW, through SSM Run Command, and fails unless the box
# is healthy and serving exactly as asked. Called by terraform_data.nextjs_ct_converge (nextjs-ct.tf),
# which the Elastic IP association and nextjs-dev's instance state wait on, so a cutover moves the
# address and stops the old box only after this box's tunnels are up, and a rollback starts the old box
# only after this box's tunnels are down. Without it the switch would wait for setup-sync's 10-minute
# timer: up to ten minutes of dead URLs on the way in, or two boxes on one tunnel on the way back.
set -euo pipefail
iid=$1 region=$2 want=$3
case "$want" in true|false) ;; *) echo "ssm-nextjs-ct-converge: serving must be true or false" >&2; exit 2 ;; esac

for _ in $(seq 1 60); do
  ping=$(aws ssm describe-instance-information --region "$region" \
    --filters "Key=InstanceIds,Values=$iid" --query 'InstanceInformationList[0].PingStatus' --output text)
  [ "$ping" = Online ] && break
  sleep 10
done
[ "$ping" = Online ] || { echo "ssm-nextjs-ct-converge: $iid is not registered with SSM after 10 minutes" >&2; exit 1; }

# Let a setup that is already running finish, then run setup-sync in the foreground (a no-op when the
# published script was already applied), then check the outcome rather than the exit status alone.
remote='cloud-init status --wait >/dev/null 2>&1 || true
flock -w 3600 /run/nextjs-ct-setup.lock true
systemctl start setup-sync.service || { journalctl -u setup-sync -n 30 --no-pager; exit 1; }
got=$(jq -r .serving /etc/nextjs-ct/config.json)
[ "$got" = "'"$want"'" ] || { echo "serving is $got on the box, wanted '"$want"' (has the new script been published?)"; exit 1; }
/usr/local/sbin/nextjs-ct-health || exit 1
for z in $(jq -r ".zones | keys[]" /etc/nextjs-ct/config.json); do
  if systemctl is-active --quiet "cloudflared@$z.service"; then up=true; else up=false; fi
  [ "$up" = "'"$want"'" ] || { echo "cloudflared@$z active=$up, wanted '"$want"'"; exit 1; }
done
echo "nextjs-ct converged: serving='"$want"'"'

params=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": ["3600"]}))' "$remote")
cid=$(aws ssm send-command --region "$region" --instance-ids "$iid" \
  --document-name AWS-RunShellScript --comment "nextjs-ct converge, serving=$want (nextjs-ct.tf)" \
  --parameters "$params" --query Command.CommandId --output text)
echo "ssm-nextjs-ct-converge: $iid command $cid"

status=Pending
for _ in $(seq 1 380); do
  sleep 10
  status=$(aws ssm get-command-invocation --region "$region" --command-id "$cid" \
    --instance-id "$iid" --query Status --output text 2>/dev/null || echo Pending)
  case "$status" in Pending|InProgress|Delayed|Cancelling) continue ;; *) break ;; esac
done
aws ssm get-command-invocation --region "$region" --command-id "$cid" --instance-id "$iid" --output json |
  python3 -c 'import json, sys; d = json.load(sys.stdin)
sys.stdout.write(d.get("StandardOutputContent", "")[-3000:])
sys.stderr.write(d.get("StandardErrorContent", "")[-1500:])'
[ "$status" = Success ] || { echo "ssm-nextjs-ct-converge: $iid finished $status" >&2; exit 1; }

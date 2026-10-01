#!/bin/bash
# ssm-agent-session-sync.sh <instance-id> <region>
#
# Installs the new-repo watcher (scripts/agent-session-sync.py, its template unit, and the Codex seeder
# scripts/codex-seed-thread.py it calls) on a RUNNING admin
# box through SSM Run Command, enables agent-session-sync@ubuntu and starts it if it is not running.
# Called by terraform_data.admin_agent_session_sync (agent-session-sync.tf) with ASS_ARGS in the
# environment (the ubuntu account's policy; none of it is a secret).
#
# Why a step and not user_data: the jumpboxes ignore user_data changes and take no downloaded script as
# root, so a running one converges only through an explicit, reviewed step like this. The files travel
# base64-encoded from this repository, so nothing is fetched on the box. A running watcher is restarted only
# when its unit or policy changed; a new script is picked up by the watcher itself (it re-executes).
set -euo pipefail
iid=$1 region=$2
: "${ASS_ARGS:?}"
here=$(cd "$(dirname "$0")" && pwd)

for _ in $(seq 1 60); do
  ping=$(aws ssm describe-instance-information --region "$region" \
    --filters "Key=InstanceIds,Values=$iid" --query 'InstanceInformationList[0].PingStatus' --output text)
  [ "$ping" = Online ] && break
  sleep 10
done
[ "$ping" = Online ] || { echo "ssm-agent-session-sync: $iid is not registered with SSM after 10 minutes" >&2; exit 1; }

script=$(base64 -w0 "$here/agent-session-sync.py")
seeder=$(base64 -w0 "$here/codex-seed-thread.py")
unit=$(base64 -w0 "$here/agent-session-sync@.service")
# Files are replaced ATOMICALLY (a half-written script must never be what a running watcher re-executes), and
# only when they differ. A changed SCRIPT needs no restart: the watcher re-executes itself when its own file
# is replaced. A changed UNIT or POLICY (the environment of a running process) does, so the watcher is
# restarted then and only then: harmless to sessions (KillMode=process, state on disk), and a re-run with
# nothing new leaves it untouched.
read -r -d '' remote <<REMOTE || true
set -e
t=\$(mktemp -d); trap 'rm -rf "\$t"' EXIT
echo $script | base64 -d > "\$t/script"
echo $seeder | base64 -d > "\$t/seeder"
echo $unit | base64 -d > "\$t/unit"
printf '[Service]\nEnvironment="ASS_ARGS=%s"\n' '$ASS_ARGS' > "\$t/policy"
restart=0
put() { cmp -s "\$1" "\$2" && return 1; install -m "\$3" "\$1" "\$2.new" && mv -f "\$2.new" "\$2"; }
put "\$t/script" /usr/local/bin/agent-session-sync 755 || true
put "\$t/seeder" /usr/local/bin/codex-seed-thread 755 || true      # the watcher seeds Codex threads through it
put "\$t/unit" /etc/systemd/system/agent-session-sync@.service 644 && restart=1
install -d /etc/systemd/system/agent-session-sync@ubuntu.service.d
put "\$t/policy" /etc/systemd/system/agent-session-sync@ubuntu.service.d/policy.conf 644 && restart=1
systemctl daemon-reload
systemctl enable agent-session-sync@ubuntu.service >/dev/null 2>&1
if systemctl is-active --quiet agent-session-sync@ubuntu.service; then
  [ "\$restart" = 1 ] && systemctl restart agent-session-sync@ubuntu.service
else
  systemctl start agent-session-sync@ubuntu.service
fi
sleep 3
systemctl is-active agent-session-sync@ubuntu.service
journalctl -u agent-session-sync@ubuntu.service --no-pager -n 3 | cut -c1-200
REMOTE

params=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": ["300"]}))' "$remote")
cid=$(aws ssm send-command --region "$region" --instance-ids "$iid" --document-name AWS-RunShellScript \
  --comment "agent-session-sync (agent-session-sync.tf)" --parameters "$params" --query Command.CommandId --output text)
echo "ssm-agent-session-sync: $iid command $cid"

status=Pending
for _ in $(seq 1 60); do
  sleep 5
  status=$(aws ssm get-command-invocation --region "$region" --command-id "$cid" --instance-id "$iid" --query Status --output text 2>/dev/null || echo Pending)
  case "$status" in Pending|InProgress|Delayed|Cancelling) continue ;; *) break ;; esac
done
aws ssm get-command-invocation --region "$region" --command-id "$cid" --instance-id "$iid" --output json |
  python3 -c 'import json, sys; d = json.load(sys.stdin)
sys.stdout.write(d.get("StandardOutputContent", "")[-1500:])
sys.stderr.write(d.get("StandardErrorContent", "")[-1500:])'
[ "$status" = Success ] || { echo "ssm-agent-session-sync: $iid finished $status" >&2; exit 1; }

#!/bin/bash
# ssm-terraform-run-lock.sh <instance-id> <region>
#
# Installs the one-run-at-a-time terraform wrapper (scripts/terraform-run-lock.sh) on a RUNNING admin box through SSM
# Run Command: /usr/local/sbin/terraform (ahead of the real /usr/local/bin/terraform on every PATH) and its lock file
# /var/lib/terraform-run-lock/lock. Called by terraform_data.admin_terraform_run_lock (terraform-run-lock.tf).
#
# Why a step and not user_data: the jumpboxes ignore user_data changes and run no downloaded script as root, so a
# running one converges only through an explicit, reviewed step like this (the same shape as ssm-agent-session-sync.sh).
# The wrapper travels base64-encoded from this repository and replaces the old one atomically, so a Terraform run that
# is already going keeps the file it started with. Restarts nothing.
set -euo pipefail
iid=$1 region=$2
here=$(cd "$(dirname "$0")" && pwd)

for _ in $(seq 1 60); do
  ping=$(aws ssm describe-instance-information --region "$region" \
    --filters "Key=InstanceIds,Values=$iid" --query 'InstanceInformationList[0].PingStatus' --output text)
  [ "$ping" = Online ] && break
  sleep 10
done
[ "$ping" = Online ] || { echo "ssm-terraform-run-lock: $iid is not registered with SSM after 10 minutes" >&2; exit 1; }

wrapper=$(base64 -w0 "$here/terraform-run-lock.sh")
read -r -d '' remote <<REMOTE || true
set -e
t=\$(mktemp -d); trap 'rm -rf "\$t"' EXIT
echo $wrapper | base64 -d > "\$t/terraform"
bash -n "\$t/terraform"
# The lock file exists before anyone needs it, owned by root and writable by every account, so opening it never has
# to create a file (a world-writable sticky directory would refuse that across accounts).
install -d -m 0755 /var/lib/terraform-run-lock
[ -e /var/lib/terraform-run-lock/lock ] || install -m 0666 /dev/null /var/lib/terraform-run-lock/lock
chmod 0666 /var/lib/terraform-run-lock/lock
if ! cmp -s "\$t/terraform" /usr/local/sbin/terraform; then
  install -m 0755 "\$t/terraform" /usr/local/sbin/terraform.new && mv -f /usr/local/sbin/terraform.new /usr/local/sbin/terraform
  echo "terraform-run-lock: installed /usr/local/sbin/terraform"
fi
echo "ubuntu resolves terraform to: \$(runuser -u ubuntu -- bash -lc 'command -v terraform')"
/usr/local/sbin/terraform version | head -1
REMOTE

params=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": ["120"]}))' "$remote")
cid=$(aws ssm send-command --region "$region" --instance-ids "$iid" --document-name AWS-RunShellScript \
  --comment "terraform-run-lock (terraform-run-lock.tf)" --parameters "$params" --query Command.CommandId --output text)
echo "ssm-terraform-run-lock: $iid command $cid"

status=Pending
for _ in $(seq 1 40); do
  sleep 3
  status=$(aws ssm get-command-invocation --region "$region" --command-id "$cid" --instance-id "$iid" --query Status --output text 2>/dev/null || echo Pending)
  case "$status" in Pending|InProgress|Delayed|Cancelling) continue ;; *) break ;; esac
done
aws ssm get-command-invocation --region "$region" --command-id "$cid" --instance-id "$iid" --output json |
  python3 -c 'import json, sys; d = json.load(sys.stdin)
sys.stdout.write(d.get("StandardOutputContent", "")[-1500:])
sys.stderr.write(d.get("StandardErrorContent", "")[-1500:])'
[ "$status" = Success ] || { echo "ssm-terraform-run-lock: $iid finished $status" >&2; exit 1; }

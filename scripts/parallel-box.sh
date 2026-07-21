#!/usr/bin/env bash
#
# Bring the on-demand 192-core Graviton box up and down.
#
# The box is a spot instance (~$3.14/hr) that only exists while you need it. Its 100GB
# work volume is separate and persistent -- it survives every down/up cycle, and
# terraform is configured with prevent_destroy so it cannot be deleted by accident.
#
#   parallel-box.sh up       launch it (takes ~2 min)
#   parallel-box.sh down     terminate it. The work volume is KEPT.
#   parallel-box.sh status   is it running, what does it cost, what is on the volume
#   parallel-box.sh ssh      connect
#
# All changes go through terraform, never the AWS CLI, so state never drifts.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY="${HOME}/.ssh/fcvm-ec2"
REGION="us-west-1"
JUMPBOX="10.0.1.72"

# This box may not be the one that can launch instances. The dev servers deliberately
# hold a restricted IAM role with no ec2:RunInstances, and terraform state + credentials
# live on the jumpbox. So when we are not on the jumpbox, delegate the terraform half
# over SSH rather than widening dev-box permissions.
on_jumpbox() { [ -d "$REPO/.terraform" ] && command -v terraform >/dev/null 2>&1; }

delegate() {
  ssh -i "$KEY" -o ConnectTimeout=10 -o StrictHostKeyChecking=no "ubuntu@$JUMPBOX" \
    "cd ~/aws && ./scripts/parallel-box.sh $*"
}

cd "$REPO" 2>/dev/null || true

box_ip() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=parallel-box" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null | grep -v '^None$' || true
}

if ! on_jumpbox; then
  case "${1:-status}" in
    up)
      delegate up || exit 1
      IP="$(delegate ip)"
      [ -n "$IP" ] || { echo "could not determine box IP" >&2; exit 1; }
      echo "Connecting to $IP ..."
      exec ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP"
      ;;
    ssh)
      IP="$(delegate ip)"
      [ -n "$IP" ] || { echo "Box is down. Run: $0 up" >&2; exit 1; }
      exec ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP"
      ;;
    *)
      exec_out=$(delegate "$@") ; echo "$exec_out"
      exit 0
      ;;
  esac
fi

case "${1:-status}" in
  ip)
    box_ip
    ;;

  up)
    echo "Launching the 192-core box (spot, ~\$3.14/hr)..."
    # -target is deliberate: bring up ONLY this box, so an unrelated pending change
    # elsewhere in the config cannot ride along with a routine 'up'.
    terraform apply -auto-approve \
      -var enable_parallel_box=true \
      -target=aws_instance.parallel_box \
      -target=aws_volume_attachment.parallel_work

    echo "Waiting for SSH..."
    for _ in $(seq 1 60); do
      IP="$(box_ip)"
      if [ -n "$IP" ] && ssh -i "$KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
           -o BatchMode=yes "ubuntu@$IP" true 2>/dev/null; then
        echo "Ready: ssh -i $KEY ubuntu@$IP"
        ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP" \
          'echo "  cores: $(nproc)"; echo "  work:  $(df -h /mnt/work 2>/dev/null | awk "NR==2{print \$2\" total, \"\$4\" free\"}")"' 2>/dev/null || true
        exit 0
      fi
      sleep 10
    done
    echo "Instance launched but SSH did not come up in 10 min; check: $0 status" >&2
    exit 1
    ;;

  down)
    # enable_parallel_box=false removes the instance and the attachment. The volume has
    # prevent_destroy and is not part of the count, so it is never considered here.
    echo "Terminating the box (the 100GB work volume is kept)..."
    terraform apply -auto-approve -var enable_parallel_box=false
    echo "Down. Volume $(terraform output -raw parallel_box_work_volume 2>/dev/null) retained."
    ;;

  status)
    IP="$(box_ip)"
    if [ -n "$IP" ]; then
      echo "  state:  RUNNING at $IP  (~\$3.14/hr)"
      ssh -i "$KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
        "ubuntu@$IP" 'echo "  cores:  $(nproc)"; echo "  load:   $(uptime | sed "s/.*load average: //")"; echo "  work:   $(df -h /mnt/work | awk "NR==2{print \$3\" used of \"\$2}")"' 2>/dev/null \
        || echo "  (running, but SSH not answering yet)"
    else
      echo "  state:  down (\$0 compute)"
    fi
    VOL=$(aws ec2 describe-volumes --region "$REGION" \
      --filters "Name=tag:Name,Values=parallel-box-work" \
      --query 'Volumes[0].[VolumeId,Size,State]' --output text 2>/dev/null)
    echo "  volume: $VOL (persistent, prevent_destroy)"
    ;;

  ssh)
    IP="$(box_ip)"
    [ -n "$IP" ] || { echo "Box is down. Run: $0 up" >&2; exit 1; }
    exec ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP"
    ;;

  *)
    sed -n '3,20p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit 1
    ;;
esac

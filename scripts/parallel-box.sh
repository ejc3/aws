#!/usr/bin/env bash
#
# Bring the on-demand many-core Graviton box up and down.
#
#   parallel-box.sh up       launch it (prints progress; tries several instance types)
#   parallel-box.sh down     terminate it. The work volume is KEPT.
#   parallel-box.sh status   running? cost? disk?
#   parallel-box.sh ssh      connect
#   parallel-box.sh ip       print the IP (used by the pbox wrapper)
#
# CAPACITY IS THE HARD PART. A 192-core spot request pinned to one AZ and one instance
# type scores 1/10 for fulfilment; allowing several instance types raises that
# materially. The persistent volume is AZ-locked, so we cannot roam AZs without
# snapshotting -- but we CAN try every Graviton family in the volume's AZ, which is what
# the list below does. Each attempt is reported, so a failure is visible rather than a
# silent five-minute hang.
#
# All changes go through terraform, never the AWS CLI, so state never drifts.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY="${HOME}/.ssh/fcvm-ec2"
REGION="us-west-1"
JUMPBOX="10.0.1.72"

# Candidate pools, all offered in us-west-1a and all Graviton4. The floor is "better
# than the dev box" (c7gd.metal, 64 cores / 128GB Graviton3), so nothing here is under
# 96 cores.
#
# Order matters:
#   1. 192-core virtualized, cheapest family first (c8g < c8gn < m8g < r8g < r8gd)
#   2. 96-core virtualized -- half the cores but a much likelier pool
#   3. .metal LAST: metal is a genuinely separate capacity pool so it adds real odds,
#      but bare metal takes many minutes to boot, which defeats fast startup. Only
#      reached when nothing virtualized is available.
TYPES="${PARALLEL_BOX_TYPES:-c8g.48xlarge c8gn.48xlarge m8g.48xlarge r8g.48xlarge r8gd.48xlarge c8g.24xlarge c8gn.24xlarge m8g.24xlarge r8g.24xlarge r8gd.24xlarge c8g.metal-48xl m8g.metal-48xl r8g.metal-48xl}"

# The dev servers hold a restricted IAM role with no ec2:RunInstances, and terraform
# state lives on the jumpbox, so delegate the terraform half rather than widening
# dev-box permissions.
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

say() { printf '%s\n' "$*" >&2; }

if ! on_jumpbox; then
  case "${1:-status}" in
    up)
      delegate up || exit 1
      IP="$(delegate ip)"
      [ -n "$IP" ] || { say "could not determine box IP"; exit 1; }
      say "Connecting to $IP ..."
      exec ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP"
      ;;
    ssh)
      IP="$(delegate ip)"
      [ -n "$IP" ] || { say "Box is down. Run: pbox up"; exit 1; }
      exec ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP"
      ;;
    *) delegate "$@"; exit $? ;;
  esac
fi

case "${1:-status}" in
  ip) box_ip ;;

  up)
    IP="$(box_ip)"
    if [ -n "$IP" ]; then say "Already running at $IP"; exit 0; fi

    AZ=$(aws ec2 describe-volumes --region "$REGION" \
      --filters "Name=tag:Name,Values=parallel-box-work" \
      --query 'Volumes[0].AvailabilityZone' --output text 2>/dev/null)
    say "Work volume is in $AZ -- the box must launch there (EBS is AZ-locked)."
    say "Spot capacity for 192-core instances is scarce; trying each type in turn."
    say ""

    ok=0
    for T in $TYPES; do
      CORES=$(aws ec2 describe-instance-types --region "$REGION" --instance-types "$T" \
        --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null)
      PRICE=$(aws ec2 describe-spot-price-history --region "$REGION" --instance-types "$T" \
        --product-descriptions "Linux/UNIX" --availability-zone "$AZ" --max-items 1 \
        --query 'SpotPriceHistory[0].SpotPrice' --output text 2>/dev/null)
      say "--> trying $T (${CORES:-?} cores, \$${PRICE:-?}/hr) in $AZ ..."

      # Stream terraform's own output so a slow step is visible as it happens.
      if terraform apply -auto-approve -no-color \
           -var enable_parallel_box=true \
           -var "parallel_box_type=$T" \
           -target=aws_instance.parallel_box \
           -target=aws_volume_attachment.parallel_work 2>&1 \
         | sed -u 's/^/    /' ; then
        # terraform's exit status, not sed's
        if [ "${PIPESTATUS[0]}" -eq 0 ]; then ok=1; CHOSEN="$T"; break; fi
      fi

      say "    no capacity for $T -- trying the next type"
      say ""
    done

    if [ "$ok" -ne 1 ]; then
      say ""
      say "FAILED: no spot capacity in $AZ for any of: $TYPES"
      say "Options:"
      say "  - retry later; spot capacity fluctuates hour to hour"
      say "  - override the list:  PARALLEL_BOX_TYPES='c8g.16xlarge' $0 up"
      say "  - move the volume to another AZ via snapshot (ask Claude for the roaming setup)"
      exit 1
    fi

    say ""
    say "Launched $CHOSEN. Waiting for SSH and the work disk to mount..."
    for i in $(seq 1 60); do
      IP="$(box_ip)"
      if [ -n "$IP" ] && ssh -i "$KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
           -o BatchMode=yes "ubuntu@$IP" true 2>/dev/null; then
        say "Ready at $IP"
        ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP" \
          'echo "  cores: $(nproc)"; echo "  work:  $(df -h /mnt/work 2>/dev/null | awk "NR==2{print \$2\" total, \"\$4\" free\"}" || echo "not mounted yet")"' 2>/dev/null || true
        exit 0
      fi
      [ $((i % 6)) -eq 0 ] && say "    still booting (${i}0s)..."
      sleep 10
    done
    say "Launched but SSH did not come up in 10 min; check: $0 status"
    exit 1
    ;;

  down)
    say "Terminating the box (the 100GB work volume is kept)..."
    terraform apply -auto-approve -no-color -var enable_parallel_box=false 2>&1 | sed -u 's/^/    /'
    say "Down. Volume retained: $(aws ec2 describe-volumes --region "$REGION" \
      --filters "Name=tag:Name,Values=parallel-box-work" --query 'Volumes[0].VolumeId' --output text 2>/dev/null)"
    ;;

  status)
    IP="$(box_ip)"
    if [ -n "$IP" ]; then
      T=$(aws ec2 describe-instances --region "$REGION" \
        --filters "Name=tag:Name,Values=parallel-box" "Name=instance-state-name,Values=running" \
        --query 'Reservations[0].Instances[0].InstanceType' --output text 2>/dev/null)
      echo "  state:  RUNNING at $IP ($T)"
      ssh -i "$KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
        "ubuntu@$IP" 'echo "  cores:  $(nproc)"; echo "  load:   $(uptime | sed "s/.*load average: //")"; echo "  work:   $(df -h /mnt/work | awk "NR==2{print \$3\" used of \"\$2}")"' 2>/dev/null \
        || echo "  (running, but SSH not answering yet)"
    else
      echo "  state:  down (\$0 compute)"
    fi
    echo "  volume: $(aws ec2 describe-volumes --region "$REGION" \
      --filters "Name=tag:Name,Values=parallel-box-work" \
      --query 'Volumes[0].[VolumeId,Size,State]' --output text 2>/dev/null) (persistent)"
    ;;

  ssh)
    IP="$(box_ip)"
    [ -n "$IP" ] || { say "Box is down. Run: $0 up"; exit 1; }
    exec ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP"
    ;;

  *)
    sed -n '3,12p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit 1
    ;;
esac

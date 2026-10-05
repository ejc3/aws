#!/usr/bin/env bash
#
# Bring the on-demand many-core Graviton boxes up and down. There are TWO independent
# boxes (parallel-box.tf, parallel-box2.tf), each with its own persistent work volume,
# so two parallel jobs can run at once.
#
#   pbox up [2]       launch box 1 (or 2); tries several instance types
#   pbox up [2] kvm   same box, METAL pools only: /dev/kvm for hypervisor
#                     workloads (fcvm/firecracker). Boots in many minutes.
#   pbox down [2]     terminate it. Its work volume is KEPT.
#   pbox status       both boxes: running? cost? disk?
#   pbox status 2     just box 2
#   pbox ssh [2]      connect
#   pbox ip [2]       print the IP
#
# CAPACITY IS THE HARD PART. A 192-core spot request pinned to one AZ and one instance
# type scores 1/10 for fulfilment; allowing several instance types raises that
# materially. The persistent volumes are AZ-locked, so we cannot roam AZs without
# snapshotting -- but we CAN try every Graviton family in the volume's AZ, which is what
# the list below does. Each attempt is reported, so a failure is visible rather than a
# silent five-minute hang.
#
# THIS SCRIPT DOES NOT RUN TERRAFORM, AND DOES NOT TOUCH THE JUMPBOX.
#
# It used to do both: a dev box SSHed to the jumpbox with a forced-command key and the
# jumpbox ran `terraform apply -target=...`, because state and the admin role live there.
# That gave every dev box a live credential aimed at the jumpbox -- the trust direction
# dev-hop-key.tf exists to prevent, on machines running agents with
# --dangerously-skip-permissions. Dev box -> jumpbox connections are not permitted.
#
# So terraform now owns only what is durable (work volume, SG, key pair, and the LAUNCH
# TEMPLATE that carries the whole launch configuration), and this script supplies the one
# thing terraform cannot know in advance -- which instance type has capacity right now --
# via ec2:RunInstances against that template. The IAM policy behind it
# (parallel-box-launch.tf) allows launching ONLY these two tagged boxes, and passing only
# the dev-ebs-only role. Terminating was already non-terraform: parallel-box-watchdog.tf
# has always reaped these boxes with tag-scoped ec2:TerminateInstances.
set -uo pipefail


# Candidate pools, all Graviton (incl. gen-5 c9g/m9g). The floor is "better
# than the dev box" (c7gd.metal, 64 cores / 128GB Graviton3), so nothing here is under
# 96 cores.
#
# Order matters:
#   1. 192-core virtualized, cheapest family first (c8g < c8gn < m8g < r8g < r8gd)
#   2. 96-core virtualized -- half the cores but a much likelier pool
# Metal is excluded from the DEFAULT list: it boots in many minutes, which defeats fast
# startup, and there are enough virtualized pools that we should never need it.
# KVM MODE ("up N kvm") is the deliberate exception: virtualized Graviton has no
# /dev/kvm (AWS exposes KVM only on *.metal ARM instances), so KVM workloads -- fcvm,
# firecracker, anything that IS a hypervisor -- get a metal-only pool list and accept
# the slow boot as the price of admission. Same ordering rules, same >=96-core floor
# (metal-24xl = 96 cores); all offered where the boxes live (a type a region lacks is skipped as unsupported).
TYPES="${PARALLEL_BOX_TYPES:-c8g.48xlarge c8gb.48xlarge c8gd.48xlarge c8gn.48xlarge c9g.48xlarge c9gd.48xlarge m8g.48xlarge m8gd.48xlarge m9g.48xlarge m9gd.48xlarge i8g.48xlarge i8ge.48xlarge r8g.48xlarge r8gd.48xlarge c8g.24xlarge c8gb.24xlarge c8gd.24xlarge c8gn.24xlarge c9g.24xlarge c9gd.24xlarge m8g.24xlarge m8gd.24xlarge m9g.24xlarge m9gd.24xlarge i8g.24xlarge i8ge.24xlarge r8g.24xlarge r8gd.24xlarge}"
# Deliberately a SEPARATE override var: reusing PARALLEL_BOX_TYPES here would let a
# virtualized-list override silently poison kvm mode with non-metal types, which
# would burn the full 25-min wait and then fail the /dev/kvm check.
KVM_TYPES="${PARALLEL_BOX_KVM_TYPES:-c8g.metal-48xl c8gd.metal-48xl c9g.metal-48xl c9gd.metal-48xl m8g.metal-48xl m8gd.metal-48xl c8g.metal-24xl c8gd.metal-24xl m8g.metal-24xl m8gd.metal-24xl}"

# Which key reaches the box, by what the caller actually holds:
#   - a dev box holds dev_hop and nothing else that reaches another host (dev-hop-key.tf);
#     the launch template's user_data authorizes dev_hop for exactly this reason
#   - a jumpbox holds fcvm-ec2, whose public half AWS injects via the key pair on the
#     instance (aws_key_pair.parallel_box)
# Prefer dev_hop when present so a dev box never depends on the admin key existing.
if [ -f "$HOME/.ssh/dev_hop" ]; then
  KEY="$HOME/.ssh/dev_hop"
else
  KEY="$HOME/.ssh/fcvm-ec2"
fi

SELF="$(basename "${BASH_SOURCE[0]}")"
say() { printf '%s\n' "$*" >&2; }

# Which region the boxes live in. Terraform publishes it (ohio-pbox.tf, /infra/parallel-box, in us-west-1), so moving them is
# one apply and never a script edit on every dev box. PARALLEL_BOX_REGION overrides it for a one-off.
REGION="${PARALLEL_BOX_REGION:-}"
if [ -z "$REGION" ]; then
  REGION="$(aws ssm get-parameter --region us-west-1 --name /infra/parallel-box --query Parameter.Value --output text 2>/dev/null \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["region"])' 2>/dev/null)"
fi
if [ -z "$REGION" ]; then
  say "FATAL: cannot read which region the parallel boxes are in (SSM /infra/parallel-box in us-west-1)."
  say "Retry, or name it yourself:  PARALLEL_BOX_REGION=us-east-2 $SELF $*"
  exit 1
fi

# ---------------------------------------------------------------------------------
# Box selection. Box 1 keeps the original, unnumbered names (tag "parallel-box",
# launch template "parallel-box") so nothing existing moves; box 2 is suffixed.
# ---------------------------------------------------------------------------------
CMD="${1:-status}"
BOX="${2:-}"
MODE="${3:-}"

# "up kvm" is shorthand for box 1 in kvm mode.
if [ "$BOX" = "kvm" ]; then MODE="kvm"; BOX=1; fi
case "$BOX" in ""|1|2) ;; *) say "unknown box '$BOX' (use 1 or 2)"; exit 1 ;; esac
case "$MODE" in ""|kvm) ;; *) say "unknown mode '$MODE' (only 'kvm')"; exit 1 ;; esac
[ "$MODE" = "kvm" ] && TYPES="$KVM_TYPES"

set_box() {
  if [ "$1" = "2" ]; then
    NAME="parallel-box-2" VOLTAG="parallel-box-2-work"
  else
    NAME="parallel-box" VOLTAG="parallel-box-work"
  fi
  LT="$NAME" # launch template name matches the box name (parallel-box-launch.tf)
}
set_box "${BOX:-1}"

box_ip() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null | grep -v '^None$' || true
}

box_id() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text 2>/dev/null | grep -v '^None$' || true
}

work_volume() {
  aws ec2 describe-volumes --region "$REGION" \
    --filters "Name=tag:Name,Values=$VOLTAG" \
    --query 'Volumes[0].VolumeId' --output text 2>/dev/null | grep -v '^None$' || true
}

status_one() {
  set_box "$1"
  echo "parallel-box $1:"
  IP="$(box_ip)"
  if [ -n "$IP" ]; then
    T=$(aws ec2 describe-instances --region "$REGION" \
      --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
      --query 'Reservations[0].Instances[0].InstanceType' --output text 2>/dev/null)
    echo "  state:  RUNNING at $IP ($T)"
    ssh -i "$KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no -o BatchMode=yes \
      "ubuntu@$IP" 'echo "  cores:  $(nproc)"; echo "  load:   $(uptime | sed "s/.*load average: //")"; echo "  work:   $(df -h /mnt/work | awk "NR==2{print \$3\" used of \"\$2}")"' 2>/dev/null \
      || echo "  (running, but SSH not answering yet)"
  else
    echo "  state:  down (\$0 compute)"
  fi
  echo "  volume: $(aws ec2 describe-volumes --region "$REGION" \
    --filters "Name=tag:Name,Values=$VOLTAG" \
    --query 'Volumes[0].[VolumeId,Size,State]' --output text 2>/dev/null) (persistent)"
}

case "$CMD" in
  ip) box_ip ;;

  up)
    IP="$(box_ip)"
    if [ -n "$IP" ]; then say "Already running at $IP"; exit 0; fi

    VOL="$(work_volume)"
    [ -n "$VOL" ] || { say "FATAL: no work volume tagged $VOLTAG in $REGION"; exit 1; }
    AZ=$(aws ec2 describe-volumes --region "$REGION" --volume-ids "$VOL" \
      --query 'Volumes[0].AvailabilityZone' --output text 2>/dev/null)
    say "Work volume $VOL is in $AZ -- the box launches there (EBS is AZ-locked)."
    say "Spot capacity for 192-core instances is scarce; trying each type in turn."
    say ""

    ID=""
    for T in $TYPES; do
      CORES=$(aws ec2 describe-instance-types --region "$REGION" --instance-types "$T" \
        --query 'InstanceTypes[0].VCpuInfo.DefaultVCpus' --output text 2>/dev/null)
      # head -1: --max-items makes the CLI emit a pagination token on a SECOND line, which
      # otherwise lands in the price and prints as "$2.219800\nNone/hr".
      PRICE=$(aws ec2 describe-spot-price-history --region "$REGION" --instance-types "$T" \
        --product-descriptions "Linux/UNIX" --availability-zone "$AZ" --max-items 1 \
        --query 'SpotPriceHistory[0].SpotPrice' --output text 2>/dev/null | head -1)
      say "--> trying $T (${CORES:-?} cores, \$${PRICE:-?}/hr) in $AZ ..."

      # Everything except the instance type comes from the launch template, so a typo
      # here cannot land the box in the wrong subnet, on the wrong AMI, or without the
      # Name tag its IAM policy and its idle watchdog both depend on.
      OUT=$(aws ec2 run-instances --region "$REGION" \
        --launch-template "LaunchTemplateName=$LT,Version=\$Latest" \
        --instance-type "$T" --count 1 \
        --query 'Instances[0].InstanceId' --output text 2>&1)
      RC=$?

      if [ "$RC" -eq 0 ] && [ -n "$OUT" ] && [ "${OUT#i-}" != "$OUT" ]; then
        ID="$OUT"; CHOSEN="$T"; break
      fi

      # Capacity is the expected failure and is worth distinguishing from a real error
      # (a bad policy, a deleted template) -- otherwise a permissions bug looks exactly
      # like a busy afternoon and gets retried 28 times.
      case "$OUT" in
        *InsufficientInstanceCapacity*|*InsufficientHostCapacity*|*Unsupported*|*capacity-not-available*)
          say "    no capacity for $T -- trying the next type" ;;
        *)
          say "    launch failed for $T:"
          printf '%s\n' "$OUT" | sed 's/^/      /' >&2 ;;
      esac
      say ""
    done

    if [ -z "$ID" ]; then
      say ""
      say "FAILED: no spot capacity in $AZ for any of: $TYPES"
      say "Options:"
      say "  - retry later; spot capacity fluctuates hour to hour"
      say "  - override the list:  PARALLEL_BOX_TYPES='c8g.16xlarge' $SELF up $BOX"
      say "  - move the volume to another AZ via snapshot (ask Claude for the roaming setup)"
      exit 1
    fi

    say ""
    say "Launched $CHOSEN as $ID. Attaching the work volume..."

    # The volume must be attached AFTER the instance exists -- a launch template cannot
    # carry a pre-existing volume -- and EC2 rejects an attach while the instance is
    # still pending. The boot-time mount loop waits for the device, so it is racing this
    # attach and wins as long as we do it promptly.
    aws ec2 wait instance-running --region "$REGION" --instance-ids "$ID" 2>/dev/null
    if ! aws ec2 attach-volume --region "$REGION" --volume-id "$VOL" \
           --instance-id "$ID" --device /dev/sdf >/dev/null 2>&1; then
      say "FAILED to attach $VOL to $ID. The box is up but has no /mnt/work."
      say "Check: aws ec2 describe-volumes --region $REGION --volume-ids $VOL"
      exit 1
    fi

    say "Waiting for SSH and the work disk to mount..."
    # Metal firmware init takes many minutes on top of the normal boot -- give kvm
    # mode 25 min before declaring failure (virtualized boxes keep the 10 min bound).
    WAIT_TRIES=60
    [ "$MODE" = "kvm" ] && WAIT_TRIES=150
    for i in $(seq 1 "$WAIT_TRIES"); do
      IP="$(box_ip)"
      if [ -n "$IP" ] && ssh -i "$KEY" -o ConnectTimeout=5 -o StrictHostKeyChecking=no \
           -o BatchMode=yes "ubuntu@$IP" true 2>/dev/null; then
        say "Ready at $IP"
        ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP" \
          'echo "  cores: $(nproc)"; echo "  work:  $(df -h /mnt/work 2>/dev/null | awk "NR==2{print \$2\" total, \"\$4\" free\"}" || echo "not mounted yet")"' 2>/dev/null || true
        if [ "$MODE" = "kvm" ]; then
          # The entire point of metal: prove /dev/kvm exists and make it usable by
          # ubuntu (group change applies from the next SSH session on).
          if ssh -i "$KEY" -o StrictHostKeyChecking=no -o BatchMode=yes "ubuntu@$IP" \
               'test -c /dev/kvm && sudo usermod -aG kvm ubuntu' 2>/dev/null; then
            say "  kvm:   /dev/kvm present; ubuntu in kvm group (applies on next login)"
          else
            say "FAILED: $CHOSEN came up WITHOUT a usable /dev/kvm."
            say "That should be impossible on a *.metal type -- investigate before using."
            exit 1
          fi
        fi
        exit 0
      fi
      [ $((i % 6)) -eq 0 ] && say "    still booting (${i}0s)..."
      sleep 10
    done
    say "Launched but SSH did not come up in $((WAIT_TRIES / 6)) min; check: $SELF status"
    exit 1
    ;;

  down)
    ID="$(box_id)"
    if [ -z "$ID" ]; then
      say "$NAME is already down."
      exit 0
    fi
    say "Terminating $NAME ($ID). Its work volume is kept."
    # Terminate detaches the work volume on its own; delete_on_termination is false for
    # it (it is not part of the launch template's block device mappings at all), so the
    # disk survives. Only the 30GB root goes away, which is what it is for.
    if ! aws ec2 terminate-instances --region "$REGION" --instance-ids "$ID" >/dev/null 2>&1; then
      say "FAILED to terminate $ID"
      exit 1
    fi
    say "Down. Volume retained: $(work_volume)"
    ;;

  status)
    # No explicit box -> show both; a numbered ask shows just that one.
    if [ -z "$BOX" ]; then
      status_one 1
      status_one 2
    else
      status_one "$BOX"
    fi
    ;;

  ssh)
    IP="$(box_ip)"
    [ -n "$IP" ] || { say "Box is down. Run: $SELF up $BOX"; exit 1; }
    exec ssh -i "$KEY" -o StrictHostKeyChecking=no "ubuntu@$IP"
    ;;

  *)
    sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit 1
    ;;
esac

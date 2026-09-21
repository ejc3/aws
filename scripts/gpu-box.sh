#!/usr/bin/env bash
#
# Bring the on-demand GPU test box up and down (gpu-box.tf).
#
#   gbox up        launch it: tries each small NVIDIA type in each us-west-2 AZ
#   gbox down      terminate it (nothing on it persists)
#   gbox status    running? type, GPU, and its on-box shutdown timer
#   gbox ssh       connect
#   gbox ip        print the IP
#
# THIS SCRIPT DOES NOT RUN TERRAFORM, AND DOES NOT TOUCH THE JUMPBOX. Terraform owns the
# launch template, security group and the tag-scoped IAM grant; this supplies the two
# things terraform cannot know in advance -- which type and which AZ have capacity right
# now -- via ec2:RunInstances against that template. The grant allows only the types in
# GPU_BOX_TYPES below (kept in step with local.gpu_box_types) and only the tagged box.
#
# It costs money only while running: parallel-box-watchdog.tf terminates it after 30 idle
# minutes and at its hard lifetime (var.gpu_box_max_hours, from LaunchTime); the box also
# arms its own shutdown timer as a backup.
set -uo pipefail

REGION="us-west-2"
NAME="gpu-box"
LT="gpu-box"
TYPES="${GPU_BOX_TYPES:-g4dn.xlarge g5.xlarge g6.xlarge g4dn.2xlarge}"
# The four default-VPC subnets, in the same order as local.gpu_box_subnets.
SUBNETS="subnet-047683926b94c92c7 subnet-00844231e2667deec subnet-0346a0cc9fe6b928f subnet-095349c0fcef8c47f"

# The dev-hop key is the only key a dev box holds that reaches another host
# (dev-hop-key.tf); the launch template's user_data authorizes exactly that key.
KEY="$HOME/.ssh/dev_hop"
SSH_OPTS=(-i "$KEY" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR)

SELF="$(basename "${BASH_SOURCE[0]}")"
say() { printf '%s\n' "$*" >&2; }

box_ip() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text 2>/dev/null | grep -v '^None$' || true
}

# Every pending/running gpu-box, one id per line: two concurrent `up`s (or a hand-rolled
# --count) must not leave a box that `down` cannot see.
box_ids() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null | tr '\t' '\n' | grep '^i-' || true
}

need_key() {
  [ -f "$KEY" ] || { say "FATAL: no dev-hop key at $KEY (dev-hop-key.tf installs it for ubuntu and ejc3)"; exit 1; }
}

CMD="${1:-status}"

case "$CMD" in
  ip) box_ip ;;

  up)
    need_key
    IP="$(box_ip)"
    if [ -n "$IP" ]; then say "Already running at $IP"; exit 0; fi
    if [ -n "$(box_ids)" ]; then say "Already launching; run: $SELF status"; exit 0; fi

    # Which (type, AZ) pairs exist at all, looked up ONCE. An error here is a permissions or
    # region problem and must stop the launch -- read as "no offerings", it would come out
    # the far end as a fake capacity shortage.
    declare -A AZ_OF=()
    # shellcheck disable=SC2086 # one argument per subnet id
    if ! MAP=$(aws ec2 describe-subnets --region "$REGION" --subnet-ids $SUBNETS \
        --query 'Subnets[].[SubnetId,AvailabilityZone]' --output text 2>&1); then
      say "FATAL: cannot describe the GPU box subnets:"; printf '%s\n' "$MAP" | sed 's/^/  /' >&2; exit 1
    fi
    while read -r SID SAZ; do [ -n "$SID" ] && AZ_OF[$SID]="$SAZ"; done <<< "$MAP"
    # shellcheck disable=SC2086 # word splitting of TYPES is the point
    if ! OFFERS=$(aws ec2 describe-instance-type-offerings --region "$REGION" --location-type availability-zone \
        --filters "Name=instance-type,Values=$(echo $TYPES | tr ' ' ',')" \
        --query 'InstanceTypeOfferings[].[InstanceType,Location]' --output text 2>&1); then
      say "FATAL: cannot list instance-type offerings:"; printf '%s\n' "$OFFERS" | sed 's/^/  /' >&2; exit 1
    fi

    ID=""
    for T in $TYPES; do
      for S in $SUBNETS; do
        # skip AZs that do not offer the type at all, so a real error never hides among
        # expected "Unsupported" noise
        grep -qx "$T[[:space:]]${AZ_OF[$S]:-none}" <<< "$OFFERS" || continue
        say "--> trying $T in $S ..."
        # Everything but type and subnet comes from the launch template, so a typo here
        # cannot change the AMI, the security group, the tags or the lifetime.
        OUT=$(aws ec2 run-instances --region "$REGION" \
          --launch-template "LaunchTemplateName=$LT,Version=\$Latest" \
          --instance-type "$T" --subnet-id "$S" --count 1 \
          --query 'Instances[0].InstanceId' --output text 2>&1)
        if [ $? -eq 0 ] && [ "${OUT#i-}" != "$OUT" ]; then
          ID="$OUT"; CHOSEN="$T"; break 2
        fi
        case "$OUT" in
          *InsufficientInstanceCapacity*|*Unsupported*)
            say "    no capacity for $T there -- trying the next" ;;
          *VcpuLimitExceeded*)
            say "    FAILED: the account's G-instance vCPU quota in $REGION is too low."
            say "    An admin can check: aws service-quotas get-service-quota --service-code ec2 --quota-code L-DB2E81BA --region $REGION"
            exit 1 ;;
          *)
            # A policy or template problem looks nothing like a busy afternoon; stop and
            # show it rather than walking every pool.
            say "    launch failed:"; printf '%s\n' "$OUT" | sed 's/^/      /' >&2; exit 1 ;;
        esac
      done
    done
    [ -n "$ID" ] || { say "FAILED: no capacity for any of: $TYPES"; exit 1; }

    say "Launched $CHOSEN as $ID. Waiting for SSH..."
    aws ec2 wait instance-running --region "$REGION" --instance-ids "$ID" 2>/dev/null
    for i in $(seq 1 60); do
      IP="$(box_ip)"
      if [ -n "$IP" ] && ssh "${SSH_OPTS[@]}" -o ConnectTimeout=5 -o BatchMode=yes "ubuntu@$IP" \
           'test -s /etc/gpu-box-ready' 2>/dev/null; then
        say "Ready at $IP: $(ssh "${SSH_OPTS[@]}" -o BatchMode=yes "ubuntu@$IP" cat /etc/gpu-box-ready 2>/dev/null)"
        exit 0
      fi
      [ $((i % 6)) -eq 0 ] && say "    still booting (${i}0s)..."
      sleep 10
    done
    say "Launched but SSH did not come up in 10 min; check: $SELF status"
    exit 1
    ;;

  down)
    IDS="$(box_ids)"
    [ -n "$IDS" ] || { say "$NAME is already down."; exit 0; }
    # shellcheck disable=SC2086 # one argument per instance id
    if ! aws ec2 terminate-instances --region "$REGION" --instance-ids $IDS >/dev/null 2>&1; then
      say "FAILED to terminate: $(echo $IDS)"; exit 1
    fi
    say "Terminating $NAME: $(echo $IDS)"
    ;;

  status)
    IP="$(box_ip)"
    if [ -z "$IP" ]; then
      if [ -n "$(box_ids)" ]; then echo "gpu-box: launching"; else echo "gpu-box: down (\$0)"; fi
      exit 0
    fi
    T=$(aws ec2 describe-instances --region "$REGION" \
      --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
      --query 'Reservations[0].Instances[0].InstanceType' --output text 2>/dev/null)
    echo "gpu-box: RUNNING at $IP ($T); the watchdog terminates it at its lifetime or after 30 idle minutes"
    [ -f "$KEY" ] || exit 0
    ssh "${SSH_OPTS[@]}" -o ConnectTimeout=5 -o BatchMode=yes "ubuntu@$IP" \
      'echo "  gpu:    $(cat /etc/gpu-box-ready 2>/dev/null)"; echo "  up:     $(uptime -p)"; E=$(sed -n "s/^USEC=//p" /run/systemd/shutdown/scheduled 2>/dev/null | head -1); if [ -n "$E" ]; then echo "  timer:  on-box shutdown at $(date -u -d @$((E/1000000)) +%H:%M) UTC"; else echo "  timer:  on-box shutdown NOT ARMED (the watchdog lifetime still applies)"; fi' 2>/dev/null \
      || echo "  (running, but SSH not answering yet)"
    ;;

  ssh)
    need_key
    IP="$(box_ip)"
    [ -n "$IP" ] || { say "Box is down. Run: $SELF up"; exit 1; }
    exec ssh "${SSH_OPTS[@]}" "ubuntu@$IP"
    ;;

  *)
    sed -n '3,9p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'
    exit 1
    ;;
esac

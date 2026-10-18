#!/usr/bin/env bash
#
# Bring the Windows playtest spot boxes up and down (playbox.tf). There are THREE slots, one
# per player, each with its own persistent D: disk, so N (1-3) is required for up, down,
# connect and ip: a slot is never picked for you.
#
#   playbox up N          launch slot N as spot (g4dn.xlarge, else g4dn.2xlarge) in us-west-1a,
#                         attach its D: disk, and wait until DCV answers (15-20 minutes from
#                         launch: Windows, the GPU driver, DCV and a reboot)
#   playbox down N        terminate it; its D: disk (Roblox and its caches) is kept
#   playbox status [N]    every slot, or slot N: running? type, private IP, when its lifetime ends
#   playbox connect N     how to reach it: the SSH tunnel, the DCV address, the login
#   playbox connect N --show-password
#                         the same, and print the Administrator password
#   playbox ip N          its private IP
#
# For scripts, `up` exits 0 when DCV answers (stdout's last line is
# "launched slot=N type=T ip=IP", or "running slot=N ip=IP" when it already was), 3 when no type
# had spot capacity, 4 when DCV did not answer in time (the box is left running; check again),
# and 1 for any other failure.
#
# THIS SCRIPT DOES NOT RUN TERRAFORM, AND DOES NOT TOUCH THE JUMPBOX. Terraform owns the launch
# templates, the D: disks, the passwords and the tag-scoped grant (playbox-control); this
# supplies the one thing terraform cannot know in advance -- which type has spot capacity now --
# via ec2:RunInstances against slot N's template, then attaches slot N's disk. The grant allows
# only the types in TYPES below (kept in step with local.playbox_types) and only the slot names.
#
# The playbox-watchdog Lambda terminates a slot after one hour below 15% CPU, and 6 hours after
# launch whatever it is doing (MAX_HOURS, kept in step with local.playbox_max_hours).
#
# Reaching it: only from the dev boxes. From your own computer, tunnel through the dev box you log
# in to: ssh -L 8443:<ip>:8443 <dev box>, then https://localhost:8443 (accept the self-signed
# certificate), or point the DCV client app at localhost:8443. On a dev box's own browser,
# https://<ip>:8443. Windows does not keep Roblox's sign-in between launches: sign in again
# (Quick Sign-in from a phone is quickest).
set -uo pipefail

REGION="us-west-1"
SLOTS="1 2 3"                     # the keys of local.playbox_slots
TYPES="g4dn.xlarge g4dn.2xlarge"  # local.playbox_types, cheapest first
MAX_HOURS=6                       # local.playbox_max_hours
DCV_WAIT_MINUTES=30

SELF="$(basename "${BASH_SOURCE[0]}")"
say() { printf '%s\n' "$*" >&2; }

usage() {
  sed -n '3,15p' "${BASH_SOURCE[0]}" | sed 's/^# \?//' >&2
  exit 2
}

set_slot() {
  case " $SLOTS " in
    *" $1 "*) ;;
    *) say "unknown slot '$1' (use 1-3)"; exit 2 ;;
  esac
  SLOT="$1"
  NAME="playbox-$SLOT"       # the instance's Name tag and its launch template
  VOLTAG="$NAME-data"        # its D: disk's Name tag
  SECRET="$NAME/administrator-password"
}

# Every pending/running instance of the slot, one id per line: two concurrent `up`s must not
# leave a box that `down` cannot see.
box_ids() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null | tr '\t' '\n' | grep '^i-' || true
}

# id type lifecycle private-ip launch-time of the slot's running box, or nothing
box_row() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
    --query 'Reservations[0].Instances[0].[InstanceId,InstanceType,InstanceLifecycle,PrivateIpAddress,LaunchTime]' \
    --output text 2>/dev/null | grep '^i-' || true
}

box_ip() {
  local row
  row="$(box_row)"
  [ -n "$row" ] && printf '%s\n' "$row" | awk '{print $4}'
}

# volume-id state availability-zone of the slot's D: disk
data_volume() {
  aws ec2 describe-volumes --region "$REGION" --filters "Name=tag:Name,Values=$VOLTAG" \
    --query 'Volumes[0].[VolumeId,State,AvailabilityZone]' --output text 2>/dev/null | grep '^vol-' || true
}

dcv_answers() { # dcv_answers IP: does anything accept a TCP connection on 8443?
  timeout 5 bash -c "exec 3<>/dev/tcp/$1/8443" 2>/dev/null
}

ends_at() { # ends_at LAUNCH-TIME: when the watchdog's lifetime ends it, local time
  local t
  t=$(date -d "$1" +%s 2>/dev/null) || { echo "$MAX_HOURS h after launch"; return; }
  date -d "@$((t + MAX_HOURS * 3600))" '+%H:%M %Z'
}

connect_hint() { # connect_hint IP
  say "$NAME is at $1 (a private address: reachable from the dev boxes only)."
  say "  From your own computer, tunnel through the dev box you log in to:"
  say "      ssh -L 8443:$1:8443 <dev box>"
  say "    then open https://localhost:8443 (accept the self-signed certificate),"
  say "    or point the DCV client app at localhost:8443."
  say "  In a dev box's own browser: https://$1:8443"
  say "  Sign in as Administrator. Password: $SELF connect $SLOT --show-password"
  say "  Roblox is on D: and stays; its sign-in does not (sign in again, e.g. Quick Sign-in from a phone)."
}

CMD="${1:-status}"
[ $# -gt 0 ] && shift
SHOW_PASSWORD=""
ARG=""
for a in "$@"; do
  case "$a" in
    --show-password) SHOW_PASSWORD=1 ;;
    *) [ -z "$ARG" ] || { say "one slot at a time"; exit 2; }; ARG="$a" ;;
  esac
done
if [ -n "$SHOW_PASSWORD" ] && [ "$CMD" != "connect" ]; then say "--show-password only goes with connect"; exit 2; fi

case "$CMD" in
  up|down|connect|ip)
    [ -n "$ARG" ] || { say "usage: $SELF $CMD N   (N is your slot, 1-3)"; exit 2; }
    set_slot "$ARG" ;;
  status)
    if [ -z "$ARG" ]; then
      # Run this script once per slot: each block is exactly that slot's own status.
      for N in $SLOTS; do "$BASH" "${BASH_SOURCE[0]}" status "$N"; done
      exit 0
    fi
    set_slot "$ARG" ;;
  *) usage ;;
esac

case "$CMD" in
  ip)
    IP="$(box_ip)"
    [ -n "$IP" ] || { say "$NAME is down. Run: $SELF up $SLOT"; exit 1; }
    printf '%s\n' "$IP"
    ;;

  status)
    ROW="$(box_row)"
    if [ -z "$ROW" ]; then
      if [ -n "$(box_ids)" ]; then echo "$NAME: launching"; else echo "$NAME: down (\$0 compute)"; fi
    else
      read -r _ T L IP LT <<< "$ROW"
      case "$L" in spot) BOUGHT="spot" ;; *) BOUGHT="ON-DEMAND" ;; esac
      if dcv_answers "$IP"; then READY="DCV answering"; else READY="still setting up (DCV not answering yet)"; fi
      echo "$NAME: RUNNING at $IP ($T, $BOUGHT), $READY; the watchdog ends it after an idle hour or at $(ends_at "$LT")"
    fi
    read -r VOL VSTATE _ <<< "$(data_volume)"
    echo "  D: disk: ${VOL:-MISSING} (${VSTATE:-?}, kept between launches)"
    ;;

  connect)
    IP="$(box_ip)"
    [ -n "$IP" ] || { say "$NAME is down. Run: $SELF up $SLOT"; exit 1; }
    connect_hint "$IP"
    dcv_answers "$IP" || say "  (DCV is not answering yet: Windows is still setting up. Try again in a few minutes.)"
    if [ -n "$SHOW_PASSWORD" ]; then
      aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET" --query SecretString --output text
    fi
    ;;

  down)
    IDS="$(box_ids)"
    [ -n "$IDS" ] || { say "$NAME is already down."; exit 0; }
    LIST=$(tr '\n' ' ' <<< "$IDS")
    # shellcheck disable=SC2086 # one argument per instance id
    if ! aws ec2 terminate-instances --region "$REGION" --instance-ids $IDS >/dev/null 2>&1; then
      say "FAILED to terminate: $LIST"; exit 1
    fi
    say "Terminating $NAME: ${LIST% }. Its D: disk is kept."
    ;;

  up)
    ROW="$(box_row)"
    if [ -n "$ROW" ]; then
      read -r _ _ _ IP _ <<< "$ROW"
      say "$NAME is already running."
      connect_hint "$IP"
      echo "running slot=$SLOT ip=$IP"
      exit 0
    fi
    if [ -n "$(box_ids)" ]; then say "$NAME is already launching; run: $SELF status $SLOT"; exit 0; fi

    read -r VOL VSTATE AZ <<< "$(data_volume)"
    [ -n "${VOL:-}" ] || { say "FATAL: no D: disk tagged $VOLTAG in $REGION (is playbox.tf applied?)"; exit 1; }
    # A box that was just terminated may still be letting go of the disk.
    for _ in $(seq 1 24); do
      [ "$VSTATE" = "available" ] && break
      say "    D: disk $VOL is $VSTATE; waiting for it to be free..."
      sleep 5
      read -r VOL VSTATE AZ <<< "$(data_volume)"
    done
    [ "$VSTATE" = "available" ] || { say "FAILED: D: disk $VOL is still $VSTATE. Is another $NAME running? $SELF status $SLOT"; exit 1; }
    say "D: disk $VOL is in $AZ; $NAME launches there (EBS is AZ-locked)."

    ID=""
    for T in $TYPES; do
      PRICE=$(aws ec2 describe-spot-price-history --region "$REGION" --instance-types "$T" \
        --product-descriptions Windows --availability-zone "$AZ" --max-items 1 \
        --query 'SpotPriceHistory[0].SpotPrice' --output text 2>/dev/null | head -1)
      say "--> trying spot $T (\$${PRICE:-?}/hour) in $AZ ..."
      # Everything but the type comes from the slot's launch template, so nothing here can change
      # the AMI, the subnet, the security group, the role or the tags.
      OUT=$(aws ec2 run-instances --region "$REGION" \
        --launch-template "LaunchTemplateName=$NAME,Version=\$Latest" \
        --instance-type "$T" --count 1 \
        --query 'Instances[0].InstanceId' --output text 2>&1)
      RC=$?
      if [ "$RC" -eq 0 ] && [ "${OUT#i-}" != "$OUT" ]; then
        ID="$OUT"; CHOSEN="$T"; break
      fi
      case "$OUT" in
        *InsufficientInstanceCapacity*|*capacity-not-available*|*Unsupported*|*SpotMaxPriceTooLow*)
          say "    no spot capacity for $T -- trying the next" ;;
        *VcpuLimitExceeded*|*MaxSpotInstanceCountExceeded*)
          say "    FAILED: the account's G-instance spot quota in $REGION is too low (the slots share it)."
          say "    An admin can check: aws service-quotas get-service-quota --service-code ec2 --quota-code L-3819A6DF --region $REGION"
          exit 1 ;;
        *)
          # A policy or template problem looks nothing like a busy afternoon: stop and show it.
          say "    launch failed:"; printf '%s\n' "$OUT" | sed 's/^/      /' >&2; exit 1 ;;
      esac
    done
    if [ -z "$ID" ]; then
      say "FAILED: no spot capacity in $AZ for any of: $TYPES. Capacity changes hour to hour; try again later."
      exit 3
    fi

    say "Launched $CHOSEN as $ID. Attaching the D: disk..."
    # A launch template cannot carry an existing volume, and EC2 refuses an attach while the
    # instance is pending. Windows' setup waits for the disk, so attaching promptly is enough.
    aws ec2 wait instance-running --region "$REGION" --instance-ids "$ID" 2>/dev/null
    if ! ATTACH=$(aws ec2 attach-volume --region "$REGION" --volume-id "$VOL" --instance-id "$ID" --device xvdf 2>&1); then
      say "FAILED to attach $VOL to $ID:"; printf '%s\n' "$ATTACH" | sed 's/^/  /' >&2
      # A box without its D: disk would only run up a bill while its setup waits: end it.
      if aws ec2 terminate-instances --region "$REGION" --instance-ids "$ID" >/dev/null 2>&1; then
        say "Terminated $ID; nothing was changed on the disk."
      else
        say "Could not terminate $ID either: run $SELF down $SLOT"
      fi
      exit 1
    fi

    IP="$(box_ip)"
    say "Windows is setting up (the GPU driver, DCV, a reboot): DCV usually answers 15-20 minutes after launch."
    for i in $(seq 1 $((DCV_WAIT_MINUTES * 4))); do
      [ -n "$IP" ] || IP="$(box_ip)"
      if [ -n "$IP" ] && dcv_answers "$IP"; then
        say "$NAME is up."
        connect_hint "$IP"
        echo "launched slot=$SLOT type=$CHOSEN ip=$IP"
        exit 0
      fi
      [ $((i % 8)) -eq 0 ] && say "    still setting up ($((i / 4)) min)..."
      sleep 15
    done
    say "$NAME is running but DCV did not answer within $DCV_WAIT_MINUTES minutes. It is left running;"
    say "check with: $SELF status $SLOT (an admin can read C:\\playbox-setup.log through Systems Manager)."
    exit 4
    ;;
esac

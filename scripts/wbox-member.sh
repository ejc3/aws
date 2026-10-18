#!/usr/bin/env bash
#
# A member's own Windows playtest spot box, wbox-<person> (member-wbox.tf). `wbox` hands every
# command of a member's account here (scripts/wbox.sh); the person is the account running it.
#
#   wbox up                launch your box as spot (g4dn.xlarge, else g4dn.2xlarge) in us-west-1a,
#                          attach your D: disk, and wait until DCV answers (15-20 minutes from
#                          launch: Windows, the GPU driver, DCV and a reboot)
#   wbox down              terminate it; your D: disk (Roblox and its caches) is kept
#   wbox status            running? type, its address, when its lifetime ends
#   wbox connect           how to reach it: the SSH tunnel, the DCV address, the login
#   wbox password          the Administrator password
#   wbox ip                its private IP (fixed: the same at every launch)
#
# For scripts, `up` exits 0 when DCV answers (stdout's last line is
# "launched box=NAME type=T ip=IP", or "running box=NAME ip=IP" when the box already existed), 3
# when no type had spot capacity, 4 when DCV did not answer in time (the box is left running;
# check again), and 1 for any other failure. An existing box is not taken as ready: `up` resumes
# its setup -- attaches the D: disk if it is not attached (a launcher interrupted between
# launch and attach leaves it so) and waits for DCV exactly as for a new launch.
#
# THIS SCRIPT DOES NOT RUN TERRAFORM, AND DOES NOT TOUCH THE JUMPBOX. Terraform owns the launch
# templates, the D: disks, the passwords and the tag-scoped grant (member-wbox-control); this
# supplies the one thing terraform cannot know in advance -- which type has spot capacity now --
# via ec2:RunInstances against the person's template, then attaches the person's disk. The
# grant allows only the types in TYPES below (kept in step with local.member_wbox_types).
#
# The member-wbox-watchdog Lambda terminates a box after one hour below 15% CPU, and 6 hours
# after launch whatever it is doing (MAX_HOURS, kept in step with local.member_box_max_hours);
# the box also ends itself at that deadline.
#
# Reaching it: from the box you log in to (nextjs-dev, or your container on its successor).
# From your own computer, tunnel through it: ssh -L 8443:<ip>:8443 <that box>, then
# https://localhost:8443 (accept the self-signed certificate), or point the DCV client app at
# localhost:8443. Windows does not keep Roblox's sign-in between launches: sign in again
# (Quick Sign-in from a phone is quickest).
set -uo pipefail

REGION="us-west-1"
MEMBERS="colton connor colin skevh" # local.member_box_people
TYPES="g4dn.xlarge g4dn.2xlarge"    # local.member_wbox_types, cheapest first
MAX_HOURS=6                         # local.member_box_max_hours
DCV_WAIT_MINUTES=30
IP_WAIT_TRIES=18                    # x10 s: a just-terminated box may still hold the fixed address

SELF="wbox"
say() { printf '%s\n' "$*" >&2; }

usage() {
  sed -n '6,13p' "${BASH_SOURCE[0]}" | sed 's/^# \?//' >&2
  exit 2
}

MEMBER="${MEMBER_BOX_USER:-$(id -un 2>/dev/null)}"
case " $MEMBERS " in
  *" $MEMBER "*) ;;
  *) say "wbox-member: '$MEMBER' has no member wbox (the members: $MEMBERS)"; exit 2 ;;
esac
NAME="wbox-$MEMBER"        # the instance's Name tag and its launch template
VOLTAG="$NAME-data"        # its D: disk's Name tag
SECRET="$NAME/administrator-password"

# Every pending/running instance of the box, one id per line: two concurrent `up`s must not
# leave a box that `down` cannot see.
box_ids() {
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null | tr '\t' '\n' | grep '^i-' || true
}

# id type lifecycle private-ip launch-time of the running box, or nothing
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

# volume-id state availability-zone attached-instance-id (None when detached) of the D: disk
data_volume() {
  aws ec2 describe-volumes --region "$REGION" --filters "Name=tag:Name,Values=$VOLTAG" \
    --query 'Volumes[0].[VolumeId,State,AvailabilityZone,Attachments[0].InstanceId]' --output text 2>/dev/null | grep '^vol-' || true
}

# wait_volume_free [ID]: wait (up to 2 minutes) for the D: disk to be available; a box that was
# just terminated may still be letting go of it. Already attached to ID (another `up` on the same
# box got there first) is success too. Sets VOL VSTATE AZ ATTACHED; fails if it stays busy.
wait_volume_free() {
  local target="${1:-}"
  read -r VOL VSTATE AZ ATTACHED <<< "$(data_volume)"
  [ -n "${VOL:-}" ] || { say "FATAL: no D: disk tagged $VOLTAG in $REGION (is member-wbox.tf applied?)"; return 1; }
  for _ in $(seq 1 24); do
    [ -n "$target" ] && [ "$ATTACHED" = "$target" ] && return 0
    [ "$VSTATE" = "available" ] && return 0
    say "    D: disk $VOL is $VSTATE; waiting for it to be free..."
    sleep 5
    read -r VOL VSTATE AZ ATTACHED <<< "$(data_volume)"
  done
  [ "$VSTATE" = "available" ] && return 0
  [ -n "$target" ] && [ "$ATTACHED" = "$target" ] && return 0
  say "FAILED: D: disk $VOL is still $VSTATE${ATTACHED:+ (attached to $ATTACHED)}. Is another $NAME running? $SELF status"
  return 1
}

# attach_data ID: make sure the D: disk is attached to instance ID, attaching it if it is not.
# A box that cannot get its disk is terminated: its setup waits for D: and would only run up a
# bill. Two `up`s racing to attach to the same box both succeed (the loser finds it attached).
attach_data() {
  local id="$1" out
  read -r VOL VSTATE AZ ATTACHED <<< "$(data_volume)"
  if [ -n "${VOL:-}" ] && [ "$ATTACHED" = "$id" ]; then
    say "D: disk $VOL is attached to $id."
    return 0
  fi
  if wait_volume_free "$id"; then
    if [ "$ATTACHED" = "$id" ]; then
      say "D: disk $VOL is attached to $id."
      return 0
    fi
    # A launch template cannot carry an existing volume, and EC2 refuses an attach while the
    # instance is pending. Windows' setup waits for the disk, so attaching promptly is enough.
    aws ec2 wait instance-running --region "$REGION" --instance-ids "$id" 2>/dev/null
    if out=$(aws ec2 attach-volume --region "$REGION" --volume-id "$VOL" --instance-id "$id" --device xvdf 2>&1); then
      say "Attached D: disk $VOL to $id."
      return 0
    fi
    read -r VOL VSTATE AZ ATTACHED <<< "$(data_volume)"
    if [ "$ATTACHED" = "$id" ]; then
      say "D: disk $VOL is attached to $id."
      return 0
    fi
    say "FAILED to attach $VOL to $id:"; printf '%s\n' "$out" | sed 's/^/  /' >&2
  fi
  if aws ec2 terminate-instances --region "$REGION" --instance-ids "$id" >/dev/null 2>&1; then
    say "Terminated $id; nothing was changed on the disk."
  else
    say "Could not terminate $id either: run $SELF down"
  fi
  return 1
}

dcv_answers() { # dcv_answers IP: does anything accept a TCP connection on 8443?
  timeout 5 bash -c "exec 3<>/dev/tcp/$1/8443" 2>/dev/null
}

# wait_dcv LINE: wait for DCV on the box, then print LINE plus " ip=IP" on stdout and exit 0;
# exit 4 if it does not answer in DCV_WAIT_MINUTES.
wait_dcv() {
  local i ip=""
  say "Windows is setting up (the GPU driver, DCV, a reboot): DCV usually answers 15-20 minutes after launch."
  for i in $(seq 1 $((DCV_WAIT_MINUTES * 4))); do
    [ -n "$ip" ] || ip="$(box_ip)"
    if [ -n "$ip" ] && dcv_answers "$ip"; then
      say "$NAME is up."
      connect_hint "$ip"
      echo "$1 ip=$ip"
      exit 0
    fi
    [ $((i % 8)) -eq 0 ] && say "    still setting up ($((i / 4)) min)..."
    sleep 15
  done
  say "$NAME is running but DCV did not answer within $DCV_WAIT_MINUTES minutes. It is left running;"
  say "check with: $SELF status (an admin can read C:\\wbox-setup.log through Systems Manager)."
  exit 4
}

ends_at() { # ends_at LAUNCH-TIME: when the watchdog's lifetime ends it, local time
  local t
  t=$(date -d "$1" +%s 2>/dev/null) || { echo "$MAX_HOURS h after launch"; return; }
  date -d "@$((t + MAX_HOURS * 3600))" '+%H:%M %Z'
}

connect_hint() { # connect_hint IP
  say "$NAME is at $1 (a private address, the same at every launch)."
  say "  From your own computer, tunnel through the box you log in to:"
  say "      ssh -L 8443:$1:8443 <that box>"
  say "    then open https://localhost:8443 (accept the self-signed certificate),"
  say "    or point the DCV client app at localhost:8443."
  say "  Sign in as Administrator. Password: $SELF password"
  say "  Roblox is on D: and stays; its sign-in does not (sign in again, e.g. Quick Sign-in from a phone)."
}

CMD="${1:-status}"
[ $# -gt 0 ] && shift
case "$CMD" in
  up|down|status|connect|ip|password)
    [ $# -eq 0 ] || { say "$SELF $CMD takes no argument: your box is $NAME"; exit 2; } ;;
  run|launch)
    say "$SELF $CMD is for the owner's wbox; on $NAME use the desktop (DCV)."; exit 2 ;;
  *) usage ;;
esac

case "$CMD" in
  ip)
    IP="$(box_ip)"
    [ -n "$IP" ] || { say "$NAME is down. Run: $SELF up"; exit 1; }
    printf '%s\n' "$IP"
    ;;

  password)
    aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET" --query SecretString --output text
    ;;

  status)
    ROW="$(box_row)"
    if [ -z "$ROW" ]; then
      if [ -n "$(box_ids)" ]; then echo "$NAME: launching"; else echo "$NAME: down (\$0 compute)"; fi
    else
      read -r _ T _ IP LT <<< "$ROW"
      # Spot only: the template asks for it and the grant refuses anything else.
      if dcv_answers "$IP"; then READY="DCV answering"; else READY="still setting up (DCV not answering yet)"; fi
      echo "$NAME: RUNNING at $IP ($T, spot), $READY; the watchdog ends it after an idle hour or at $(ends_at "$LT")"
    fi
    read -r VOL VSTATE _ _ <<< "$(data_volume)"
    echo "  D: disk: ${VOL:-MISSING} (${VSTATE:-?}, kept between launches)"
    ;;

  connect)
    IP="$(box_ip)"
    [ -n "$IP" ] || { say "$NAME is down. Run: $SELF up"; exit 1; }
    connect_hint "$IP"
    dcv_answers "$IP" || say "  (DCV is not answering yet: Windows is still setting up. Try again in a few minutes.)"
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
    # A box already exists: resume its setup rather than trust it. It is `running` long before
    # its D: disk is attached and DCV is up, and a launcher interrupted between run-instances and
    # attach-volume leaves one that would wait for its disk forever.
    IDS="$(box_ids)"
    if [ -n "$IDS" ]; then
      if [ "$(wc -l <<< "$IDS")" -gt 1 ]; then
        say "FAILED: more than one $NAME exists ($(tr '\n' ' ' <<< "$IDS")). Run: $SELF down, then $SELF up"
        exit 1
      fi
      ID="$IDS"
      say "$NAME already exists ($ID); making sure its D: disk is attached and its setup finishes."
      attach_data "$ID" || exit 1
      wait_dcv "running box=$NAME"
    fi

    wait_volume_free || exit 1
    say "D: disk $VOL is in $AZ; $NAME launches there (EBS is AZ-locked)."

    ID=""
    for T in $TYPES; do
      PRICE=$(aws ec2 describe-spot-price-history --region "$REGION" --instance-types "$T" \
        --product-descriptions Windows --availability-zone "$AZ" --max-items 1 \
        --query 'SpotPriceHistory[0].SpotPrice' --output text 2>/dev/null | head -1)
      say "--> trying spot $T (\$${PRICE:-?}/hour) in $AZ ..."
      for _ in $(seq 1 "$IP_WAIT_TRIES"); do
        # Everything but the type comes from the launch template, so nothing here can change the
        # AMI, the subnet, the address, the security group, the role, the tags or the spot market.
        OUT=$(aws ec2 run-instances --region "$REGION" \
          --launch-template "LaunchTemplateName=$NAME,Version=\$Latest" \
          --instance-type "$T" --count 1 \
          --query 'Instances[0].InstanceId' --output text 2>&1)
        RC=$?
        case "$OUT" in
          *InvalidIPAddress.InUse*) say "    the previous $NAME still holds its fixed address; waiting..."; sleep 10 ;;
          *) break ;;
        esac
      done
      if [ "$RC" -eq 0 ] && [ "${OUT#i-}" != "$OUT" ]; then
        ID="$OUT"; CHOSEN="$T"; break
      fi
      case "$OUT" in
        *InsufficientInstanceCapacity*|*capacity-not-available*|*Unsupported*|*SpotMaxPriceTooLow*)
          say "    no spot capacity for $T -- trying the next" ;;
        *VcpuLimitExceeded*|*MaxSpotInstanceCountExceeded*)
          say "    FAILED: the account's G-instance spot quota in $REGION is too low (the boxes share it)."
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
    attach_data "$ID" || exit 1
    wait_dcv "launched box=$NAME type=$CHOSEN"
    ;;
esac

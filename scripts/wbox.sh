#!/usr/bin/env bash
#
# Start and stop the Windows playtest box (wbox.tf).
#
#   wbox up         start it and wait until Windows answers
#   wbox down       stop it (the game disk D: and everything on C: stay)
#   wbox status     state, private IP, and how to connect
#   wbox ip         its private IP
#   wbox password   the Administrator password (for the DCV or RDP login)
#
# THIS SCRIPT DOES NOT RUN TERRAFORM. Terraform owns the instance and its disks; the dev
# boxes' grant (wbox-control) allows only start, stop and reboot of this one instance and
# reading its password. It stops itself after one idle hour (wbox-auto-stop).
#
# Reaching it: only from the dev boxes. Open https://<ip>:8443 in a dev box browser (DCV's
# web client; accept the self-signed certificate), or tunnel it to your own machine over the
# SSH you already use: ssh -L 8443:<ip>:8443 fcvm-arm, then https://localhost:8443.
set -uo pipefail

REGION="us-west-1"
NAME="wbox"
SECRET="wbox/administrator-password"

say() { printf '%s\n' "$*" >&2; }

instance() {   # prints: id state private-ip
  aws ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[0].Instances[0].[InstanceId,State.Name,PrivateIpAddress]' --output text 2>/dev/null
}

read -r ID STATE IP <<< "$(instance)"
if [ -z "${ID:-}" ] || [ "$ID" = "None" ]; then
  say "wbox: no Windows box found (is enable_wbox on in the aws repo?)"
  exit 1
fi

connect_hint() {
  say "Connect from a dev box: https://$IP:8443 (DCV web client), or RDP to $IP:3389."
  say "From your own machine: ssh -L 8443:$IP:8443 fcvm-arm, then https://localhost:8443."
  say "User Administrator; password: wbox password"
}

case "${1:-status}" in
  up)
    if [ "$STATE" != "running" ]; then
      aws ec2 start-instances --region "$REGION" --instance-ids "$ID" >/dev/null || exit 1
      say "Starting $ID ..."
      aws ec2 wait instance-running --region "$REGION" --instance-ids "$ID" || exit 1
    fi
    say "Waiting for Windows to answer on 8443 (a minute or two after boot) ..."
    for _ in $(seq 1 60); do
      (exec 3<>"/dev/tcp/$IP/8443") 2>/dev/null && { say "wbox is up."; connect_hint; exit 0; }
      sleep 5
    done
    say "wbox is running but 8443 is not answering yet; try again in a minute."
    connect_hint
    ;;
  down)
    aws ec2 stop-instances --region "$REGION" --instance-ids "$ID" >/dev/null && say "Stopping $ID (disks kept)."
    ;;
  status)
    say "wbox $ID: $STATE, private IP $IP"
    [ "$STATE" = "running" ] && connect_hint
    ;;
  ip)
    printf '%s\n' "$IP"
    ;;
  password)
    aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET" --query SecretString --output text
    ;;
  *)
    say "usage: wbox up | down | status | ip | password"
    exit 2
    ;;
esac

#!/usr/bin/env bash
#
# Start and stop the Windows playtest box (wbox.tf).
#
#   wbox up         start it and wait until Windows answers
#   wbox down       stop it (the game disk D: and everything on C: stay)
#   wbox status     state, private IP, and how to connect
#   wbox ip         its private IP
#   wbox password   the Administrator password (for the DCV or RDP login)
#   wbox run <powershell>      run PowerShell on the box through SSM and print the output
#   wbox launch <exe> [args]   start a program on the desktop of the logged-in Administrator
#                              (needs a DCV or RDP session open: SSM itself has no desktop)
#
# THIS SCRIPT DOES NOT RUN TERRAFORM. Terraform owns the instance and its disks; the dev
# boxes' grant (wbox-control) allows only start, stop and reboot of this one instance, running
# PowerShell on it through SSM (output read from its S3 bucket), and reading its password. It stops itself after one idle hour (wbox-auto-stop).
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

# Send PowerShell ($1) to the box through SSM and print what it wrote. The dev roles may not call
# ssm:GetCommandInvocation (no resource scope: it would expose the jumpboxes' command output), so
# SSM writes the output to our bucket and we read it from there. Exit 1 if nothing came back.
run_ps() {
  local params cid bucket prefix key out
  bucket="wbox-output-$(aws sts get-caller-identity --query Account --output text)" || return 1
  params=$(mktemp) || return 1
  PS_SCRIPT="$1" python3 -c 'import json,os; print(json.dumps({"commands":[os.environ["PS_SCRIPT"]]}))' > "$params"
  cid=$(aws ssm send-command --region "$REGION" --instance-ids "$ID" --document-name AWS-RunPowerShellScript \
    --parameters "file://$params" --output-s3-bucket-name "$bucket" --output-s3-key-prefix wbox \
    --query Command.CommandId --output text)
  rm -f "$params"
  [ -n "$cid" ] || { say "wbox: SendCommand failed (is the box running and SSM Online?)"; return 1; }
  prefix="wbox/$cid/"
  for _ in $(seq 1 60); do
    key=$(aws s3api list-objects-v2 --bucket "$bucket" --prefix "$prefix" --query 'Contents[].Key' --output text 2>/dev/null |
          tr '\t' '\n' | grep '/stdout$' | head -1)
    if [ -n "$key" ]; then
      sleep 2   # stderr is uploaded alongside stdout
      out=${key%stdout}
      aws s3 cp "s3://$bucket/${out}stdout" - 2>/dev/null
      aws s3 cp "s3://$bucket/${out}stderr" - 2>/dev/null >&2 || true
      return 0
    fi
    sleep 2
  done
  say "wbox: no output after 2 minutes (command $cid). It may still be running; look under s3://$bucket/$prefix"
  return 1
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
  run)
    [ "$STATE" = "running" ] || { say "wbox is $STATE; run: wbox up"; exit 1; }
    [ $# -ge 2 ] || { say "usage: wbox run <powershell>"; exit 2; }
    shift
    run_ps "$*"
    ;;
  launch)
    [ "$STATE" = "running" ] || { say "wbox is $STATE; run: wbox up"; exit 1; }
    [ $# -ge 2 ] || { say "usage: wbox launch <exe> [args...]"; exit 2; }
    shift
    # A SSM command runs in session 0, which has no desktop. A scheduled task with /IT runs in
    # the interactive session of the logged-in Administrator, so the game window appears in DCV.
    exe=$1; shift
    tr_cmd="\"$exe\" $*"
    run_ps "\$tr = '${tr_cmd//\'/\'\'}'
schtasks /Create /TN wbox-launch /TR \$tr /SC ONCE /ST 00:00 /RU Administrator /IT /F
if (\$LASTEXITCODE -eq 0) { schtasks /Run /TN wbox-launch }
exit \$LASTEXITCODE"
    ;;
  *)
    say "usage: wbox up | down | status | ip | password | run <powershell> | launch <exe> [args]"
    exit 2
    ;;
esac

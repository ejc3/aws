#!/usr/bin/env bash
#
# windows-pc-bootstrap -- the first step for a new Windows PC, on fcvm-arm (as ubuntu): print the one script the person
# at the PC pastes (scripts/windows/bootstrap.ps1 with this PC's values), and afterwards authorize the key it prints
# for the reverse tunnel only. Then a jumpbox runs scripts/windows-pc-setup.sh ALIAS --bootstrap-port PORT.
#
#   scripts/windows-pc-bootstrap.sh print ALIAS PORT            the paste, on stdout (publish it behind a Copy button)
#   scripts/windows-pc-bootstrap.sh allow ALIAS PORT 'KEY LINE'  authorize the PC's key: -R 127.0.0.1:PORT only
#
# ALIAS is the PC's row in mac-ssh-client.tf; its key here (~/.ssh/to-ALIAS, made by mac-ssh-client.tf) is the one the
# paste authorizes on the PC. PORT is a port of the PC's block in the port registry (`ports claim <alias>`).
# The PC's key may open that one remote forward and nothing else: no shell (command=/bin/false), no -L or -D
# (permitopen to a closed port: permitopen="none" makes sshd refuse the key outright), no other listener.
set -euo pipefail

usage() { echo "usage: windows-pc-bootstrap.sh print ALIAS PORT | allow ALIAS PORT 'ssh-ed25519 KEY [comment]'" >&2; exit 2; }
ACTION=${1:-}; ALIAS=${2:-}; PORT=${3:-}
[[ $ALIAS =~ ^[a-z][a-z0-9-]{0,30}$ ]] && [[ $PORT =~ ^[0-9]{4,5}$ ]] || usage
HERE=$(cd "$(dirname "$0")" && pwd)

case "$ACTION" in
  print)
    client=$(cut -d' ' -f1-2 "$HOME/.ssh/to-$ALIAS.pub")
    [[ $client =~ ^ssh-ed25519\ AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{48}$ ]] ||
      { echo "no ~/.ssh/to-$ALIAS.pub here (mac-ssh-client.tf makes it)" >&2; exit 1; }
    ip=$(curl -fsS -m 5 -H "X-aws-ec2-metadata-token: $(curl -fsS -m 5 -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' http://169.254.169.254/latest/api/token)" \
      http://169.254.169.254/latest/meta-data/public-ipv4)
    hostkey=$(cut -d' ' -f1-2 /etc/ssh/ssh_host_ed25519_key.pub)
    sed -e "s|@@BOX@@|$(id -un)@$ip|" -e "s|@@BOX_HOST_KEY_LINE@@|$ip $hostkey|" \
      -e "s|@@CLIENT_KEY@@|$client fcvm-arm-to-$ALIAS|" -e "s|@@PORT@@|$PORT|" "$HERE/windows/bootstrap.ps1"
    ;;
  allow)
    raw=${4:-}
    case "$raw" in *$'\n'* | *$'\r'*) usage ;; esac
    key=$(printf '%s' "$raw" | cut -d' ' -f1-2)
    [[ $key =~ ^ssh-ed25519\ AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{48}$ ]] || { echo "not one ssh-ed25519 public key" >&2; exit 1; }
    line="restrict,port-forwarding,permitlisten=\"127.0.0.1:$PORT\",permitopen=\"127.0.0.1:1\",command=\"/bin/false\" $key $ALIAS-bootstrap-tunnel"
    ak="$HOME/.ssh/authorized_keys"
    if grep -qF " $ALIAS-bootstrap-tunnel" "$ak" 2>/dev/null; then
      # grep exits 1 when that was the only line; an empty file is the right result then.
      { grep -vF " $ALIAS-bootstrap-tunnel" "$ak" || true; } > "$ak.new"
    else
      cp "$ak" "$ak.new" 2>/dev/null || : > "$ak.new"
    fi
    printf '%s\n' "$line" >> "$ak.new" && chmod 600 "$ak.new" && mv -f "$ak.new" "$ak"
    echo "authorized $ALIAS's key for -R 127.0.0.1:$PORT only; the tunnel connects within seconds"
    ;;
  *) usage ;;
esac

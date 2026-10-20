#!/usr/bin/env bash
#
# windows-pc-bootstrap -- the first step for a new Windows PC, on fcvm-arm (as ubuntu): print the one script the person
# at the PC pastes (scripts/windows/bootstrap.ps1 with this PC's values), and afterwards authorize the key it prints
# for the reverse tunnel only. Then a jumpbox runs scripts/windows-pc-setup.sh ALIAS --bootstrap-port PORT.
#
#   scripts/windows-pc-bootstrap.sh print ALIAS PORT            the paste, on stdout (publish it behind a Copy button)
#   scripts/windows-pc-bootstrap.sh allow ALIAS PORT 'KEY LINE'  authorize the PC's key: -R 127.0.0.1:PORT only
#   scripts/windows-pc-bootstrap.sh retire ALIAS                remove the PC's key once its Cloudflare route works
#
# The tunnel ends on `pcboot`, never on ubuntu or any account an agent runs as (AGENTS.md, "Machines outside AWS"):
# a system account with no shell whose keys live in /etc/ssh/pcboot/authorized_keys (root-owned; this script writes it
# with sudo), and whose sshd Match block refuses Unix-socket and local forwards, agent, X11 and a TTY
# (mac-ssh-client.tf, pc_bootstrap_account). The forward lands on fcvm-arm's loopback, where setup reaches it as ubuntu.
#
# ALIAS is the PC's row in mac-ssh-client.tf; its key here (~/.ssh/to-ALIAS, made by mac-ssh-client.tf) is the one the
# paste authorizes on the PC. PORT is a port of the PC's block in the port registry (`ports claim <alias>`).
# The PC's key may open that one remote TCP forward: no shell (command=/bin/false), no -L or -D (permitopen to a closed
# port: permitopen="none" makes sshd refuse the key outright), no other TCP listener. OpenSSH has no per-key option
# against remote Unix-socket forwards (-R /path:...), which `port-forwarding` re-enables, so the key also expires two
# days after it is allowed (expiry-time): the tunnel is for the first setup only, and is retired after it.
set -euo pipefail

usage() { echo "usage: windows-pc-bootstrap.sh print ALIAS PORT | allow ALIAS PORT 'ssh-ed25519 KEY [comment]' | retire ALIAS" >&2; exit 2; }
ACTION=${1:-}; ALIAS=${2:-}; PORT=${3:-}
[[ $ALIAS =~ ^[a-z][a-z0-9-]{0,30}$ ]] || usage
[ "$ACTION" = retire ] || [[ $PORT =~ ^[0-9]{4,5}$ ]] || usage
HERE=$(cd "$(dirname "$0")" && pwd)
KEYS=${PCBOOT_KEYS:-/etc/ssh/pcboot/authorized_keys} # tests point this at a temporary file
SUDO=${PCBOOT_SUDO-sudo}                           # and run without sudo

# Write the pcboot key file without $ALIAS's line, plus $1 when given; never anyone's ~/.ssh/authorized_keys.
write_keys() {
  local tmp; tmp=$(mktemp)
  { [ -r "$KEYS" ] && grep -vF " $ALIAS-bootstrap-tunnel" "$KEYS"; } > "$tmp" || true
  [ -z "${1:-}" ] || printf '%s\n' "$1" >> "$tmp"
  $SUDO install -m 0644 "$tmp" "$KEYS"
  rm -f "$tmp"
}

case "$ACTION" in
  print)
    client=$(cut -d' ' -f1-2 "$HOME/.ssh/to-$ALIAS.pub")
    [[ $client =~ ^ssh-ed25519\ AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{48}$ ]] ||
      { echo "no ~/.ssh/to-$ALIAS.pub here (mac-ssh-client.tf makes it)" >&2; exit 1; }
    ip=${FCVM_HOST:-184.72.40.255} # fcvm-metal-arm's Elastic IP, as in the other scripts
    hostkey=$(cut -d' ' -f1-2 /etc/ssh/ssh_host_ed25519_key.pub)
    sed -e "s|@@BOX@@|pcboot@$ip|" -e "s|@@BOX_HOST_KEY_LINE@@|$ip $hostkey|" \
      -e "s|@@CLIENT_KEY@@|$client fcvm-arm-to-$ALIAS|" -e "s|@@PORT@@|$PORT|" "$HERE/windows/bootstrap.ps1"
    ;;
  allow)
    raw=${4:-}
    case "$raw" in *$'\n'* | *$'\r'*) usage ;; esac
    key=$(printf '%s' "$raw" | cut -d' ' -f1-2)
    [[ $key =~ ^ssh-ed25519\ AAAAC3NzaC1lZDI1NTE5[A-Za-z0-9+/]{48}$ ]] || { echo "not one ssh-ed25519 public key" >&2; exit 1; }
    expiry=$(date -d '+2 days' +%Y%m%d%H%M)
    line="restrict,port-forwarding,permitlisten=\"127.0.0.1:$PORT\",permitopen=\"127.0.0.1:1\",command=\"/bin/false\",expiry-time=\"$expiry\" $key $ALIAS-bootstrap-tunnel"
    [ -e "$KEYS" ] || { echo "no $KEYS here: the pcboot account comes from mac-ssh-client.tf (pc_bootstrap_account)" >&2; exit 1; }
    write_keys "$line"
    echo "authorized $ALIAS's key for pcboot, -R 127.0.0.1:$PORT only, until $expiry; the tunnel connects within seconds"
    ;;
  retire)
    write_keys
    echo "removed $ALIAS's key from pcboot; on the PC, Unregister-ScheduledTask 'devbox tunnel' (over ssh $ALIAS)"
    ;;
  *) usage ;;
esac

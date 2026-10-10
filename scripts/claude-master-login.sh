#!/usr/bin/env bash
#
# claude-master-login -- log the Claude subscriptions into claude-master on fcvm-metal-arm.
#
#   scripts/claude-master-login.sh                        the three: connor, colton, ejc3 (on fcvm-metal-arm)
#   scripts/claude-master-login.sh claude-ejc3 ...        only those profiles
#   scripts/claude-master-login.sh --server [PROFILE...]  the same, on the shared claude-master server
#                                                         (claude-master-server.tf), then starts its service
#
# One profile at a time: it prints a claude.ai link, you open it in a browser signed in to THAT
# subscription's account (incognito if you are signed in to another), approve, and paste the
# CODE#STATE string Claude shows. A profile that already has a login is skipped, so it is safe to
# re-run after a failure. It checks that a login EXISTS, not that it is still valid; claude-master
# refreshes tokens itself, and `claude-master probe NAME --model MODEL` is the live check. Each profile is its own OAuth login on this box; nothing is copied from
# the native Claude login or between profiles.
set -euo pipefail

HOST=${FCVM_HOST:-184.72.40.255}   # fcvm-metal-arm's Elastic IP
KEY=${FCVM_KEY:-$HOME/.ssh/fcvm-ec2}
SERVER=0
if [ "${1:-}" = "--server" ]; then
  SERVER=1; shift
  HOST=${CLAUDE_MASTER_SERVER_HOST:-10.0.1.50}   # claude-master-server.tf: a fixed private address
fi

profiles=("$@")
[ ${#profiles[@]} -gt 0 ] || profiles=(claude-connor claude-colton claude-colin claude-ejc3)

for p in "${profiles[@]}"; do
  [[ $p =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]] || { echo "bad profile name: $p" >&2; exit 2; }
  if [ "$SERVER" = 1 ]; then
    # The server's logins belong to its own service account; `claude-master-login` runs as it.
    ssh -t -i "$KEY" "ubuntu@$HOST" "
      if sudo claude-master-status | grep -qx 'login $p: present'; then
        echo '$p: already has a login (skipped)'
      else
        printf '\n== $p: sign in to THAT subscription account, approve, paste the code ==\n'
        sudo claude-master-login $p
      fi"
    continue
  fi
  ssh -t -i "$KEY" "ubuntu@$HOST" "
    export PATH=\$HOME/.local/bin:\$PATH
    tmux -L cmlogin kill-server 2>/dev/null
    if [ -d \$HOME/.local/share/claude-master/profiles/$p/current ]; then
      echo '$p: already has a login (skipped). To replace a revoked one, log in under a NEW name; claude-master login refuses an existing profile.'
    else
      printf '\n== $p: sign in to THAT subscription account, approve, paste the code ==\n'
      claude-master login $p
    fi"
done
if [ "$SERVER" = 1 ]; then
  # Starting a server that is not running is safe; restarting a running one is the owner's call.
  ssh -t -i "$KEY" "ubuntu@$HOST" "sudo claude-master-status; if ! sudo claude-master-status | grep -q MISSING && ! sudo claude-master-status | grep -q '^server: active'; then sudo claude-master-rollout && sudo claude-master-status; fi"
fi
echo "done: $(printf '%s ' "${profiles[@]}")"

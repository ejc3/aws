#!/bin/bash
# SSM document claude-master-cert-request: on the CLIENT box, as the account, make a NEW key and certificate request in
# ~/.config/claude-master.new (the live ~/.config/claude-master is not touched) and print the request.
# The key is made here and never leaves this machine. Output: CSR=<base64, one line>.
set -u
ACCOUNT='{{ Account }}'
NAME='{{ Name }}'
CM_BIN=${CM_BIN:-/usr/local/bin/claude-master}
HOME_DIR=${CM_HOME_OVERRIDE:-$(getent passwd "$ACCOUNT" | cut -d: -f6)}
[ -n "$HOME_DIR" ] && [ -d "$HOME_DIR" ] || { echo "REQUEST=no-such-account"; exit 1; }
[ -x "$CM_BIN" ] || { echo "REQUEST=no-client-binary"; exit 1; }
NEW="$HOME_DIR/.config/claude-master.new"
as_account() {
  if [ "$(id -u)" = 0 ]; then runuser -u "$ACCOUNT" -- env HOME="$HOME_DIR" "$@"; else env HOME="$HOME_DIR" "$@"; fi
}
as_account rm -rf "$NEW"
as_account "$CM_BIN" client-init --dir "$NEW" --name "$NAME" >/dev/null 2>&1 || { echo "REQUEST=client-init-failed"; exit 1; }
[ -s "$NEW/client.csr" ] && [ -s "$NEW/client.key" ] || { echo "REQUEST=incomplete"; exit 1; }
echo "CSR=$(base64 -w0 < "$NEW/client.csr")"

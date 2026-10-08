#!/bin/bash
# SSM document claude-master-cert-install: on the CLIENT box, install a renewed certificate. Nothing replaces the live
# directory until the new certificate has been checked: it chains to the server's CA, names this client, matches the key made
# by the request step, and lasts at least MinDays. Then ~/.config/claude-master.new becomes ~/.config/claude-master in two
# renames, so a failure leaves the old identity in place and working.
set -u
ACCOUNT='{{ Account }}'
NAME='{{ Name }}'
CERT_B64='{{ Cert }}'
CA_B64='{{ Ca }}'
MIN_DAYS='{{ MinDays }}'
HOME_DIR=${CM_HOME_OVERRIDE:-$(getent passwd "$ACCOUNT" | cut -d: -f6)}
[ -n "$HOME_DIR" ] && [ -d "$HOME_DIR" ] || { echo "INSTALL=no-such-account"; exit 1; }
DIR="$HOME_DIR/.config/claude-master"; NEW="$DIR.new"; OLD="$DIR.old"
[ -s "$NEW/client.key" ] && [ -s "$NEW/client.csr" ] || { echo "INSTALL=no-pending-request"; exit 1; }
WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
printf '%s' "$CERT_B64" | base64 -d > "$WORK/client.pem" 2>/dev/null && printf '%s' "$CA_B64" | base64 -d > "$WORK/ca.pem" 2>/dev/null \
  || { echo "INSTALL=bad-encoding"; exit 1; }
openssl verify -CAfile "$WORK/ca.pem" "$WORK/client.pem" >/dev/null 2>&1 || { echo "INSTALL=not-signed-by-the-ca"; exit 1; }
CN=$(openssl x509 -noout -subject -nameopt RFC2253 -in "$WORK/client.pem" 2>/dev/null | sed -n 's/^subject=//p' | sed -n 's/^CN=\([^,]*\)$/\1/p')
[ "$CN" = "$NAME" ] || { echo "INSTALL=wrong-name (got $CN)"; exit 1; }
[ "$(openssl x509 -pubkey -noout -in "$WORK/client.pem" 2>/dev/null)" = "$(openssl pkey -pubout -in "$NEW/client.key" 2>/dev/null)" ] \
  || { echo "INSTALL=key-mismatch"; exit 1; }
openssl x509 -checkend $((MIN_DAYS * 86400)) -noout -in "$WORK/client.pem" >/dev/null 2>&1 || { echo "INSTALL=expires-too-soon"; exit 1; }
install -m 0644 "$WORK/client.pem" "$NEW/client.pem" && install -m 0644 "$WORK/ca.pem" "$NEW/ca.pem" || { echo "INSTALL=write-failed"; exit 1; }
if [ "$(id -u)" = 0 ]; then chown "$ACCOUNT": "$NEW/client.pem" "$NEW/ca.pem" || { echo "INSTALL=chown-failed"; exit 1; }; fi
rm -rf "$OLD"
if [ -d "$DIR" ]; then mv "$DIR" "$OLD" || { echo "INSTALL=swap-failed"; exit 1; }; fi
if ! mv "$NEW" "$DIR"; then [ -d "$OLD" ] && mv "$OLD" "$DIR"; echo "INSTALL=swap-failed"; exit 1; fi
rm -rf "$OLD"
echo "INSTALL=ok"
echo "NOTAFTER=$(date -u -d "$(openssl x509 -noout -enddate -in "$DIR/client.pem" | cut -d= -f2)" +%s)"

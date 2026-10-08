#!/bin/bash
# SSM document claude-master-cert-status: read-only. Prints how long an account's claude-master client certificate lasts.
# Output (one KEY=value per line): STATUS=ok|missing, NOTAFTER=<epoch seconds>, SUBJECT=<subject>.
set -u
ACCOUNT='{{ Account }}'
HOME_DIR=${CM_HOME_OVERRIDE:-$(getent passwd "$ACCOUNT" | cut -d: -f6)}
[ -n "$HOME_DIR" ] && [ -d "$HOME_DIR" ] || { echo "STATUS=no-such-account"; exit 1; }
CERT="$HOME_DIR/.config/claude-master/client.pem"
if [ ! -r "$CERT" ]; then echo "STATUS=missing"; exit 0; fi
END=$(openssl x509 -noout -enddate -in "$CERT" 2>/dev/null | cut -d= -f2)
[ -n "$END" ] || { echo "STATUS=unreadable"; exit 1; }
echo "STATUS=ok"
echo "NOTAFTER=$(date -u -d "$END" +%s)"
echo "SUBJECT=$(openssl x509 -noout -subject -nameopt RFC2253 -in "$CERT" | sed 's/^subject=//')"

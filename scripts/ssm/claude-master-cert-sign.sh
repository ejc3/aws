#!/bin/bash
# SSM document claude-master-cert-sign: on the SERVER, sign a client's certificate request, but only for the name the caller
# asked for. The request's own CN must equal Name exactly, so this cannot be used to mint a certificate for any other client.
# Output: the client certificate PEM, then the CA certificate PEM (what claude-master-sign prints).
set -u
NAME='{{ Name }}'
CSR='{{ Csr }}'
DAYS='{{ Days }}'
CM_SIGN=${CM_SIGN:-/usr/local/bin/claude-master-sign}
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
[ "$DAYS" -ge 1 ] 2>/dev/null && [ "$DAYS" -le 90 ] || { echo "SIGN=bad-days" >&2; exit 1; }
printf '%s' "$CSR" | base64 -d > "$TMP/request.csr" 2>/dev/null || { echo "SIGN=bad-request-encoding" >&2; exit 1; }
openssl req -in "$TMP/request.csr" -noout -verify >/dev/null 2>&1 || { echo "SIGN=request-signature-invalid" >&2; exit 1; }
CN=$(openssl req -in "$TMP/request.csr" -noout -subject -nameopt RFC2253 2>/dev/null | sed -n 's/^subject=//p' | sed -n 's/^CN=\([^,]*\)$/\1/p')
[ "$CN" = "$NAME" ] || { echo "SIGN=name-mismatch (asked for $NAME)" >&2; exit 1; }
printf '%s' "$CSR" | "$CM_SIGN" "$DAYS"

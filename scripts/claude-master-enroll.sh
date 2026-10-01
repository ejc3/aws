#!/usr/bin/env bash
#
# claude-master-enroll -- give a machine a client certificate for the shared claude-master server.
#
#   scripts/claude-master-enroll.sh NAME                 enroll THIS machine (a Mac, a jumpbox)
#   scripts/claude-master-enroll.sh NAME --ssh HOST      enroll another box over SSH (ubuntu@HOST)
#   ... --days N                                         certificate lifetime, 1-90 (default 30)
#
# NAME is the identity the server sees: lowercase letters, digits and hyphens, e.g. box-fcvm-arm,
# dolphin, mac-eric. Run it again before the certificate expires: it makes a NEW key each time and
# swaps the directory only after the new certificate is in place, so a failure leaves the old one
# working.
#
# WHO MAY RUN IT: anyone with AWS access to the server (an administrator). Signing happens on the
# server box through SSM Run Command, so issuing a certificate is an AWS-authenticated action and
# the CA key never leaves that box. The machine being enrolled needs the `claude-master` binary
# (github.com/ejc3/CLIProxyAPI releases) and nothing from AWS.
#
# The key is made ON the enrolled machine and never travels; only the certificate request goes up
# and the signed certificate and the server's public CA certificate come back.
set -euo pipefail

REGION=${AWS_REGION:-us-west-1}
DIR=${CLAUDE_MASTER_CLIENT_DIR:-.config/claude-master}   # relative to the enrolled user's home
DAYS=30
NAME=${1:-}; shift || true
HOST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ssh) HOST=${2:-}; shift 2 ;;
    --days) DAYS=${2:-}; shift 2 ;;
    *) echo "usage: claude-master-enroll.sh NAME [--ssh HOST] [--days N]" >&2; exit 2 ;;
  esac
done
[[ $NAME =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || { echo "NAME must be 1-63 lowercase letters, digits or hyphens" >&2; exit 2; }
[[ $DAYS =~ ^[0-9]+$ ]] && [ "$DAYS" -ge 1 ] && [ "$DAYS" -le 90 ] || { echo "--days must be 1-90" >&2; exit 2; }

# Run a command on the machine being enrolled.
on_target() {
  if [ -n "$HOST" ]; then ssh -o BatchMode=yes -i "${FCVM_KEY:-$HOME/.ssh/fcvm-ec2}" "ubuntu@$HOST" "$@"; else bash -c "$*"; fi
}

SERVER_ID=$(aws ec2 describe-instances --region "$REGION" \
  --filters Name=tag:Name,Values=claude-master-server Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
[ -n "$SERVER_ID" ] && [ "$SERVER_ID" != None ] || { echo "no running claude-master-server instance" >&2; exit 1; }

NEW="$DIR.new"
echo "1/4 making a key and a certificate request on ${HOST:-this machine}"
on_target "set -e; cd \"\$HOME\"; command -v claude-master >/dev/null || { echo 'claude-master is not installed here' >&2; exit 1; }; rm -rf '$NEW'; claude-master client-init --dir '$NEW' --name '$NAME' >/dev/null"
CSR=$(on_target "base64 -w0 \"\$HOME/$NEW/client.csr\"")

echo "2/4 asking the server ($SERVER_ID) to sign it for $DAYS days"
CMD_ID=$(aws ssm send-command --region "$REGION" --instance-ids "$SERVER_ID" --document-name AWS-RunShellScript \
  --parameters "commands=[\"echo $CSR | /usr/local/bin/claude-master-sign $DAYS\"]" --query Command.CommandId --output text)
aws ssm wait command-executed --region "$REGION" --command-id "$CMD_ID" --instance-id "$SERVER_ID"
STATUS=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$SERVER_ID" --query Status --output text)
[ "$STATUS" = Success ] || { echo "signing failed ($STATUS):" >&2; aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$SERVER_ID" --query StandardErrorContent --output text >&2; exit 1; }
OUT=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$SERVER_ID" --query StandardOutputContent --output text)

# The output is the client certificate, then the CA certificate.
CLIENT_PEM=$(printf '%s\n' "$OUT" | awk '/BEGIN CERTIFICATE/{n++} n==1')
CA_PEM=$(printf '%s\n' "$OUT" | awk '/BEGIN CERTIFICATE/{n++} n==2')
[ -n "$CLIENT_PEM" ] && [ -n "$CA_PEM" ] || { echo "the server did not return both certificates" >&2; exit 1; }

echo "3/4 installing the certificate and the server's CA"
printf '%s\n' "$CLIENT_PEM" | on_target "cat > \"\$HOME/$NEW/client.pem\" && chmod 644 \"\$HOME/$NEW/client.pem\""
printf '%s\n' "$CA_PEM" | on_target "cat > \"\$HOME/$NEW/ca.pem\" && chmod 644 \"\$HOME/$NEW/ca.pem\""

echo "4/4 switching to the new identity"
on_target "set -e; cd \"\$HOME\"; rm -rf '$DIR.old'; [ -d '$DIR' ] && mv '$DIR' '$DIR.old'; mv '$NEW' '$DIR'; rm -rf '$DIR.old'; openssl x509 -noout -subject -enddate -in '$DIR/client.pem'"
echo "done. Connect with: claude-master connect --server $(terraform -chdir="$(dirname "$0")/.." output -raw claude_master_server_address 2>/dev/null || echo 10.0.1.50:8443) --dir ~/$DIR -- ..."

#!/usr/bin/env bash
#
# claude-master-mac-bundle -- make the small file a Mac needs to use the shared claude-master server
# through the Cloudflare tunnel (claude-master-tunnel.tf). Run it as an AWS administrator; hand the
# result to the person whose Mac it is, privately. It holds a long-lived Cloudflare Access service
# token, so treat it like a password.
#
#   scripts/claude-master-mac-bundle.sh OUTDIR
#
# Writes OUTDIR/ (mode 0700): cloudflare.env (the token, 0600), ca.pem (the server's PUBLIC CA
# certificate) and README.txt with the two commands to run. The Mac needs `cloudflared` (brew install
# cloudflared) and the `claude-master` binary (github.com/ejc3/CLIProxyAPI releases); no AWS access
# and no client certificate.
set -euo pipefail

REGION=${AWS_REGION:-us-west-1}
OUT=${1:-}
[ -n "$OUT" ] || { echo "usage: claude-master-mac-bundle.sh OUTDIR" >&2; exit 2; }
[ ! -e "$OUT" ] || { echo "$OUT already exists; choose a new directory" >&2; exit 1; }

{ set +x; } 2>/dev/null   # the service token below must never reach a trace
SECRET=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id claude-master/mac-access --query SecretString --output text)
SERVER_ID=$(aws ec2 describe-instances --region "$REGION" \
  --filters Name=tag:Name,Values=claude-master-server Name=instance-state-name,Values=running \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)
[ -n "$SERVER_ID" ] && [ "$SERVER_ID" != None ] || { echo "no running claude-master-server instance" >&2; exit 1; }

# READINESS GATE. The tunnel can be live while the server is stopped, or still running from before
# the open listener existed (a running server keeps its old configuration, and nothing restarts it
# for you). A bundle made then would be dead on arrival, so refuse until the listener is up.
READY_ID=$(aws ssm send-command --region "$REGION" --instance-ids "$SERVER_ID" --document-name AWS-RunShellScript \
  --parameters 'commands=["claude-master-status"]' --query Command.CommandId --output text)
aws ssm wait command-executed --region "$REGION" --command-id "$READY_ID" --instance-id "$SERVER_ID"
STATUS_OUT=$(aws ssm get-command-invocation --region "$REGION" --command-id "$READY_ID" --instance-id "$SERVER_ID" --query StandardOutputContent --output text)
if ! printf '%s\n' "$STATUS_OUT" | grep -qx 'open listener: listening'; then
  echo "refusing to make a bundle: the server's open listener is not up." >&2
  printf '%s\n' "$STATUS_OUT" >&2
  exit 1
fi

# The CA certificate is public; fetch it from the server over SSM.
CMD_ID=$(aws ssm send-command --region "$REGION" --instance-ids "$SERVER_ID" --document-name AWS-RunShellScript \
  --parameters 'commands=["cat /var/lib/claude-master/state/ca.pem"]' --query Command.CommandId --output text)
aws ssm wait command-executed --region "$REGION" --command-id "$CMD_ID" --instance-id "$SERVER_ID"
CA=$(aws ssm get-command-invocation --region "$REGION" --command-id "$CMD_ID" --instance-id "$SERVER_ID" --query StandardOutputContent --output text)
printf '%s\n' "$CA" | grep -q 'BEGIN CERTIFICATE' || { echo "the server did not return its CA certificate (has it started once?)" >&2; exit 1; }

umask 077
mkdir -p "$OUT"
HOSTNAME_=$(printf '%s' "$SECRET" | python3 -c 'import json,sys; print(json.load(sys.stdin)["hostname"])')
PORT=$(printf '%s' "$SECRET" | python3 -c 'import json,sys; print(json.load(sys.stdin)["local_port"])')
printf '%s' "$SECRET" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("TUNNEL_SERVICE_TOKEN_ID=" + d["client_id"])
print("TUNNEL_SERVICE_TOKEN_SECRET=" + d["client_secret"])
print("CLAUDE_MASTER_TUNNEL_HOSTNAME=" + d["hostname"])
print("CLAUDE_MASTER_TUNNEL_PORT=%s" % d["local_port"])' > "$OUT/cloudflare.env"
chmod 0600 "$OUT/cloudflare.env"
printf '%s\n' "$CA" > "$OUT/ca.pem"; chmod 0644 "$OUT/ca.pem"
cat > "$OUT/README.txt" <<TXT
Use the shared Claude subscriptions from this Mac (nothing here is a login).

One-time:  brew install cloudflared      and install claude-master (github.com/ejc3/CLIProxyAPI releases)
           mkdir -p ~/.config/claude-master && cp cloudflare.env ca.pem ~/.config/claude-master/ && chmod 600 ~/.config/claude-master/cloudflare.env

Each time, in one terminal (leave it running):
  set -a; . ~/.config/claude-master/cloudflare.env; set +a
  cloudflared access tcp --hostname "\$CLAUDE_MASTER_TUNNEL_HOSTNAME" --url 127.0.0.1:\$CLAUDE_MASTER_TUNNEL_PORT

In another terminal:
  claude-master connect --open 127.0.0.1:$PORT --ca ~/.config/claude-master/ca.pem -- --remote-control

You still sign in to Claude Code with YOUR OWN account as usual; only inference is shared.
TXT
echo "wrote $OUT (hostname $HOSTNAME_). cloudflare.env is a secret: hand it over privately."

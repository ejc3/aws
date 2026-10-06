#!/bin/bash
# Install the Workers deploy token as the CLOUDFLARE_API_TOKEN Actions secret of a site repo.
#
#   scripts/workers-deploy-secret.sh OWNER/REPO            as the logged-in gh user (needs admin on the repo)
#   scripts/workers-deploy-secret.sh OWNER/REPO colton     as that account on nextjs-dev (its own gh login)
#
# The value is read from Secrets Manager and piped to `gh secret set` on stdin; it never appears
# on a command line, in a file, or in this script's output. With an account name the pipe goes
# through ssh to nextjs-dev and that account's own existing gh login: nothing is copied off it.
set -euo pipefail
{ set +x; } 2>/dev/null

REPO=${1:?usage: workers-deploy-secret.sh OWNER/REPO [nextjs-dev-account]}
AS=${2:-}
[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || { echo "not an OWNER/REPO: $REPO" >&2; exit 2; }
[[ -z "$AS" || "$AS" =~ ^[a-z][a-z0-9_-]*$ ]] || { echo "not an account name: $AS" >&2; exit 2; }

TOKEN=$(aws secretsmanager get-secret-value --region us-west-1 --secret-id cloudflare-workers-deploy-token --query SecretString --output text)
[ "${#TOKEN}" -ge 30 ] || { echo "the stored token looks empty; run scripts/workers-deploy-token.sh first" >&2; exit 1; }

if [ -z "$AS" ]; then
  printf '%s' "$TOKEN" | gh secret set CLOUDFLARE_API_TOKEN --repo "$REPO"
else
  HOST=$(aws ec2 describe-instances --region us-west-1 --filters Name=tag:Name,Values=nextjs-dev Name=instance-state-name,Values=running \
    --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
  [ -n "$HOST" ] && [ "$HOST" != None ] || { echo "nextjs-dev is not running" >&2; exit 1; }
  printf '%s' "$TOKEN" | ssh -i ~/.ssh/fcvm-ec2 -o BatchMode=yes -o ConnectTimeout=8 "ubuntu@$HOST" \
    "sudo -n -u $AS gh secret set CLOUDFLARE_API_TOKEN --repo $REPO"
fi
unset TOKEN

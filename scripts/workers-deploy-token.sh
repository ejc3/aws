#!/bin/bash
# Mint (or rotate) the Cloudflare Workers deploy token and store it in Secrets Manager.
#
#   scripts/workers-deploy-token.sh
#
# The token is created by the account-owned `cloudflare-account-token` with exactly two
# permissions on this one account: Workers Scripts Write and Account Settings Read. It is not
# IP-pinned (GitHub's runners have no stable address). The previous token with the same name is
# revoked only AFTER the new one is stored and verified, so a failure leaves the old one working.
# Nothing secret is ever on a command line: each token travels on a pipe or curl's stdin.
set -euo pipefail
{ set +x; } 2>/dev/null

ACCOUNT_ID=12ea67fb7ced068de03f35c22688e436
REGION=us-west-1
SECRET_ID=cloudflare-workers-deploy-token
NAME=workers-deploy
# Permission group ids (GET /accounts/$ACCOUNT_ID/tokens/permission_groups).
WORKERS_SCRIPTS_WRITE=e086da7e2179491d91ee5f35b3ca210a
ACCOUNT_SETTINGS_READ=c1fde68c7bcc44588cbb6ddbc16d6480
API=https://api.cloudflare.com/client/v4/accounts/$ACCOUNT_ID

# curl -4: the account token is pinned to the jumpboxes' IPv4 addresses. The header goes in on
# stdin so the token is never in argv.
cf() { # cf TOKEN METHOD PATH [JSON]
  local token=$1 method=$2 path=$3 body=${4:-}
  if [ -n "$body" ]; then
    printf 'Authorization: Bearer %s\n' "$token" | curl -4 -sS -X "$method" -H @- -H 'Content-Type: application/json' -d "$body" "$API$path"
  else
    printf 'Authorization: Bearer %s\n' "$token" | curl -4 -sS -X "$method" -H @- "$API$path"
  fi
}

MINT=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id cloudflare-account-token --query SecretString --output text)

BODY=$(printf '{"name":"%s","policies":[{"effect":"allow","resources":{"com.cloudflare.api.account.%s":"*"},"permission_groups":[{"id":"%s"},{"id":"%s"}]}]}' \
  "$NAME" "$ACCOUNT_ID" "$WORKERS_SCRIPTS_WRITE" "$ACCOUNT_SETTINGS_READ")

RESPONSE=$(cf "$MINT" POST /tokens "$BODY")
NEW_ID=$(printf '%s' "$RESPONSE" | python3 -c 'import json,sys; r=json.load(sys.stdin); sys.exit("mint failed: %s" % r.get("errors")) if not r.get("success") else print(r["result"]["id"])')

# Store the value without it touching a command line or a file.
printf '%s' "$RESPONSE" | python3 -c 'import json,sys; sys.stdout.write(json.load(sys.stdin)["result"]["value"])' \
  | aws secretsmanager put-secret-value --region "$REGION" --secret-id "$SECRET_ID" --secret-string file:///dev/stdin \
      --query '[Name,VersionId]' --output text | cut -c1-70

# The stored token must verify as itself before anything old is revoked.
STORED=$(aws secretsmanager get-secret-value --region "$REGION" --secret-id "$SECRET_ID" --query SecretString --output text)
VERIFY=$(cf "$STORED" GET /tokens/verify)
printf '%s' "$VERIFY" | python3 -c 'import json,sys; r=json.load(sys.stdin); ok=r.get("success") and r["result"]["status"]=="active"; sys.exit(0 if ok else "stored token does not verify: %s" % r.get("errors"))'
echo "stored token: ${#STORED} chars, id ${NEW_ID:0:8}..., verifies active"
unset STORED

# Revoke every OTHER token with this name.
cf "$MINT" GET /tokens | python3 -c '
import json,sys
new=sys.argv[1]
for t in json.load(sys.stdin).get("result", []):
    if t["name"] == sys.argv[2] and t["id"] != new:
        print(t["id"])' "$NEW_ID" "$NAME" | while read -r OLD; do
  cf "$MINT" DELETE "/tokens/$OLD" > /dev/null && echo "revoked previous token ${OLD:0:8}..."
done
unset MINT

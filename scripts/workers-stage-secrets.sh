#!/bin/bash
# Load a staging Worker's runtime secrets from AWS into Cloudflare.
#
#   scripts/workers-stage-secrets.sh SITE          e.g. imagine -> reads workers-stage/imagine, loads Worker imagine-stage
#
# Source of truth: the Secrets Manager container workers-stage/SITE (JSON of variable name -> value; see
# workers-stage-secrets.tf). Dolphin's sites (dolphin-labs, dolphin-films, dolphin-maps) keep theirs in the Dolphin account
# (dolphin-labs-hq/dolphin-aws, secrets.tf), read through OrganizationAccountAccessRole from this account's
# administration; every other site's container is in this account. It is piped to `wrangler secret bulk`, so no value is on a command line, in a file or in this
# script's output; only names are printed. Run it AFTER the site's own deploy has finished: a secret change made while a
# deploy is uploading can be lost when that deploy activates.
set -euo pipefail
{ set +x; } 2>/dev/null

SITE=${1:?usage: workers-stage-secrets.sh SITE}
[[ "$SITE" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "not a site name: $SITE" >&2; exit 2; }
WORKER="$SITE-stage"
ACCOUNT_ID=12ea67fb7ced068de03f35c22688e436

DOLPHIN_ACCOUNT_ID=484888470752
case "$SITE" in
  dolphin-labs | dolphin-films | dolphin-maps) SOURCE_ACCOUNT=$DOLPHIN_ACCOUNT_ID ;;
  *) SOURCE_ACCOUNT="" ;;
esac

read_container() {
  if [[ -z "$SOURCE_ACCOUNT" ]]; then
    aws secretsmanager get-secret-value --region us-west-1 --secret-id "workers-stage/$SITE" --query SecretString --output text
    return
  fi
  # A one-hour session in the Dolphin account, held only in a subshell's environment (never on a command line or on
  # disk), used for this one read. It fails closed: with no complete session there is no read at all, because the
  # caller's own credentials would read this account's copy, which is not the source any more.
  local key secret token
  read -r key secret token < <(aws sts assume-role --role-arn "arn:aws:iam::$SOURCE_ACCOUNT:role/OrganizationAccountAccessRole" \
    --role-session-name "workers-stage-secrets-$(id -un)" \
    --query 'Credentials.[AccessKeyId,SecretAccessKey,SessionToken]' --output text) || true
  if [[ -z "${key:-}" || -z "${secret:-}" || -z "${token:-}" || "$key" == None || "$token" == None ]]; then
    echo "no session in account $SOURCE_ACCOUNT (assume-role of OrganizationAccountAccessRole failed)" >&2
    return 1
  fi
  (
    export AWS_ACCESS_KEY_ID="$key" AWS_SECRET_ACCESS_KEY="$secret" AWS_SESSION_TOKEN="$token"
    # Either profile variable makes the CLI ignore the session in the environment.
    unset AWS_PROFILE AWS_DEFAULT_PROFILE
    exec aws secretsmanager get-secret-value --region us-west-1 --secret-id "workers-stage/$SITE" --query SecretString --output text
  )
}

# AWS's error text names the cause (AccessDenied, ExpiredToken, ResourceNotFound) and carries no secret; show it.
ERR=$(mktemp)
trap 'rm -f "$ERR"' EXIT
if ! JSON=$(read_container 2>"$ERR"); then
  echo "could not read workers-stage/$SITE in account ${SOURCE_ACCOUNT:-this one}; if it has no value yet, put one first (see workers-stage-secrets.tf):" >&2
  sed 's/^/  /' "$ERR" >&2
  exit 1
fi

# Refuse anything that is not a plain {NAME: non-empty string} object, or that is Vercel's placeholder for a hidden value.
printf '%s' "$JSON" | python3 -c '
import json, re, sys
d = json.loads(sys.stdin.read())
assert isinstance(d, dict) and d, "empty or not an object"
for k, v in d.items():
    assert re.fullmatch(r"[A-Z][A-Z0-9_]*", k), "bad variable name: %s" % k
    assert isinstance(v, str) and v.strip(), "empty value for %s" % k
    assert not re.fullmatch(r"\[[A-Za-z _]{4,14}\]", v), "%s is a placeholder, not a value" % k
print("workers-stage/%s: loading %d names into %s: %s" % (sys.argv[1], len(d), sys.argv[2], ", ".join(sorted(d))), file=sys.stderr)
' "$SITE" "$WORKER"

export CLOUDFLARE_ACCOUNT_ID=$ACCOUNT_ID
CLOUDFLARE_API_TOKEN=$(aws secretsmanager get-secret-value --region us-west-1 --secret-id cloudflare-workers-deploy-token --query SecretString --output text | tr -d '\n')
export CLOUDFLARE_API_TOKEN
printf '%s' "$JSON" | (cd /tmp && npx --yes wrangler@4.147.0 secret bulk --name "$WORKER" 2>&1 | grep -E "Successfully|Error|✘" | sed 's/^/  wrangler: /')
unset JSON CLOUDFLARE_API_TOKEN

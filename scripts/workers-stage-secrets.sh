#!/bin/bash
# Load a staging Worker's runtime secrets from AWS into Cloudflare.
#
#   scripts/workers-stage-secrets.sh SITE          e.g. imagine -> reads workers-stage/imagine, loads Worker imagine-stage
#
# Source of truth: the Secrets Manager container workers-stage/SITE (JSON of variable name -> value; see
# workers-stage-secrets.tf). Dolphin's sites (dolphin-labs, dolphin-films, dolphin-maps) keep theirs in the Dolphin account
# (dolphin-labs-hq/dolphin-aws, secrets.tf), read through OrganizationAccountAccessRole from this account's
# administration; every other site's container is in this account. A site whose Worker also gets values Terraform makes
# (dolphin-maps: the moderation key, dolphin-maps-moderation.tf) has a second container, workers-stage/SITE/terraform, beside
# it; the two are merged, and a name in both refuses the whole load. The result is piped to `wrangler secret bulk`, so no
# value is on a command line, in a file or in this script's output; only names are printed. Run it AFTER the site's own
# deploy has finished: a secret change made while a deploy is uploading can be lost when that deploy activates.
set -euo pipefail
{ set +x; } 2>/dev/null

SITE=${1:?usage: workers-stage-secrets.sh SITE}
[[ "$SITE" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "not a site name: $SITE" >&2; exit 2; }
WORKER="$SITE-stage"
ACCOUNT_ID=12ea67fb7ced068de03f35c22688e436

DOLPHIN_ACCOUNT_ID=484888470752
MANAGED_CONTAINER="" # set: workers-stage/SITE/terraform exists too (Terraform writes it)
case "$SITE" in
  dolphin-labs | dolphin-films) SOURCE_ACCOUNT=$DOLPHIN_ACCOUNT_ID ;;
  dolphin-maps) SOURCE_ACCOUNT=$DOLPHIN_ACCOUNT_ID MANAGED_CONTAINER=1 ;;
  *) SOURCE_ACCOUNT="" ;;
esac

read_container() {
  if [[ -z "$SOURCE_ACCOUNT" ]]; then
    aws secretsmanager get-secret-value --region us-west-1 --secret-id "$1" --query SecretString --output text
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
    exec aws secretsmanager get-secret-value --region us-west-1 --secret-id "$1" --query SecretString --output text
  )
}

# AWS's error text names the cause (AccessDenied, ExpiredToken, ResourceNotFound) and carries no secret; show it.
ERR=$(mktemp)
trap 'rm -f "$ERR"' EXIT
read_or_say() {
  if ! read_container "$1" 2>"$ERR"; then
    echo "could not read $1 in account ${SOURCE_ACCOUNT:-this one}; if it has no value yet, put one first (see workers-stage-secrets.tf; a /terraform container is written by an apply here):" >&2
    sed 's/^/  /' "$ERR" >&2
    return 1
  fi
}
OUT_OF_BAND=$(read_or_say "workers-stage/$SITE") || exit 1
MANAGED=""
if [[ -n "$MANAGED_CONTAINER" ]]; then
  MANAGED=$(read_or_say "workers-stage/$SITE/terraform") || exit 1
fi

# Refuse anything that is not a plain {NAME: non-empty string} object, or that is Vercel's placeholder for a hidden value,
# and a name in both containers; the merged object goes to stdout, names and refusals to stderr.
JSON=$(OUT_OF_BAND="$OUT_OF_BAND" MANAGED="$MANAGED" python3 -c '
import json, os, re, sys
merged = {}
for label in ("OUT_OF_BAND", "MANAGED"):
    if label == "MANAGED" and not os.environ[label]:
        continue
    try:
        d = json.loads(os.environ[label])
    except ValueError:
        sys.exit("%s: not JSON" % label.lower())
    if not isinstance(d, dict) or not d:
        sys.exit("%s: empty or not an object" % label.lower())
    for k, v in d.items():
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", k):
            sys.exit("bad variable name: %s" % k)
        if not isinstance(v, str) or not v.strip():
            sys.exit("empty value for %s" % k)
        if re.fullmatch(r"\[[A-Za-z _]{4,14}\]", v):
            sys.exit("%s is a placeholder, not a value" % k)
        if k in merged:
            sys.exit("%s is in both containers: each name has one owner (workers-stage-secrets.tf)" % k)
        merged[k] = v
if sys.argv[1] == "dolphin-maps":
    required = {"MODERATION_AWS_ACCESS_KEY_ID", "MODERATION_AWS_SECRET_ACCESS_KEY",
                "MODERATION_AWS_REGION", "MODERATION_TEXT_REGION"}
    missing = required.difference(json.loads(os.environ["MANAGED"] or "{}"))
    if missing:
        sys.exit("managed container is missing required names: " + ", ".join(sorted(missing)))
print("workers-stage/%s: loading %d names into %s: %s" % (sys.argv[1], len(merged), sys.argv[2], ", ".join(sorted(merged))), file=sys.stderr)
sys.stdout.write(json.dumps(merged))
' "$SITE" "$WORKER")
unset OUT_OF_BAND MANAGED

export CLOUDFLARE_ACCOUNT_ID=$ACCOUNT_ID
CLOUDFLARE_API_TOKEN=$(aws secretsmanager get-secret-value --region us-west-1 --secret-id cloudflare-workers-deploy-token --query SecretString --output text | tr -d '\n')
export CLOUDFLARE_API_TOKEN
printf '%s' "$JSON" | (cd /tmp && npx --yes wrangler@4.147.0 secret bulk --name "$WORKER" 2>&1 | grep -E "Successfully|Error|✘" | sed 's/^/  wrangler: /')
unset JSON CLOUDFLARE_API_TOKEN

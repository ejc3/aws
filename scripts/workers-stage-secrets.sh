#!/bin/bash
# Load a staging Worker's runtime secrets from AWS into Cloudflare.
#
#   scripts/workers-stage-secrets.sh SITE          e.g. imagine -> reads workers-stage/imagine, loads Worker imagine-stage
#
# Source of truth: the Secrets Manager container workers-stage/SITE (JSON of variable name -> value; see
# workers-stage-secrets.tf). It is piped to `wrangler secret bulk`, so no value is on a command line, in a file or in this
# script's output; only names are printed. Run it AFTER the site's own deploy has finished: a secret change made while a
# deploy is uploading can be lost when that deploy activates.
set -euo pipefail
{ set +x; } 2>/dev/null

SITE=${1:?usage: workers-stage-secrets.sh SITE}
[[ "$SITE" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "not a site name: $SITE" >&2; exit 2; }
WORKER="$SITE-stage"
ACCOUNT_ID=12ea67fb7ced068de03f35c22688e436

JSON=$(aws secretsmanager get-secret-value --region us-west-1 --secret-id "workers-stage/$SITE" --query SecretString --output text 2>/dev/null) \
  || { echo "workers-stage/$SITE has no value yet: put one first (see workers-stage-secrets.tf)" >&2; exit 1; }

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

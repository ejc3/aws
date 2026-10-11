#!/bin/bash
# Load a PRODUCTION Worker's runtime secrets from AWS into Cloudflare.
#
#   scripts/workers-prod-secrets.sh SITE          e.g. colton-games -> loads Worker colton-games-prod
#
# Source of truth (workers-prod-secrets.tf), both JSON of variable name -> value:
#   workers-prod/SITE             what an administrator put (values Terraform does not hold)
#   workers-prod/SITE/terraform   what Terraform writes (the same values it writes to Vercel production)
# The two are merged; a name in both, an empty value or a placeholder refuses the whole load before Cloudflare is called.
# The merged JSON is piped to `wrangler secret bulk`, so no value is on a command line, in a file or in this script's
# output; only names are printed. Run it AFTER the site's own deploy has finished: a secret change made while a deploy is
# uploading can be lost when that deploy activates.
#
# Production values only ever go to `<SITE>-prod`; staging has its own containers and loader (workers-stage-secrets.sh).
set -euo pipefail
{ set +x; } 2>/dev/null

SITE=${1:?usage: workers-prod-secrets.sh SITE}
[[ "$SITE" =~ ^[a-z][a-z0-9-]*$ ]] || { echo "not a site name: $SITE" >&2; exit 2; }
WORKER="$SITE-prod"
ACCOUNT_ID=12ea67fb7ced068de03f35c22688e436

read_container() {
  aws secretsmanager get-secret-value --region us-west-1 --secret-id "$1" --query SecretString --output text 2>/dev/null \
    || { echo "$1 has no value yet: put one first (see workers-prod-secrets.tf)" >&2; return 1; }
}
OUT_OF_BAND=$(read_container "workers-prod/$SITE") || exit 1
MANAGED=$(read_container "workers-prod/$SITE/terraform") || exit 1

# Merge and check in one place; the merged object goes to stdout, names and refusals to stderr.
JSON=$(OUT_OF_BAND="$OUT_OF_BAND" MANAGED="$MANAGED" python3 -c '
import json, os, re, sys
merged = {}
for label in ("OUT_OF_BAND", "MANAGED"):
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
            sys.exit("%s is in both containers: each name has one owner (workers-prod-secrets.tf)" % k)
        merged[k] = v
print("workers-prod/%s: loading %d names into %s: %s" % (sys.argv[1], len(merged), sys.argv[2], ", ".join(sorted(merged))), file=sys.stderr)
sys.stdout.write(json.dumps(merged))
' "$SITE" "$WORKER")
unset OUT_OF_BAND MANAGED

export CLOUDFLARE_ACCOUNT_ID=$ACCOUNT_ID
CLOUDFLARE_API_TOKEN=$(aws secretsmanager get-secret-value --region us-west-1 --secret-id cloudflare-workers-deploy-token --query SecretString --output text | tr -d '\n')
export CLOUDFLARE_API_TOKEN
printf '%s' "$JSON" | (cd /tmp && npx --yes wrangler@4.147.0 secret bulk --name "$WORKER" 2>&1 | grep -E "Successfully|Error|✘" | sed 's/^/  wrangler: /')
unset JSON CLOUDFLARE_API_TOKEN

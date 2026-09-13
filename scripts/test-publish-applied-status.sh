#!/usr/bin/env bash
# Offline checks for scripts/publish-applied-status.sh: a fake `aws`, a throwaway git repo,
# no credentials and no network.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
pub="$here/publish-applied-status.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir -p "$tmp/bin"
cat > "$tmp/bin/aws" <<'AWS'
#!/usr/bin/env bash
while [ $# -gt 0 ]; do
  case "$1" in
    --value) printf '%s' "$2" > "$FAKE_AWS_OUT"; shift 2 ;;
    *) shift ;;
  esac
done
AWS
chmod +x "$tmp/bin/aws"
export PATH="$tmp/bin:$PATH" FAKE_AWS_OUT="$tmp/published"

git init -q "$tmp/repo"
cd "$tmp/repo"
git -c user.email=test@example.com -c user.name=test commit -q --allow-empty -m "Merge the thing (#42)"
git update-ref refs/remotes/origin/main HEAD

run() { rm -f "$FAKE_AWS_OUT"; bash "$pub" "$@" >/dev/null; }
field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$FAKE_AWS_OUT" "$1"; }

# clean plan of origin/main
: > "$tmp/clean.log"
run 0 "$tmp/clean.log"
[ "$(field plan)" = clean ] || fail "clean plan not published as clean"
[ "$(field pending_count)" = 0 ] || fail "clean plan has pending items"
[ "$(field main)" = "$(git rev-parse HEAD)" ] || fail "wrong main sha"
[ "$(field subject)" = "Merge the thing (#42)" ] || fail "wrong subject"

# pending changes: addresses and actions only, never attribute values
cat > "$tmp/pending.log" <<'LOG'
  # aws_instance.box[0] must be replaced
  # aws_network_interface.box[0] (deposed object feefc6b3) will be destroyed
  # aws_route_table_association.subnet_b will be imported
      ~ user_data = "never-publish-this-value"
Plan: 1 to import, 1 to add, 0 to change, 2 to destroy.
LOG
run 2 "$tmp/pending.log"
[ "$(field plan)" = pending ] || fail "pending plan not published as pending"
[ "$(field pending_count)" = 3 ] || fail "expected 3 pending items"
python3 - "$FAKE_AWS_OUT" <<'PY' || fail "pending list is wrong"
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["pending"] == ["aws_instance.box[0] replaced",
                          "aws_network_interface.box[0] destroyed",
                          "aws_route_table_association.subnet_b imported"], doc["pending"]
assert "never-publish-this-value" not in open(sys.argv[1]).read()
PY

# a failed plan
run 1 "$tmp/clean.log"
[ "$(field plan)" = error ] || fail "failed plan not published as error"

# a big plan stays inside the 4 KB parameter and keeps the full count
for i in $(seq 1 400); do echo "  # aws_ssm_parameter.item_$i will be created"; done > "$tmp/big.log"
run 2 "$tmp/big.log"
[ "$(wc -c < "$FAKE_AWS_OUT")" -le 4096 ] || fail "published value exceeds 4 KB"
[ "$(field pending_count)" = 400 ] || fail "big plan lost its full count"

# never publish from a checkout that is not a clean origin/main
git -c user.email=test@example.com -c user.name=test commit -q --allow-empty -m "local only"
run 0 "$tmp/clean.log"
[ ! -e "$FAKE_AWS_OUT" ] || fail "published from a commit that is not origin/main"
git reset -q --hard origin/main
touch untracked-file
run 0 "$tmp/clean.log"
[ ! -e "$FAKE_AWS_OUT" ] || fail "published from a dirty checkout"

echo "publish-applied-status: all checks passed"

#!/usr/bin/env bash
# Offline checks: a fake AWS CLI, IMDS, Terraform wrapper, and a local git remote.
set -euo pipefail

here=$(cd "$(dirname "$0")" && pwd)
pub="$here/publish-applied-status.sh"
tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
fail() { echo "FAIL: $*" >&2; exit 1; }

mkdir "$tmp/bin"
cat > "$tmp/bin/aws" <<'AWS'
#!/usr/bin/env bash
case "$1 $2" in
  'sts get-caller-identity') printf '%s\n' "$FAKE_CALLER" ;;
  'ssm put-parameter')
    while [ "$#" -gt 0 ]; do
      if [ "$1" = --value ]; then printf '%s' "$2" > "$FAKE_AWS_OUT"; exit 0; fi
      shift
    done
    exit 1 ;;
  *) exit 1 ;;
esac
AWS
cat > "$tmp/bin/curl" <<'CURL'
#!/usr/bin/env bash
[ "${FAKE_IMDS_DISABLED:-0}" = 0 ] || exit 7
case "${*: -1}" in
  */api/token) echo test-imds-token ;;
  */dynamic/instance-identity/document)
    [ "$(cat)" = 'X-aws-ec2-metadata-token: test-imds-token' ] || exit 7
    echo '{"instanceId":"i-00000000123456789","accountId":"123456789012"}' ;;
  *) exit 1 ;;
esac
CURL
cat > "$tmp/bin/terraform" <<'TF'
#!/usr/bin/env bash
    # TERRAFORM-RUN-LOCK-WRAPPER mock
printf 'idle=%s host_wait=%s workspace=%s global=%s per_command=%s args=%s\n' \
  "${TF_RUN_LOCK_NOWAIT:-}" "${TF_RUN_LOCK_WAIT:-}" "${TF_WORKSPACE:-}" "${TF_CLI_ARGS:-}" "${TF_CLI_ARGS_plan:-}" "$*" >"$FAKE_PLAN_CALL"
if [ -n "${FAKE_MOVE_MAIN:-}" ]; then
  git --git-dir="$FAKE_MOVE_MAIN" update-ref refs/heads/main refs/heads/later
fi
cat "$FAKE_PLAN_LOG"
exit "$FAKE_PLAN_EXIT"
TF
cat > "$tmp/bin/pgrep" <<'PGREP'
#!/usr/bin/env bash
[ "$1" = -x ] || exit 1
if [[ "${FAKE_ACTIVE_TF:-none}" =~ ^($2)$ ]]; then
  echo 1234
else
  exit 1
fi
PGREP
chmod +x "$tmp/bin/"*
export PATH="$tmp/bin:$PATH" FAKE_AWS_OUT="$tmp/published" FAKE_PLAN_CALL="$tmp/terraform-call"
export XDG_STATE_HOME="$tmp/state"
export FAKE_CALLER="arn:aws:sts::123456789012:assumed-role/jumpbox-admin-role/i-00000000123456789"

git init -q -b main "$tmp/repo"
cd "$tmp/repo"
git config user.email test@example.com
git config user.name test
printf '.terraform/\n' > .gitignore
git add .gitignore
git commit -q -m "Merge the thing (#42)"
git init -q --bare "$tmp/origin"
git remote add origin "$tmp/origin"
git push -q origin HEAD:main

install_backend() {
  mkdir -p .terraform
  printf '{"backend":{"type":"s3","config":{"bucket":"%s","key":"aws-infrastructure/terraform.tfstate","region":"us-west-1","dynamodb_table":"ejc3-terraform-locks"}}}\n' \
    "$1" > .terraform/terraform.tfstate
}
install_backend ejc3-terraform-state
run() {
  local want=$1 rc=0
  export FAKE_PLAN_EXIT=$2 FAKE_PLAN_LOG=$3
  shift 3
  rm -f "$FAKE_AWS_OUT" "$FAKE_PLAN_CALL"
  bash "$pub" "$@" >"$tmp/result" 2>"$tmp/diagnostic" || rc=$?
  if [ "$rc" -ne "$want" ]; then
    cat "$tmp/result" "$tmp/diagnostic" >&2
    fail "publisher exited $rc, expected $want"
  fi
}
not_published() { [ ! -e "$FAKE_AWS_OUT" ] || fail "$1 published a status"; }
not_planned() { [ ! -e "$FAKE_PLAN_CALL" ] || fail "$1 ran a plan"; }
field() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$FAKE_AWS_OUT" "$1"; }
diagnostic="$XDG_STATE_HOME/aws-applied-status/last-plan.log"

printf 'No changes. Your infrastructure matches the configuration.\n' > "$tmp/clean.log"
TF_RUN_LOCK_WAIT=86400 TF_CLI_ARGS='-refresh=false' TF_CLI_ARGS_plan='-target=aws_vpc.wrong' TF_WORKSPACE=other run 0 0 "$tmp/clean.log"
[ "$(field plan)" = clean ] || fail "clean plan not published as clean"
[ "$(field pending_count)" = 0 ] || fail "clean plan has pending items"
[ "$(field main)" = "$(git rev-parse HEAD)" ] || fail "wrong main sha"
[ "$(field subject)" = "Merge the thing (#42)" ] || fail "wrong subject"
[ "$(cat "$FAKE_PLAN_CALL")" = 'idle= host_wait=120 workspace=default global= per_command= args=plan -parallelism=2 -detailed-exitcode -lock=true -lock-timeout=10m -input=false -no-color' ] ||
  fail "explicit plan did not lock and refresh the entire default workspace"
[ "$(stat -c %a "$diagnostic")" = 600 ] || fail "diagnostic is not private"

# A traced caller must not expose the IMDS token when it is fetched or sent.
bash -x "$pub" >"$tmp/traced-result" 2>"$tmp/traced-output"
! grep -Fq 'test-imds-token' "$tmp/traced-output" || fail "IMDS token escaped into the shell trace"

cat > "$tmp/pending.log" <<'LOG'
Note: Objects have changed outside of Terraform
  # aws_instance.unrelated will be destroyed
Terraform will perform the following actions:
  # aws_instance.box[0] must be replaced
  # aws_network_interface.box[0] (deposed object feefc6b3) will be destroyed
  # aws_route_table_association.subnet_b will be imported
  # data.aws_instance.example will be read during apply
  # aws_resource.item["two words"] will no longer be managed by Terraform, but will not be destroyed
      ~ user_data = "never-publish-this-value"
Plan: 1 to import, 1 to add, 0 to change, 2 to destroy.
LOG
run 2 2 "$tmp/pending.log" --if-idle
[ "$(field plan)" = pending ] || fail "pending plan not published as pending"
[ "$(field pending_count)" = 4 ] || fail "expected four managed-resource actions"
[ "$(cat "$FAKE_PLAN_CALL")" = 'idle=1 host_wait=120 workspace=default global= per_command= args=plan -parallelism=2 -detailed-exitcode -lock=true -lock-timeout=0s -input=false -no-color' ] ||
  fail "Stop hook would wait instead of using both nonblocking locks"
python3 - "$FAKE_AWS_OUT" <<'PY' || fail "pending list is wrong"
import json, sys
doc = json.load(open(sys.argv[1]))
assert doc["pending"] == ["aws_instance.box[0] replaced",
                          "aws_network_interface.box[0] destroyed",
                          "aws_route_table_association.subnet_b imported",
                          'aws_resource.item["two words"] removed from state'], doc["pending"]
assert "never-publish-this-value" not in open(sys.argv[1]).read()
PY

# Neither a local wrapper collision nor a held shared backend lock invalidates
# the prior result. A permission/network error while acquiring a lock does.
cp "$diagnostic" "$tmp/previous-diagnostic"
FAKE_ACTIVE_TF=terraform run 75 0 "$tmp/clean.log" --if-idle
not_planned "a direct Terraform invocation without the wrapper"
not_published "a direct Terraform invocation without the wrapper"
FAKE_ACTIVE_TF=terraform-provi run 75 0 "$tmp/clean.log"
not_planned "a provider surviving its Terraform parent"
not_published "a provider surviving its Terraform parent"
TF_RUN_LOCK_WAIT=86400 run 75 75 "$tmp/clean.log"
not_published "local lock collision in explicit mode"
case "$(cat "$FAKE_PLAN_CALL")" in
  'idle= host_wait=120 '*) ;;
  *) fail "local lock wait can outlive the publisher";;
esac
TF_RUN_LOCK_WAIT=86400 run 75 75 "$tmp/clean.log" --if-idle
not_published "local lock collision in idle mode"
case "$(cat "$FAKE_PLAN_CALL")" in
  'idle=1 host_wait=120 '*) ;;
  *) fail "idle plan would wait on the host lock";;
esac
cat > "$tmp/remote-lock.log" <<'LOG'
Error: Error acquiring the state lock
Error message: operation error DynamoDB: PutItem, api error ConditionalCheckFailedException: The conditional request failed
Lock Info:
  ID: previous
LOG
run 75 1 "$tmp/remote-lock.log" --if-idle
not_published "backend lock collision"
# The prior owner can release before Terraform looks up who held the lock.
cat > "$tmp/released-lock.log" <<'LOG'
Error: Error acquiring the state lock
Error message: operation error DynamoDB: PutItem, api error
ConditionalCheckFailedException: The conditional request failed
Unable to retrieve item from the DynamoDB table
LOG
run 75 1 "$tmp/released-lock.log" --if-idle
not_published "backend lock released before Terraform read Lock Info"
cmp "$diagnostic" "$tmp/previous-diagnostic" || fail "lock contention hid the prior diagnostic"
printf 'Error: Error acquiring the state lock\noperation error DynamoDB: PutItem, api error AccessDeniedException: should-stay-private\n' > "$tmp/denied.log"
run 1 1 "$tmp/denied.log"
[ "$(field plan)" = error ] || fail "failed plan not published as error"
! grep -q 'should-stay-private' "$FAKE_AWS_OUT" || fail "private error leaked"
grep -q 'should-stay-private' "$diagnostic" || fail "private diagnostic missing"
run 124 124 "$tmp/denied.log"
[ "$(field plan)" = error ] || fail "timed-out plan not published as error"

# Large output stays bounded in both places while preserving the count.
{
  printf 'Terraform will perform the following actions:\n'
  for i in $(seq 1 400); do printf '  # aws_ssm_parameter.item_%s will be created\n' "$i"; done
  head -c 4300000 /dev/zero | tr '\0' x
} > "$tmp/big.log"
run 2 2 "$tmp/big.log"
[ "$(wc -c < "$FAKE_AWS_OUT")" -le 4096 ] || fail "published value exceeds 4 KB"
[ "$(field pending_count)" = 400 ] || fail "big plan lost its full count"
[ "$(wc -c < "$diagnostic")" -le 4194500 ] || fail "retained diagnostic is oversized"

FAKE_IMDS_DISABLED=1 run 75 0 "$tmp/clean.log"
not_published "Mac with copied EC2 credentials"
not_planned "Mac with copied EC2 credentials"
FAKE_CALLER=arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_Administrator/sso run 75 0 "$tmp/clean.log"
not_published "SSO session"
not_planned "SSO session"
FAKE_CALLER=arn:aws:sts::123456789012:assumed-role/jumpbox-admin-role/i-00000000000000000 run 75 0 "$tmp/clean.log"
not_published "mismatched AWS session"
not_planned "mismatched AWS session"
mv "$tmp/bin/terraform" "$tmp/bin/terraform-wrapper"
printf '#!/usr/bin/env bash
exit 0
' >"$tmp/bin/terraform"
chmod +x "$tmp/bin/terraform"
run 1 0 "$tmp/clean.log"
not_published "Terraform without the installed wrapper"
not_planned "Terraform without the installed wrapper"
mv "$tmp/bin/terraform-wrapper" "$tmp/bin/terraform"
install_backend wrong-bucket
run 75 0 "$tmp/clean.log"
not_planned "different backend"
install_backend ejc3-terraform-state

git commit -q --allow-empty -m "local only"
run 75 0 "$tmp/clean.log"
not_planned "local commit"
git reset -q --hard origin/main
touch untracked-file
run 75 0 "$tmp/clean.log"
not_planned "dirty checkout"
rm untracked-file
git remote set-url origin "$tmp/missing"
run 75 0 "$tmp/clean.log"
not_planned "failed main fetch"
git remote set-url origin "$tmp/origin"
old_head=$(git rev-parse HEAD)
git commit -q --allow-empty -m "remote advances during plan"
git push -q origin HEAD:later
git reset -q --hard "$old_head"
FAKE_MOVE_MAIN="$tmp/origin" run 75 0 "$tmp/clean.log"
not_published "main advanced during a plan"
[ -e "$FAKE_PLAN_CALL" ] || fail "branch move was not simulated"

echo "publish-applied-status: all checks passed"

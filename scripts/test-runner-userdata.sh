#!/bin/bash
# Exercise the runner user_data's IPv6 readiness gate against fakes.
#
# Why this exists: the user_data is an inline heredoc inside runner-autoscale.tf,
# so nothing runs it before it is deployed onto a real runner. On 2026-08-15 a CI
# job landed on a runner whose ENI already held 2600:1f1c:208:c01::baca while the
# guest OS had no global IPv6 at all. Every routed and IPv6 test failed with
# "No global IPv6 address found on host" -- a runner defect that reads as a code
# flake. The gate added in response refuses to register a runner in that state,
# and a gate that cannot fail is worse than no gate, so this proves it fails.
#
# Like scripts/test-runner-lambdas.py, this extracts the REAL heredoc body out of
# runner-autoscale.tf rather than keeping a copy that can drift.
#
# Run from the repo root:  bash scripts/test-runner-userdata.sh
# Exit code 1 if any case fails.
set -uo pipefail

TF_FILE="$(dirname "$0")/../runner-autoscale.tf"
[ -f "$TF_FILE" ] || { echo "cannot find runner-autoscale.tf next to $0" >&2; exit 2; }
VPC_FILE="$(dirname "$0")/../runner-vpc.tf"
[ -f "$VPC_FILE" ] || { echo "cannot find runner-vpc.tf next to $0" >&2; exit 2; }
BOOTSTRAP_FILE="$(dirname "$0")/../runner-bootstrap.tf"
[ -f "$BOOTSTRAP_FILE" ] || { echo "cannot find runner-bootstrap.tf next to $0" >&2; exit 2; }

# `<<-EOF` here only strips tabs and the content starts at column 0, so the body
# needs no dedent. Terraform's `$${` escape becomes a literal `${` on the host.
USERDATA=$(mktemp)
trap 'rm -f "$USERDATA"' EXIT
awk '/runner_user_data = <<-EOF/{f=1;next} f&&/^EOF$/{exit} f' "$TF_FILE" \
  | sed 's/\$\${/${/g' > "$USERDATA"
[ -s "$USERDATA" ] || { echo "extracted an EMPTY user_data from $TF_FILE" >&2; exit 2; }
# Registration runs as its own script, written out by user data at boot and run
# again after every job, so that body is what the registration cases execute.
JOB_SCRIPT=$(mktemp)
trap 'rm -f "$USERDATA" "$JOB_SCRIPT"' EXIT
awk "/<<'RUNNER_JOB'\$/{f=1;next} f&&/^RUNNER_JOB\$/{exit} f" "$USERDATA" > "$JOB_SCRIPT"
[ -s "$JOB_SCRIPT" ] || { echo "extracted an EMPTY fcvm-runner-job from $TF_FILE" >&2; exit 2; }

PASS=0; FAIL=0
ok()   { echo "  ok   $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL $1"; FAIL=$((FAIL+1)); }

# --- 1. The whole script must parse. A syntax error here bricks every runner
#        launched afterwards, and the failure would only show up on a live box.
echo "user_data:"
if bash -n "$USERDATA" 2>/tmp/ud-syntax.$$; then
    ok "user_data parses ($(wc -l < "$USERDATA") lines)"
else
    bad "user_data has a syntax error: $(head -2 /tmp/ud-syntax.$$)"
fi
rm -f /tmp/ud-syntax.$$
if bash -n "$JOB_SCRIPT" 2>/dev/null; then
    ok "fcvm-runner-job parses ($(wc -l < "$JOB_SCRIPT") lines)"
else
    bad "fcvm-runner-job has a syntax error"
fi
# EC2 receives the script without its whole-line comments (runner_user_data_document).
SHIPPED=$(mktemp)
awk '!/^[[:space:]]*#/ || /^#!/' "$USERDATA" > "$SHIPPED"
if bash -n "$SHIPPED" 2>/dev/null \
   && awk "/<<'RUNNER_JOB'\$/{f=1;next} f&&/^RUNNER_JOB\$/{exit} f" "$SHIPPED" | bash -n \
   && [ "$(grep -c '^#!/bin/bash$' "$SHIPPED")" = 2 ] \
   && grep -q 'if !startswith(trimspace(line), "#") || startswith(line, "#!")' "$TF_FILE" \
   && grep -q 'value = base64gzip(local.runner_user_data_document)' "$TF_FILE"; then
    ok "the comment-stripped document EC2 receives parses and keeps both shebangs"
else
    bad "the shipped user data is not the parseable comment-stripped script"
fi
rm -f "$SHIPPED"

# --- 2. The gate itself, lifted from the real file.
FN=$(awk '/^have_global_v6\(\) \{/{f=1} f{print} f&&/^\}$/{exit}' "$USERDATA")
if [ -z "$FN" ]; then
    bad "have_global_v6 not found in user_data (was the IPv6 gate removed?)"
    echo; echo "passed=$PASS failed=$FAIL"; exit 1
fi
ok "extracted have_global_v6 from the deployed user_data"

eval "$FN"

ADDR_OUT=""; ROUTE_OUT=""
ip() {  # stub: only the two queries the gate makes
    case "$*" in
        *"addr show scope global"*) printf '%s\n' "$ADDR_OUT" ;;
        *"route show"*)             printf '%s\n' "$ROUTE_OUT" ;;
    esac
}

check() {  # name want_rc
    have_global_v6; local got=$?
    if [ "$got" = "$2" ]; then ok "$1 (rc=$got)"; else bad "$1: wanted rc=$2, got rc=$got"; fi
}

echo "must REFUSE to register:"
ADDR_OUT=""; ROUTE_OUT=""
check "no global address at all -- the 2026-08-15 shape" 1

ADDR_OUT="    inet6 fd00:1234::5/64 scope global"
ROUTE_OUT=""
check "ULA only" 1

ADDR_OUT="    inet6 2600:1f1c:208:c01::baca/64 scope global deprecated"
ROUTE_OUT=""
check "deprecated /64 only" 1

ADDR_OUT="    inet6 2600:1f1c:208:c01::baca/128 scope global"
ROUTE_OUT="fe80::/64 dev enp0s1 proto kernel metric 256"
check "/128 with no on-link /64 route" 1

echo "must ALLOW registration:"
ADDR_OUT="    inet6 2600:1f1c:208:c01::baca/64 scope global dynamic mngtmpaddr"
ROUTE_OUT=""
check "plain /64" 0

ADDR_OUT="    inet6 2600:1f1c:208:c01::baca/128 scope global dynamic"
ROUTE_OUT="2600:1f1c:208:c01::/64 dev enp0s1 proto ra metric 100 pref medium"
check "/128 backed by an on-link /64 -- the AWS DHCPv6 shape" 0

ADDR_OUT="    inet6 fd00:1::1/64 scope global
    inet6 2600:1f1c:208:c01::baca/128 scope global"
ROUTE_OUT="2600:1f1c:208:c01::/64 dev enp0s1 proto ra metric 100"
check "ULA alongside a usable /128" 0

# --- 3. A storeless instance type must fail legibly, not take the bootstrap
#        down at an unrelated line. `grep -v` matching nothing returns 1, and
#        under `set -e` that killed everything after it (2026-08-15, c7g.metal).
echo "instance store:"
# shellcheck disable=SC2016 # The extracted script must contain literal $ROOT_DEV.
if grep -q 'grep -v "\^\$ROOT_DEV\$" || true' "$USERDATA"; then
    ok "NVMe probe tolerates a no-match instead of aborting the bootstrap"
else
    bad "NVMe probe can still abort the bootstrap when there is no instance store"
fi
if grep -q "no instance-store NVMe on this instance type" "$USERDATA"; then
    ok "storeless instance refuses to register, with a reason"
else
    bad "no explicit refusal for a storeless instance"
fi

# --- 4. The gate must actually be wired to the registration decision. A gate
#        nothing consults is the same as no gate.
echo "wiring:"
CONFIG_LINE=$(grep -n "config.sh --url" "$USERDATA" | head -1 | cut -d: -f1)
if [ -z "$CONFIG_LINE" ]; then
    bad "config.sh registration not found -- cannot tell whether any gate precedes it"
else
    ok "found the registration call (line $CONFIG_LINE)"
fi

# Each gate is checked BY ITS OWN message: both say "refusing to register", so a
# single grep would let one of them sit after registration unnoticed.
gate_precedes_registration() {  # label, message
    local line
    line=$(grep -n "$2" "$USERDATA" | head -1 | cut -d: -f1)
    if [ -z "$line" ]; then
        bad "$1: no refusal path found -- does a failed gate still register the runner?"
    elif [ -n "$CONFIG_LINE" ] && [ "$line" -lt "$CONFIG_LINE" ]; then
        ok "$1 refuses before registration (line $line < $CONFIG_LINE)"
    else
        bad "$1 does not precede registration (gate=$line config=$CONFIG_LINE)"
    fi
}
gate_precedes_registration "IPv6 gate"  "no global IPv6 on"
gate_precedes_registration "NVMe gate"  "no instance-store NVMe on this instance type"

# A bare bracket test at statement level is a SILENT gate: under `set -e` it exits
# 1 and writes nothing, and the console is the only diagnostic channel a metal
# instance has. Every refusal must name itself first. This checks the shape, not
# one message, so a new silent gate is caught wherever it is added.
SILENT_GATES=$(grep -nE '^[[:space:]]*\[[[:space:]].*\][[:space:]]*$' "$USERDATA" || true)
if [ -z "$SILENT_GATES" ]; then
    ok "no bare bracket test stands alone as a gate"
else
    bad "silent gate(s) exit without a FATAL line: $(printf '%s' "$SILENT_GATES" | tr '\n' ' ')"
fi

# --- 4b. A runner must not take a job while a package install is running. A CI
#         job's apt-get fails at once on a held lock, and on 2026-09-13 three
#         fcvm jobs lost "Install dependencies" that way: Amazon Inspector's
#         SSM agent ran `apt install ./inspector-vm-scanner.deb` about 90 s after
#         boot, while the runner was already taking work.
echo "package installs:"
BUSY_FN=$(awk '/^apt_locks_busy\(\) \{/{f=1} f{print} f&&/^\}$/{exit}' "$USERDATA")
QUIET_FN=$(awk '/^wait_for_apt_quiet\(\) \{/{f=1} f{print} f&&/^\}$/{exit}' "$USERDATA")
if [ -z "$BUSY_FN" ] || [ -z "$QUIET_FN" ]; then
    bad "apt_locks_busy / wait_for_apt_quiet not found in user_data (no install gate)"
else
    ok "extracted apt_locks_busy and wait_for_apt_quiet from the deployed user_data"
    PROC_TMP=$(mktemp -d)
    fake_proc() {  # name, then the files one process holds open
        local root="$PROC_TMP/$1" n=0 f
        shift
        mkdir -p "$root/4242/fd"
        for f in "$@"; do ln -s "$f" "$root/4242/fd/$n"; n=$((n + 1)); done
        printf '%s' "$root"
    }
    busy_case() {  # label, want_rc, proc root
        ( eval "$BUSY_FN"; apt_locks_busy "$3" ); local got=$?
        if [ "$got" = "$2" ]; then ok "$1 (rc=$got)"; else bad "$1: wanted rc=$2, got rc=$got"; fi
    }
    busy_case "no process holds an apt lock" 1 "$(fake_proc quiet /var/log/syslog /dev/null)"
    busy_case "the dpkg frontend lock is held" 0 "$(fake_proc frontend /dev/null /var/lib/dpkg/lock-frontend)"
    busy_case "the apt lists lock is held" 0 "$(fake_proc lists /var/lib/apt/lists/lock)"
    busy_case "the archives lock is held" 0 "$(fake_proc archives /var/cache/apt/archives/lock)"

    quiet_case() {  # label, want_rc, busy checks before quiet ("forever" = never quiet), deadline
        (
          eval "$QUIET_FN"
          CHECKS=0
          sleep() { :; }
          BUSY_FOR=$3
          apt_locks_busy() { CHECKS=$((CHECKS + 1)); [ "$BUSY_FOR" = forever ] || [ "$CHECKS" -le "$BUSY_FOR" ]; }
          wait_for_apt_quiet 3 "$4"
        ); local got=$?
        if [ "$got" = "$2" ]; then ok "$1 (rc=$got)"; else bad "$1: wanted rc=$2, got rc=$got"; fi
    }
    quiet_case "an install that finishes lets the runner register" 0 5 30
    quiet_case "an install still running at the deadline refuses registration" 1 forever 1
    rm -rf "$PROC_TMP"
fi
gate_precedes_registration "package install gate" "apt or dpkg is still installing"
SSM_START_LINE=$(grep -n "snap start amazon-ssm-agent" "$USERDATA" | head -1 | cut -d: -f1)
QUIET_CALL_LINE=$(grep -n "if ! wait_for_apt_quiet" "$USERDATA" | head -1 | cut -d: -f1)
if [ -n "$SSM_START_LINE" ] && [ -n "$QUIET_CALL_LINE" ] && [ "$SSM_START_LINE" -lt "$QUIET_CALL_LINE" ]; then
    ok "the install gate runs after the SSM agent that pushes installs starts (line $QUIET_CALL_LINE > $SSM_START_LINE)"
else
    bad "the install gate does not follow the SSM agent start (ssm=$SSM_START_LINE gate=$QUIET_CALL_LINE)"
fi

# --- 5. Registration provenance must be persisted before the service can take
#        work. Successful config reads the runner id GitHub assigned, atomically
#        claims this instance ARN in DynamoDB, and only then starts the service.
echo "registration provenance:"
TRACE_OFF_LINE=$(grep -n '^set +x$' "$USERDATA" | head -1 | cut -d: -f1)
TOKEN_LINE=$(grep -n 'REG_TOKEN=$(bootstrap_ssm' "$USERDATA" | head -1 | cut -d: -f1)
RUNNER_ID_LINE=$(grep -n '\.agentId' "$USERDATA" | head -1 | cut -d: -f1)
CLAIM_LINE=$(grep -n 'aws dynamodb put-item' "$USERDATA" | head -1 | cut -d: -f1)
CONSISTENT_LINE=$(grep -n -- '--consistent-read' "$USERDATA" | head -1 | cut -d: -f1)
INSTALL_LINE=$(grep -n './svc.sh install' "$USERDATA" | head -1 | cut -d: -f1)
START_LINE=$(grep -n './svc.sh start' "$USERDATA" | head -1 | cut -d: -f1)

if [ -n "$TRACE_OFF_LINE" ] && [ -n "$TOKEN_LINE" ] \
   && [ "$TRACE_OFF_LINE" -lt "$TOKEN_LINE" ]; then
    ok "xtrace is disabled before the registration credential is read"
else
    bad "registration credential can reach cloud-init xtrace (set+x=$TRACE_OFF_LINE token=$TOKEN_LINE)"
fi

if ! grep -Eq '^PAT=|ssm get-parameter --name /github-runner/pat|Authorization: token' "$USERDATA" \
   && grep -q -- '--ephemeral' "$USERDATA" \
   && grep -q -- '--disableupdate' "$USERDATA" \
   && grep -q 'BOOTSTRAP_PARAM="/github-runner/bootstrap/\$INSTANCE_ID"' "$USERDATA"; then
    ok "bootstrap fetches only its instance-bound token and registers an ephemeral runner"
else
    bad "bootstrap retains a reusable GitHub credential or omits ephemeral registration"
fi

if grep -q '^Restart=no$' "$USERDATA" \
   && grep -q '^Environment=GITHUB_ACTIONS_SERVICE_EXIT_AFTER_N_FAILURES=1$' "$USERDATA" \
   && grep -q '^ExecStopPost=+/usr/local/sbin/fcvm-runner-job after$' "$USERDATA" \
   && grep -q "InstanceInitiatedShutdownBehavior='terminate'" "$TF_FILE"; then
    ok "a finished job hands its host to fcvm-runner-job, and a host that powers off is terminated"
else
    bad "a finished job can leave an idle host, restart the service, or skip the next-job decision"
fi

NEXT_UNIT=$(awk "/<<'NEXT_JOB_SERVICE'\$/{f=1;next} f&&/^NEXT_JOB_SERVICE\$/{exit} f" "$USERDATA")
if printf '%s\n' "$NEXT_UNIT" | grep -qx 'FailureAction=poweroff' \
   && printf '%s\n' "$NEXT_UNIT" | grep -qx 'Type=oneshot' \
   && printf '%s\n' "$NEXT_UNIT" | grep -qx 'EnvironmentFile=/etc/fcvm-runner.env' \
   && printf '%s\n' "$NEXT_UNIT" | grep -qx 'ExecStart=/usr/local/sbin/fcvm-runner-job next' \
   && printf '%s\n' "$NEXT_UNIT" | grep -qx 'TimeoutStartSec=600'; then
    ok "the next-job unit runs once, bounded, and powers the host off when it fails"
else
    bad "the next-job unit can hang, restart, or fail without powering the host off"
fi

JOB_END_LINE=$(grep -n '^RUNNER_JOB$' "$USERDATA" | head -1 | cut -d: -f1)
FIRST_LINE=$(grep -n '/usr/local/sbin/fcvm-runner-job first$' "$USERDATA" | head -1 | cut -d: -f1)
if grep -q '> /etc/fcvm-runner.env$' "$USERDATA" \
   && grep -q '^chmod 755 /usr/local/sbin/fcvm-runner-job$' "$USERDATA" \
   && [ -n "$JOB_END_LINE" ] && [ -n "$FIRST_LINE" ] && [ "$JOB_END_LINE" -lt "$FIRST_LINE" ]; then
    ok "boot installs the job script and its environment, then registers through it"
else
    bad "boot does not register through the installed fcvm-runner-job"
fi

if grep -q 'AWS_MAX_ATTEMPTS=1 timeout --kill-after=2 12 aws ssm' "$USERDATA" \
   && grep -q 'BOOTSTRAP_DEADLINE=$((SECONDS + 180))' "$USERDATA" \
   && grep -q 'for BOOTSTRAP_ATTEMPT in $(seq 1 36)' "$USERDATA" \
   && grep -q 'trap runner_bootstrap_exit EXIT' "$USERDATA"; then
    ok "credential polling and failure cleanup are bounded and request shutdown"
else
    bad "credential fetch/failure handling can stall bootstrap or leave it running"
fi

if [ -n "$RUNNER_ID_LINE" ] && [ -n "$CLAIM_LINE" ] \
   && [ -n "$INSTALL_LINE" ] && [ -n "$START_LINE" ] \
   && [ "$CONFIG_LINE" -lt "$RUNNER_ID_LINE" ] \
   && [ "$RUNNER_ID_LINE" -lt "$CLAIM_LINE" ] \
   && [ "$CLAIM_LINE" -lt "$INSTALL_LINE" ] \
   && [ "$CLAIM_LINE" -lt "$START_LINE" ]; then
    ok "config identity is claimed before service installation and startup"
else
    bad "registration identity ordering is unsafe (config=$CONFIG_LINE id=$RUNNER_ID_LINE claim=$CLAIM_LINE install=$INSTALL_LINE start=$START_LINE)"
fi

if [ -n "$CONSISTENT_LINE" ] \
   && grep -q "attribute_not_exists(InstanceArn)" "$USERDATA" \
   && grep -q 'State: {S: "registered"}' "$USERDATA" \
   && grep -q 'refusing to start runner service' "$USERDATA"; then
    ok "uncertain or lost registration claims fail closed through a consistent read"
else
    bad "registration claim lacks conditional-create, strong-read, or fail-closed handling"
fi

# shellcheck disable=SC2016 # Terraform must emit the literal IAM policy variable.
if grep -q 'dynamodb:LeadingKeys' "$BOOTSTRAP_FILE" \
   && grep -q '\$${ec2:SourceInstanceARN}' "$BOOTSTRAP_FILE" \
   && grep -q 'dynamodb:GetItem' "$BOOTSTRAP_FILE" \
   && grep -q 'dynamodb:PutItem' "$BOOTSTRAP_FILE" \
   && ! grep -Eq 'dynamodb:(Scan|Query|UpdateItem|DeleteItem)' "$BOOTSTRAP_FILE"; then
    ok "runner role can claim only its source-instance ARN row"
else
    bad "runner role lacks source-instance-scoped registration-table access"
fi

# The registration row is the only thing that says a ddb-v1 instance registered.
# Losing the table (a rename, a hash_key change, `enable_github_runner = false`
# with runners live) leaves every running instance without one. The cleanup
# Lambda holds those to the ceiling rather than reaping them, and every new boot
# fails closed, but the fleet still stops working, so the table is not deletable
# by an apply that did not mean to delete it.
if awk '/resource "aws_dynamodb_table" "runner_registration"/{f=1} f{print} f&&/^\}$/{exit}' \
     "$TF_FILE" | grep -q 'deletion_protection_enabled = true'; then
    ok "the registration table cannot be deleted by an unintended apply"
else
    bad "aws_dynamodb_table.runner_registration has no deletion protection"
fi

# Within one apply the user-data parameter can be written before the grant that
# lets the instance use it. The old webhook reads that parameter on every launch,
# so a launch in that window boots a claiming bootstrap with no PutItem
# permission: it fails closed, deregisters, and costs a metal spot box-hour.
if awk '/resource "aws_ssm_parameter" "runner_user_data"/{f=1} f{print} f&&/^\}$/{exit}' \
     "$TF_FILE" | grep -q 'aws_iam_role_policy.runner_bootstrap,'; then
    ok "user data that claims registration waits for the grant that allows it"
else
    bad "aws_ssm_parameter.runner_user_data can be written before the runner grant"
fi

if grep -q 'aws_lambda_function.runner_cleanup,' "$TF_FILE" \
   && grep -q 'aws_iam_role_policy.runner_lambda,' "$TF_FILE" \
   && grep -q 'aws_iam_role_policy.runner_bootstrap,' "$TF_FILE" \
   && grep -q 'aws_lambda_function.runner_webhook,' "$TF_FILE" \
   && grep -q 'WEBHOOK_FUNCTION   = "github-runner-webhook"' "$TF_FILE"; then
    ok "broker user data waits for the controller, cleanup, and additive IAM"
else
    bad "Terraform rollout can activate ddb-v1 before both protocol participants"
fi

# --- 6. Execute the real registration tail against command fakes. Static line
#        ordering cannot prove the losing branch actually stops before svc.sh.
echo "registration interleavings:"
REG_TMP=$(mktemp -d)
trap 'rm -f "$USERDATA" "$JOB_SCRIPT"; rm -rf "$REG_TMP"' EXIT
mkdir -p "$REG_TMP/bin" "$REG_TMP/work"

# Execute the real package block without network access. Good-package cases mock
# only digest success; the corrupt-package case uses the real checksum utility.
# Every case checks the selected official digest and requires verification before
# the fake extractor can run. Actual release packages are verified separately at
# each reviewed version bump.
echo "verified runner package:"
if grep -q '^RUNNER_VERSION="2.337.0"$' "$USERDATA" \
   && ! grep -q 'actions/runner/releases/latest' "$USERDATA"; then
    ok "runner version is pinned instead of resolved from mutable latest"
else
    bad "runner package can drift away from the reviewed wrapper version"
fi
DOWNLOAD_BLOCK="$REG_TMP/download.sh"
{
    echo 'set -euo pipefail'
    sed -n '/^RUNNER_VERSION=/,/^chown -R ubuntu:ubuntu \/opt\/actions-runner$/p' "$USERDATA" \
      | sed "s|/opt/actions-runner|$REG_TMP/package|g"
} > "$DOWNLOAD_BLOCK"

download_case() { # scenario arch expected_rc expected_extract
    local scenario=$1 arch=$2 want_rc=$3 want_extract=$4 rc=0
    local journal="$REG_TMP/download-$scenario-$arch.log"
    : > "$journal"
    (
      curl() {
        echo download >> "$JOURNAL"
        case " $* " in
          *" https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-$RUNNER_ARCH-2.337.0.tar.gz -o runner.tar.gz ") : ;;
          *) return 90 ;;
        esac
        [ "$FAKE_SCENARIO" != download_failure ] || return 22
        printf 'deliberately not a runner archive\n' > runner.tar.gz
      }
      sha256sum() {
        local digest filename
        read -r digest filename
        [ "$filename" = runner.tar.gz ] || return 91
        case "$RUNNER_ARCH:$digest" in
          arm64:9b1dc70626422526e3c94767cf024896beb15da5342a3f4819bf2feac13e0393) : ;;
          x64:70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613) : ;;
          *) return 91 ;;
        esac
        if [ "$FAKE_SCENARIO" = checksum_failure ]; then
          printf '%s  %s\n' "$digest" "$filename" | command sha256sum "$@"
          return $?
        fi
        echo verified >> "$JOURNAL"
      }
      tar() {
        [ "$*" = 'xzf runner.tar.gz' ] || return 92
        grep -q '^verified$' "$JOURNAL" || return 92
        echo extract >> "$JOURNAL"
      }
      chown() { :; }
      export -f curl sha256sum tar chown
      FAKE_SCENARIO="$scenario" JOURNAL="$journal" RUNNER_ARCH="$arch" \
        bash "$DOWNLOAD_BLOCK"
    ) > "$REG_TMP/download-$scenario-$arch.output" 2>&1 || rc=$?
    local extracted=0
    grep -q '^extract$' "$journal" && extracted=1
    if [ "$rc" = "$want_rc" ] && [ "$extracted" = "$want_extract" ] \
       && { [ "$rc" = 0 ] || grep -q '^FATAL:' "$REG_TMP/download-$scenario-$arch.output"; }; then
      ok "package $scenario/$arch (rc=$rc extracted=$extracted)"
    else
      bad "package $scenario/$arch: got $rc/$extracted expected $want_rc/$want_extract"
    fi
}
download_case verified arm64 0 1
download_case verified x64 0 1
download_case checksum_failure arm64 1 0
download_case download_failure x64 1 0
download_case unsupported ppc64 1 0

REG_BLOCK="$REG_TMP/fcvm-runner-job"
# shellcheck disable=SC2016 # Replace a literal Terraform interpolation in the extracted text.
sed 's/${local\.runner_registration_table_name}/github-runner-registration/g' "$JOB_SCRIPT" \
  | sed "s|/etc/systemd/system|$REG_TMP/systemd|g; s|^cd /opt/actions-runner\$|cd \"$REG_TMP/work\"|" \
  > "$REG_BLOCK"

cat > "$REG_TMP/bin/aws" <<'FAKE_AWS'
#!/bin/bash
set -eu
if [ "$1 $2" = "ssm get-parameter" ]; then
    [ "$AWS_MAX_ATTEMPTS" = 1 ] || exit 95
    echo token-read >> "$JOURNAL"
    case " $* " in
      *" --name /github-runner/bootstrap/i-test "*) : ;;
      *) echo "bootstrap requested someone else's credential: $*" >&2; exit 90 ;;
    esac
    case "$FAKE_SCENARIO" in credential_missing|next_no_token) exit 1 ;; esac
    if [ "$FAKE_SCENARIO" = credential_late ] && [ "$(grep -c '^token-read$' "$JOURNAL")" = 1 ]; then
        exit 1
    fi
    echo registration-token
elif [ "$1 $2" = "ssm delete-parameter" ]; then
    [ "$AWS_MAX_ATTEMPTS" = 1 ] || exit 95
    case " $* " in
      *" --name /github-runner/bootstrap/i-test "*) : ;;
      *) echo "bootstrap deletes someone else's credential" >&2; exit 90 ;;
    esac
    case "$FAKE_SCENARIO" in credential_delete_failure|next_delete_failure) exit 1 ;; esac
    echo delete >> "$JOURNAL"
elif [ "$1 $2" = "dynamodb put-item" ]; then
    echo put-item >> "$JOURNAL"
    # The claim IS the conditional write: a create at boot, and for a next job a
    # replace of only the row that still names the previous runner (77). A put
    # without its condition overwrites a row cleanup may already own, so the fake
    # refuses to answer one.
    case "$FAKE_MODE: $* " in
      "first:"*" --condition-expression attribute_not_exists(InstanceArn) "*) : ;;
      "next:"*" --condition-expression #s = :registered AND RunnerId = :previous --expression-attribute-names {\"#s\":\"State\"} --expression-attribute-values {\":registered\":{\"S\":\"registered\"},\":previous\":{\"N\":\"77\"}} "*) : ;;
      *) echo "put-item is not the expected conditional write: $*" >&2; exit 90 ;;
    esac
    case "$FAKE_SCENARIO" in
      bootstrap|credential_delete_failure|credential_late|invalid_service|install_failure|start_failure) exit 0 ;;
      next_token|next_delete_failure) exit 0 ;;
    esac
    exit 1
elif [ "$1 $2" = "dynamodb get-item" ]; then
    echo get-item >> "$JOURNAL"
    # An eventually consistent read can miss the write it is resolving, which
    # is the one question this read exists to answer.
    case " $* " in
      *" --consistent-read "*) : ;;
      *) echo "get-item is not a consistent read: $*" >&2; exit 90 ;;
    esac
    registered_row() { # runner id
        jq -cn --arg arn "arn:aws:ec2:$REGION:928413605543:instance/$INSTANCE_ID" \
          --arg instance "$INSTANCE_ID" --arg id "$1" \
          '{Item:{InstanceArn:{S:$arn},State:{S:"registered"},
            InstanceId:{S:$instance},RunnerName:{S:("runner-"+$instance)},
            RunnerId:{N:$id},RegisteredAt:{S:"2026-08-07T20:00:00Z"}}}'
    }
    reaping_row() {
        jq -cn --arg arn "arn:aws:ec2:$REGION:928413605543:instance/$INSTANCE_ID" \
          --arg instance "$INSTANCE_ID" \
          '{Item:{InstanceArn:{S:$arn},State:{S:"reaping"},
            InstanceId:{S:$instance},ReapingAt:{S:"2026-08-07T20:00:00Z"}}}'
    }
    if ! grep -q '^put-item$' "$JOURNAL"; then
        # A next job reads the row it is about to replace, before it registers.
        if [ "$FAKE_SCENARIO" = next_not_registered ]; then reaping_row; else registered_row 77; fi
        exit 0
    fi
    case "$FAKE_SCENARIO" in
      unknown_registered) registered_row 77 ;;
      foreign_registered|next_lost_answer) registered_row 78 ;;
      next_row_moved) registered_row 99 ;;
      cleanup) reaping_row ;;
      *) exit 1 ;;
    esac
else
    echo "unexpected aws call: $*" >&2
    exit 90
fi
FAKE_AWS

cat > "$REG_TMP/bin/curl" <<'FAKE_CURL'
#!/bin/bash
set -eu
case "$*" in
  *latest/api/token*)
    echo imdsv2-token ;;
  *dynamic/instance-identity/document*)
    if [ "$FAKE_SCENARIO" = wrong_region ]; then
        printf '%s\n' '{"accountId":"928413605543","region":"us-east-1"}'
        exit 0
    fi
    printf '%s\n' '{"accountId":"928413605543","region":"us-west-1"}' ;;
  *)
    echo "unexpected curl call: $*" >&2; exit 91 ;;
esac
FAKE_CURL

cat > "$REG_TMP/bin/sudo" <<'FAKE_SUDO'
#!/bin/bash
set -eu
if [ "${1:-}" = -u ]; then shift 2; fi
exec "$@"
FAKE_SUDO

# The runner writes `.runner` with camelCase keys (RunnerSettings goes
# through VssCamelCasePropertyNamesContractResolver); the fake writes the
# same shape, so a bootstrap that reads the wrong key fails here.
cat > "$REG_TMP/work/config.sh" <<'FAKE_CONFIG'
#!/bin/bash
set -eu
case " $* " in
  *" --ephemeral "*) : ;;
  *) echo "registration is not ephemeral" >&2; exit 92 ;;
esac
case " $* " in
  *" --disableupdate "*) : ;;
  *) echo "registration permits an unverified wrapper update" >&2; exit 92 ;;
esac
echo config >> "$JOURNAL"
# The real config.sh refuses to configure over a previous registration's files.
if [ -e .runner ] || [ -e .credentials ] || [ -e .credentials_rsaparams ]; then
    echo "runner is already configured" >&2
    exit 93
fi
[ "$FAKE_SCENARIO" = config_failure ] && exit 1
case "$FAKE_SCENARIO" in
  invalid_identity|next_invalid_identity)
    printf '{"agentId":%s,"agentName":"runner-someone-else"}\n' "$FAKE_AGENT_ID" > .runner
    exit 0 ;;
esac
printf '{"agentId":%s,"agentName":"runner-%s","poolId":1,"poolName":"Default","serverUrl":"https://pipelines.actions.githubusercontent.com/x","gitHubUrl":"https://github.com/ejc3/fcvm","workFolder":"_work"}\n' "$FAKE_AGENT_ID" "$INSTANCE_ID" > .runner
FAKE_CONFIG

cat > "$REG_TMP/work/svc.sh" <<'FAKE_SVC'
#!/bin/bash
set -eu
if [ "$1" = start ]; then
    # Installing/enabling must not expose a service without its single-job
    # shutdown drop-in. This runs before the fake records a successful start.
    test -f "$FAKE_SYSTEMD_DIR/$(tr -d '\r\n' < .service).d/ephemeral.conf"
    [ "$FAKE_SCENARIO" = start_failure ] && exit 1
fi
echo "svc:$*" >> "$JOURNAL"
if [ "$1" = install ]; then
    [ "$FAKE_SCENARIO" = install_failure ] && exit 1
    if [ "$FAKE_SCENARIO" = invalid_service ]; then
        echo '../../unsafe.service' > .service
    else
        echo "actions.runner.ejc3-fcvm.runner-$INSTANCE_ID.service" > .service
    fi
fi
FAKE_SVC
cat > "$REG_TMP/bin/systemctl" <<'FAKE_SYSTEMCTL'
#!/bin/bash
set -eu
echo "systemctl:$*" >> "$JOURNAL"
FAKE_SYSTEMCTL
cat > "$REG_TMP/bin/sleep" <<'FAKE_SLEEP'
#!/bin/bash
exit 0
FAKE_SLEEP
chmod +x "$REG_TMP/bin/aws" "$REG_TMP/bin/curl" "$REG_TMP/bin/sudo" \
  "$REG_TMP/bin/systemctl" "$REG_TMP/bin/sleep" \
  "$REG_TMP/work/config.sh" "$REG_TMP/work/svc.sh" "$REG_BLOCK"

registration_case() { # scenario expected_rc expected_service expected_delete [first|next]
    local scenario=$1 want_rc=$2 want_service=$3 want_delete=$4 mode=${5:-first} rc=0
    local journal="$REG_TMP/$scenario.log" agent_id=77
    : > "$journal"
    rm -rf "$REG_TMP/work/.runner" "$REG_TMP/work/.credentials" \
      "$REG_TMP/work/.credentials_rsaparams" "$REG_TMP/work/.service" "$REG_TMP/systemd"
    if [ "$mode" = next ]; then
        # What the host's first registration and its finished job leave behind.
        # GitHub gives the next registration a new id.
        agent_id=78
        printf '{"agentId":77,"agentName":"runner-i-test"}\n' > "$REG_TMP/work/.runner"
        echo '{}' > "$REG_TMP/work/.credentials"
        echo '{}' > "$REG_TMP/work/.credentials_rsaparams"
        echo "actions.runner.ejc3-fcvm.runner-i-test.service" > "$REG_TMP/work/.service"
        mkdir -p "$REG_TMP/systemd/actions.runner.ejc3-fcvm.runner-i-test.service.d"
        : > "$REG_TMP/systemd/actions.runner.ejc3-fcvm.runner-i-test.service.d/ephemeral.conf"
    fi
    (
      cd "$REG_TMP/work"
      PATH="$REG_TMP/bin:$PATH" \
      FAKE_SCENARIO="$scenario" FAKE_MODE="$mode" FAKE_AGENT_ID="$agent_id" \
      JOURNAL="$journal" FAKE_SYSTEMD_DIR="$REG_TMP/systemd" \
      INSTANCE_ID=i-test RUNNER_LABEL=ARM64 REGION=us-west-1 \
        bash "$REG_BLOCK" "$mode"
    ) >"$REG_TMP/$scenario.output" 2>&1 || rc=$?
    if [ "$mode" = next ] && grep -q '^svc:install' "$journal"; then
        bad "$scenario: a next job reinstalled the runner service"
        return
    fi
    if grep -q 'registration-token' "$REG_TMP/$scenario.output"; then
        bad "$scenario: registration credential leaked to bootstrap output"
        return
    fi
    if [ "$rc" != 0 ] && ! grep -q '^systemctl:--no-block poweroff$' "$journal"; then
        bad "$scenario: failed bootstrap did not request disposable-host shutdown"
        return
    fi
    if [ "$rc" = 0 ] && grep -q '^systemctl:--no-block poweroff$' "$journal"; then
        bad "$scenario: successful bootstrap powered off before its job"
        return
    fi
    if { [ "$scenario" = credential_missing ] || [ "$scenario" = next_no_token ]; } \
       && [ "$(grep -c '^token-read$' "$journal")" != 36 ]; then
        bad "$scenario: credential polling exceeded or missed the bounded retry gate"
        return
    fi
    local service=0 deleted=0
    grep -q '^svc:start$' "$journal" && service=1
    grep -q '^delete$' "$journal" && deleted=1
    if [ "$service" = 1 ]; then
        local delete_line start_line
        delete_line=$(grep -n '^delete$' "$journal" | head -1 | cut -d: -f1)
        start_line=$(grep -n '^svc:start$' "$journal" | head -1 | cut -d: -f1)
        if [ -z "$delete_line" ] || [ "$delete_line" -ge "$start_line" ]; then
            bad "$scenario: service started before credential deletion"
            return
        fi
    fi
    if [ "$rc" = "$want_rc" ] && [ "$service" = "$want_service" ] \
       && [ "$deleted" = "$want_delete" ]; then
        ok "$scenario (rc=$rc service=$service delete=$deleted)"
    else
        bad "$scenario: rc=$rc service=$service delete=$deleted, wanted $want_rc/$want_service/$want_delete ($(tr '\n' ' ' < "$journal"))"
    fi
}

registration_case bootstrap          0 1 1
registration_case unknown_registered 0 1 1
registration_case credential_late    0 1 1
registration_case credential_missing 1 0 1
registration_case credential_delete_failure 1 0 0
registration_case invalid_service    1 0 1
registration_case config_failure     1 0 1
registration_case invalid_identity   1 0 1
registration_case wrong_region       1 0 1
registration_case install_failure    1 0 1
registration_case start_failure      1 0 1
registration_case cleanup            1 0 1
registration_case unread             1 0 1
# A `registered` row under this ARN that names a different runner id is not
# this bootstrap's claim. Only the identity predicates refuse it: its State is
# `registered` and its ARN, InstanceId and RunnerName all match.
registration_case foreign_registered 1 0 1

echo "next job on the same host:"
# The row a next job replaces must still name the host's previous runner (77);
# GitHub gives the new registration 78. No credential, a failed delete, a wrong
# identity or a row that moved on all power the host off without a service.
registration_case next_token            0 1 1 next
registration_case next_lost_answer      0 1 1 next
registration_case next_no_token         1 0 1 next
registration_case next_delete_failure   1 0 0 next
registration_case next_invalid_identity 1 0 1 next
registration_case next_row_moved        1 0 1 next
registration_case next_not_registered   1 0 1 next

echo "after a job:"
after_case() { # service_result expected_systemctl_call
    local result=$1 want=$2 journal="$REG_TMP/after-${1:-unset}.log"
    : > "$journal"
    ( PATH="$REG_TMP/bin:$PATH" JOURNAL="$journal" SERVICE_RESULT="$result" \
        bash "$REG_BLOCK" after ) > "$REG_TMP/after-${1:-unset}.output" 2>&1 || true
    if [ "$(tr '\n' ' ' < "$journal")" = "systemctl:$want " ]; then
        ok "service result '${result:-unset}' -> systemctl $want"
    else
        bad "service result '${result:-unset}': wanted systemctl $want, got $(tr '\n' ' ' < "$journal")"
    fi
}
after_case success   "--no-block start fcvm-runner-next.service"
after_case exit-code "--no-block poweroff"
after_case signal    "--no-block poweroff"
after_case ""        "--no-block poweroff"

echo
echo "passed=$PASS failed=$FAIL"
[ "$FAIL" = 0 ]

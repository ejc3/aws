#!/bin/bash
# github-app-runner bootstrap. User data for one ephemeral x86 runner, rendered per launch by
# runner-app/app_runner.py (it fills in the repo and labels; nothing secret is in here).
#
# Canonical's Ubuntu 24.04: no NVMe, no fcvm. The repos' workflows install their own Node/Python
# (setup-node, setup-python) and browsers (`playwright install --with-deps`, which uses apt
# through sudo), so this installs only the runner and passwordless sudo for it.
#
# A repo that opts in (`image` in runner-app.tf) boots a prebuilt image instead
# (runner-app-image.tf), whose builder ran this same file's install steps (PREPARE_ONLY, below).
# Each install step skips what is already there, so such a host goes straight to registration;
# whatever is missing, or older than this file's pins, is installed as on Canonical's image.
set -euo pipefail
exec >>/var/log/github-app-runner.log 2>&1

# The image builder (runner-app/image-build.sh) runs this file with
# GITHUB_APP_RUNNER_PREPARE_ONLY=1: install, then stop before anything that registers a runner.
# User data never sets it, so a job host always registers, runs its job and powers off.
PREPARE_ONLY=${GITHUB_APP_RUNNER_PREPARE_ONLY:-0}

# Whatever happens -- the job finished, registration failed, any step below broke -- the host
# powers off, and InstanceInitiatedShutdownBehavior=terminate deletes it. Nothing is reused.
if [ "$PREPARE_ONLY" != 1 ]; then
  trap 'sync; shutdown -h now' EXIT
fi

REPO='@@REPO@@'
LABELS='@@LABELS@@'
RUNNER_VERSION='2.337.0'
RUNNER_SHA256='70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613'
export AWS_DEFAULT_REGION=us-west-1 DEBIAN_FRONTEND=noninteractive

IMDS=http://169.254.169.254/latest
IMDS_TOKEN=$(curl -fsS -X PUT "$IMDS/api/token" -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
INSTANCE_ID=$(curl -fsS -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" "$IMDS/meta-data/instance-id")
echo "github-app-runner: $INSTANCE_ID for $REPO [$LABELS] at $(date -Is)"
if [ -f /etc/github-app-runner/image ]; then
  echo "github-app-runner: prebuilt image $(cat /etc/github-app-runner/image)"
fi

# Wait for the dpkg lock instead of failing: a fresh Ubuntu often runs apt-daily or
# unattended-upgrades while user data starts. System-wide, so it also covers apt calls made
# by the runner's own installdependencies.sh.
echo 'DPkg::Lock::Timeout "300";' > /etc/apt/apt.conf.d/90github-app-runner-lock-timeout
PACKAGES=(python3-boto3 git curl jq unzip zip build-essential ca-certificates)
MISSING=()
for package in "${PACKAGES[@]}"; do
  dpkg-query -W -f='${Status}\n' "$package" 2>/dev/null | grep -q ' ok installed$' || MISSING+=("$package")
done
if [ ${#MISSING[@]} -gt 0 ]; then
  apt-get update -q
  apt-get install -y -q "${PACKAGES[@]}"
fi

id -u runner >/dev/null 2>&1 || useradd -m -s /bin/bash runner
echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner
chmod 440 /etc/sudoers.d/runner

# A prebuilt image already has this runner, unpacked by this block, which names its version in
# .runner-version last. Any other version, or none, is downloaded and verified afresh.
DIR=/home/runner/actions-runner
if [ "$(cat "$DIR/.runner-version" 2>/dev/null || true)" != "$RUNNER_VERSION" ]; then
  rm -rf "$DIR"
  mkdir -p "$DIR"
  curl -fsSL -o /tmp/actions-runner.tgz \
    "https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz"
  echo "$RUNNER_SHA256  /tmp/actions-runner.tgz" | sha256sum -c -
  tar xzf /tmp/actions-runner.tgz -C "$DIR"
  rm -f /tmp/actions-runner.tgz
  "$DIR/bin/installdependencies.sh"
  echo "$RUNNER_VERSION" > "$DIR/.runner-version"
  chown -R runner:runner "$DIR"
fi

# The tool cache lives where GitHub's hosted images keep it. setup-python's prebuilt
# interpreters are built with that prefix: from the runner's default _work/_tool, python runs
# only with the LD_LIBRARY_PATH setup-python exports, so any subprocess started with a clean
# environment dies with "libpython3.11.so.1.0: cannot open shared object file".
TOOL_CACHE=/opt/hostedtoolcache
install -d -o runner -g runner "$TOOL_CACHE"

# Jobs run in /home/runner/work/<repo>/<repo>, the hosted runners' layout. actions/cache stores
# a path outside the workspace (~/.cache/ms-playwright, ~/.npm) relative to the workspace, so a
# cache saved on a hosted runner restored into actions-runner/.cache instead: a
# "cache hit" that left Playwright's browsers where nothing looks for them.
WORK=/home/runner/work
install -d -o runner -g runner "$WORK"

if [ "$PREPARE_ONLY" = 1 ]; then
  echo "github-app-runner: installed for an image build at $(date -Is); not registering"
  exit 0
fi

# This host's one-time registration token. The controller writes it only after EC2 accepted
# the launch, tagged with this instance's ARN, which is the only parameter this role can read;
# it is deleted as soon as it is read.
REG_TOKEN=$(python3 - "$INSTANCE_ID" <<'PY'
import sys
import time

import boto3
import botocore.exceptions

ssm = boto3.client('ssm')
name = f'/github-runner/bootstrap/{sys.argv[1]}'
for _ in range(60):
    try:
        value = ssm.get_parameter(Name=name, WithDecryption=True)['Parameter']['Value']
        break
    except ssm.exceptions.ParameterNotFound:
        time.sleep(2)
    except botocore.exceptions.ClientError as error:
        # The role may read only a parameter tagged with this instance's ARN. Before the
        # controller has written it there is no tag to match, so IAM answers AccessDenied, not
        # ParameterNotFound: that is "not yet", within the same bounded wait.
        if error.response.get('Error', {}).get('Code') not in ('AccessDeniedException', 'AccessDenied'):
            raise
        time.sleep(2)
else:
    sys.exit('no bootstrap credential arrived')
ssm.delete_parameter(Name=name)
print(value)
PY
)

# The token reaches config.sh as ACTIONS_RUNNER_INPUT_TOKEN, never as an argument: argv is
# readable by every local process through /proc/<pid>/cmdline, and the token is live until
# registration completes. Same as fcvm's bootstrap (runner-autoscale.tf).
cd "$DIR"
printf '%s' "$REG_TOKEN" | sudo -u runner -H bash -c \
  'ACTIONS_RUNNER_INPUT_TOKEN=$(cat) exec ./config.sh --unattended --url "https://github.com/$1" --name "$2" --labels "$3" --ephemeral --disableupdate --work "$4"' \
  _ "$REPO" "$INSTANCE_ID" "$LABELS" "$WORK"
unset REG_TOKEN

# .env is read by run.sh and handed to every job: AGENT_TOOLSDIRECTORY for the setup-* actions,
# RUNNER_TOOL_CACHE for the runner's own ${{ runner.tool_cache }}.
printf 'AGENT_TOOLSDIRECTORY=%s\nRUNNER_TOOL_CACHE=%s\n' "$TOOL_CACHE" "$TOOL_CACHE" >> "$DIR/.env"

# .path is the PATH config.sh saw (sudo's secure_path), which run.sh hands every job. A prebuilt
# image lists the directories its toolchains live in (runner-app/images/), one per line, so a
# job's setup finds them already installed; Canonical's image has no such file.
if [ -s /etc/github-app-runner/path ]; then
  EXTRA_PATH=$(paste -sd: /etc/github-app-runner/path)
  JOB_PATH=$(cat "$DIR/.path")
  printf '%s:%s\n' "$EXTRA_PATH" "$JOB_PATH" > "$DIR/.path"
fi

sudo -u runner -H ./run.sh
echo "github-app-runner: job finished at $(date -Is); powering off"

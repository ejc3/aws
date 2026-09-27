#!/bin/bash
# github-app-runner bootstrap. User data for one ephemeral x86 runner, rendered per launch by
# runner-app/app_runner.py (it fills in the repo and labels; nothing secret is in here).
#
# Stock Ubuntu 24.04: no NVMe, no fcvm, no baked image. The repos' workflows install their own
# Node/Python (setup-node, setup-python) and browsers (`playwright install --with-deps`, which
# uses apt through sudo), so this installs only the runner and passwordless sudo for it.
set -euo pipefail
exec >>/var/log/github-app-runner.log 2>&1

# Whatever happens -- the job finished, registration failed, any step below broke -- the host
# powers off, and InstanceInitiatedShutdownBehavior=terminate deletes it. Nothing is reused.
trap 'sync; shutdown -h now' EXIT

REPO='@@REPO@@'
LABELS='@@LABELS@@'
RUNNER_VERSION='2.337.0'
RUNNER_SHA256='70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613'
export AWS_DEFAULT_REGION=us-west-1 DEBIAN_FRONTEND=noninteractive

IMDS=http://169.254.169.254/latest
IMDS_TOKEN=$(curl -fsS -X PUT "$IMDS/api/token" -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')
INSTANCE_ID=$(curl -fsS -H "X-aws-ec2-metadata-token: $IMDS_TOKEN" "$IMDS/meta-data/instance-id")
echo "github-app-runner: $INSTANCE_ID for $REPO [$LABELS] at $(date -Is)"

# Wait for the dpkg lock instead of failing: a fresh Ubuntu often runs apt-daily or
# unattended-upgrades while user data starts. System-wide, so it also covers apt calls made
# by the runner's own installdependencies.sh.
echo 'DPkg::Lock::Timeout "300";' > /etc/apt/apt.conf.d/90github-app-runner-lock-timeout
apt-get update -q
apt-get install -y -q python3-boto3 git curl jq unzip zip build-essential ca-certificates

useradd -m -s /bin/bash runner
echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner
chmod 440 /etc/sudoers.d/runner

DIR=/home/runner/actions-runner
mkdir -p "$DIR"
curl -fsSL -o /tmp/actions-runner.tgz \
  "https://github.com/actions/runner/releases/download/v$RUNNER_VERSION/actions-runner-linux-x64-$RUNNER_VERSION.tar.gz"
echo "$RUNNER_SHA256  /tmp/actions-runner.tgz" | sha256sum -c -
tar xzf /tmp/actions-runner.tgz -C "$DIR"
rm -f /tmp/actions-runner.tgz
"$DIR/bin/installdependencies.sh"
chown -R runner:runner "$DIR"

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
  'ACTIONS_RUNNER_INPUT_TOKEN=$(cat) exec ./config.sh --unattended --url "https://github.com/$1" --name "$2" --labels "$3" --ephemeral --disableupdate --work _work' \
  _ "$REPO" "$INSTANCE_ID" "$LABELS"
unset REG_TOKEN

sudo -u runner -H ./run.sh
echo "github-app-runner: job finished at $(date -Is); powering off"

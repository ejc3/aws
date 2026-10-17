#!/bin/bash
# github-app-runner image build. User data for one builder host, rendered by
# runner-app/image_builder.py (runner-app-image.tf); nothing secret is in here.
#
# Builds the prebuilt image @@IMAGE@@ for @@REPO@@ on Canonical's Ubuntu 24.04, in two steps:
#   1. everything a job host's bootstrap installs, by that same bootstrap (runner-app/bootstrap.sh)
#      with GITHUB_APP_RUNNER_PREPARE_ONLY=1, so the runner is its pinned, verified version and
#      nothing is registered;
#   2. the image's own toolchains (runner-app/images/), at the versions runner-app-image.tf pins.
# The host reports by one tag, BuildStatus=complete, then powers off (shutdown behaviour: stop).
# The builder Lambda images a host that stopped with that tag and terminates any other.
set -euo pipefail
exec >>/var/log/github-app-runner-image.log 2>&1

# Whatever happens, the host powers off. On a failure the end of both logs goes to the serial
# console first, which the builder Lambda prints to its own log before it terminates the host.
trap 'status=$?
if [ "$status" -ne 0 ]; then
  { echo "github-app-runner-image: FAILED ($status)"; tail -n 30 /var/log/github-app-runner.log
    tail -n 60 /var/log/github-app-runner-image.log; } > /dev/ttyS0 2>&1 || true
fi
sync; shutdown -h now' EXIT

export DEBIAN_FRONTEND=noninteractive AWS_DEFAULT_REGION=us-west-1
export IMAGE='@@IMAGE@@' REPO='@@REPO@@'
@@VERSIONS@@
echo "github-app-runner-image: building $IMAGE for $REPO at $(date -Is)"

cat > /root/github-app-runner-bootstrap.sh <<'GITHUB_APP_RUNNER_BOOTSTRAP'
@@BOOTSTRAP@@
GITHUB_APP_RUNNER_BOOTSTRAP
GITHUB_APP_RUNNER_PREPARE_ONLY=1 bash /root/github-app-runner-bootstrap.sh

cat > /root/github-app-runner-image.sh <<'GITHUB_APP_RUNNER_IMAGE'
@@IMAGE_SCRIPT@@
GITHUB_APP_RUNNER_IMAGE
bash /root/github-app-runner-image.sh

# What a job host's bootstrap reports, so a job's log says which image it started from.
mkdir -p /etc/github-app-runner
echo "$IMAGE for $REPO, built $(date -u +%Y-%m-%dT%H:%MZ)" > /etc/github-app-runner/image

# Nothing of this host's own identity or logs goes into the image. Package lists go too, as on
# Canonical's image: a job that installs anything updates them first.
apt-get clean
rm -rf /var/lib/apt/lists/* /root/github-app-runner-*.sh /root/.npm /root/.cache /tmp/* /var/tmp/*
: > /var/log/github-app-runner.log
truncate -s 0 /etc/machine-id
rm -f /var/lib/dbus/machine-id
sync

python3 - <<'PY'
import urllib.request

import boto3

imds = 'http://169.254.169.254/latest'
token = urllib.request.urlopen(urllib.request.Request(
    f'{imds}/api/token', method='PUT', headers={'X-aws-ec2-metadata-token-ttl-seconds': '300'}), timeout=5).read().decode()
instance_id = urllib.request.urlopen(urllib.request.Request(
    f'{imds}/meta-data/instance-id', headers={'X-aws-ec2-metadata-token': token}), timeout=5).read().decode()
boto3.client('ec2', region_name='us-west-1').create_tags(
    Resources=[instance_id], Tags=[{'Key': 'BuildStatus', 'Value': 'complete'}])
PY
echo "github-app-runner-image: $IMAGE complete at $(date -Is); powering off"

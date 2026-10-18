# nextjs-ct-user-data.tf
#
# Setup script of the containerized nextjs box (nextjs-ct.tf), published to S3 and applied at
# boot and by setup-sync whenever it changes. The host is an admin and routing layer: base
# packages, the agents AWS needs on every host (SSM, the hop key for the admin account), and
# setup-sync itself. The per-person containers are added on top of this.
#
# Plain scripts live in scripts/nextjs-ct/ and are embedded base64-encoded, so they are never
# re-read as Terraform templates and the tests run the exact bytes the box runs.

locals {
  nextjs_ct_files = {
    "/usr/local/bin/setup-sync"        = "scripts/nextjs-ct/setup-sync"
    "/usr/local/sbin/nextjs-ct-health" = "scripts/nextjs-ct/nextjs-ct-health"
  }

  nextjs_ct_install_files = join("\n", [
    for dest, src in local.nextjs_ct_files :
    "install -d \"$(dirname '${dest}')\" && printf '%s' '${base64encode(file("${path.module}/${src}"))}' | base64 -d > '${dest}.new' && chmod 755 '${dest}.new' && mv -f '${dest}.new' '${dest}'"
  ])

  nextjs_ct_user_data = <<-SCRIPT
#!/bin/bash
set -uxo pipefail
export DEBIAN_FRONTEND=noninteractive
# One setup at a time: the first boot's run and setup-sync's can otherwise overlap. setup-sync takes this
# lock itself and says so; a run started any other way waits for it.
if [ -z "$${NEXTJS_CT_SETUP_LOCK_HELD:-}" ]; then
  exec 9>/run/nextjs-ct-setup.lock
  flock -w 3600 9 || { echo "ERROR: another nextjs-ct setup still holds the lock"; exit 1; }
fi

# setup-sync first: if anything below fails, the timer is already there to retry the whole script.
# ---------------------------------------------------------------- plain scripts
${local.nextjs_ct_install_files}

# ---------------------------------------------------------------- setup-sync
cat > /etc/systemd/system/setup-sync.service <<'UNIT'
[Unit]
Description=Apply the published nextjs-ct setup script when it changes
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/setup-sync
UNIT
cat > /etc/systemd/system/setup-sync.timer <<'UNIT'
[Unit]
Description=Check for a newly published nextjs-ct setup script

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now setup-sync.timer >/dev/null 2>&1 || true

# ---------------------------------------------------------------- base packages
# Required: a failure stops the run, so setup-sync does not record a half-done setup and retries.
apt-get -o DPkg::Lock::Timeout=600 update || true
apt-get -o DPkg::Lock::Timeout=600 install -y curl git jq unzip python3 rsync systemd-container debootstrap nftables \
  || { echo "ERROR: required packages did not install"; exit 1; }
if ! command -v aws >/dev/null 2>&1; then
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip \
    && (cd /tmp && unzip -qo awscliv2.zip && ./aws/install) \
    || { echo "ERROR: the AWS CLI did not install"; exit 1; }
  rm -rf /tmp/aws /tmp/awscliv2.zip
fi

# ---------------------------------------------------------------- ssm agent
# Ubuntu ships it as a snap; the IAM policy that lets it register is on nextjs-dev-role.
snap list amazon-ssm-agent >/dev/null 2>&1 \
  && systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service >/dev/null 2>&1 \
  || echo "WARNING: ssm agent not running"

# ---------------------------------------------------------------- admin hops
# The hop key for the admin account (ubuntu) only: it reaches the other dev servers, never a jumpbox.
${local.dev_hop_setup}

# ---------------------------------------------------------------- done
# Healthy or not done: the exit status is what setup-sync (and the first boot) go by. The ETag of the
# script that ran is recorded here as well as by setup-sync, so the timer's first tick after the first
# boot does not run the same script again.
/usr/local/sbin/nextjs-ct-health || { echo "ERROR: nextjs-ct host is not healthy"; exit 1; }
if [ -n "$${NEXTJS_CT_ETAG:-}" ]; then
  mkdir -p /var/lib/nextjs-setup && printf '%s' "$NEXTJS_CT_ETAG" > /var/lib/nextjs-setup/applied-etag
fi
echo "nextjs-ct host ready"
SCRIPT
}

resource "aws_s3_object" "nextjs_ct_user_data" {
  count        = var.enable_nextjs_ct ? 1 : 0
  bucket       = aws_s3_bucket.dev_scripts.id
  key          = "user-data/nextjs-ct.sh"
  content      = local.nextjs_ct_user_data
  content_type = "text/x-shellscript"
  tags         = { Name = "nextjs-ct-user-data" }
}

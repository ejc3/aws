# dev-hop-key.tf
#
# A dedicated SSH key for hopping BETWEEN dev servers.
#
# THE PROBLEM THIS SOLVES: fcvm-metal-arm was carrying ~/.ssh/fcvm-ec2 -- the private half
# of the key that logs in to the jumpbox. Nothing on the box used it (runner access goes
# through ~/.ssh/runner_key, per ~/.ssh/config), but anything able to read that file could
# SSH to the jumpbox, which is where the AWS admin session lives. That inverts the intended
# trust direction: phone -> jumpbox -> dev server, never dev server -> jumpbox. It matters
# more than usual here because the dev boxes run agents with --dangerously-skip-permissions.
#
# THE FIX: a separate keypair whose public half is authorized ONLY on dev servers, never on
# the jumpbox. Dev boxes can then reach each other, and holding this key gets you nothing
# but another dev box. The fcvm-ec2 private key is deleted from the dev servers.
#
# WHY A KEY AT ALL, rather than `ssh -A` agent forwarding: agent forwarding would leave no
# key material on disk and is strictly better -- but only when you arrive via the jumpbox.
# Connecting from the phone straight to a dev server, which is a supported path here, has
# no agent to forward, and hops would silently stop working.

resource "tls_private_key" "dev_hop" {
  algorithm = "ED25519"
}

resource "aws_secretsmanager_secret" "dev_hop" {
  name        = "dev-hop-ssh-key"
  description = "SSH key for dev-server-to-dev-server hops. Deliberately NOT authorized on the jumpbox."

  # Long enough to notice and restore a mistake, short enough that a rotation actually
  # takes effect rather than leaving the old key recoverable for a month.
  recovery_window_in_days = 7

  tags = { Name = "dev-hop-ssh-key", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_version" "dev_hop" {
  secret_id = aws_secretsmanager_secret.dev_hop.id
  secret_string = jsonencode({
    private = tls_private_key.dev_hop.private_key_openssh
    public  = trimspace(tls_private_key.dev_hop.public_key_openssh)
  })
}

# ---------------------------------------------------------------------------------
# Hop targets, keyed by stable addresses -- never an auto-assigned address.
#
# Private IPs change whenever an instance is replaced (a spot reclaim, an on-demand
# rebuild). We hit this directly: nextjs-dev's private IP moved from 10.0.1.243 to
# 10.0.1.68 during the spot->on-demand migration, the old address stayed cached in
# ~/.ssh/config.d-devhop on fcvm-metal-arm until its next boot, and the hop silently timed
# out. The three us-west-1 boxes therefore use Elastic IPs, which Terraform re-associates
# on replacement. io-box is the deliberate exception: it has an explicitly assigned
# 172.31.48.10 address reached over VPC peering, so that private endpoint is just as stable
# without paying for an always-reserved public IPv4 address.
# ---------------------------------------------------------------------------------
locals {
  dev_hop_targets = {
    for k, v in {
      fcvm-arm = { tag = "fcvm-metal-arm", user = "ubuntu", enabled = var.enable_firecracker_instance, ip = var.enable_firecracker_instance ? aws_eip.firecracker_dev[0].public_ip : "" }
      fcvm-x86 = { tag = "fcvm-metal-x86", user = "ubuntu", enabled = var.enable_x86_dev_instance, ip = var.enable_x86_dev_instance ? aws_eip.x86_dev[0].public_ip : "" }
      nextjs   = { tag = "nextjs-dev", user = "ubuntu", enabled = var.enable_nextjs_dev, ip = var.enable_nextjs_dev ? aws_eip.nextjs_dev[0].public_ip : "" }
      # Same box as nextjs, but the dolphin-labs checkout lives in ejc3's account, so this hop
      # logs in as ejc3. nextjs-user-data.tf authorizes the hop key for ejc3 to match.
      dolphin = { tag = "dolphin-labs", user = "ejc3", enabled = var.enable_nextjs_dev, ip = var.enable_nextjs_dev ? aws_eip.nextjs_dev[0].public_ip : "" }
      io      = { tag = "io-box", user = "ubuntu", enabled = true, ip = local.io_box_private_ip }
    } : k => v if v.enabled
  }

  dev_hop_ssh_config = join("\n", [
    for alias, t in local.dev_hop_targets :
    "Host ${alias} ${t.tag}\n    HostName ${t.ip}\n    User ${t.user}\n    IdentityFile ~/.ssh/dev_hop\n    StrictHostKeyChecking accept-new\n"
  ])
}

# ---------------------------------------------------------------------------------
# Installed for the `ubuntu` user only -- NOT for the kids' accounts on the Next.js box.
# colton and connor have no business reaching the Firecracker metal boxes, and /home/ubuntu
# is 0700 so they cannot read it there either.
# ---------------------------------------------------------------------------------
locals {
  dev_hop_setup = <<-HOP
# Fetch the shared dev-hop key and wire up host aliases.
#
# HOPJSON holds the PRIVATE key, so tracing is off from the fetch until it is unset. The
# metal boxes run this under `set -euxo pipefail`, where the assignment, the `[ -n ]` test and
# both printf pipelines each printed the key into cloud-init-output.log, the journal,
# dev-selfupdate.log and the EC2 serial console (readable through ec2:GetConsoleOutput).
# The caller's tracing is restored afterwards; nothing below this block touches the key.
case $- in *x*) HOP_XTRACE=1 ;; *) HOP_XTRACE="" ;; esac
set +x
HOP_OK=""
HOPJSON=$(aws secretsmanager get-secret-value --secret-id dev-hop-ssh-key \
  --region us-west-1 --query SecretString --output text 2>/dev/null) || HOPJSON=""
if [ -n "$HOPJSON" ]; then
  install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
  printf '%s' "$HOPJSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["private"])' \
    > /home/ubuntu/.ssh/dev_hop
  printf '%s' "$HOPJSON" | python3 -c 'import sys,json;print(json.load(sys.stdin)["public"])' \
    > /home/ubuntu/.ssh/dev_hop.pub
  HOP_OK=1
fi
unset HOPJSON
if [ -n "$HOP_XTRACE" ]; then set -x; fi

if [ -n "$HOP_OK" ]; then
  chmod 600 /home/ubuntu/.ssh/dev_hop
  chmod 644 /home/ubuntu/.ssh/dev_hop.pub
  chown ubuntu:ubuntu /home/ubuntu/.ssh/dev_hop /home/ubuntu/.ssh/dev_hop.pub

  # Authorize it for inbound hops. grep -qxF so re-running never appends a duplicate --
  # the runner_key block in ~/.ssh/config grew to three identical copies exactly this way.
  HOPPUB=$(cat /home/ubuntu/.ssh/dev_hop.pub)
  touch /home/ubuntu/.ssh/authorized_keys
  grep -qxF "$HOPPUB" /home/ubuntu/.ssh/authorized_keys 2>/dev/null || \
    echo "$HOPPUB" >> /home/ubuntu/.ssh/authorized_keys
  chmod 600 /home/ubuntu/.ssh/authorized_keys
  chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys

  # Host aliases, written to their own file and Included, so the block is replaceable
  # wholesale and can never accumulate duplicates in the main config.
  #
  # This content is STATIC -- rendered by terraform from stable addresses at apply time,
  # not looked up from the AWS API at boot. There is nothing here that goes stale between
  # boots; it only changes when terraform re-applies, at which point the updated setup
  # script needs to be re-fetched and re-run on each box (same as any other change here).
  cat > /home/ubuntu/.ssh/config.d-devhop <<'DEVHOPCFG'
# generated by terraform (dev-hop-key.tf) from stable addresses -- do not edit by hand

${local.dev_hop_ssh_config}
DEVHOPCFG
  chown ubuntu:ubuntu /home/ubuntu/.ssh/config.d-devhop
  chmod 600 /home/ubuntu/.ssh/config.d-devhop

  # Include must be the FIRST line: ssh takes the first value it sees for any keyword, so
  # an Include placed after an existing `Host *` block would be shadowed by it.
  #
  # Prepend with cat, NOT `sed -i '1i'` -- sed's insert is a no-op on an empty file (there
  # is no line 1 to insert before). That silently did nothing on the Next.js box, whose
  # config had just been created empty, while working on the metal box whose config already
  # had content. The failure mode is invisible: the aliases file exists and looks right,
  # but ssh never reads it.
  touch /home/ubuntu/.ssh/config
  if ! grep -qxF "Include ~/.ssh/config.d-devhop" /home/ubuntu/.ssh/config 2>/dev/null; then
    TMPCFG=$(mktemp)
    printf 'Include ~/.ssh/config.d-devhop\n\n' > "$TMPCFG"
    cat /home/ubuntu/.ssh/config >> "$TMPCFG"
    cat "$TMPCFG" > /home/ubuntu/.ssh/config
    rm -f "$TMPCFG"
  fi
  chown ubuntu:ubuntu /home/ubuntu/.ssh/config
  chmod 600 /home/ubuntu/.ssh/config

  # The whole point: the jumpbox's key must not live on a dev server. Nothing here uses it
  # (verified: ~/.ssh/config routes runners through runner_key, and the only reference to
  # fcvm-ec2 on the box was the PUBLIC half in authorized_keys, for inbound login).
  if [ -f /home/ubuntu/.ssh/fcvm-ec2 ]; then
    shred -u /home/ubuntu/.ssh/fcvm-ec2 2>/dev/null || rm -f /home/ubuntu/.ssh/fcvm-ec2
    echo "removed jumpbox private key from this dev server"
  fi
  rm -f /home/ubuntu/.ssh/fcvm-ec2.pub
else
  echo "WARNING: could not read dev-hop-ssh-key; leaving ssh config alone"
fi
HOP
}

output "dev_hop_usage" {
  description = "How to hop between dev servers"
  value       = "from any dev server, as ubuntu: ssh fcvm-arm | ssh fcvm-x86 | ssh nextjs | ssh dolphin (ejc3 on nextjs) | ssh io"
}

# nextjs-user-data.tf
#
# Setup script for the Next.js dev box, published to S3 and fetched at boot (same
# pattern as the other dev boxes, so it can be updated without recreating the instance).
#
# Three things it sets up:
#   1. Separate Unix users (colton, connor, ejc3, skevh) so each has their own home, their own
#      `gh auth login`, and their own projects. Nobody shares a GitHub identity.
#   2. cloudflared, running as a system service, holding the tunnel to Cloudflare.
#   3. `ndev` -- run it in a Next.js project and it starts `next dev` on a deterministic
#      port, registers <$USER> or <$USER>-<slug>.<their zone> -> that port with the tunnel,
#      and prints the URL. Multiple worktree publishes stay up concurrently. Cloudflare
#      Access gates every hostname; which zone and which identity provider follows from the
#      account (cc-games/Google, dolphin-labs/GitHub).

locals {
  nextjs_tunnel_id = "60234535-279b-4b20-bbc3-7fd353abb7f6"
  nextjs_domain    = "cc-games.dev"

  # Second zone on the same box. Deliberately a SEPARATE tunnel from cc-games: a bad
  # ingress rewrite on one project must not take the other project's URLs down, and these
  # are different people's work.
  #
  # Read from the resource rather than pasted in, so that recreating the tunnel cannot
  # leave the box pointing at a UUID that no longer exists. Null when the zone is disabled.
  dolphin_tunnel_id = one(cloudflare_zero_trust_tunnel_cloudflared.dolphin_labs[*].id)
  dolphin_domain    = "dolphin-labs.dev"

  # Zone -> tunnel. dolphin drops out entirely when the zone is off, which is what makes
  # the "no zone" branch in ndev-zone reachable instead of theoretical.
  nextjs_zone_tunnel = merge(
    { (local.nextjs_domain) = local.nextjs_tunnel_id },
    local.dolphin_tunnel_id == null ? {} : { (local.dolphin_domain) = local.dolphin_tunnel_id },
  )

  # Which zone a user publishes to. Derived from the account rather than chosen at publish
  # time, so nobody can accidentally put a preview on the other project's domain -- and so
  # `ndev` stays a bare command with no flags to remember. Users whose zone is not currently
  # enabled are simply absent, and ndev refuses for them by name.
  nextjs_user_zone = {
    for u, z in {
      colton = local.nextjs_domain
      connor = local.nextjs_domain
      ejc3   = local.dolphin_domain
      skevh  = local.dolphin_domain
    } : u => z if contains(keys(local.nextjs_zone_tunnel), z)
  }

  # The case arms below are built as data rather than with %{ for } directives inside the
  # heredoc: the directive form trims the newlines between arms and emits the whole case
  # statement on one line. Valid shell, but unreadable when debugging on the box.
  nextjs_zone_arms = join("\n", [for z, t in local.nextjs_zone_tunnel : "      ${z}) echo \"${t}\" ;;"])
  nextjs_user_arms = join("\n", [for u, z in local.nextjs_user_zone : "      ${u}) echo \"${z}\" ;;"])

  # Same reason as the case arms: these emit whole STATEMENTS, and the directive form ran
  # them together into "systemctl daemon-reloadsystemctl enable ..." -- a syntax error
  # rather than merely ugly, because statements need a separator and case arms do not.
  nextjs_zone_rebuild = join("\n", [
    for z, t in local.nextjs_zone_tunnel :
    "CHANGED=0\n/usr/local/bin/ndev-rebuild ${z} || CHANGED=$?\nif [ \"$CHANGED\" = \"10\" ]; then\n  systemctl restart \"cloudflared@${z}\"\nelse\n  systemctl is-active --quiet \"cloudflared@${z}\" || systemctl start \"cloudflared@${z}\"\nfi\ntrue"
  ])
  # Enable for boot, and START only if it is down. An unchanged setup must not restart
  # either tunnel. cloudflared cannot reload local ingress on SIGHUP; only a changed
  # zone is restarted by nextjs_zone_rebuild, briefly interrupting that zone's connections.
  nextjs_zone_enable = join("\n", [
    for z, t in local.nextjs_zone_tunnel :
    "systemctl enable \"cloudflared@${z}\" >/dev/null 2>&1 || true\nsystemctl is-active --quiet \"cloudflared@${z}\" || systemctl start \"cloudflared@${z}\" || echo \"WARNING: cloudflared@${z} did not start\""
  ])

  # Unix accounts. Each gets the fcvm key initially so you can get in as them and help;
  # they can add their own keys to ~/.ssh/authorized_keys afterwards.
  # `ej` was replaced by `ejc3`, matching the GitHub login so the account name, the git
  # identity and the repos all read the same. Created FRESH rather than renamed: the old
  # home held only tooling (zsh/atuin/fzf) with no credentials and no repos, and building
  # it from nothing exercises the same path a new person takes -- which is the point, since
  # `skevh` is arriving the same way. A rename would have preserved 363MB and proven
  # nothing. This list only decides which accounts must EXIST; removing a name from it does
  # not delete the account.
  nextjs_users = ["colton", "connor", "ejc3", "skevh"]

  # Keys the account owner logs in with, on top of the shared fcvm key above. Declared
  # here rather than pasted into the box so a rebuild does not lose someone's access --
  # the fcvm key is for US getting in to help, this is for THEM getting in at all.
  # Public keys only; nothing secret lives in this file.
  # A LIST per user, not a single string. ejc3 legitimately carries several keys (a personal
  # one plus service keys like the instinct bot), and the old map(string) could hold exactly
  # one -- so every additional key had to be installed by hand, which meant terraform did not
  # know about it and a rebuild would silently drop it.
  nextjs_user_keys = {
    skevh = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEdvVbYeu8+3tHPYk/A/67qa5yoTaagVSaW+iQQncUVA stevekrutzler@Steves-iMac.local"]
    # All of ejc3's keys, including two that were installed by hand and existed only on
    # the running instance -- invisible to terraform and silently lost on any rebuild.
    # Adopted 2026-09-11 by reading them off the box; declaring them is what makes them
    # survive. If a key here is no longer wanted, move it to nextjs_retired_user_keys
    # rather than deleting the line, or the append-only installer leaves it authorized.
    ejc3 = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIPSxIJ95P2xn4qJpFoGlRMpzstp5RTbj5KJAh2JH5UVi dolphin-labs-instinct-2026-09-11",
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGZTP2GqL7R1kFzSqoI6QLo3j/VacE9MK+tuXmHLCAFn hatch",
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAILzudSh+XRY5YsnNnAWDTjKXNeZUueYq/etoVXsTrpx4 grok-bot-box-ejc3",
    ]
  }

  # Flattened to "user|key" lines so the script can be fed ONE interpolation instead of a
  # nested terraform for-directive. The ~ trim markers on those directives strip the newline
  # on both sides and weld generated statements onto the previous line; that has already
  # broken this file once.
  nextjs_user_key_lines = flatten([
    for ku, keys in local.nextjs_user_keys : [for kk in keys : "${ku}|${kk}"]
  ])

  # Keys that were here and are NOT any more. The block below only ever APPENDS to
  # authorized_keys (grep -qxF || echo >>), which is right -- it must never clobber a key
  # someone added by hand -- but it means editing nextjs_user_keys adds the new key and
  # leaves the old one authorized forever. Anything retired therefore has to be named here
  # so it can be removed explicitly.
  #
  # skevh@yahoo.com: generated for Steve with a passphrase that was then lost, so nobody
  # can use it. Retired 2026-08-17.
  nextjs_retired_user_keys = [
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIA3GGQEbA+7pChjnXMBagHA1G26vH8BQJj9Bgva21eVH skevh@yahoo.com",
  ]

  # Repositories to lay down for a user on first setup, so the box is useful the moment
  # they log in instead of starting at an empty home directory. Cloned only when that
  # user's own `gh` is authenticated -- these are private repos, and cloning them with
  # anyone else's credentials would either fail or, worse, leave a token behind.
  nextjs_user_repos = {
    skevh = ["dolphin-labs-hq/dolphin-labs"]
    ejc3  = ["dolphin-labs-hq/dolphin-labs"]
  }

  nextjs_user_data = <<-SCRIPT
#!/bin/bash
set -uxo pipefail

# ---------------------------------------------------------------- base packages
export DEBIAN_FRONTEND=noninteractive
apt-get update || true
# python3-venv is here because Ubuntu ships `venv` without `ensurepip`, so `python3 -m venv`
# fails on a stock image with an error that reads like a Python bug rather than a missing
# package. dolphin-labs' data pipeline is the first thing a new person runs, and it starts
# with exactly that command.
apt-get install -y curl git build-essential zsh jq unzip python3-venv || echo "WARNING: some packages failed"
# No awscli apt package on Ubuntu 24.04 -- use the official installer.
if ! command -v aws >/dev/null 2>&1; then
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip
  (cd /tmp && unzip -qo awscliv2.zip && ./aws/install && rm -rf /tmp/aws /tmp/awscliv2.zip)
fi

# Node 22 (Next.js needs a modern runtime)
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
  apt-get install -y nodejs
fi
npm install -g pnpm >/dev/null 2>&1 || true

# ---------------------------------------------------------------- cloudflared
if ! command -v cloudflared >/dev/null 2>&1; then
  ARCH=$(dpkg --print-architecture)
  curl -fsSL -o /tmp/cloudflared.deb \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$${ARCH}.deb"
  dpkg -i /tmp/cloudflared.deb && rm -f /tmp/cloudflared.deb
fi

mkdir -p /etc/cloudflared
# Credentials come from Secrets Manager, never baked into this script or the AMI.
# One file per zone; the tunnel id is embedded so cloudflared can match them up.
aws secretsmanager get-secret-value --secret-id cloudflare-tunnel-credentials \
  --region us-west-1 --query SecretString --output text > /etc/cloudflared/creds-cc-games.dev.json
aws secretsmanager get-secret-value --secret-id cloudflare-dolphin-tunnel-credentials \
  --region us-west-1 --query SecretString --output text > /etc/cloudflared/creds-dolphin-labs.dev.json
chmod 600 /etc/cloudflared/creds-*.json

# The dolphin secret holds only the TunnelSecret; cloudflared also wants AccountTag and
# TunnelID in the same file. Fill them in rather than storing three copies of the same
# facts in Secrets Manager. Both come from the Terraform resource, so replacing the tunnel
# cannot leave the credentials naming one id while the ingress config names another.
python3 - <<'CREDFIX'
import json
p = "/etc/cloudflared/creds-dolphin-labs.dev.json"
d = json.load(open(p))
d.setdefault("AccountTag", "${var.cloudflare_account_id}")
d.setdefault("TunnelID", "${local.dolphin_tunnel_id}")
json.dump(d, open(p, "w"))
CREDFIX
chmod 600 /etc/cloudflared/creds-dolphin-labs.dev.json

# Keep the old path working for anything that still references it.
ln -sf /etc/cloudflared/creds-cc-games.dev.json /etc/cloudflared/credentials.json

# ------------------------------------------------------------- cloudwatch agent
# Memory is the constraint on this box and CloudWatch does not collect it by default. When
# the box fell over we had to INFER memory pressure from VolumeReadBytes, because no memory
# or swap metric existed. This publishes mem_used_percent and swap_used_percent so the
# leading indicator is visible instead of the consequence.
if ! systemctl is-active --quiet amazon-cloudwatch-agent 2>/dev/null; then
  CWA_ARCH=arm64
  case "$(uname -m)" in x86_64) CWA_ARCH=amd64 ;; esac
  CWA_TMP=$(mktemp /tmp/cwagent.XXXXXX.deb)
  if curl -fsSL --retry 3 \
    "https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/$CWA_ARCH/latest/amazon-cloudwatch-agent.deb" \
    -o "$CWA_TMP" && [ -s "$CWA_TMP" ]; then
    dpkg -i "$CWA_TMP" >/dev/null 2>&1 || apt-get install -y -f >/dev/null 2>&1
    mkdir -p /opt/aws/amazon-cloudwatch-agent/etc
    cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json <<'CWACONF'
{
  "agent": { "metrics_collection_interval": 60 },
  "metrics": {
    "namespace": "CWAgent",
    "append_dimensions": { "InstanceId": "$${aws:InstanceId}" },
    "aggregation_dimensions": [["InstanceId"]],
    "metrics_collected": {
      "mem": { "measurement": ["mem_used_percent", "mem_available"], "metrics_collection_interval": 60 },
      "swap": { "measurement": ["swap_used_percent"], "metrics_collection_interval": 60 }
    }
  }
}
CWACONF
    /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a fetch-config -m ec2 \
      -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json -s >/dev/null 2>&1 \
      || echo "WARNING: cloudwatch agent installed but did not start"
  else
    echo "WARNING: could not download the cloudwatch agent; memory stays invisible"
  fi
  rm -f "$CWA_TMP"
fi

# ---------------------------------------------------------------------- ssm agent
# The out-of-band way in. sshd on this box once stopped completing its handshake under load,
# and there was no second channel: no way to read the load average, name the runaway, or
# even tell whether the machine was alive.
#
# The agent was NOT missing -- Ubuntu's AMI ships it as a SNAP, running as
# snap.amazon-ssm-agent.amazon-ssm-agent.service. Two things hid that:
#   * `systemctl is-active amazon-ssm-agent` reports inactive, because that unit name does
#     not exist on a snap install.
#   * installing the .deb does not "fix" it -- the package preinst refuses outright
#     ("installed in this instance by snap, please use snap") and exits 1.
# What was actually missing was AmazonSSMManagedInstanceCore on the instance role, so the
# agent ran for weeks and could never register. That is in nextjs-dev.tf.
if snap list amazon-ssm-agent >/dev/null 2>&1; then
  systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent.service >/dev/null 2>&1 \
    || echo "WARNING: snap ssm agent present but would not start"
elif ! systemctl is-active --quiet amazon-ssm-agent 2>/dev/null; then
  SSM_ARCH=arm64
  case "$(uname -m)" in x86_64) SSM_ARCH=amd64 ;; esac
  SSM_TMP=$(mktemp /tmp/ssm-agent.XXXXXX.deb)
  if curl -fsSL --retry 3 \
    "https://s3.us-east-1.amazonaws.com/amazon-ssm-us-east-1/latest/debian_$SSM_ARCH/amazon-ssm-agent.deb" \
    -o "$SSM_TMP" && [ -s "$SSM_TMP" ]; then
    dpkg -i "$SSM_TMP" || apt-get install -y -f
    systemctl enable --now amazon-ssm-agent >/dev/null 2>&1 \
      || echo "WARNING: amazon-ssm-agent installed but did not start"
  else
    echo "WARNING: could not download amazon-ssm-agent; this host stays ssh-only"
  fi
  rm -f "$SSM_TMP"
fi

# ------------------------------------------------------------ setup self-update
# Terraform publishing this script to S3 does NOT reach a running box: the instance's
# user_data has ignore_changes, so it is read exactly once, at first boot. Every change
# therefore needed someone to SSH in and re-run it by hand -- and the times nobody did are
# how a stale copy once recreated a deleted account.
#
# This closes that gap, with three guards, because a script that runs unattended on the box
# that hosts everyone's work deserves them:
#   1. Only acts when the object's ETag actually changes, so it is a no-op almost always.
#   2. Refuses to run a script that fails `bash -n` -- a half-written publish cannot brick
#      the box, it just leaves the previous version in place and says so.
#   3. Checks the units that matter afterwards and logs loudly if any died, so a bad change
#      surfaces in `journalctl -u setup-sync` instead of as a mystery outage tomorrow.
cat > /usr/local/bin/setup-sync <<'SETUPSYNC'
#!/bin/bash
set -uo pipefail
BUCKET=ejc3-dev-scripts
KEY=user-data/nextjs.sh
STATE=/var/lib/nextjs-setup
mkdir -p "$STATE"

ETAG=$(aws s3api head-object --bucket "$BUCKET" --key "$KEY" --region us-west-1          --query ETag --output text 2>/dev/null)
[ -n "$ETAG" ] || { echo "setup-sync: cannot reach s3://$BUCKET/$KEY"; exit 0; }
[ "$ETAG" = "$(cat "$STATE/applied-etag" 2>/dev/null)" ] && exit 0

echo "setup-sync: new script published (etag $ETAG), fetching"
NEXT=$(mktemp /tmp/nextjs-setup.XXXXXX.sh)
trap 'rm -f "$NEXT"' EXIT
aws s3 cp "s3://$BUCKET/$KEY" "$NEXT" --region us-west-1 >/dev/null 2>&1   || { echo "setup-sync: download failed, keeping current setup"; exit 0; }

if ! bash -n "$NEXT" 2>/tmp/setup-syntax.err; then
  echo "setup-sync: REFUSING to run -- published script has a syntax error:"
  sed 's/^/  /' /tmp/setup-syntax.err
  exit 0
fi

echo "setup-sync: running"
bash "$NEXT" >/var/log/setup-sync.log 2>&1
RC=$?
echo "setup-sync: finished rc=$RC"

FAILED=""
for unit in cloudflared@cc-games.dev cloudflared@dolphin-labs.dev; do
  systemctl is-active --quiet "$unit" || FAILED="$FAILED $unit"
done
# Primary labels plus any user-<slug> preview instances that are enabled.
for envf in /var/lib/ndev/instances/*.env /var/lib/ndev/*.env; do
  [ -f "$envf" ] || continue
  case "$envf" in
    */instances/*) label=$(basename "$envf" .env) ;;
    */registry*|*/registry.*) continue ;;
    *)
      base=$(basename "$envf" .env)
      # Skip non-user legacy files; only bare $user.env
      case " ${join(" ", local.nextjs_users)} " in
        *" $base "*) label=$base ;;
        *) continue ;;
      esac
      ;;
  esac
  systemctl is-enabled --quiet "ndev@$label" 2>/dev/null || continue
  systemctl is-active --quiet "ndev@$label" || FAILED="$FAILED ndev@$label"
done

if [ -n "$FAILED" ]; then
  echo "setup-sync: WARNING these units are not active after the run:$FAILED"
  echo "setup-sync: NOT recording this etag, so the next run retries"
  exit 1
fi

printf '%s' "$ETAG" > "$STATE/applied-etag"
echo "setup-sync: healthy, recorded etag"
SETUPSYNC
chmod 755 /usr/local/bin/setup-sync

cat > /etc/systemd/system/setup-sync.service <<'SSSVC'
[Unit]
Description=Apply the published nextjs-dev setup script when it changes
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/setup-sync
SSSVC

cat > /etc/systemd/system/setup-sync.timer <<'SSTIMER'
[Unit]
Description=Check for a newly published setup script

[Timer]
OnBootSec=5min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
SSTIMER
systemctl daemon-reload
systemctl enable --now setup-sync.timer >/dev/null 2>&1 || true

# Record the running script's etag so the very first timer tick does not re-run the setup
# that is executing right now.
mkdir -p /var/lib/nextjs-setup
aws s3api head-object --bucket ejc3-dev-scripts --key user-data/nextjs.sh --region us-west-1   --query ETag --output text 2>/dev/null > /var/lib/nextjs-setup/applied-etag || true

# --------------------------------------------------- remote control: ensure it took
# Remote Control is bound to the CONVERSATION, not the process. Resuming one -- which an
# unattended launcher must do, or the user loses their work -- replays the RC session id
# recorded in that transcript. If the process that owned it is gone (a crash, an OOM, a
# restart) the reconnect fails and the session runs with no remote control at all. Retrying
# /remote-control cannot help: it only ever attempts the same doomed RECONNECT.
#
# The fix is what the TUI offers: disconnect the dead binding, then enable a fresh one --
# which keeps the conversation AND gets a working connection.
cat > /usr/local/bin/claude-rc-ensure <<'RCENSURE'
#!/bin/bash
# claude-rc-ensure <user> -- repair a DEAD Remote Control binding, and nothing else.
#
# THIS SCRIPT TYPES INTO A LIVE SESSION SOMEONE IS USING. Read the next paragraph before
# changing it.
#
# The first version triggered on the ABSENCE of a success string ("/rc active") in the
# pane. A Claude Code update changed that footer to render as just "/rc", so success became
# undetectable and the script "repaired" a perfectly healthy connection every time it ran
# -- 21 times in six hours per user. Each repair DISCONNECTED a working session, and the
# blind "1" keystroke meant for the enable menu landed in the prompt instead and was SENT
# AS A MESSAGE, so the kids' conversations filled with "1" / "Standing by." and burned
# tokens toward the usage limit. The repair's own side effect ("Remote Control
# disconnected.") then looked like the fault it was meant to fix, so it fed itself.
#
# Three rules keep that from recurring:
#   1. Trigger on an EXPLICIT failure string, never on the absence of a success string.
#      If upstream changes its wording again this does nothing, which is the safe way to
#      be wrong.
#   2. Never send a menu selection without first SEEING that menu on screen. Escape out
#      instead -- a stray keystroke in this TUI is a message, not a no-op.
#   3. Rate limit. A repair loop that can run every ten minutes must not be able to touch
#      the same session more than once an hour.
set -uo pipefail
WHO="$${1:?usage: claude-rc-ensure <user>}"
H="/home/$WHO"
tm() { sudo -u "$WHO" -H env HOME="$H" tmux "$@" 2>/dev/null; }
win=$(tm list-windows -a -F '#{window_id}' | head -1)
[ -n "$win" ] || { echo "claude-rc-ensure: $WHO has no tmux window"; exit 0; }
pgrep -u "$WHO" -x claude >/dev/null 2>&1 || { echo "claude-rc-ensure: $WHO has no claude"; exit 0; }

# Only the CURRENT screen. Reading the whole scrollback means text left by an earlier
# repair re-triggers the repair, forever.
pane() { tm capture-pane -p -S -25 -t "$win"; }
P="$(pane)"

# The one fault this fixes: a stale binding on a resumed conversation, whose reconnect
# cannot succeed no matter how many times /remote-control retries it. Note this is NOT the
# same as the bare "Remote Control disconnected." line, which is also what a deliberate
# disconnect prints -- matching that would make the script chase its own tail.
case "$P" in
  *"Couldn't reconnect to your Remote Control session"*) ;;
  *) echo "claude-rc-ensure: $WHO nothing to repair"; exit 0 ;;
esac

# An upstream 503 is not ours to fix; it recovers on its own and restarting costs the session.
case "$P" in
  *"Session creation failed"*) echo "claude-rc-ensure: $WHO upstream 503, leaving alone"; exit 0 ;;
esac

STATEDIR=/var/lib/claude-rc-ensure
mkdir -p "$STATEDIR"
STAMP="$STATEDIR/$WHO"
now=$(date +%s)
last=$(cat "$STAMP" 2>/dev/null || echo 0)
if [ $((now - last)) -lt 3600 ]; then
  echo "claude-rc-ensure: $WHO repaired within the last hour, backing off"
  exit 0
fi
echo "$now" > "$STAMP"

echo "claude-rc-ensure: $WHO reconnect is dead -- dropping the stale binding"
tm send-keys -t "$win" '/remote-control' Enter; sleep 8
case "$(pane)" in
  *"Disconnect this session"*)
    tm send-keys -t "$win" Up; tm send-keys -t "$win" Up; tm send-keys -t "$win" Enter; sleep 6
    ;;
  *)
    echo "claude-rc-ensure: $WHO disconnect menu never appeared; leaving the session alone" >&2
    tm send-keys -t "$win" Escape
    exit 0
    ;;
esac

tm send-keys -t "$win" '/remote-control' Enter; sleep 8
case "$(pane)" in
  *"Enable Remote Control"*)
    tm send-keys -t "$win" '1'; sleep 1; tm send-keys -t "$win" Enter; sleep 8
    echo "claude-rc-ensure: $WHO re-enabled"
    ;;
  *)
    echo "claude-rc-ensure: $WHO enable menu never appeared; leaving the session alone" >&2
    tm send-keys -t "$win" Escape
    exit 0
    ;;
esac
RCENSURE
chmod 755 /usr/local/bin/claude-rc-ensure

# ------------------------------------------------------- remote-control watchdog
# Remote Control wedges without the session dying: the pane still works and the phone can
# no longer drive it. This never restarts a unit and never kills tmux -- it delegates to
# claude-rc-ensure, which repairs the binding in place.
#
# ONE PLACE DECIDES WHETHER SOMETHING IS BROKEN, and it is claude-rc-ensure. This used to
# have its own copy of that judgement, and the copy was wrong in the worst possible way: it
# matched "Remote Control disconnected", which is precisely what ensure's own Disconnect
# step prints. The watchdog therefore saw the repair's side effect, called it a fault, and
# repaired again -- ten minutes apart, forever, tearing down healthy connections and
# spraying stray keystrokes into the prompt as messages.
#
# So there is no detection here any more, and no fails counter either: ensure exits
# immediately when there is nothing to fix, and rate-limits itself to one repair per user
# per hour. Duplicated state was what let the two layers disagree.
cat > /usr/local/bin/claude-rc-watchdog <<'RCWD'
#!/bin/bash
set -uo pipefail
for u in ${join(" ", local.nextjs_users)}; do
  id -u "$u" >/dev/null 2>&1 || continue
  pgrep -u "$u" -x claude >/dev/null 2>&1 || continue
  /usr/local/bin/claude-rc-ensure "$u" || true
done
RCWD
chmod 755 /usr/local/bin/claude-rc-watchdog

cat > /etc/systemd/system/claude-rc-watchdog.service <<'RCWDSVC'
[Unit]
Description=Repair Claude Remote Control when a session reports it failed

[Service]
Type=oneshot
ExecStart=/usr/local/bin/claude-rc-watchdog
RCWDSVC

cat > /etc/systemd/system/claude-rc-watchdog.timer <<'RCWDTIMER'
[Unit]
Description=Check for wedged Claude Remote Control sessions

[Timer]
OnBootSec=6min
OnUnitActiveSec=10min

[Install]
WantedBy=timers.target
RCWDTIMER
systemctl daemon-reload
systemctl enable --now claude-rc-watchdog.timer >/dev/null 2>&1 || true

# --------------------------------------------------------- agent enable watcher
# The units above are enabled only when a user's credentials already exist, and that check
# ran at boot. Someone who logs in to Claude afterwards used to wait for "the next setup
# run" -- an event they could not trigger -- or reach for sudo. This timer closes that gap:
# it re-runs the same guarded enable every few minutes, so logging in is the only step.
cat > /usr/local/bin/agents-enable <<'AGENTSENABLE'
#!/bin/bash
# Enable each user's agent units once their credentials exist. Idempotent and quiet.
set -uo pipefail
for u in ${join(" ", local.nextjs_users)}; do
  id -u "$u" >/dev/null 2>&1 || continue
  # Enable and start are checked SEPARATELY. `enable --now` can enable the unit and still
  # fail to start it -- remote control not up yet, say -- and after that `is-enabled` is
  # true forever, so a watcher keyed on it would never retry the start and the agent would
  # stay down until someone noticed. Keyed on is-active, this converges on the next tick.
  if [ -s "/home/$u/.claude/.credentials.json" ]; then
    systemctl enable "claude-rc@$u.service" >/dev/null 2>&1 || true
    systemctl is-active --quiet "claude-rc@$u.service"       || systemctl start "claude-rc@$u.service" >/dev/null 2>&1 || true
  fi
  if [ -s "/home/$u/.codex/auth.json" ]; then
    systemctl enable "codex-rc@$u.service" >/dev/null 2>&1 || true
    systemctl is-active --quiet "codex-rc@$u.service"       || systemctl start "codex-rc@$u.service" >/dev/null 2>&1 || true
  fi
done
AGENTSENABLE
chmod 755 /usr/local/bin/agents-enable

cat > /etc/systemd/system/agents-enable.service <<'AESVC'
[Unit]
Description=Enable per-user agent units once their credentials exist

[Service]
Type=oneshot
ExecStart=/usr/local/bin/agents-enable
AESVC

cat > /etc/systemd/system/agents-enable.timer <<'AETIMER'
[Unit]
Description=Check for newly logged-in agent users

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
AETIMER
systemctl daemon-reload
systemctl enable --now agents-enable.timer >/dev/null 2>&1 || true

# ------------------------------------------------------- playwright system deps
# Installed once, as root, at provision time so that nobody needs `--with-deps` (which
# shells out to apt and therefore needs sudo) just to run the e2e suite. Playwright is
# asked what it wants rather than us pinning a package list that rots with each Ubuntu
# release -- the browser binaries themselves stay per-user and per-version.
if command -v npx >/dev/null 2>&1; then
  npx --yes playwright install-deps chromium >/dev/null 2>&1 \
    || echo "WARNING: playwright system deps not installed; e2e will need --with-deps"
fi

# ------------------------------------------------------------- polkit: own units
# systemctl talks to systemd over D-Bus, and D-Bus asks polkit, which by default demands
# interactive auth for managing system units. That is why restarting your own dev server
# needed `sudo systemctl restart ndev@$USER`. Everyone here has passwordless root anyway,
# so the sudo was never a barrier -- just friction on the most common command on the box,
# and friction that teaches people to reach for root by reflex.
#
# This grants each user start/stop/restart/reload on THEIR OWN instances of these templates
# and nothing else: ndev@bob and ndev@bob-* are bob's; bob still cannot touch ndev@alice.
mkdir -p /etc/polkit-1/rules.d
cat > /etc/polkit-1/rules.d/50-ndev-own-units.rules <<'POLKIT'
polkit.addRule(function(action, subject) {
  if (action.id !== "org.freedesktop.systemd1.manage-units") {
    return polkit.Result.NOT_HANDLED;
  }
  var verb = action.lookup("verb");
  var allowed = ["start", "stop", "restart", "reload", "reload-or-restart", "try-restart"];
  if (allowed.indexOf(verb) < 0) {
    return polkit.Result.NOT_HANDLED;
  }
  var unit = action.lookup("unit");
  // Claude/Codex stay one-per-user. ndev allows the bare user instance plus any
  // user-<slug> preview (multi-worktree publishes).
  var exact = ["claude-rc@", "codex-rc@"];
  for (var i = 0; i < exact.length; i++) {
    if (unit === exact[i] + subject.user + ".service") {
      return polkit.Result.YES;
    }
  }
  var prefix = "ndev@";
  if (unit.indexOf(prefix) === 0 && unit.slice(-8) === ".service") {
    var inst = unit.substring(prefix.length, unit.length - 8);
    if (inst === subject.user || inst.indexOf(subject.user + "-") === 0) {
      return polkit.Result.YES;
    }
  }
  return polkit.Result.NOT_HANDLED;
});
POLKIT
chmod 644 /etc/polkit-1/rules.d/50-ndev-own-units.rules
systemctl restart polkit 2>/dev/null || true

# ---------------------------------------------------------------- ndev registry
# hostname<TAB>port, one per line. ndev-register rewrites the cloudflared ingress from
# this file, so adding a project never means hand-editing YAML.
mkdir -p /var/lib/ndev /var/lib/ndev/instances
touch /var/lib/ndev/registry
chmod 666 /var/lib/ndev/registry

# ---------------------------------------------------------------- zone lookup
# One place that answers "which zone does this user publish to" and "which tunnel serves
# that zone". Both ndev and ndev-register read it, so the mapping cannot drift between the
# thing that builds a hostname and the thing that validates it -- which would be a
# privilege bug, not just an inconsistency, since ndev-register runs as root.
cat > /usr/local/bin/ndev-zone <<'ZONE'
#!/bin/bash
set -euo pipefail
case "$${1:-}" in
  --tunnel)
    case "$${2:-}" in
${local.nextjs_zone_arms}
      *) exit 1 ;;
    esac ;;
  *)
    case "$${1:-}" in
${local.nextjs_user_arms}
      *) exit 0 ;;   # unknown user: no zone, callers must refuse
    esac ;;
esac
ZONE
chmod 755 /usr/local/bin/ndev-zone

# Regenerates one zone's cloudflared ingress from that zone's registry. Split out so the
# publish path and the boot path build the config the same way -- when they were separate,
# boot wrote a 404-only config and silently dropped every already-published hostname.
cat > /usr/local/bin/ndev-rebuild <<'REBUILD'
#!/bin/bash
set -euo pipefail
ZONE="$1"
TUNNEL_ID=$(/usr/local/bin/ndev-zone --tunnel "$ZONE")
REGISTRY=/var/lib/ndev/registry-$ZONE
# Written to a temp file and compared: an unchanged config must not touch the live one,
# because every rewrite otherwise invites a reload of a tunnel that had nothing to learn.
TMP=$(mktemp)
{
  echo "tunnel: $TUNNEL_ID"
  echo "credentials-file: /etc/cloudflared/creds-$ZONE.json"
  echo "ingress:"
  # Reserved infrastructure route, before project routes so a stale registry row
  # cannot turn SSH into an HTTP application. Other zones remain HTTP-only.
  if [ "$ZONE" = "${local.dolphin_domain}" ]; then
    echo "  - hostname: ssh.${local.dolphin_domain}"
    echo "    service: ssh://127.0.0.1:22"
  fi
  if [ -f "$REGISTRY" ]; then
    while IFS=$'\t' read -r h p rest; do
      [ -n "$h" ] || continue
      [ "$h" != "ssh.${local.dolphin_domain}" ] || continue
      echo "  - hostname: $h"
      echo "    service: http://127.0.0.1:$p"
    done < "$REGISTRY"
  fi
  echo "  - service: http_status:404"
} > "$TMP"

if cmp -s "$TMP" "/etc/cloudflared/config-$ZONE.yml" 2>/dev/null; then
  rm -f "$TMP"
  exit 0            # unchanged: caller has nothing to reload
fi
mv "$TMP" "/etc/cloudflared/config-$ZONE.yml"
exit 10             # changed: caller should restart this zone's connector
REBUILD
chmod 755 /usr/local/bin/ndev-rebuild


cat > /usr/local/bin/ndev-register <<'REG'
#!/bin/bash
# ndev-register <hostname> <port> <user> <projectdir>
#
# Records a published project, rewrites the cloudflared ingress, and enables the systemd
# unit that actually runs it. Runs as root via sudoers, so it validates every argument
# rather than trusting the caller -- the kids' accounts hold no other privilege and this
# is the single door through it.
#
# Instance label is the hostname left-label (ejc3, ejc3-dolphin-nav-jargon). Multiple
# labels per user stay up concurrently; each has its own env file and ndev@<label> unit.
set -euo pipefail
HOST="$1"; PORT="$2"; WHO="$3"; DIR="$4"
if [ "$HOST" = "ssh.${local.dolphin_domain}" ]; then
  echo "refusing: $HOST is reserved for SSH" >&2; exit 1
fi

# The caller's zone decides what they may publish. Previously this hardcoded one domain,
# which is the check that has to change for a second project -- and it must stay a check:
# this script runs as root via sudoers, so the hostname is attacker-controlled input.
ZONE=$(/usr/local/bin/ndev-zone "$WHO")
case "$ZONE" in "") echo "refusing: $WHO has no publishing zone" >&2; exit 1 ;; esac
case "$HOST" in
  *".$ZONE") ;;
  *) echo "refusing: $HOST is not under $ZONE (the zone for $WHO)" >&2; exit 1 ;;
esac
LABEL="$${HOST%.$ZONE}"
# Hostnames must be $USER or $USER-<slug> under their zone -- no bare unrelated names.
case "$LABEL" in
  "$WHO"|"$WHO"-*) ;;
  *) echo "refusing: $HOST must be $WHO.$ZONE or $WHO-<slug>.$ZONE" >&2; exit 1 ;;
esac
case "$LABEL" in
  *[!a-z0-9-]*|"") echo "refusing: bad instance label $LABEL" >&2; exit 1 ;;
esac
case "$PORT" in ''|*[!0-9]*) echo "refusing: bad port $PORT" >&2; exit 1 ;; esac
id "$WHO" >/dev/null 2>&1 || { echo "refusing: no such user $WHO" >&2; exit 1; }

# A user may only publish as themselves. Without this, colton could register a service
# that runs as connor, since sudoers cannot express "only your own username".
if [ -n "$${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ] && [ "$SUDO_USER" != "$WHO" ]; then
  echo "refusing: $SUDO_USER may not publish as $WHO" >&2; exit 1
fi
case "$DIR" in "/home/$WHO"|"/home/$WHO"/*) ;; *) echo "refusing: $DIR is outside /home/$WHO" >&2; exit 1 ;; esac
[ -f "$DIR/package.json" ] || { echo "refusing: no package.json in $DIR" >&2; exit 1; }

# One registry and one cloudflared config PER ZONE. A single shared registry would mean
# rewriting both tunnels' ingress on every publish, so a malformed entry from one project
# could break the other -- the exact coupling the separate tunnels exist to avoid.
REGISTRY=/var/lib/ndev/registry-$ZONE
grep -v -P "^\Q$HOST\E\t" "$REGISTRY" > "$REGISTRY.new" 2>/dev/null || true
printf '%s\t%s\t%s\t%s\n' "$HOST" "$PORT" "$WHO" "$DIR" >> "$REGISTRY.new"
sort -u "$REGISTRY.new" > "$REGISTRY" && rm -f "$REGISTRY.new"

# Per-instance env: ndev@<label> reads this. Concurrent publishes for one user each keep
# their own file; writing here no longer clobbers another label's DIR/PORT.
mkdir -p /var/lib/ndev/instances
printf 'HOST=%s\nPORT=%s\nDIR=%s\nWHO=%s\n' "$HOST" "$PORT" "$DIR" "$WHO" > "/var/lib/ndev/instances/$LABEL.env"
# Legacy path kept in sync for the primary ($WHO) label so older helpers that still read
# /var/lib/ndev/$WHO.env (agent-dir, kid-agents) keep working until they are updated.
if [ "$LABEL" = "$WHO" ]; then
  printf 'HOST=%s\nPORT=%s\nDIR=%s\nWHO=%s\n' "$HOST" "$PORT" "$DIR" "$WHO" > "/var/lib/ndev/$WHO.env"
fi

# %i is the label, not always a unix user. Drop-in forces the real account so
# ndev@ejc3-foo runs as ejc3 with HOME under /home/ejc3.
DROP_DIR="/etc/systemd/system/ndev@$LABEL.service.d"
mkdir -p "$DROP_DIR"
cat > "$DROP_DIR/user.conf" <<EOF
[Service]
User=$WHO
Group=$WHO
WorkingDirectory=/home/$WHO
Environment=HOME=/home/$WHO
EOF

# ndev-rebuild exits 10 when it changed the config, 0 when it did not. Capture it rather
# than letting set -e abort, and restart ONLY on a real change -- a publish that changes
# nothing must not disturb anyone else's hostname on this tunnel.
CHANGED=0
/usr/local/bin/ndev-rebuild "$ZONE" || CHANGED=$?
if [ "$CHANGED" = "10" ]; then
  systemctl restart "cloudflared@$ZONE"
else
  systemctl is-active --quiet "cloudflared@$ZONE" || systemctl start "cloudflared@$ZONE"
fi
true
systemctl daemon-reload
systemctl enable --now "ndev@$LABEL.service" >/dev/null 2>&1 || systemctl restart "ndev@$LABEL.service"
echo "registered $HOST -> 127.0.0.1:$PORT (ndev@$LABEL, enabled at boot)"
REG
chmod 755 /usr/local/bin/ndev-register

# ---------------------------------------------------------------- durability
# WHY THESE UNITS EXIST: this box is a spot instance, so AWS reclaims it and restarts it
# without warning -- twice in one hour on 2026-07-25. cloudflared came back on its own
# (it is a systemd service) but the `next dev` processes did not, because they had been
# started by hand in a tmux session. The tunnel kept resolving and forwarding to a dead
# port, so every URL authenticated correctly and then 502'd.
#
# On-demand would not have fixed this. Instances still reboot for host maintenance and
# kernel updates; the purchase model only changes how OFTEN. Durability has to come from
# something that restarts the work, which is what these units are.

cat > /usr/local/bin/ndev-run <<'RUN'
#!/bin/bash
# Started by ndev@<label>.service. Reads what ndev-register recorded and serves it.
# %i is the hostname left-label (ejc3 or ejc3-<slug>), not necessarily a unix user.
set -euo pipefail
LABEL="$1"
ENVF="/var/lib/ndev/instances/$LABEL.env"
# Legacy single-file layout (pre multi-instance): /var/lib/ndev/$USER.env
if [ ! -f "$ENVF" ] && [ -f "/var/lib/ndev/$LABEL.env" ]; then
  ENVF="/var/lib/ndev/$LABEL.env"
fi
[ -f "$ENVF" ] || { echo "$LABEL has not published a project yet" >&2; exit 0; }
. "$ENVF"
cd "$DIR"
# node_modules lives on the root volume and survives reboots, but a fresh volume (or a
# dependency change) would otherwise leave the service crash-looping on a missing module.
[ -d node_modules ] || { echo "installing dependencies..."; pnpm install || npm install; }
# Prefer `npm run dev` so package.json lifecycle hooks fire. dolphin-labs generates a
# git-ignored dataset in `predev`; running `next dev` directly skipped it and every page
# 500'd on a missing import -- which reads as a broken checkout, not a missing build step.
if node -e 'process.exit(require("./package.json").scripts && require("./package.json").scripts.dev ? 0 : 1)' 2>/dev/null; then
  exec npm run dev -- --port "$PORT" --hostname 127.0.0.1
fi
exec npx next dev --port "$PORT" --hostname 127.0.0.1
RUN
chmod 755 /usr/local/bin/ndev-run

cat > /etc/systemd/system/ndev@.service <<'UNIT'
[Unit]
Description=next dev server for %i (published through that user's zone tunnel)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# Default assumes %i is a unix user (ejc3, colton). Prefixed labels (ejc3-foo) get a
# drop-in from ndev-register that sets User=/Group=/HOME to the real account.
User=%i
WorkingDirectory=/home/%i
Environment=HOME=/home/%i
Environment=NODE_ENV=development
ExecStart=/usr/local/bin/ndev-run %i
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

# Claude's --remote-control is the interactive TUI and refuses to run without a pty --
# piping it anywhere makes it fall into --print mode and exit. systemd gives a service no
# pty, so it runs inside tmux, on a dedicated socket so it never collides with the
# interactive t-claude session the kid may also have open.
# Where the agents should sit: the project the user actually published, so that the thing
# being served and the thing being edited are the same directory by construction. Falls
# back to $HOME, which matters on a fresh box -- before anyone has run ndev there is no
# project, and a unit pointed at a non-existent directory refuses to start at all.
cat > /usr/local/bin/agent-dir <<'ADIR'
#!/bin/bash
WHO="$1"
ENVF=""
[ -f "/var/lib/ndev/instances/$WHO.env" ] && ENVF="/var/lib/ndev/instances/$WHO.env"
[ -z "$ENVF" ] && [ -f "/var/lib/ndev/$WHO.env" ] && ENVF="/var/lib/ndev/$WHO.env"
DIR=""
[ -n "$ENVF" ] && . "$ENVF"
[ -n "$DIR" ] && [ -d "$DIR" ] || DIR="/home/$WHO"

# Anchor on the REPO ROOT, not the directory ndev happens to publish.
#
# t-claude keys a tmux window by its folder, ndev records the Next.js app dir (.../web), and
# the docs tell people to run `cd ~/<repo> && t-claude` from the root. Those are two
# different folders, so the managed session and the one a person starts were guaranteed to
# be different windows -- both running claude, and attaching landed you in the wrong one.
# Anchoring here makes them the same window, and gives claude the whole repo besides.
ROOT="$(sudo -u "$WHO" git -C "$DIR" rev-parse --show-toplevel 2>/dev/null)"
[ -n "$ROOT" ] && [ -d "$ROOT" ] && DIR="$ROOT"
printf '%s\n' "$DIR"
ADIR
chmod 755 /usr/local/bin/agent-dir

# Regenerates a user's ~/Documents/Codex/AGENTS.md from the repos actually on disk.
# GENERATED because a kid can add a second project at any time, and a hardcoded path would
# then be wrong. Runs at boot and from the nightly updater.
cat > /usr/local/bin/kid-agents-refresh <<'KIDAGENTS'
#!/bin/bash
set -uo pipefail
WHO="$${1:?usage: kid-agents-refresh <user>}"
id "$WHO" >/dev/null 2>&1 || { echo "no such user $WHO" >&2; exit 1; }
HOMEDIR="/home/$WHO"
OUT="$HOMEDIR/Documents/Codex/AGENTS.md"
install -d -o "$WHO" -g "$WHO" -m 755 "$HOMEDIR/Documents/Codex"

HOST=""; PORT=""
if [ -f "/var/lib/ndev/instances/$WHO.env" ]; then
  . "/var/lib/ndev/instances/$WHO.env"
elif [ -f "/var/lib/ndev/$WHO.env" ]; then
  . "/var/lib/ndev/$WHO.env"
fi

# git must run AS the user: root gets "dubious ownership" on a user-owned repo and every
# command returns empty, which silently produces a project list with no projects in it.
#
# Built in this loop rather than inside `sudo -u ... bash -c '...'`. In that form the
# markdown backticks landed inside double quotes in the inner shell and were executed as
# command substitution -- `~/%s` became an attempt to run /home/<user>/%s, and one repo
# came out as three garbled lines. Here the backticks are backslash-escaped and never
# reach a nested shell.
BT='`'
REPOS=""
for d in "$HOMEDIR"/*/; do
  [ -d "$d/.git" ] || continue
  n=$(basename "$d")
  r=$(sudo -u "$WHO" git -C "$d" remote get-url origin 2>/dev/null | sed 's|https://github.com/||;s|\.git$||')
  b=$(sudo -u "$WHO" git -C "$d" branch --show-current 2>/dev/null)
  REPOS="$${REPOS}- $${BT}~/$${n}$${BT} — $${r:-no remote} (branch $${BT}$${b:-detached}$${BT})
"
done
REPOS=$(printf '%s' "$REPOS")

# Fail loudly rather than shipping an empty list. The metal-box generator produced one on
# its first run and `|| true` hid it; the file looked fine and told an agent nothing.
if [ -z "$REPOS" ]; then
  echo "kid-agents-refresh: found no git repos for $WHO -- keeping existing AGENTS.md" >&2
  [ -s "$OUT" ] && exit 0
  REPOS="- (no git repositories yet -- ask the user what to work on)"
fi

TMP=$(mktemp)
{
  printf '# Read this first\n\n'
  printf 'You have been started in an empty scratch folder under Documents/Codex/<date>/<task>/.\n'
  printf '**That folder is not the project.** The real work is in one of these:\n\n'
  printf '%s\n\n' "$REPOS"
  printf 'They are writable inside your sandbox. Run commands from the project directory.\n\n'
  printf '## The dev server is already running -- do not start one\n\n'
  if [ -n "$HOST" ]; then
    printf 'This project is live at https://%s on port %s.\n\n' "$HOST" "$PORT"
  fi
  printf 'It is a systemd service that starts at boot and restarts if it crashes. Running\n'
  printf '`next dev` or `npm run dev` yourself collides on the port and breaks the live site.\n\n'
  printf '**Your sandbox may have no network access, even to localhost.** When it does not,\n'
  printf '`curl http://localhost:...` fails identically whether the server is healthy or truly\n'
  printf 'stopped, so it tells you nothing. Never conclude from a failed curl that it is down.\n'
  printf 'Check the service instead -- these need no network:\n\n'
  printf '```bash\n'
  printf 'systemctl is-active ndev@%s      # "active" means the site is up\n' "$WHO"
  printf 'journalctl -u ndev@%s -n 50      # recent output, including compile errors\n' "$WHO"
  printf 'sudo systemctl restart ndev@%s   # only if a change needs a full restart\n' "$WHO"
  printf '```\n\n'
  printf 'Next.js hot-reloads on save, so most edits need no restart at all.\n\n'
  printf '## This machine\n\n'
  printf -- '- You have **full passwordless sudo**. Install whatever you need.\n'
  printf -- '- Your home directory persists across reboots and is backed up daily.\n'
  printf -- '- `git` is authenticated as your own GitHub account, and `vercel` as your own\n'
  printf '  Vercel account. Both kids share one Vercel project, so pushing to `main`\n'
  printf '  deploys the shared site.\n'
} > "$TMP"
install -m 644 -o "$WHO" -g "$WHO" "$TMP" "$OUT"
rm -f "$TMP"
echo "wrote $OUT ($(wc -c < "$OUT") bytes, $(printf '%s' "$REPOS" | grep -c '^- ') repo(s))"
KIDAGENTS
chmod 755 /usr/local/bin/kid-agents-refresh

# Runs t-claude itself, on the user's DEFAULT tmux socket -- deliberately not a separate
# "-L agents" socket. Earlier this ran bare `claude --remote-control` isolated on its own
# socket specifically to avoid colliding with an interactive t-claude session; the isolation
# is no longer wanted -- the remote-control session from the phone and a session opened by
# SSHing in and running `t-claude` should be the SAME session, not two independent ones.
# Since t-claude computes the session name from the folder path (no explicit name given
# here), the phone-launched session and one opened later by hand in the same directory
# resolve to the identical tmux session and are picked up, not duplicated.
#
# `t-claude --remote-control` passes --remote-control straight through to claude
# (t-claude.zsh, ejc3/t-claude) -- everything it does not itself recognise is relayed
# verbatim rather than silently dropped.
cat > /usr/local/bin/agents-start <<'AGENTS'
#!/bin/bash
# NOT set -e: t-claude's final step attaches to the tmux session, which fails harmlessly
# here ("open terminal failed: not a terminal") since systemd gives this no tty -- by that
# point the session, window and claude launch have already succeeded. -e would treat that
# expected failure as agents-start itself having failed.
set -uo pipefail
WHO="$1"
export HOME="/home/$WHO"
WORKDIR="$(/usr/local/bin/agent-dir "$WHO")"
zsh -c "
  source ~/.config/t-claude.zsh 2>/dev/null || { echo 'agents-start: t-claude.zsh missing' >&2; exit 1; }
  cd '$WORKDIR' || exit 1
  # --auto is REQUIRED for an unattended launcher. Without it t-claude takes the interactive
  # path, `claude --resume`, which renders the session PICKER as soon as the user has any
  # transcript -- and a picker waits for a keypress systemd will never send. The window then
  # sits there with no claude running, so remote control has nothing to attach to. --auto
  # resolves to `claude --continue` when transcripts exist, plain `claude` when they do not.
  t-claude --auto --remote-control
"
echo "agents-start: t-claude invoked for $WHO in $WORKDIR"

# Durability at LAUNCH, not by later detection. --auto resumes the user's conversation, and a
# resumed conversation carries a Remote Control binding that is dead whenever the process that
# owned it is gone. Handing that over "started but unreachable" is how the user finds out on
# their phone. Give claude a moment to render, then repair before reporting success.
sleep 20
/usr/local/bin/claude-rc-ensure "$WHO" || true
AGENTS
chmod 755 /usr/local/bin/agents-start

cat > /etc/systemd/system/claude-rc@.service <<'UNIT'
[Unit]
Description=Claude Code (t-claude) with remote control for %i
After=network-online.target
Wants=network-online.target

[Service]
# oneshot + RemainAfterExit, not forking -- same fix as codex-rc@ below and for the same
# reason: t-claude's `tmux new-session` daemonises the tmux SERVER independently of the
# process that called it, so the launching zsh/bash returns quickly regardless of whether
# any child survives. Type=forking expects a forked child for systemd to adopt; there
# isn't one, so the unit flips to failed even though the session was created successfully.
Type=oneshot
RemainAfterExit=yes
User=%i
Environment=HOME=/home/%i
Environment=TERM=xterm-256color
WorkingDirectory=/home/%i
ExecStart=/usr/local/bin/agents-start %i
# No Restart=, matching codex-rc@ below exactly -- checked its LIVE deployed unit rather
# than trust memory of it, and confirmed the source here has none either. Caught the hard
# way: `systemd-analyze verify` refuses Restart=always/on-success on a Type=oneshot service
# outright ("isn't allowed"), so an earlier version of this unit with that combination
# would never have started at all. codex-rc@'s actual resilience comes from the nightly
# agent-update.timer calling `systemctl restart` explicitly, which works on a oneshot unit
# regardless of Restart= -- that already covers this unit too, so nothing else is needed.
#
# No ExecStop either: there is no "stop remote control" operation to run here the way
# codex has one. Killing the tmux server on stop would end every OTHER session in it too,
# including ones opened by hand over SSH -- exactly the collision this unification is
# meant to avoid, not reintroduce on the stop path. `systemctl stop` just marks the unit
# inactive; the tmux session and claude process are untouched and still reachable.

[Install]
WantedBy=multi-user.target
UNIT

# Codex's remote control is a real daemon rather than a TUI, so it needs no pty and no
# tmux. It is NOT Type=forking though: codex backgrounds the daemon itself (pid backend,
# own socket under ~/.codex/app-server-control) and the CLI returns immediately, so
# systemd has no forked child to adopt and marks the unit failed while the daemon happily
# runs. oneshot + RemainAfterExit matches what actually happens.
#
# Success is asserted against `--json` rather than the exit status. The text output prints
# "the connection is errored" and exits non-zero while it is still establishing, even
# though it goes on to connect -- only the JSON reports the settled truth.
#
# Uses the standalone binary explicitly: `codex remote-control` refuses to run against the
# npm/system install, and $PATH is not dependable inside a unit.
cat > /etc/systemd/system/codex-rc@.service <<'UNIT'
[Unit]
Description=Codex app-server daemon with remote control for %i
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
User=%i
Environment=HOME=/home/%i
WorkingDirectory=/home/%i
# No -c overrides here on purpose. `remote-control start` DISCARDS them: only the bare
# foreground `codex remote-control` forwards root config overrides. And even if it did
# forward them they would lose -- the ChatGPT client sends approvalPolicy and sandbox with
# each thread/start, and codex merges them as
#   sandbox_mode_override.or(self.sandbox_mode)
# i.e. client beats config.toml (SessionFlags layer 30 > user config layer 20). Verified
# on this box: a session rollout recorded approval_policy="on-request" and
# sandbox_policy=workspace-write while config.toml said never/danger-full-access.
# The approval mode is the app's to choose; nothing host-side can outrank it.
ExecStart=/bin/sh -c 'cd "$(/usr/local/bin/agent-dir %i)" || cd /home/%i; CX="/home/%i/.local/bin/codex"; "$CX" remote-control start >/dev/null 2>&1 || true; for i in 1 2 3 4 5; do "$CX" remote-control start --json 2>/dev/null | grep -q "\\"status\\":\\"connected\\"" && exit 0; sleep 5; done; exit 1'
ExecStop=-/home/%i/.local/bin/codex remote-control stop

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload

# Split the old single shared registry into one per zone. Without this the per-zone config
# is rebuilt from a registry that does not exist yet, so every already-published hostname
# silently drops to the 404 catch-all: the site stays reachable through Access and then
# 404s, which looks like the app broke rather than the routing.
if [ -f /var/lib/ndev/registry ]; then
  while IFS=$'\t' read -r h p who dir; do
    [ -n "$h" ] || continue
    z=$(/usr/local/bin/ndev-zone "$who")
    [ -n "$z" ] || { echo "migrate: skipping $h -- $who has no zone"; continue; }
    printf '%s\t%s\t%s\t%s\n' "$h" "$p" "$who" "$dir" >> "/var/lib/ndev/registry-$z"
  done < /var/lib/ndev/registry
  for f in /var/lib/ndev/registry-*; do [ -f "$f" ] && sort -u "$f" -o "$f"; done
  mv /var/lib/ndev/registry /var/lib/ndev/registry.migrated
  echo "migrated shared ndev registry into per-zone registries"
fi

# Migrate legacy /var/lib/ndev/$USER.env into instances/$USER.env (multi-instance layout).
# Idempotent: skips when the per-instance file already exists.
mkdir -p /var/lib/ndev/instances
for u in ${join(" ", local.nextjs_users)}; do
  legacy="/var/lib/ndev/$u.env"
  dest="/var/lib/ndev/instances/$u.env"
  [ -s "$legacy" ] || continue
  if [ ! -s "$dest" ]; then
    # Subshell so HOST/PORT/DIR from the legacy file do not leak into this script
    # (set -u would then trip on a later user whose legacy file is missing a key).
    bash -c 'set -euo pipefail; . "$1"; printf "HOST=%s\\nPORT=%s\\nDIR=%s\\nWHO=%s\\n" "$HOST" "$PORT" "$DIR" "$2" > "$3"' \
      bash "$legacy" "$u" "$dest"
    echo "migrated $legacy -> $dest"
  fi
  # Ensure User= drop-in exists for the primary label (harmless if User=%i already matches).
  DROP_DIR="/etc/systemd/system/ndev@$u.service.d"
  mkdir -p "$DROP_DIR"
  cat > "$DROP_DIR/user.conf" <<EOF
[Service]
User=$u
Group=$u
WorkingDirectory=/home/$u
Environment=HOME=/home/$u
EOF
done

# Build each zone's ingress from its registry. Also the reason a fresh box has a working
# tunnel service before anyone runs `ndev`: with no registry it writes the 404 catch-all.
${local.nextjs_zone_rebuild}

# Templated per zone: cloudflared serves ONE tunnel per process, so two zones means two
# services. cloudflared@cc-games.dev and cloudflared@dolphin-labs.dev fail and restart
# independently -- which is the point of separate tunnels in the first place.
cat > /etc/systemd/system/cloudflared@.service <<'SVC'
[Unit]
Description=Cloudflare Tunnel for %i
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/cloudflared --config /etc/cloudflared/config-%i.yml --no-autoupdate tunnel run
# Local ingress is read on startup. No ExecReload: cloudflared does not handle SIGHUP.
# ndev restarts only the changed zone; existing connections briefly disconnect.
Restart=always
RestartSec=5
User=root

[Install]
WantedBy=multi-user.target
SVC
systemctl daemon-reload
${local.nextjs_zone_enable}
# The old un-templated unit is superseded by cloudflared@<zone>; stop it so two processes
# do not both claim the cc-games tunnel (cloudflared allows it and the traffic splits).
# Tested with [ -f ] rather than `list-unit-files | grep -q`: this script runs under
# `set -o pipefail`, and grep -q exits at the first match, which kills systemctl with
# SIGPIPE. The pipeline then reports 141 and the branch is skipped BECAUSE it matched.
if [ -f /etc/systemd/system/cloudflared.service ]; then
  systemctl disable --now cloudflared 2>/dev/null || true
  rm -f /etc/systemd/system/cloudflared.service
  systemctl daemon-reload
fi

# ---------------------------------------------------------------- ndev
cat > /usr/local/bin/ndev <<'NDEV'
#!/bin/bash
# ndev [name] -- start `next dev` in this project and expose it under your zone.
#
# Defaults:
#   - primary checkout (e.g. ~/dolphin-labs/web) -> https://$USER.<zone>
#   - worktree under ~/worktrees/<slug>/... or ~/<repo>/.claude/worktrees/<slug>/...
#     -> https://$USER-<slug>.<zone>
# Explicit arg becomes $USER-<arg> unless it is already $USER or $USER-*.
# Hostnames must stay $USER or $USER-* under the account's zone (no bare unrelated names).
#
# Multiple labels for one user stay up concurrently (ndev@ejc3 and ndev@ejc3-foo).
#
# The port is derived from the hostname, so restarting gives you the same port and the
# tunnel mapping stays valid. Binds 127.0.0.1 only -- the box has no inbound web ports;
# the tunnel is the sole path in, and Cloudflare Access gates it (Google for cc-games,
# GitHub for dolphin-labs).
set -euo pipefail
DOMAIN=$(/usr/local/bin/ndev-zone "$USER")
[ -n "$DOMAIN" ] || { echo "no publishing zone for $USER -- add them to nextjs_user_zone" >&2; exit 1; }

sanitize() {
  printf '%s' "$1" | tr -c 'a-zA-Z0-9-' '-' | tr 'A-Z' 'a-z' | sed 's/^-*//;s/-*$//'
}

HERE=$(pwd -P 2>/dev/null || pwd)
if [ -n "$${1:-}" ]; then
  ARG=$(sanitize "$1")
  [ -n "$ARG" ] || { echo "usage: ndev [name]" >&2; exit 1; }
  case "$ARG" in
    "$USER"|"$USER"-*) LABEL="$ARG" ;;
    *) LABEL="$USER-$ARG" ;;
  esac
else
  LABEL=""
  case "$HERE" in
    "/home/$USER/worktrees/"*)
      SLUG="$${HERE#/home/$USER/worktrees/}"
      SLUG="$${SLUG%%/*}"
      LABEL="$USER-$(sanitize "$SLUG")"
      ;;
    *)
      # e.g. /home/ejc3/dolphin-labs/.claude/worktrees/<slug>/web
      SLUG=$(printf '%s' "$HERE" | sed -n "s|^/home/$USER/[^/]*/\\.claude/worktrees/\\([^/]*\\).*|\\1|p")
      if [ -n "$SLUG" ]; then
        LABEL="$USER-$(sanitize "$SLUG")"
      fi
      ;;
  esac
  [ -n "$LABEL" ] || LABEL="$USER"
fi

HOST="$LABEL.$DOMAIN"
# Belt-and-suspenders with ndev-register's check.
case "$LABEL" in
  "$USER"|"$USER"-*) ;;
  *) echo "refusing: hostname must be $USER.$DOMAIN or $USER-<slug>.$DOMAIN" >&2; exit 1 ;;
esac
PORT=$(( 3100 + ( $(printf '%s' "$HOST" | cksum | awk '{print $1}') % 800 ) ))

[ -f package.json ] || { echo "no package.json here -- run ndev inside a Next.js project" >&2; exit 1; }

# Hand the project to systemd rather than running it in this terminal. Publishing and
# making it durable are then the same action -- there is no way to end up with a URL
# registered in the tunnel but nothing set to restart behind it, which is exactly the
# state a spot interruption left this box in before.
sudo /usr/local/bin/ndev-register "$HOST" "$PORT" "$USER" "$(pwd)"
echo ""
echo "  https://$HOST   (port $PORT)"
echo "  sign in when prompted -- Cloudflare Access gates every hostname"
echo ""
echo "  it keeps running after you log out, and comes back by itself if the box reboots."
echo "    logs:    journalctl -u ndev@$LABEL -f"
echo "    restart: systemctl restart ndev@$LABEL"
echo "    stop:    systemctl stop ndev@$LABEL"
echo ""
NDEV
chmod 755 /usr/local/bin/ndev

# ---------------------------------------------------------------- users
# One Unix account each, so GitHub logins, dotfiles and projects stay separate.
FCVM_KEY=$(cat /home/ubuntu/.ssh/authorized_keys 2>/dev/null | head -1)
for u in ${join(" ", local.nextjs_users)}; do
  if ! id "$u" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$u"
  fi
  install -d -m 700 -o "$u" -g "$u" "/home/$u/.ssh"
  if [ -n "$FCVM_KEY" ]; then
    grep -qxF "$FCVM_KEY" "/home/$u/.ssh/authorized_keys" 2>/dev/null || \
      echo "$FCVM_KEY" >> "/home/$u/.ssh/authorized_keys"
  fi
  chown "$u:$u" "/home/$u/.ssh/authorized_keys" 2>/dev/null || true
  chmod 600 "/home/$u/.ssh/authorized_keys" 2>/dev/null || true

  # The account owner's declared keys. Appended, never replacing the file: a user may have
  # added keys by hand and this must not take their access away.
  while IFS='|' read -r ku kk; do
    [ -n "$ku" ] || continue
    [ "$ku" = "$u" ] || continue
    grep -qxF "$kk" "/home/$u/.ssh/authorized_keys" 2>/dev/null || \
      echo "$kk" >> "/home/$u/.ssh/authorized_keys"
    chown "$u:$u" "/home/$u/.ssh/authorized_keys" 2>/dev/null || true
    chmod 600 "/home/$u/.ssh/authorized_keys" 2>/dev/null || true
  done <<'USERKEYS'
${join("\n", local.nextjs_user_key_lines)}
USERKEYS

  # Retire superseded keys. Without this a rotated key stays authorized forever, because
  # the append above has no way to know a line is obsolete. Matched with grep -vxF on the
  # exact line, so a key someone added by hand is never touched -- only the specific
  # strings listed in nextjs_retired_user_keys.
  #
  # Fed in as DATA through one join(), not with a terraform for-directive. A directive
  # with the ~ trim markers strips the newline on both sides and welds the generated
  # statement onto the previous line; that produced a broken setup script once already in
  # this file. One interpolation of a newline-separated list cannot do that, and it
  # degrades cleanly to a no-op when the list is empty.
  #
  # (Note for whoever edits this next: do not write a directive's literal syntax in a
  # comment here either. This heredoc is a TEMPLATE, so terraform parses the directive
  # inside the comment and fails with "Invalid 'for' directive". That happened writing
  # this very block.)
  while IFS= read -r rk; do
    [ -n "$rk" ] || continue
    grep -qxF "$rk" "/home/$u/.ssh/authorized_keys" 2>/dev/null || continue
    grep -vxF "$rk" "/home/$u/.ssh/authorized_keys" > "/home/$u/.ssh/authorized_keys.tmp" && \
      mv "/home/$u/.ssh/authorized_keys.tmp" "/home/$u/.ssh/authorized_keys"
    chown "$u:$u" "/home/$u/.ssh/authorized_keys" 2>/dev/null || true
    chmod 600 "/home/$u/.ssh/authorized_keys" 2>/dev/null || true
    echo "removed retired key from $u"
  done <<'RETIREDKEYS'
${join("\n", local.nextjs_retired_user_keys)}
RETIREDKEYS

  # FULL passwordless sudo, deliberately.
  #
  # This box IS the sandbox: it holds nothing but these projects, exposes no inbound web
  # ports, and its IAM role can read exactly one S3 object and two secrets. There is
  # nothing here worth protecting the kids from, and no credential they could reach that
  # they do not already own.
  #
  # The earlier version allowed only ndev-register plus their own three systemd units.
  # That looked tidy and cost real work: `sudo npx playwright install-deps` failed, so
  # Chromium could not launch, and an agent working in their account hits a wall it cannot
  # explain or escape. Every such wall becomes a support request rather than a safeguard.
  #
  # The protections that actually matter are elsewhere and unaffected: Cloudflare Access
  # still gates every URL, the instance still has no inbound web ports, and the jumpbox
  # key is no longer on any dev server.
  printf '%s ALL=(ALL) NOPASSWD: ALL\n' "$u" > "/etc/sudoers.d/ndev-$u.new"
  if visudo -cf "/etc/sudoers.d/ndev-$u.new" >/dev/null 2>&1; then
    mv "/etc/sudoers.d/ndev-$u.new" "/etc/sudoers.d/ndev-$u"
    chmod 440 "/etc/sudoers.d/ndev-$u"
  else
    rm -f "/etc/sudoers.d/ndev-$u.new"
    echo "WARNING: sudoers for $u failed validation; leaving previous file in place"
  fi

  # Read your own service's logs without sudo. These accounts already have full root, so
  # this grants no new reach -- it removes a sudo from `journalctl -u ndev@$USER -f`, which
  # people run twenty times a day while chasing a broken page.
  usermod -aG systemd-journal "$u" 2>/dev/null || true

  # Let their user services and tmux survive logout / start without a login session.
  loginctl enable-linger "$u" 2>/dev/null || true

  # Bring back the agents on every boot. Guarded on credentials existing: on a fresh box
  # nobody has logged in to Claude or Codex yet, and enabling the units then would just
  # crash-loop until someone does.
  [ -s "/home/$u/.claude/.credentials.json" ] && systemctl enable --now "claude-rc@$u.service" 2>/dev/null || true

  # `codex remote-control start` refuses to run against the system/npm codex -- it needs
  # the STANDALONE install, because the daemon self-updates app-server from a fixed path
  # (~/.codex/packages/standalone/current/codex). Without this the unit crash-loops with
  # "managed standalone Codex install not found". Per-user, since it lives under $HOME.
  # The -H and the cd are both load-bearing: the installer shells out to `find`, and if the
  # working directory is one the target user cannot read (e.g. /home/ubuntu, where this
  # script runs) it dies with "Failed to restore initial working directory" AFTER creating
  # releases/ but BEFORE creating current/ -- leaving a half-install that looks plausible.
  CODEX_STANDALONE="/home/$u/.codex/packages/standalone/current/codex"
  [ -x "$CODEX_STANDALONE" ] || sudo -u "$u" -H sh -c "cd /home/$u && curl -fsSL https://chatgpt.com/codex/install.sh | sh" >/dev/null 2>&1 || true

  # Codex's counterpart to Claude's --dangerously-skip-permissions. Deliberate: the EC2
  # instance IS the sandbox. It holds nothing but these projects, has no inbound web
  # ports, and its IAM role can read exactly one S3 object and one secret -- so a second
  # sandbox inside it buys approval prompts and no real containment. Codex's own help
  # says this mode is "intended solely for running in environments that are externally
  # sandboxed", which is this.
  #
  # Edited in place rather than appended: config.toml ends with [projects."..."] sections,
  # so a naive >> would bury these keys INSIDE a section and silently not apply. Missing
  # keys are inserted at line 1, above any section header.
  CFG="/home/$u/.codex/config.toml"
  if [ -f "$CFG" ]; then
    sed -i 's|^ *approval_policy *=.*|approval_policy = "never"|' "$CFG"
    sed -i 's|^ *sandbox_mode *=.*|sandbox_mode = "danger-full-access"|' "$CFG"
    grep -q '^approval_policy' "$CFG" || sed -i '1i approval_policy = "never"' "$CFG"
    grep -q '^sandbox_mode' "$CFG" || sed -i '1i sandbox_mode = "danger-full-access"' "$CFG"
  else
    install -d -o "$u" -g "$u" -m 700 "/home/$u/.codex"
    printf 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n' > "$CFG"
  fi
  # Pre-trust the published project so it never stops to ask on first use.
  PDIR=$(/usr/local/bin/agent-dir "$u" 2>/dev/null || echo "/home/$u")
  grep -qF "[projects.\"$PDIR\"]" "$CFG" 2>/dev/null || \
    printf '\n[projects."%s"]\ntrust_level = "trusted"\n' "$PDIR" >> "$CFG"

  # Guidance for remote-control sessions. ~/Documents/Codex is the parent of every
  # session's scratch dir (~/Documents/Codex/<date>/<task-name>), and codex reads AGENTS.md
  # by walking UP from its cwd -- so this reaches sessions the host cannot otherwise
  # configure. It is the only lever over app-created sessions besides writable_roots.
  install -d -o "$u" -g "$u" -m 755 "/home/$u/Documents/Codex"
  /usr/local/bin/kid-agents-refresh "$u" || echo "WARNING: AGENTS.md generation failed for $u"
  cat > "/home/$u/Documents/Codex/AGENTS.md.static" <<AGENTSMD
# Read this first

You have been started in an empty scratch folder under Documents/Codex/<date>/<task>/.
That folder is NOT the project.

The project is a Next.js app at:

    $PDIR

Work there: read, search and edit files in that directory and run commands from it. It is
a git repository, and it is writable inside your sandbox.

## The dev server is already running -- do not start one

It is a systemd service, not something you launch. It is published at
https://$u.$(/usr/local/bin/ndev-zone "$u") and listens on 127.0.0.1 only.

**Your sandbox has no network access, so \`curl http://localhost:...\` WILL FAIL even
though the server is running fine.** A failed curl does not mean the server is down. Do
not conclude that it stopped, and do not start your own \`next dev\` -- two servers on one
port just breaks the site.

To check on it, use these instead. They need no network:

    systemctl is-active ndev@$u          # "active" means it is up
    journalctl -u ndev@$u -n 50          # recent output, including compile errors
    sudo systemctl restart ndev@$u       # pick up a change that needs a restart

Next.js hot-reloads on save, so most edits need no restart at all.
AGENTSMD
  chown "$u:$u" "/home/$u/Documents/Codex/AGENTS.md"

  # THIS is what actually stops the endless "Always approve" prompts.
  #
  # The ChatGPT app sends sandbox="workspace-write" per session and that beats
  # sandbox_mode here -- the client always wins. But it sends the MODE, not the roots:
  # in workspace-write the writable set is the session cwd PLUS whatever config lists.
  # Remote-control sessions start in a throwaway ~/Documents/Codex/<date>/<task>/ dir, so
  # without this every single edit to the actual project is outside the sandbox and
  # escalates for approval.
  #
  # Appending a new [table] header is safe -- unlike bare keys, it cannot be swallowed by
  # a preceding [projects."..."] section.
  if ! grep -q '^\[sandbox_workspace_write\]' "$CFG" 2>/dev/null; then
    printf '\n[sandbox_workspace_write]\nwritable_roots = ["%s"]\nnetwork_access = true\n' "$PDIR" >> "$CFG"
  fi
  chown "$u:$u" "$CFG" 2>/dev/null || true
  { [ -s "/home/$u/.codex/auth.json" ] && [ -x "$CODEX_STANDALONE" ]; } && systemctl enable --now "codex-rc@$u.service" 2>/dev/null || true
  # Re-enable previously published projects (per-instance env files).
  for envf in /var/lib/ndev/instances/*.env; do
    [ -s "$envf" ] || continue
    # shellcheck disable=SC1090
    WHO_LINE=$(grep -E '^WHO=' "$envf" | head -1 | cut -d= -f2- || true)
    [ "$WHO_LINE" = "$u" ] || continue
    label=$(basename "$envf" .env)
    DROP_DIR="/etc/systemd/system/ndev@$label.service.d"
    mkdir -p "$DROP_DIR"
    cat > "$DROP_DIR/user.conf" <<EOF
[Service]
User=$u
Group=$u
WorkingDirectory=/home/$u
Environment=HOME=/home/$u
EOF
    systemctl enable --now "ndev@$label.service" 2>/dev/null || true
  done
  # Legacy single-file layout until migrated below.
  if [ -s "/var/lib/ndev/$u.env" ] && [ ! -s "/var/lib/ndev/instances/$u.env" ]; then
    systemctl enable --now "ndev@$u.service" 2>/dev/null || true
  fi
done

# ---------------------------------------------------------------- nightly agent update
# Keeps Claude and Codex current and bounces the units so the new binaries are actually in
# use -- an updated package does nothing while the old process is still running.
#
# Scheduled at 08:00 UTC (01:00 Pacific) because restarting claude-rc kills whatever
# conversation is open in it. RandomizedDelaySec spreads it so both kids' agents are not
# torn down in the same second. Persistent=true means a box that was off overnight catches
# up on the next boot rather than skipping a week of updates.
cat > /usr/local/bin/agent-update <<'AGENTUPD'
#!/bin/bash
# Update Claude Code, Codex and Vercel, then restart the agent services.
set -uo pipefail
export DEBIAN_FRONTEND=noninteractive
echo "=== agent-update $(date -u '+%F %T UTC') ==="

# claude updates itself now that it is a native per-user install, so there is nothing to
# npm-install here. Re-run the installer per account: it is a no-op when current, and it
# repairs an account whose copy went missing.
for u in ${join(" ", local.nextjs_users)} ubuntu; do
  id -u "$u" >/dev/null 2>&1 || continue
  BEFORE=$(sudo -u "$u" -H bash -lc 'claude --version 2>/dev/null | head -1')
  sudo -u "$u" -H bash -c '[ -x "$HOME/.local/bin/claude" ] || curl -fsSL https://claude.ai/install.sh | bash' >/dev/null 2>&1 || true
  AFTER=$(sudo -u "$u" -H bash -lc 'claude --version 2>/dev/null | head -1')
  echo "claude[$u]: $${BEFORE:-none} -> $${AFTER:-none}"
done
npm install -g vercel >/dev/null 2>&1 || echo "WARNING: vercel update failed"


for u in ${join(" ", local.nextjs_users)}; do
  id "$u" >/dev/null 2>&1 || continue
  # Codex ships its own installer and self-updates the standalone bundle; re-running it is
  # the supported refresh. Must run AS the user from a directory they can read -- from
  # elsewhere it dies in `find` after creating releases/ but before current/.
  if [ -s "/home/$u/.codex/auth.json" ]; then
    B=$(sudo -u "$u" -H /home/$u/.local/bin/codex --version 2>/dev/null | head -1)
    sudo -u "$u" -H sh -c "cd /home/$u && curl -fsSL https://chatgpt.com/codex/install.sh | sh" >/dev/null 2>&1 \
      || echo "WARNING: codex update failed for $u"
    echo "codex[$u]: $${B:-none} -> $(sudo -u "$u" -H /home/$u/.local/bin/codex --version 2>/dev/null | head -1)"
  fi
  # Refresh the project list too, so a repo added during the day shows up.
  /usr/local/bin/kid-agents-refresh "$u" 2>&1 | sed "s/^/agents-md[$u]: /"
done

for u in ${join(" ", local.nextjs_users)}; do
  id "$u" >/dev/null 2>&1 || continue
  for s in claude-rc codex-rc; do
    systemctl is-enabled "$s@$u" >/dev/null 2>&1 || continue
    systemctl restart "$s@$u" && echo "restarted $s@$u"
  done
done

# ndev is deliberately NOT restarted: nothing here changes Next.js, and bouncing it would
# drop the live site for no reason.
echo "=== done ==="
AGENTUPD
chmod 755 /usr/local/bin/agent-update

cat > /etc/systemd/system/agent-update.service <<'UNIT'
[Unit]
Description=Update Claude/Codex and restart the agent services
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/bin/agent-update
UNIT

cat > /etc/systemd/system/agent-update.timer <<'UNIT'
[Unit]
Description=Nightly Claude/Codex update

[Timer]
OnCalendar=*-*-* 08:00:00 UTC
RandomizedDelaySec=900
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now agent-update.timer >/dev/null 2>&1 || true

# ---------------------------------------------------------------- swap
# 8GB is comfortable for two kids, but each `next dev` compile spikes hard and there are
# now six long-lived node processes (2x next, 2x claude, 2x codex). A swapfile costs
# nothing on a 50GB volume and turns a would-be OOM kill into a slow moment.
# Same pipefail/SIGPIPE trap as above; command substitution has no pipeline to poison.
if [ -z "$(swapon --show=NAME --noheadings)" ]; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# ---------------------------------------------------------------- per-user shell env
# t-claude, atuin, starship, fzf, zsh plugins and the native-scrollback tmux.conf --
# identical for every account. This is local.user_shell_env, the SAME body the other dev
# boxes run for ubuntu, so there is one definition of "our shell" and no duplication.
# tmux and claude are installed once, system-wide, below.
if ! command -v tmux >/dev/null 2>&1 || ! /usr/local/bin/tmux -V 2>/dev/null | grep -q "3\.[6-9]"; then
  TARCH=$(uname -m)
  if curl -fsSL --retry 3 "https://github.com/ejc3/tmux/releases/download/binaries-3.x/tmux-$TARCH.tar.gz" -o /tmp/tmux.tgz && [ -s /tmp/tmux.tgz ]; then
    tar xzf /tmp/tmux.tgz -C /tmp && install -m 755 /tmp/tmux /usr/local/bin/tmux && rm -f /tmp/tmux.tgz /tmp/tmux
  else
    apt-get install -y tmux || true
  fi
fi
# Claude Code -- the NATIVE installer, per user. See dev-user-data.tf for the full reasoning:
# npm's "latest" lagged the native channel (2.1.241 vs 2.1.246), a root-owned global install
# cannot be updated by a normal user so every start retried and failed, and having both
# installs meant systemd sessions and interactive shells resolved DIFFERENT binaries.
#
# Native installs under each user's ~/.local, which suits this box better anyway: five
# accounts, each with its own copy and its own updates, none needing sudo.
#
# Tested with [ -x ~/.local/bin/claude ], never `command -v claude` -- on a box that still
# has the npm copy, command -v succeeds, the native install gets skipped, and the uninstall
# below would then leave that account with NO claude.
for u in ${join(" ", local.nextjs_users)} ubuntu; do
  id -u "$u" >/dev/null 2>&1 || continue
  sudo -u "$u" -H bash -c '[ -x "$HOME/.local/bin/claude" ] || curl -fsSL https://claude.ai/install.sh | bash' >/dev/null 2>&1 \
    || echo "WARNING: claude native install failed for $u"
  sudo -u "$u" -H bash -c 'grep -q "HOME/.local/bin" ~/.zshrc 2>/dev/null || printf "%s\n" "export PATH=\"\$HOME/.local/bin:\$PATH\"" >> ~/.zshrc'
done
# Drop the shared npm copy only once every account has its own native one.
MISSING=0
for u in ${join(" ", local.nextjs_users)} ubuntu; do
  id -u "$u" >/dev/null 2>&1 || continue
  sudo -u "$u" -H test -x "/home/$u/.local/bin/claude" || MISSING=1
done
if [ "$MISSING" = "0" ]; then
  npm uninstall -g @anthropic-ai/claude-code >/dev/null 2>&1 || true
else
  echo "WARNING: some accounts lack a native claude -- keeping the npm copy"
fi

# Vercel CLI. The project deploys to Vercel (the git integration on CoderColton/colton-games
# builds main automatically), and both kids are members of the Pro team that owns it, so
# they can also deploy and inspect from the box directly.
#
# Only the CLI is installed here. `vercel login` is an interactive device-code flow and
# each account must authenticate as ITSELF, so it cannot be baked in -- the tokens live in
# ~/.local/share/com.vercel.cli/auth.json, which is on the persistent volume and survives
# reboots and instance replacement.
command -v vercel >/dev/null 2>&1 || npm install -g vercel >/dev/null 2>&1 || echo "WARNING: vercel install failed"

# ---------------------------------------------------------------- dev-server hops
# Runs as root (it installs into /home/ubuntu as the ubuntu user) and only touches the
# ubuntu account -- colton and connor get no key to the Firecracker boxes, and cannot read
# /home/ubuntu anyway since it is 0700.
${local.dev_hop_setup}

# ---------------------------------------------------------------- pbox
# The on-demand 192-core Graviton boxes, launched straight from here. No jumpbox is
# involved: this box's instance role carries the tag-scoped parallel-box-control policy
# (parallel-box-launch.tf), so `pbox up` calls ec2:RunInstances itself.
#
# BE HONEST ABOUT WHO THIS GIVES IT TO. An instance-role grant is a BOX-wide grant, and
# every account here has full passwordless sudo, so colton, connor and skevh can launch
# one exactly as ejc3 can -- no arrangement of key files changes that. What bounds it is
# the policy (only these two tagged boxes, only the dev-ebs-only role, no other instance
# reachable) and parallel-box-watchdog.tf, which terminates an idle box after 30 minutes.
# The exposure is therefore cost, not access, and it is capped.
${local.pbox_setup}

# ejc3's copy of the hop key. dev_hop_setup above installs it for `ubuntu` only, and
# /home/ubuntu is 0700 -- but `pbox up` ends by SSHing to the new box as the invoking
# user, so without this ejc3 could launch a box and then not reach it.
#
# This is convenience, NOT isolation: with full sudo on this box any account can already
# read the ubuntu copy. It is deliberately not installed for colton, connor or skevh
# because nothing they do needs it, not because it would stop them.
if [ -f /home/ubuntu/.ssh/dev_hop ] && id ejc3 >/dev/null 2>&1; then
  install -d -m 700 -o ejc3 -g ejc3 /home/ejc3/.ssh
  install -m 600 -o ejc3 -g ejc3 /home/ubuntu/.ssh/dev_hop /home/ejc3/.ssh/dev_hop
fi

# Private NFSv4 automount for the shared us-west-2 NVMe scratch box.
${local.io_box_client_setup}

# ---------------------------------------------------------------- codex sandbox
# Ubuntu 24.04 blocks unprivileged user namespaces via AppArmor. Codex's sandbox is bwrap,
# which needs a userns to bring up its loopback interface; it cannot, so EVERY sandboxed
# command fails and the model asks permission to run outside the sandbox instead. That is
# the "May I run ... outside the sandbox? The sandbox failed to initialize its loopback
# interface" prompt, on every single command.
#
# Verified on this host with the real bundled binary:
#   .../codex-resources/bwrap --unshare-all --ro-bind / / --proc /proc --dev /dev /bin/true
#   -> bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted
#
# Do NOT test this with `unshare` -- it exits 0 here even while bwrap fails, because
# unconfined shells are exempt from the restriction. Testing unshare is what led me to the
# wrong conclusion the first time.
#
# Turning the restriction off is defensible on this box specifically: the EC2 instance IS
# the isolation boundary. It holds only these projects, exposes no inbound web ports, and
# its IAM role can read exactly one S3 object and one secret. Note this re-enables
# unprivileged userns for every process here, not just codex. 20- beats Ubuntu's own
# /usr/lib/sysctl.d/10-apparmor.conf, which is the override name Ubuntu recommends.
if [ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null)" != "0" ]; then
  echo 'kernel.apparmor_restrict_unprivileged_userns = 0' > /etc/sysctl.d/20-apparmor-userns.conf
  sysctl --system >/dev/null 2>&1 || true
  echo "userns restriction now: $(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null)"
fi

# ---------------------------------------------------------------- eternal terminal
# So the phone can reach THIS box directly, not only via the jumpbox. The security group
# has always opened 2022, but nothing was listening: ET was never installed here, so that
# rule was open to no purpose and the only way in was plain ssh on 22.
#
# Same vendored 7.x build the other dev boxes run (ejc3/EternalTerminal, binaries-7.x),
# fetched prebuilt rather than compiled -- building ET from source takes minutes on a
# 2 vCPU box and this script runs on every boot.
if ! /usr/bin/etserver --version 2>/dev/null | grep -q "7\."; then
  # The prebuilt binaries are dynamically linked. The metal dev boxes happen to have
  # protobuf because they build it, but this box never did, so etserver died with
  # "error while loading shared libraries: libprotobuf.so.32". Install the runtime first
  # -- the version probe below correctly refuses to install a binary that cannot run, so
  # without this ET silently never appears.
  apt-get install -y libprotobuf32t64 libsodium23 >/dev/null 2>&1 || \
    apt-get install -y libprotobuf32t64 >/dev/null 2>&1 || \
    echo "WARNING: could not install ET runtime deps"
  EARCH=$(uname -m)
  if curl -fsSL --retry 3 "https://github.com/ejc3/EternalTerminal/releases/download/binaries-7.x/et-$EARCH.tar.gz" -o /tmp/et.tgz && [ -s /tmp/et.tgz ]; then
    if tar xzf /tmp/et.tgz -C /tmp && [ -s /tmp/etserver ]; then
      # Prove it runs on this box before installing over anything. Check the OUTPUT, not
      # the exit status: this build prints its version and then aborts non-zero, so
      # `if /tmp/etserver --version` silently takes the failure branch and skips the
      # install even though the binary is fine. (dev-selfupdate.tf hit the same trap.)
      chmod +x /tmp/et /tmp/etserver /tmp/etterminal 2>/dev/null
      if /tmp/etserver --version 2>&1 </dev/null | grep -q "7\."; then
        install -m 755 /tmp/et /tmp/etserver /tmp/etterminal /usr/bin/
      else
        echo "WARNING: downloaded etserver did not report a 7.x version; leaving ET uninstalled"
      fi
    fi
    rm -f /tmp/et.tgz /tmp/et /tmp/etserver /tmp/etterminal
  else
    echo "WARNING: could not fetch Eternal Terminal binary"
  fi
fi

if [ -x /usr/bin/etserver ]; then
cat > /etc/systemd/system/etserver.service <<'UNIT'
[Unit]
Description=Eternal Terminal Server
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/etserver --port 2022
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now etserver.service 2>/dev/null || true
fi
curl -fsSL --retry 3 https://raw.githubusercontent.com/ejc3/t-claude/main/nosync-wrap -o /tmp/nw \
  && [ -s /tmp/nw ] && install -m 755 /tmp/nw /usr/local/bin/nosync-wrap && rm -f /tmp/nw

for u in ${join(" ", local.nextjs_users)} ubuntu; do
  id -u "$u" >/dev/null 2>&1 || continue
  echo "--- shell env for $u ---"
  sudo -u "$u" bash << 'USERSHELL' || echo "WARNING: shell env for $u had errors"
${local.user_shell_env}
USERSHELL
  chsh -s /usr/bin/zsh "$u" 2>/dev/null || true
done

# ---------------------------------------------------------------- codex hook paths
# atuin's codex integration writes hooks.json with a bare `atuin hook codex`. atuin is
# installed to ~/.atuin/bin, which is on the login PATH but NOT in the environment Codex
# runs hooks in -- so every Bash tool call fired the hook and it died with exit 127,
# printing "error: hook exited with code 127" into the middle of the session. Harmless but
# constant, and it looks like the agent is broken. Rewrite to the absolute path.
for u in ${join(" ", local.nextjs_users)} ubuntu; do
  id -u "$u" >/dev/null 2>&1 || continue
  HOOKS="/home/$u/.codex/hooks.json"
  ATUIN="/home/$u/.atuin/bin/atuin"
  [ -f "$HOOKS" ] && [ -x "$ATUIN" ] || continue
  python3 - "$HOOKS" "$ATUIN" <<'HOOKFIX'
import json, sys
path, atuin = sys.argv[1], sys.argv[2]
try:
    doc = json.load(open(path))
except Exception:
    raise SystemExit(0)
changed = 0
def walk(node):
    global changed
    if isinstance(node, dict):
        cmd = node.get("command")
        if isinstance(cmd, str) and cmd.startswith("atuin "):
            node["command"] = atuin + cmd[len("atuin"):]
            changed += 1
        for v in node.values():
            walk(v)
    elif isinstance(node, list):
        for v in node:
            walk(v)
walk(doc)
if changed:
    json.dump(doc, open(path, "w"), indent=2)
    print(f"codex hooks: absolute-pathed {changed} command(s)")
HOOKFIX
  chown "$u:$u" "$HOOKS" 2>/dev/null || true
done

# ---------------------------------------------------------------- git identity
# Derived from `gh api user`, never hardcoded, so it cannot drift from the account that is
# actually logged in.
#
# WHY THIS MATTERS: without user.name/user.email git refuses to commit at all --
# "Author identity unknown / Please tell me who you are". An agent that hits this reports
# it as "you are not logged in to GitHub", which sends you looking at `gh auth` (which is
# fine) instead of at git config. Both kids had working gh auth AND a working credential
# helper and still could not commit.
#
# Uses GitHub's noreply address (<id>+<login>@users.noreply.github.com) so commits still
# attribute to their accounts without publishing a child's real email address in public
# commit history. Any per-repo override is cleared for the same reason -- a local
# user.email silently wins over this and re-exposes the real address.
for u in ${join(" ", local.nextjs_users)}; do
  id -u "$u" >/dev/null 2>&1 || continue
  GH_ID=$(sudo -u "$u" -H env HOME="/home/$u" gh api user --jq '.id' 2>/dev/null)
  GH_LOGIN=$(sudo -u "$u" -H env HOME="/home/$u" gh api user --jq '.login' 2>/dev/null)
  GH_NAME=$(sudo -u "$u" -H env HOME="/home/$u" gh api user --jq '.name // .login' 2>/dev/null)
  if [ -n "$GH_ID" ] && [ -n "$GH_LOGIN" ]; then
    sudo -u "$u" -H env HOME="/home/$u" git config --global user.name "$GH_NAME"
    sudo -u "$u" -H env HOME="/home/$u" git config --global user.email "$GH_ID+$GH_LOGIN@users.noreply.github.com"
    # git needs a credential helper of its own; gh being authenticated is not enough.
    sudo -u "$u" -H env HOME="/home/$u" gh auth setup-git >/dev/null 2>&1 || true

    # Lay down this user's starter repositories. Deliberately inside the "gh is
    # authenticated" branch: these are private, so cloning them as anyone else would
    # either fail outright or leave another account's token in the checkout. Skipped
    # when the directory already exists, so it never clobbers local work.
    case "$u" in
%{~for ru, rl in local.nextjs_user_repos~}
      ${ru})
%{~for repo in rl~}
        if [ ! -d "/home/$u/${basename(repo)}" ]; then
          echo "cloning ${repo} for $u"
          sudo -u "$u" -H env HOME="/home/$u" \
            gh repo clone ${repo} "/home/$u/${basename(repo)}" >/dev/null 2>&1 \
            || echo "WARNING: could not clone ${repo} for $u (no access?)"
        fi
%{~endfor~}
        ;;
%{~endfor~}
    esac
    for r in /home/$u/*/; do
      [ -d "$r/.git" ] || continue
      sudo -u "$u" -H env HOME="/home/$u" git -C "$r" config --unset user.email 2>/dev/null || true
      sudo -u "$u" -H env HOME="/home/$u" git -C "$r" config --unset user.name 2>/dev/null || true
    done
    echo "git identity[$u]: $GH_NAME <$GH_ID+$GH_LOGIN@users.noreply.github.com>"
  else
    echo "git identity[$u]: not logged in to gh yet -- run 'gh auth login'"
  fi
done

# GitHub CLI, so each user can `gh auth login` under their own account
if ! command -v gh >/dev/null 2>&1; then
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    | dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
    > /etc/apt/sources.list.d/github-cli.list
  apt-get update || true
  apt-get install -y gh || echo "WARNING: gh install failed"
fi

echo "nextjs-dev ready: users=${join(",", local.nextjs_users)}, tunnel=${local.nextjs_tunnel_id}"
SCRIPT
}

resource "aws_s3_object" "nextjs_user_data" {
  count        = var.enable_nextjs_dev ? 1 : 0
  bucket       = aws_s3_bucket.dev_scripts.id
  key          = "user-data/nextjs.sh"
  content      = local.nextjs_user_data
  content_type = "text/x-shellscript"
  tags         = { Name = "nextjs-dev-user-data" }
}

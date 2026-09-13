# codex-remote-control.tf
#
# Codex remote control on the metal dev boxes, using everything learned getting it working
# on nextjs-dev. Shared as a local so both ARM and x86 get the identical setup.
#
# WHAT THE CLIENT CONTROLS AND WE CANNOT
# --------------------------------------
# The ChatGPT app sends approvalPolicy and sandbox with every thread/start, and codex
# merges them as `sandbox_mode_override.or(self.sandbox_mode)` -- the client is checked
# first, so it beats anything in config.toml. Verified on nextjs-dev: a session recorded
# approval_policy="on-request" while config.toml said "never".
#
# So approval mode is set in the APP, not here ("dangerous mode" in the remote-control
# settings). Once set, sessions record approval_policy="never" and
# sandbox_policy=danger-full-access.
#
# What config.toml still buys, because it MERGES rather than being overridden:
# writable_roots. In workspace-write the writable set is the session cwd plus whatever
# config lists -- and remote-control sessions start in a throwaway
# ~/Documents/Codex/<date>/<task>/ directory, not the project. Without listing the real
# working roots, every edit escalates for approval.
#
# THE SANDBOX ITSELF
# ------------------
# codex sandboxes with bwrap, which needs an unprivileged user namespace. On stock Ubuntu
# 24.04 that is blocked by AppArmor and bwrap dies with
#   "loopback: Failed RTM_NEWADDR: Operation not permitted"
# so EVERY command fails and the model escalates each time -- which reads as the sandbox
# asking permission constantly. The metal boxes run the custom nested-virt kernel, which
# does NOT carry that restriction (the sysctl does not even exist) and bwrap works as-is;
# the guard below only acts where the restriction is present, so it is a no-op here and
# does the right thing if a metal box is ever moved to a stock kernel.
#
# Do NOT test this with `unshare` -- it succeeds on hosts where bwrap still fails, because
# unconfined shells are exempt. Test the bwrap binary itself.

locals {
  codex_remote_control = <<-CODEX
# ---------------------------------------------------------------- codex remote control
CODEX_BIN="/home/ubuntu/.local/bin/codex"
CODEX_STANDALONE="/home/ubuntu/.codex/packages/standalone/current/codex"

# `codex remote-control` refuses to run against the npm/system install -- it needs the
# standalone one, because the daemon self-updates app-server from a fixed path. The -H and
# the cd are both load-bearing: the installer shells out to `find`, and from a directory
# the target user cannot read it dies AFTER creating releases/ but BEFORE current/, leaving
# a half-install that looks plausible.
if [ ! -x "$CODEX_STANDALONE" ]; then
  sudo -u ubuntu -H sh -c 'cd /home/ubuntu && curl -fsSL https://chatgpt.com/codex/install.sh | sh' >/dev/null 2>&1 \
    || echo "WARNING: standalone codex install failed"
fi

# Only act if the restriction exists (stock Ubuntu). Absent on the nested-virt kernel.
if [ -n "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null)" ] \
   && [ "$(sysctl -n kernel.apparmor_restrict_unprivileged_userns 2>/dev/null)" != "0" ]; then
  echo 'kernel.apparmor_restrict_unprivileged_userns = 0' > /etc/sysctl.d/20-apparmor-userns.conf
  sysctl --system >/dev/null 2>&1 || true
  echo "codex: relaxed userns restriction so bwrap can sandbox"
fi

CFG=/home/ubuntu/.codex/config.toml
if [ -f "$CFG" ]; then
  # Edit in place; NEVER append bare keys. This file ends with [projects."..."] tables, so
  # an appended key silently lands inside the last section and is ignored -- sandboxing
  # would look configured while doing nothing. Missing keys go in at line 1, above any
  # table header. A new [table] header is safe to append.
  sed -i 's|^ *approval_policy *=.*|approval_policy = "never"|' "$CFG"
  sed -i 's|^ *sandbox_mode *=.*|sandbox_mode = "danger-full-access"|' "$CFG"
  grep -q '^approval_policy' "$CFG" || sed -i '1i approval_policy = "never"' "$CFG"
  grep -q '^sandbox_mode' "$CFG" || sed -i '1i sandbox_mode = "danger-full-access"' "$CFG"
else
  install -d -o ubuntu -g ubuntu -m 700 /home/ubuntu/.codex
  printf 'approval_policy = "never"\nsandbox_mode = "danger-full-access"\n' > "$CFG"
fi

# Home is the working root: this box has many projects rather than one, so scoping to a
# single repo would be wrong. Matters only if a session lands in workspace-write.
if ! grep -q '^\[sandbox_workspace_write\]' "$CFG" 2>/dev/null; then
  printf '\n[sandbox_workspace_write]\nwritable_roots = ["/home/ubuntu"]\nnetwork_access = true\n' >> "$CFG"
fi
chown ubuntu:ubuntu "$CFG" 2>/dev/null || true

# oneshot, NOT forking: codex backgrounds its own daemon (pid backend, its own socket under
# ~/.codex/app-server-control) and the CLI returns immediately, so systemd has no forked
# child to adopt and marks the unit failed while the daemon runs happily.
#
# Success is asserted against --json rather than the exit status: the text output prints
# "the connection is errored" and exits non-zero while still establishing, and only the
# JSON reports the settled truth.
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
ExecStart=/bin/sh -c 'CX="/home/%i/.local/bin/codex"; "$CX" remote-control start >/dev/null 2>&1 || true; for i in 1 2 3 4 5; do "$CX" remote-control start --json 2>/dev/null | grep -q "\\"status\\":\\"connected\\"" && exit 0; sleep 5; done; exit 1'
ExecStop=-/home/%i/.local/bin/codex remote-control stop

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload

# ---------------------------------------------------------------- session pointer
# ~/Documents/Codex is the parent of every remote-control session's scratch directory
# (~/Documents/Codex/<date>/<task-name>/), and codex reads AGENTS.md by walking UP from its
# cwd -- so a file here reaches sessions the host cannot otherwise configure. Together with
# writable_roots it is the only lever over app-created sessions.
#
# GENERATED, not static: this box carries ~26 repositories and the interesting ones change.
# A hardcoded project list would be wrong within weeks, so it is rebuilt from what is
# actually on disk, newest first. Regenerate any time with `codex-agents-refresh`.
#
# Host-aware: a metal dev box is a disposable sandbox with full sudo, while an admin jumpbox
# holds AWS administrator credentials and the key to every dev box. Telling an agent on a
# jumpbox that "the VM is the isolation boundary, install whatever you need" is dangerous, so
# the caller sets FLEET_HOST_ROLE (metal, the default, or admin) and it is recorded for later
# refreshes.
printf '%s\n' "$${FLEET_HOST_ROLE:-metal}" > /etc/fleet-host-role
cat > /usr/local/bin/codex-agents-refresh <<'GENAGENTS'
#!/bin/bash
# Rebuild ~/Documents/Codex/AGENTS.md from the repos currently on this box.
set -uo pipefail
OUT=/home/ubuntu/Documents/Codex/AGENTS.md
ROLE=$(cat /etc/fleet-host-role 2>/dev/null || echo metal)
install -d -o ubuntu -g ubuntu -m 755 /home/ubuntu/Documents/Codex
TMP=$(mktemp)
{
  cat <<'HEAD'
# Read this first

You have been started in an empty scratch folder under Documents/Codex/<date>/<task>/.
**That folder is not a project and is not where the work is.**

This machine is a development server with many repositories. There is no single default
project, so unless the task names one, ask which repository to work in rather than
guessing or working in the scratch directory.

## Repositories

Checked out under `/home/ubuntu/`, most recently committed first:

HEAD
  for d in /home/ubuntu/*/; do
    [ -d "$d/.git" ] || continue
    n=$(basename "$d")
    case "$n" in actions-runner*) continue ;; esac
    ts=$(git -C "$d" log -1 --format=%ct 2>/dev/null) || continue
    date=$(git -C "$d" log -1 --format=%cd --date=short 2>/dev/null)
    remote=$(git -C "$d" remote get-url origin 2>/dev/null | sed 's|https://github.com/||;s|git@github.com:||;s|\.git$||')
    printf '%s|%s|%s|%s\n' "$${ts:-0}" "$n" "$date" "$${remote:-no remote}"
  done | sort -rn | head -14 | while IFS='|' read -r _ n date remote; do
    printf -- '- `~/%s` — %s (last commit %s)\n' "$n" "$remote" "$date"
  done
  cat <<'COMMON'

Repositories under `github.com/ejc3/` are the user's own. Others are upstream forks —
treat those as read-only unless explicitly asked to change them.

## This machine

COMMON
  if [ "$ROLE" = admin ]; then
    cat <<'ADMIN'
- An **admin jumpbox**. Its instance role has **AWS AdministratorAccess**, and it holds (or
  can restore) the key that reaches every dev box. It is **not a sandbox**: treat every
  command as able to change production, and stay read-only unless the task is a change.
- Small: 2 vCPU on a burstable instance, with `/home/ubuntu` on a gp3 volume capped at
  125 MB/s. Do not run builds, test suites, or recursive searches over `/home/ubuntu` or `/`
  here; saturating the disk stalls SSH for everyone. Heavy work belongs on a dev box.
- Infrastructure lives in `~/aws` (Terraform, remote state). Apply only from a fresh git
  worktree of `origin/main`, read the plan first, and never leave a change applied but
  uncommitted.
- Ask before restarting, stopping, or rebooting any host.

## Other machines

The dev boxes accept SSH from here as `ubuntu` with `~/.ssh/fcvm-ec2`. Look up current
addresses instead of trusting a remembered one:

```bash
aws ec2 describe-instances --region us-west-1 \
  --filters Name=instance-state-name,Values=running \
  --query 'Reservations[].Instances[].[Tags[?Key==`Name`]|[0].Value,PublicIpAddress]' \
  --output text
```

Nothing on a dev box may ever be given a way to connect back to a jumpbox: no key, no
forced command, and no SSM or queue that a jumpbox acts on.

## Long jobs

Run anything longer than a minute under `tmux` or `nohup`, and put the heavy part on a dev
box.
ADMIN
  else
    case "$(uname -m)" in
      aarch64) NAME=fcvm-metal-arm ;;
      *) NAME=fcvm-metal-x86 ;;
    esac
    printf -- '- `%s`: %s-vCPU %s bare metal. Build in parallel: `make -j$(nproc)` and\n' "$NAME" "$(nproc)" "$(uname -m)"
    printf -- '  `cargo build -j$(nproc)` are expected.\n'
    cat <<'METAL'
- You have **full passwordless sudo**. The VM is the isolation boundary, so install
  whatever you need.
- It is a **spot instance**: it can be reclaimed and restarted at any time. Anything not
  committed and pushed can disappear. Push work in progress to a branch rather than
  leaving it only on disk.
- `/home/ubuntu` is on a persistent EBS volume, backed up daily. **`/mnt/fcvm-btrfs` is
  instance NVMe and is wiped on stop/start** — use it for build caches and VM images, never
  for anything you cannot regenerate.

## Other machines

Reachable from here as `ubuntu` with a dedicated hop key, no password:

```bash
METAL
    grep -hE '^Host ' /home/ubuntu/.ssh/config.d-devhop 2>/dev/null | awk '{print "ssh " $2}'
    cat <<'METALTAIL'
```

There is deliberately **no key to any jumpbox** on this machine, and there must never be
one: no SSH key, no forced command, and no SSM or queue that a jumpbox acts on.

## Long jobs

Builds and test suites here can run for many minutes. Run them under `tmux` or `nohup` so
they survive your session ending, and avoid unbounded recursive searches over `/home/ubuntu`
— it holds multiple kernel trees and node_modules directories.
METALTAIL
  fi
} > "$TMP"
install -m 644 -o ubuntu -g ubuntu "$TMP" "$OUT"
rm -f "$TMP"
echo "codex: wrote $OUT ($(wc -c < "$OUT") bytes)"
GENAGENTS
chmod 755 /usr/local/bin/codex-agents-refresh
/usr/local/bin/codex-agents-refresh || true

# Guarded on credentials: on a fresh box nobody has logged in to Codex yet, and enabling
# the unit then would just fail on every boot until someone does.
if [ -s /home/ubuntu/.codex/auth.json ] && [ -x "$CODEX_STANDALONE" ]; then
  systemctl enable --now codex-rc@ubuntu.service >/dev/null 2>&1 || true
  echo "codex: remote control enabled for ubuntu"
else
  echo "codex: no auth yet -- run 'codex login' then 'systemctl enable --now codex-rc@ubuntu'"
fi
CODEX
}

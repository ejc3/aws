# These associations install only the cleanup job. Using setup-sync would rerun
# the whole Next.js setup, including its credential-service restarts.
#
# One installer, two hosts. The Next.js host cleans ubuntu and every running
# container account; fcvm-metal-arm has no nextjs-ct and cleans only ubuntu
# (--host-only). Each host's units, entry point and lock carry its own name;
# the scripts stay in /usr/local/lib/nextjs-worktree-prune, the path the driver
# loads its worker from.
locals {
  worktree_prune_jobs = {
    nextjs = { name = "nextjs-worktree-prune", args = "--apply", what = "Next.js" }
    fcvm   = { name = "fcvm-worktree-prune", args = "--apply --host-only", what = "fcvm-metal-arm" }
  }
  worktree_prune_install = { for key, job in local.worktree_prune_jobs : key => <<-SH
        set -eu
        export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
        d=$(mktemp -d)
        trap 'rm -rf "$d"' EXIT
        printf '%s' '${base64gzip(file("${path.module}/scripts/nextjs-worktree-prune-host.py"))}' | base64 -d | gzip -d > "$d/driver"
        printf '%s' '${base64gzip(file("${path.module}/scripts/nextjs-worktree-prune.py"))}' | base64 -d | gzip -d > "$d/worker.py"
        printf '%s  %s\n' '${filesha256("${path.module}/scripts/nextjs-worktree-prune-host.py")}' "$d/driver" \
          '${filesha256("${path.module}/scripts/nextjs-worktree-prune.py")}' "$d/worker.py" | sha256sum --check --quiet
        cat > "$d/entry" <<'ENTRY'
        #!/bin/sh
        # Lock before Python loads the driver; do not pass the lock fd to account commands.
        exec /usr/bin/flock --close --nonblock --conflict-exit-code 0 /run/${job.name}.lock \
          /bin/sh -c '
            set -eu
            printf "%s  %s\n" "${filesha256("${path.module}/scripts/nextjs-worktree-prune-host.py")}" /usr/local/lib/nextjs-worktree-prune/driver.py \
              "${filesha256("${path.module}/scripts/nextjs-worktree-prune.py")}" /usr/local/lib/nextjs-worktree-prune/worker.py | /usr/bin/sha256sum --check --status ||
              { echo "${job.name}: payload checksum mismatch" >&2; exit 1; }
            exec /usr/bin/python3 -I /usr/local/lib/nextjs-worktree-prune/driver.py "$@"
          ' sh "$@"
        ENTRY
        cat > "$d/service" <<'UNIT'
        [Unit]
        Description=Prune merged ${job.what} Git worktrees under disk pressure

        [Service]
        Type=oneshot
        ExecStart=/usr/local/sbin/${job.name} ${job.args}
        Nice=15
        IOSchedulingClass=idle
        NoNewPrivileges=yes
        TimeoutStartSec=45min
        UNIT
        cat > "$d/timer" <<'UNIT'
        [Unit]
        Description=Hourly ${job.what} worktree cleanup

        [Timer]
        OnCalendar=*-*-* *:30:00 UTC
        Persistent=false

        [Install]
        WantedBy=timers.target
        UNIT
        exec 9>/run/${job.name}.lock
        flock -w 15 9 || { echo '${job.name}: cleanup busy; installation deferred' >&2; exit 1; }
        install -d -m 755 /usr/local/lib/nextjs-worktree-prune
        publish() {
          tmp=$(mktemp "$3.new.XXXXXXXX")
          install -o root -g root -m "$1" "$2" "$tmp"
          mv -fT "$tmp" "$3"
        }
        publish 644 "$d/driver" /usr/local/lib/nextjs-worktree-prune/driver.py
        publish 644 "$d/worker.py" /usr/local/lib/nextjs-worktree-prune/worker.py
        publish 644 "$d/service" /etc/systemd/system/${job.name}.service
        publish 644 "$d/timer" /etc/systemd/system/${job.name}.timer
        # The wrapper verifies the exact driver/worker pair after taking the lock.
        # Publish it last so an interrupted install cannot execute mixed versions.
        publish 755 "$d/entry" /usr/local/sbin/${job.name}
        systemctl daemon-reload
        systemctl enable --now ${job.name}.timer
        systemctl is-enabled --quiet ${job.name}.timer
        systemctl is-active --quiet ${job.name}.timer
        next=$(systemctl show ${job.name}.timer -p NextElapseUSecRealtime --value)
        case "$next" in ''|n/a|0) exit 1 ;; esac
        printf '${job.name}: timer active; next run %s\n' "$next"
    SH
  }
}

resource "aws_ssm_association" "nextjs_worktree_prune" {
  count = var.enable_nextjs_ct && local.nextjs_ct_serving ? 1 : 0

  name                             = "AWS-RunShellScript"
  association_name                 = "nextjs-worktree-prune-install"
  schedule_expression              = "rate(1 day)"
  wait_for_success_timeout_seconds = 300
  max_concurrency                  = "1"
  max_errors                       = "1"

  targets {
    key    = "InstanceIds"
    values = [local.nextjs_live_instance.id]
  }

  parameters = {
    commands         = local.worktree_prune_install["nextjs"]
    executionTimeout = "120"
  }

  lifecycle {
    # Leave room for the SSM document wrapper and its encoded parameters.
    precondition {
      condition     = length(local.worktree_prune_install["nextjs"]) < 60000
      error_message = "The compressed Next.js worktree prune installer exceeds the SSM payload budget."
    }
  }
}

# fcvm-metal-arm's ~/src fills its one root disk with agents' linked worktrees.
# Same installer and safety rules; only the ubuntu account, no machinectl. Its
# --host-only run then also clears idle worktrees' ignored node_modules, .next
# and target (the worker's build pass); the Next.js job never runs that pass.
resource "aws_ssm_association" "fcvm_arm_worktree_prune" {
  count = var.enable_firecracker_instance ? 1 : 0

  name                             = "AWS-RunShellScript"
  association_name                 = "fcvm-arm-worktree-prune-install"
  schedule_expression              = "rate(1 day)"
  wait_for_success_timeout_seconds = 300
  max_concurrency                  = "1"
  max_errors                       = "1"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.firecracker_dev[0].id]
  }

  parameters = {
    commands         = local.worktree_prune_install["fcvm"]
    executionTimeout = "120"
  }

  lifecycle {
    precondition {
      condition     = length(local.worktree_prune_install["fcvm"]) < 60000
      error_message = "The compressed fcvm-metal-arm worktree prune installer exceeds the SSM payload budget."
    }
  }
}

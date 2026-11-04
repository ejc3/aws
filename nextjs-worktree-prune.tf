# This association installs only the cleanup job. Using setup-sync would rerun
# the whole Next.js setup, including its credential-service restarts.
locals {
  nextjs_worktree_prune_install = <<-SH
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
    exec /usr/bin/flock --close --nonblock --conflict-exit-code 0 /run/nextjs-worktree-prune.lock \
      /bin/sh -c '
        set -eu
        printf "%s  %s\n" "${filesha256("${path.module}/scripts/nextjs-worktree-prune-host.py")}" /usr/local/lib/nextjs-worktree-prune/driver.py \
          "${filesha256("${path.module}/scripts/nextjs-worktree-prune.py")}" /usr/local/lib/nextjs-worktree-prune/worker.py | /usr/bin/sha256sum --check --status ||
          { echo "nextjs-worktree-prune: payload checksum mismatch" >&2; exit 1; }
        exec /usr/bin/python3 -I /usr/local/lib/nextjs-worktree-prune/driver.py "$@"
      ' sh "$@"
    ENTRY
    cat > "$d/service" <<'UNIT'
    [Unit]
    Description=Prune merged Next.js Git worktrees under disk pressure

    [Service]
    Type=oneshot
    ExecStart=/usr/local/sbin/nextjs-worktree-prune --apply
    Nice=15
    IOSchedulingClass=idle
    NoNewPrivileges=yes
    TimeoutStartSec=45min
    UNIT
    cat > "$d/timer" <<'UNIT'
    [Unit]
    Description=Nightly Next.js worktree cleanup

    [Timer]
    OnCalendar=*-*-* 10:30:00 UTC
    Persistent=false

    [Install]
    WantedBy=timers.target
    UNIT
    exec 9>/run/nextjs-worktree-prune.lock
    flock -w 15 9 || { echo 'nextjs-worktree-prune: cleanup busy; installation deferred' >&2; exit 1; }
    install -d -m 755 /usr/local/lib/nextjs-worktree-prune
    publish() {
      tmp=$(mktemp "$3.new.XXXXXXXX")
      install -o root -g root -m "$1" "$2" "$tmp"
      mv -fT "$tmp" "$3"
    }
    publish 644 "$d/driver" /usr/local/lib/nextjs-worktree-prune/driver.py
    publish 644 "$d/worker.py" /usr/local/lib/nextjs-worktree-prune/worker.py
    publish 644 "$d/service" /etc/systemd/system/nextjs-worktree-prune.service
    publish 644 "$d/timer" /etc/systemd/system/nextjs-worktree-prune.timer
    # The wrapper verifies the exact driver/worker pair after taking the lock.
    # Publish it last so an interrupted install cannot execute mixed versions.
    publish 755 "$d/entry" /usr/local/sbin/nextjs-worktree-prune
    systemctl daemon-reload
    systemctl enable --now nextjs-worktree-prune.timer
    systemctl is-enabled --quiet nextjs-worktree-prune.timer
    systemctl is-active --quiet nextjs-worktree-prune.timer
    next=$(systemctl show nextjs-worktree-prune.timer -p NextElapseUSecRealtime --value)
    case "$next" in ''|n/a|0) exit 1 ;; esac
    printf 'nextjs-worktree-prune: timer active; next run %s\n' "$next"
  SH
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
    commands         = local.nextjs_worktree_prune_install
    executionTimeout = "120"
  }

  lifecycle {
    # Leave room for the SSM document wrapper and its encoded parameters.
    precondition {
      condition     = length(local.nextjs_worktree_prune_install) < 60000
      error_message = "The compressed Next.js worktree prune installer exceeds the SSM payload budget."
    }
  }
}

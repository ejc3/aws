# pbox-work-clean.tf
#
# The disk cleaner for the parallel boxes' work volumes (owner, 2026-11-26: "Please have a cleaner on these boxes like we
# have for others"). Each slot's /mnt/work (ohio-pbox.tf, member-pbox.tf) survives every launch, and agents leave
# checkouts, Cargo target directories, node_modules and logs on it; on 2026-11-26 several slots were 100% full. The
# cleaner (scripts/pbox-work-clean.py, whose docstring is the full rule set) runs hourly and once after boot: idle build
# caches after 2 days, idle top-level task directories after 7, and under pressure (over 85%) the oldest idle ones until
# under 70%. It never touches a directory with a process in it, anything changed in the last 24 hours, or anything with
# a .pbox-keep inside, and the unit can write nothing but /mnt/work and its lock.
#
# Installed the way nextjs-worktree-prune.tf installs its cleaner: an SSM association that writes the checksummed script,
# a flock entry point and the units. It is not in the boot script because EC2 user data is capped at 16 KiB raw and a
# home box's (parallel-box-launch.tf) is already about 14 KiB. A State Manager association applies to a new instance
# whose Name matches as soon as its agent registers, so a box gets the cleaner within minutes of every launch, and a box
# already running gets it on apply (the timer's boot run is then already due, so it runs at once). Every pbox region:
# home (us-east-2) and the fallback sites (pbox-fallback.tf; a fallback box's fresh volume is cleaned by the same rules,
# harmlessly). Members' pboxes too: same volume, same problem.
locals {
  pbox_work_clean_names = concat(local.parallel_box_names, local.member_pbox_names)
  # State Manager permits only five values for a tag:Name target per association.
  pbox_work_clean_groups = { for index, names in chunklist(local.pbox_work_clean_names, 5) : tostring(index) => names }

  pbox_work_clean_install = <<-SH
    set -eu
    export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
    d=$(mktemp -d)
    trap 'rm -rf "$d"' EXIT
    printf '%s' '${base64gzip(file("${path.module}/scripts/pbox-work-clean.py"))}' | base64 -d | gzip -d > "$d/clean.py"
    printf '%s  %s\n' '${filesha256("${path.module}/scripts/pbox-work-clean.py")}' "$d/clean.py" | sha256sum --check --quiet
    cat > "$d/entry" <<'ENTRY'
    #!/bin/sh
    # Lock before Python loads the script, so two runs never overlap.
    exec /usr/bin/flock --close --nonblock --conflict-exit-code 0 /run/lock/pbox-work-clean.lock \
      /bin/sh -c '
        set -eu
        printf "%s  %s\n" "${filesha256("${path.module}/scripts/pbox-work-clean.py")}" /usr/local/lib/pbox-work-clean/clean.py |
          /usr/bin/sha256sum --check --status || { echo "pbox-work-clean: payload checksum mismatch" >&2; exit 1; }
        exec /usr/bin/python3 -I /usr/local/lib/pbox-work-clean/clean.py "$@"
      ' sh "$@"
    ENTRY
    cat > "$d/service" <<'UNIT'
    [Unit]
    Description=Remove idle build caches and task directories from the pbox work volume
    ConditionPathIsMountPoint=/mnt/work

    [Service]
    Type=oneshot
    ExecStart=/usr/local/sbin/pbox-work-clean --apply
    Nice=15
    IOSchedulingClass=idle
    NoNewPrivileges=yes
    TimeoutStartSec=45min
    # Nothing outside the work volume and the lock is writable, whatever the script does.
    ProtectSystem=strict
    ProtectHome=read-only
    PrivateTmp=yes
    ReadWritePaths=-/mnt/work /run/lock
    UNIT
    cat > "$d/timer" <<'UNIT'
    [Unit]
    Description=Hourly pbox work volume cleanup, and once after boot

    [Timer]
    OnBootSec=10min
    OnUnitInactiveSec=1h

    [Install]
    WantedBy=timers.target
    UNIT
    exec 9>/run/lock/pbox-work-clean.lock
    flock -w 15 9 || { echo 'pbox-work-clean: cleanup busy; installation deferred' >&2; exit 1; }
    install -d -m 755 /usr/local/lib/pbox-work-clean
    publish() {
      tmp=$(mktemp "$3.new.XXXXXXXX")
      install -o root -g root -m "$1" "$2" "$tmp"
      mv -fT "$tmp" "$3"
    }
    publish 644 "$d/clean.py" /usr/local/lib/pbox-work-clean/clean.py
    publish 644 "$d/service" /etc/systemd/system/pbox-work-clean.service
    publish 644 "$d/timer" /etc/systemd/system/pbox-work-clean.timer
    # The entry point verifies the script after taking the lock. Publish it last.
    publish 755 "$d/entry" /usr/local/sbin/pbox-work-clean
    systemctl daemon-reload
    systemctl enable --now pbox-work-clean.timer
    systemctl is-enabled --quiet pbox-work-clean.timer
    systemctl is-active --quiet pbox-work-clean.timer
    printf 'pbox-work-clean: timer active; %s\n' "$(systemctl show pbox-work-clean.timer -p NextElapseUSecMonotonic --value)"
  SH

  pbox_work_clean_parameters = {
    commands         = local.pbox_work_clean_install
    executionTimeout = "120"
  }
}

# Home (us-east-2): the owner's slots and the members' boxes, and the us-east-2 fallback site.
resource "aws_ssm_association" "pbox_work_clean_ohio" {
  provider            = aws.ohio
  for_each            = local.pbox_work_clean_groups
  name                = "AWS-RunShellScript"
  association_name    = "pbox-work-clean-${each.key}"
  schedule_expression = "rate(1 day)"
  targets {
    key    = "tag:Name"
    values = each.value
  }
  parameters = local.pbox_work_clean_parameters

  lifecycle {
    # Leave room for the SSM document wrapper and its encoded parameters.
    precondition {
      condition     = length(local.pbox_work_clean_install) < 60000
      error_message = "The compressed pbox work volume cleaner installer exceeds the SSM payload budget."
    }
  }
}

# The fallback sites in the other US regions (pbox-fallback.tf).
resource "aws_ssm_association" "pbox_work_clean_east1" {
  provider            = aws.dr
  for_each            = local.pbox_work_clean_groups
  name                = "AWS-RunShellScript"
  association_name    = "pbox-work-clean-${each.key}"
  schedule_expression = "rate(1 day)"
  targets {
    key    = "tag:Name"
    values = each.value
  }
  parameters = local.pbox_work_clean_parameters
}

resource "aws_ssm_association" "pbox_work_clean_west1" {
  for_each            = local.pbox_work_clean_groups
  name                = "AWS-RunShellScript"
  association_name    = "pbox-work-clean-${each.key}"
  schedule_expression = "rate(1 day)"
  targets {
    key    = "tag:Name"
    values = each.value
  }
  parameters = local.pbox_work_clean_parameters
}

resource "aws_ssm_association" "pbox_work_clean_west2" {
  provider            = aws.west2
  for_each            = local.pbox_work_clean_groups
  name                = "AWS-RunShellScript"
  association_name    = "pbox-work-clean-${each.key}"
  schedule_expression = "rate(1 day)"
  targets {
    key    = "tag:Name"
    values = each.value
  }
  parameters = local.pbox_work_clean_parameters
}

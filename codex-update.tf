# codex-update.tf
#
# Keeps the standalone Codex (~/.codex/packages/standalone) current on the four boxes that run it
# as `ubuntu`: both jumpboxes, fcvm-metal-arm and fcvm-metal-x86.
#
# WHY. Nothing refreshed it. The installer swaps `current` to the newest release, but the
# metal boxes ran it only when Codex was MISSING, and the jumpboxes had no Terraform path at all,
# so on 2026-09-30 three boxes were on 0.154.0 while 0.159.2 was out (and the model list a client
# gets is filtered by its version, so an old client does not see new models). nextjs-dev already
# refreshes every account daily in its own updater (nextjs-user-data.tf) and is not touched here.
#
# HOW. A weekly SSM State Manager association: Run Command on a schedule, no key on disk, no
# service on the box, and the permitted admin -> box direction. The installer runs as ubuntu,
# never as root (the jumpboxes must not host a root service that re-runs a downloaded script,
# dev-selfupdate.tf), and writes only under ~/.codex and ~/.local/bin. The same command installs
# /usr/local/bin/codex-restart, so the jumpboxes have it too (the dev boxes get it at boot,
# dev-selfupdate.tf).
#
# WHAT IT DELIBERATELY DOES NOT DO: restart anything. A running daemon keeps the binary it
# started with, and restarting interrupts turns, so applying an update is a decision:
# `codex-restart` shows running vs installed per account, `codex-restart --restart` applies it.
#
# A box that is stopped on Monday (the x86 spot box often is) misses that run; it catches up at
# the next one, or by running the same installer by hand.

locals {
  codex_update_instances = concat(
    var.enable_jumpbox ? [aws_instance.jumpbox[0].id] : [],
    var.enable_jumpbox_2 ? [aws_instance.jumpbox_2[0].id] : [],
    var.enable_firecracker_instance ? [aws_instance.firecracker_dev[0].id] : [],
    var.enable_x86_dev_instance ? [aws_instance.x86_dev[0].id] : [],
  )

  codex_update_commands = <<-CMD
    set -u
    echo ${base64encode(file("${path.module}/scripts/codex-restart.sh"))} | base64 -d > /usr/local/bin/codex-restart.new
    chmod 755 /usr/local/bin/codex-restart.new && mv -f /usr/local/bin/codex-restart.new /usr/local/bin/codex-restart
    # As ubuntu, from a directory it can read: from elsewhere the installer dies in `find` after
    # creating releases/ but before current/ (codex-remote-control.tf).
    before=$(runuser -u ubuntu -- /home/ubuntu/.local/bin/codex --version 2>/dev/null | head -1)
    runuser -u ubuntu -- env HOME=/home/ubuntu PATH=/usr/local/bin:/usr/bin:/bin \
      sh -c 'cd /home/ubuntu && curl -fsSL https://chatgpt.com/codex/install.sh | sh' >/dev/null 2>&1 \
      || { echo "codex update FAILED (installer)"; exit 1; }
    after=$(runuser -u ubuntu -- /home/ubuntu/.local/bin/codex --version 2>/dev/null | head -1)
    echo "codex: $${before:-none} -> $${after:-none}. A running daemon keeps its old binary until: codex-restart --restart"
  CMD
}

resource "aws_ssm_association" "codex_update" {
  count = length(local.codex_update_instances) > 0 ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "codex-update"
  schedule_expression = "cron(0 9 ? * MON *)"
  max_concurrency     = "1"
  max_errors          = "1"

  targets {
    key    = "InstanceIds"
    values = local.codex_update_instances
  }

  parameters = {
    commands         = local.codex_update_commands
    executionTimeout = "900"
  }
}

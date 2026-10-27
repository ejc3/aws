# claude-update.tf
#
# Keeps the native Claude Code (~/.local/bin/claude) current, every day, on the four boxes that run it as `ubuntu`: both
# jumpboxes, fcvm-metal-arm and fcvm-metal-x86.
#
# WHY. Nothing updated it. The boot scripts run the installer only when Claude is MISSING, and Claude's own background
# updater does not run in pool sessions: `claw-master connect`, which t-claude launches every pool session through, sets
# DISABLE_AUTOUPDATER=1 for the Claude it starts. On 2026-10-27 fcvm-metal-arm was on 2.1.296 while 2.1.311 was out. The
# nextjs-ct containers are outside this association: their own nightly agent-update (scripts/nextjs-ct/ct/agent-update)
# covers their accounts.
#
# HOW. The pattern of codex-update.tf: an SSM State Manager association (Run Command on a schedule, no key on disk, no
# service on the box, the permitted admin -> box direction), daily at 08:00 UTC. `claude update` runs as ubuntu, never as
# root, from ubuntu's home with stdin closed and DISABLE_AUTOUPDATER removed, and writes only under ~/.local. A box with
# no native copy is reported and left alone (installing is the boot script's job).
#
# WHAT IT DELIBERATELY DOES NOT DO: restart anything. A running session keeps the binary it started with; the next session
# (and each t-claude relaunch) starts the new one. A box that is stopped at 08:00 catches up the next day.

locals {
  claude_update_instances = concat(
    var.enable_jumpbox ? [aws_instance.jumpbox[0].id] : [],
    var.enable_jumpbox_2 ? [aws_instance.jumpbox_2[0].id] : [],
    var.enable_firecracker_instance ? [aws_instance.firecracker_dev[0].id] : [],
    var.enable_x86_dev_instance ? [aws_instance.x86_dev[0].id] : [],
  )

  claude_update_commands = <<-CMD
    set -u
    before=$(runuser -u ubuntu -- /home/ubuntu/.local/bin/claude --version 2>/dev/null | head -1)
    if [ -z "$before" ]; then echo "claude: no native install at /home/ubuntu/.local/bin/claude; nothing to update"; exit 0; fi
    runuser -u ubuntu -- env -u DISABLE_AUTOUPDATER HOME=/home/ubuntu PATH=/home/ubuntu/.local/bin:/usr/local/bin:/usr/bin:/bin \
      sh -c 'cd /home/ubuntu && /home/ubuntu/.local/bin/claude update < /dev/null' >/dev/null 2>&1 \
      || { echo "claude update FAILED (was $before)"; exit 1; }
    after=$(runuser -u ubuntu -- /home/ubuntu/.local/bin/claude --version 2>/dev/null | head -1)
    echo "claude: $before -> $${after:-none}. Running sessions keep their binary until they restart."
  CMD
}

resource "aws_ssm_association" "claude_update" {
  count = length(local.claude_update_instances) > 0 ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "claude-update"
  schedule_expression = "cron(0 8 * * ? *)"
  max_concurrency     = "2"
  max_errors          = "1"

  targets {
    key    = "InstanceIds"
    values = local.claude_update_instances
  }

  parameters = {
    commands         = local.claude_update_commands
    executionTimeout = "600"
  }
}

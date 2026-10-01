# agent-session-sync.tf
#
# A cheap 5-second watch that makes a NEW repository appear in Claude Code and Codex within seconds,
# on every box that keeps remote-control sessions: both metal boxes, nextjs-dev's accounts and both
# jumpboxes. Before this, the metal boxes looked once at boot (fcvm-claude-rc), nextjs-dev started only
# each user's one working folder every 5 minutes (agents-enable), and the jumpboxes looked nowhere, so
# a repo cloned today waited for a reboot or a person. See scripts/agent-session-sync.py for what
# counts as new: a MAIN checkout only (a linked worktree never does), a finished clone, an allowed
# owner, and nothing that existed when the watcher first looked.
#
# ONE script and ONE template unit, installed by the same snippet everywhere, so the boxes cannot
# drift apart the way three bespoke launchers did. Per-box policy is a drop-in setting ASS_ARGS.
locals {
  agent_session_sync_install = <<-INSTALL
# Replaced atomically: a running watcher re-executes itself when this file changes (it needs no restart),
# and must never find it half written.
cat > /usr/local/bin/agent-session-sync.new <<'AGENTSYNC'
${file("${path.module}/scripts/agent-session-sync.py")}
AGENTSYNC
chmod 755 /usr/local/bin/agent-session-sync.new
mv -f /usr/local/bin/agent-session-sync.new /usr/local/bin/agent-session-sync
cat > /etc/systemd/system/agent-session-sync@.service <<'AGENTSYNCUNIT'
${file("${path.module}/scripts/agent-session-sync@.service")}
AGENTSYNCUNIT
systemctl daemon-reload
INSTALL

  # What the ubuntu account is allowed to start sessions for on the metal boxes and the jumpboxes:
  # ejc3's own repositories (its gh login is added automatically) and ONE collaboration repository by
  # exact name, never an organisation wildcard (the same gate fcvm-claude-rc uses at boot).
  agent_session_sync_ubuntu_args = "--owner ejc3 --repo dolphin-labs-hq/dolphin-labs"

  agent_session_sync_enable_ubuntu = <<-ENABLE
install -d /etc/systemd/system/agent-session-sync@ubuntu.service.d
cat > /etc/systemd/system/agent-session-sync@ubuntu.service.d/policy.conf <<'POLICY'
[Service]
Environment="ASS_ARGS=${local.agent_session_sync_ubuntu_args}"
POLICY
systemctl daemon-reload
systemctl enable agent-session-sync@ubuntu.service >/dev/null 2>&1 || true
# Starting a stopped watcher is safe; a running one is left alone so a re-run never resets it.
systemctl is-active --quiet agent-session-sync@ubuntu.service || systemctl start agent-session-sync@ubuntu.service || true
ENABLE
}

# The jumpboxes ignore user_data changes and run no downloaded script as root, so a running one gets
# the watcher through this explicit step (SSM Run Command, from either admin box: the permitted
# admin -> admin direction), re-run only when the script, the unit, the policy or the instance changes.
resource "terraform_data" "admin_agent_session_sync" {
  for_each = local.admin_tmux_boxes

  triggers_replace = {
    instance = each.value
    script   = filesha256("${path.module}/scripts/agent-session-sync.py")
    seeder   = filesha256("${path.module}/scripts/codex-seed-thread.py")
    unit     = filesha256("${path.module}/scripts/agent-session-sync@.service")
    helper   = filesha256("${path.module}/scripts/ssm-agent-session-sync.sh")
    args     = local.agent_session_sync_ubuntu_args
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/scripts/ssm-agent-session-sync.sh ${each.value} ${var.aws_region}"
    environment = {
      ASS_ARGS = local.agent_session_sync_ubuntu_args
    }
  }
}

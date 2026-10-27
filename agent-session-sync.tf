# agent-session-sync.tf
#
# A cheap 5-second watch that makes a NEW repository appear in Claude Code and Codex within seconds,
# on every box that keeps remote-control sessions: both metal boxes, nextjs-dev's accounts and both
# jumpboxes. Before this, the metal boxes looked once at boot (fcvm-claude-rc), nextjs-dev started only
# each user's one working folder every 5 minutes (agents-enable), and the jumpboxes looked nowhere, so
# a repo cloned today waited for a reboot or a person. See scripts/agent-session-sync.py for what
# counts as new: any checkout on dev boxes, or a checkout MARKED for interactive use on admin jumpboxes;
# worktrees always require that marker (claude-interactive in the git dir). A finished clone, an allowed
# owner, and nothing that existed when the watcher first looked. After a reboot
# it opens the folders scripts/agent-active-dirs.py names (prompted or marked in the last 30 days).
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
# The one rule for which folders get a session after a boot (prompted or marked in 30 days); the boot
# launchers and the watcher all call it.
cat > /usr/local/bin/agent-active-dirs.new <<'AGENTACTIVE'
${file("${path.module}/scripts/agent-active-dirs.py")}
AGENTACTIVE
chmod 755 /usr/local/bin/agent-active-dirs.new
mv -f /usr/local/bin/agent-active-dirs.new /usr/local/bin/agent-active-dirs
cat > /etc/systemd/system/agent-session-sync@.service <<'AGENTSYNCUNIT'
${file("${path.module}/scripts/agent-session-sync@.service")}
AGENTSYNCUNIT
systemctl daemon-reload
INSTALL

  # What the ubuntu account is allowed to start sessions for on the metal boxes and the jumpboxes:
  # ejc3's own repositories (its gh login is added automatically) and ONE collaboration repository by
  # exact name, never an organisation wildcard (the same gate fcvm-claude-rc uses at boot).
  agent_session_sync_ubuntu_args = "--owner ejc3 --repo dolphin-labs-hq/dolphin-labs"
  # An allowed origin does not approve its hooks running with jumpbox administrator credentials.
  agent_session_sync_admin_args = "${local.agent_session_sync_ubuntu_args} --require-clone-marker"

  # Fence an old watcher before installing code whose dev-box default admits unmarked clones. Stopping
  # this unit leaves interactive agents and tmux alive (KillMode=process). A failed stop must stop setup.
  agent_session_sync_prepare_admin = <<-PREPARE
policy=/etc/systemd/system/agent-session-sync@ubuntu.service.d/policy.conf
install -d /etc/systemd/system/agent-session-sync@ubuntu.service.d || exit 1
printf '[Service]\nEnvironment="ASS_ARGS=%s"\n' '${local.agent_session_sync_admin_args}' > "$policy.new" || exit 1
chmod 644 "$policy.new" || exit 1
if ! cmp -s "$policy.new" "$policy"; then
  if [ "$(systemctl show agent-session-sync@ubuntu.service -p LoadState --value 2>/dev/null)" != not-found ]; then
    systemctl stop agent-session-sync@ubuntu.service || { rm -f "$policy.new"; exit 1; }
  fi
  mv -f "$policy.new" "$policy" || exit 1
  systemctl daemon-reload || exit 1
else
  rm -f "$policy.new"
fi
PREPARE

  agent_session_sync_enable_ubuntu = <<-ENABLE
install -d /etc/systemd/system/agent-session-sync@ubuntu.service.d || exit 1
cat > /etc/systemd/system/agent-session-sync@ubuntu.service.d/policy.conf.new <<'POLICY' || exit 1
[Service]
Environment="ASS_ARGS=${local.agent_session_sync_ubuntu_args}"
POLICY
chmod 644 /etc/systemd/system/agent-session-sync@ubuntu.service.d/policy.conf.new \
  && mv -f /etc/systemd/system/agent-session-sync@ubuntu.service.d/policy.conf.new /etc/systemd/system/agent-session-sync@ubuntu.service.d/policy.conf || exit 1
systemctl daemon-reload
systemctl enable agent-session-sync@ubuntu.service >/dev/null 2>&1 || true
# Starting a stopped watcher is safe; a running one is left alone so a re-run never resets it.
systemctl is-active --quiet agent-session-sync@ubuntu.service || systemctl start agent-session-sync@ubuntu.service || true
ENABLE

  agent_session_sync_enable_admin = replace(
    local.agent_session_sync_enable_ubuntu,
    local.agent_session_sync_ubuntu_args,
    local.agent_session_sync_admin_args,
  )
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
    active   = filesha256("${path.module}/scripts/agent-active-dirs.py")
    unit     = filesha256("${path.module}/scripts/agent-session-sync@.service")
    helper   = filesha256("${path.module}/scripts/ssm-agent-session-sync.sh")
    args     = local.agent_session_sync_admin_args
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/scripts/ssm-agent-session-sync.sh ${each.value} ${var.aws_region}"
    environment = {
      ASS_ARGS = local.agent_session_sync_admin_args
    }
  }
}

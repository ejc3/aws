# claw-master-client.tf
#
# The CLIENT side of the shared claw-master server on fcvm-metal-arm and the main jumpbox: the pinned `claw-master` binary and the
# routing that makes t-claude send inference through the pool. (nextjs-dev gets the same two things from its own
# setup script, nextjs-user-data.tf; the server is claw-master-server.tf.) The pin is the server's: one tag and one
# sha256 (local.claw_master_tag / claw_master_sha256_aarch64), so client and server are always the same build.
#
# HOW. A State Manager association (the pattern of codex-update.tf): Run Command as root on a schedule, no key on disk,
# no service on the box, the permitted admin -> box direction. It is idempotent: a box already on the pinned build and
# with the block in place changes nothing. Applying it creates the association, which runs once at once.
#
# WHAT IT DOES NOT DO. It restarts nothing and ends no session. The ubuntu account's sessions share ONE tmux server
# (CLAUDE.md, Metal Claude startup); restarting it kills every one of them. A session already running keeps plain claude;
# a window launched afterwards (a new t-claude, the boot launcher after the next reboot) goes through the pool, and the
# window saves the server so a relaunch (/clear, /cd) stays there.
#
# THE SWITCH IS THE CERTIFICATE. The block exports TCLAUDE_INFERENCE_SERVER only when the client binary is installed AND the
# account's own client certificate is readable, so nothing is routed until `scripts/claw-master-enroll.sh fcvm-arm --ssh HOST` has run. t-claude reads it at
# launch (`--inference-server`, ejc3/t-claude) and runs `claw-master connect` in place of plain claude. It also exports
# TCLAUDE_CLAUDE_MASTER=/usr/local/bin/claude-master: this box has a personal build in ~/.local/bin ahead of the shared one on
# PATH (an older one that refuses the t-claude flags), and the route must use the pinned build, not whichever comes first.
#
# THE OLD NAME IS KEPT ON PURPOSE. Since the rename (2026-10-11) the binary is /usr/local/bin/claw-master, and
# /usr/local/bin/claude-master is a link to it. TCLAUDE_CLAUDE_MASTER names the LINK: t-claude decides whether a window's
# agent is still running by the process name, and counts a process whose name starts with "claude" (so claw-master
# matches, claw-master would not). Through the link the process keeps the name claw-master; pointed at claw-master, a
# running pool session would look dead and t-claude would type a second launch into it. Switch it once t-claude knows the name.
# The client directory stays ~/.config/claude-master: t-claude passes it, and the certificate renewal works on it.

locals {
  # fcvm-metal-arm, and the main jumpbox (owner, 2026-10-18: "flip this machine over to the pool too"). Its sessions take the
  # pool only once its ubuntu account is enrolled (scripts/claw-master-enroll.sh jumpbox), as on fcvm.
  claw_master_client_instances = concat(
    var.enable_firecracker_instance ? [aws_instance.firecracker_dev[0].id] : [],
    var.enable_jumpbox ? [aws_instance.jumpbox[0].id] : [],
  )

  claw_master_client_commands = <<-CMD
    set -u
    if [ "$(uname -m)" != aarch64 ]; then echo "claw-master client: no pinned build for $(uname -m); nothing done"; exit 0; fi
    CM_TAG="${local.claw_master_tag}"
    CM_SHA="${local.claw_master_sha256_aarch64}"
    if [ "$(sha256sum /usr/local/bin/claw-master 2>/dev/null | cut -d' ' -f1)" = "$CM_SHA" ]; then
      echo "claw-master $CM_TAG already installed"
    else
      CMTMP=$(mktemp -d)
      # Every step is part of the condition: a failed install or rename (full or read-only disk) must fail the association,
      # not fall through to an echo that reports success.
      if curl -fsSL --retry 3 "https://github.com/ejc3/CLIProxyAPI/releases/download/$CM_TAG/claw-master-linux-arm64" -o "$CMTMP/claw-master" \
         && echo "$CM_SHA  $CMTMP/claw-master" | sha256sum -c --quiet - \
         && install -m 0755 "$CMTMP/claw-master" /usr/local/bin/claw-master.new \
         && mv -f /usr/local/bin/claw-master.new /usr/local/bin/claw-master; then
        echo "claw-master $CM_TAG installed"
      else
        echo "claw-master client FAILED: download, sha256 or install (kept any installed copy)"; rm -f /usr/local/bin/claw-master.new; rm -rf "$CMTMP"; exit 1
      fi
      rm -rf "$CMTMP"
    fi
    # The name before the rename stays as a link to the same binary (see TCLAUDE_CLAUDE_MASTER below). A running session keeps
    # the file it started from.
    if [ "$(readlink /usr/local/bin/claude-master)" != claw-master ]; then
      ln -sfn claw-master /usr/local/bin/claude-master.new && mv -Tf /usr/local/bin/claude-master.new /usr/local/bin/claude-master \
        || { echo "claw-master client FAILED: could not link /usr/local/bin/claude-master"; rm -f /usr/local/bin/claude-master.new; exit 1; }
    fi
    ZE=/etc/zsh/zshenv
    if [ -f "$ZE" ]; then
      # Also the block from before this file was renamed (managed by claude-master-client.tf).
      sed -i '/^# >>> claude-master (managed by cla[a-z]*-master-client.tf) >>>$/,/^# <<< claude-master <<<$/d' "$ZE"
      cat >> "$ZE" <<'CMZSHENV'
    # >>> claude-master (managed by claw-master-client.tf) >>>
    if [ -x /usr/local/bin/claude-master ] && [ -r "$HOME/.config/claude-master/client.pem" ]; then
      export TCLAUDE_INFERENCE_SERVER="${local.claw_master_server_ip}:${local.claw_master_server_port}"
      export TCLAUDE_CLAUDE_MASTER=/usr/local/bin/claude-master
    fi
    # <<< claude-master <<<
    CMZSHENV
    fi
  CMD
}

resource "aws_ssm_association" "claw_master_client" {
  count = length(local.claw_master_client_instances) > 0 ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "claude-master-client"
  schedule_expression = "rate(1 day)"
  max_concurrency     = "1"
  max_errors          = "1"

  targets {
    key    = "InstanceIds"
    values = local.claw_master_client_instances
  }

  parameters = {
    commands         = local.claw_master_client_commands
    executionTimeout = "300"
  }
}

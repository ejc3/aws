# claude-master-client.tf
#
# The CLIENT side of the shared claude-master server on fcvm-metal-arm: the pinned `claude-master` binary and the
# routing that makes t-claude send inference through the pool. (nextjs-dev gets the same two things from its own
# setup script, nextjs-user-data.tf; the server is claude-master-server.tf.) The pin is the server's: one tag and one
# sha256 (local.claude_master_tag / claude_master_sha256_aarch64), so client and server are always the same build.
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
# account's own client certificate is readable, so nothing is routed until `scripts/claude-master-enroll.sh fcvm-arm --ssh HOST` has run. t-claude reads it at
# launch (`--inference-server`, ejc3/t-claude) and runs `claude-master connect` in place of plain claude.

locals {
  claude_master_client_instances = var.enable_firecracker_instance ? [aws_instance.firecracker_dev[0].id] : []

  claude_master_client_commands = <<-CMD
    set -u
    if [ "$(uname -m)" != aarch64 ]; then echo "claude-master client: no pinned build for $(uname -m); nothing done"; exit 0; fi
    CM_TAG="${local.claude_master_tag}"
    CM_SHA="${local.claude_master_sha256_aarch64}"
    if [ "$(sha256sum /usr/local/bin/claude-master 2>/dev/null | cut -d' ' -f1)" = "$CM_SHA" ]; then
      echo "claude-master $CM_TAG already installed"
    else
      CMTMP=$(mktemp -d)
      # Every step is part of the condition: a failed install or rename (full or read-only disk) must fail the association,
      # not fall through to an echo that reports success.
      if curl -fsSL --retry 3 "https://github.com/ejc3/CLIProxyAPI/releases/download/$CM_TAG/claude-master-linux-arm64" -o "$CMTMP/claude-master" \
         && echo "$CM_SHA  $CMTMP/claude-master" | sha256sum -c --quiet - \
         && install -m 0755 "$CMTMP/claude-master" /usr/local/bin/claude-master.new \
         && mv -f /usr/local/bin/claude-master.new /usr/local/bin/claude-master; then
        echo "claude-master $CM_TAG installed"
      else
        echo "claude-master client FAILED: download, sha256 or install (kept any installed copy)"; rm -f /usr/local/bin/claude-master.new; rm -rf "$CMTMP"; exit 1
      fi
      rm -rf "$CMTMP"
    fi
    ZE=/etc/zsh/zshenv
    if [ -f "$ZE" ]; then
      sed -i '/^# >>> claude-master (managed by claude-master-client.tf) >>>$/,/^# <<< claude-master <<<$/d' "$ZE"
      cat >> "$ZE" <<'CMZSHENV'
    # >>> claude-master (managed by claude-master-client.tf) >>>
    if [ -x /usr/local/bin/claude-master ] && [ -r "$HOME/.config/claude-master/client.pem" ]; then
      export TCLAUDE_INFERENCE_SERVER="${local.claude_master_server_ip}:${local.claude_master_server_port}"
    fi
    # <<< claude-master <<<
    CMZSHENV
    fi
  CMD
}

resource "aws_ssm_association" "claude_master_client" {
  count = length(local.claude_master_client_instances) > 0 ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "claude-master-client"
  schedule_expression = "rate(1 day)"
  max_concurrency     = "1"
  max_errors          = "1"

  targets {
    key    = "InstanceIds"
    values = local.claude_master_client_instances
  }

  parameters = {
    commands         = local.claude_master_client_commands
    executionTimeout = "300"
  }
}

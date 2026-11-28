locals {
  # Keep the native installer and its Markdown identical on Linux and the Macs.
  antigravity_setup = <<-SETUP
    AGY_MODEL_GUIDE=$(cat <<'GUIDE'
    ${chomp(file("${path.module}/scripts/antigravity-models.md"))}
    GUIDE
    )
    ${file("${path.module}/scripts/antigravity-install.sh")}
  SETUP

  # These clients can start in a Git checkout with its own root. Install the guide
  # in their effective global instructions, retaining personal settings.
  antigravity_owner_models = <<-SETUP
    /usr/bin/perl - <<'OWNER_MODELS'
    ${file("${path.module}/scripts/antigravity-owner-models.pl")}
    OWNER_MODELS
  SETUP
}

# As on the Codex refresh, give each ubuntu host its own SSM delivery so a stopped
# metal box can catch up independently. agy handles updates in the user's home.
resource "aws_ssm_association" "antigravity_install" {
  for_each = local.codex_update_instances

  name                = "AWS-RunShellScript"
  association_name    = "antigravity-install-${each.key}"
  schedule_expression = "cron(0 10 ? * MON *)"

  targets {
    key    = "InstanceIds"
    values = [each.value]
  }

  parameters = {
    commands         = <<-CMD
      cd /home/ubuntu || exit 1
      exec runuser -u ubuntu -- env HOME=/home/ubuntu PATH=/usr/local/bin:/usr/bin:/bin /bin/bash -s -- ${startswith(each.key, "jumpbox") ? "--binary-only" : ""} <<'AGY_INSTALL'
      ${startswith(each.key, "jumpbox") ? file("${path.module}/scripts/antigravity-install.sh") : local.antigravity_setup}
      ${each.key == "fcvm-metal-x86" ? local.antigravity_owner_models : ""}
      AGY_INSTALL
    CMD
    executionTimeout = "600"
  }
}

# Each container has one interactive account. Enter it as that user, after its
# persistent home is mounted. A software install need not replay the host bootstrap.
resource "aws_ssm_association" "antigravity_nextjs" {
  count = var.enable_nextjs_ct && local.nextjs_ct_serving ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "antigravity-nextjs"
  schedule_expression = "rate(1 day)"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.nextjs_ct[0].id]
  }

  parameters = {
    commands         = <<-CMD
      failed=0
      for u in ejc3 colton connor colin; do
        /usr/bin/systemd-run --machine="$u" --uid="$u" --setenv="HOME=/home/$u" --pipe --wait --collect --quiet --service-type=exec \
          /bin/bash -c 'mountpoint -q "$HOME" && exec /bin/bash -s' <<'AGY_INSTALL' || failed=1
      ${local.antigravity_setup}
      AGY_INSTALL
      done
      exit "$failed"
    CMD
    executionTimeout = "1000"
  }
}

# Laptops can be asleep during a deploy. Reconcile through the ARM dev box's
# existing host-key-pinned SSH routes until each Mac has received the installer.
resource "aws_ssm_association" "antigravity_macs" {
  count = var.enable_firecracker_instance ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "antigravity-macs"
  schedule_expression = "rate(1 hour)"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.firecracker_dev[0].id]
  }

  parameters = {
    commands         = <<-CMD
      cd /home/ubuntu || exit 1
      exec runuser -u ubuntu -- env HOME=/home/ubuntu PATH=/usr/local/bin:/usr/bin:/bin /bin/bash -s <<'MACS'
      ${file("${path.module}/scripts/antigravity-tunnel-status.sh")}
      errors=$(mktemp) || exit 1
      trap 'rm -f -- "$errors"' EXIT
      failed=0
      for mac in mac air mini colton-mac colin-mac; do
        rc=0
        timeout 180 ssh -T -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=yes -o ForwardAgent=no "$mac" '/bin/bash -s || exit 42' 2>"$errors" <<'AGY_INSTALL' || rc=$?
      ${local.antigravity_setup}
      ${local.antigravity_owner_models}
      # mac-setup owns the renderer and template. Update the renderer on an already set-up
      # Mac as it returns online; the normal LaunchAgent also refreshes ~/AGENTS.md hourly.
      renderer=$HOME/.local/bin/mac-agents-md
      if [[ -x $renderer && -f $HOME/.local/share/mac-setup/AGENTS.template.md ]]; then
        update=$(mktemp "$HOME/.local/bin/.mac-agents-md.XXXXXXXX")
        cat > "$update" <<'RENDERER'
      ${chomp(file("${path.module}/scripts/mac/bin/mac-agents-md"))}
      RENDERER
        if cmp -s "$update" "$renderer"; then
          rm -f "$update"
        else
          chmod 755 "$update" && mv -f "$update" "$renderer" && "$renderer"
        fi
      fi
      AGY_INSTALL
        if [[ $rc == 0 ]]; then
          echo "$mac: installed"
        elif [[ $rc == 255 ]] && grep -Fxq 'websocket: bad handshake' "$errors" && antigravity_tunnel_down "$mac"; then
          echo "$mac: deferred (Cloudflare 1033, tunnel down)"
        else
          cat "$errors" >&2
          echo "$mac: install failed (exit $rc)" >&2
          failed=1
        fi
      done
      exit "$failed"
      MACS
    CMD
    executionTimeout = "1000"
  }

  depends_on = [aws_ssm_association.fcvm_mac_ssh_clients]
}

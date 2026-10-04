# tmux-scroll.tf
#
# ONE pin for the scroll-native tmux (`tmux-scroll`) and for t-claude, shared by every box.
#
# t-claude does not run plain `tmux`: it shims ~/.cache/t-claude/bin/tmux to the first of
# $TCLAUDE_TMUX, ~/.local/bin/tmux-scroll, /usr/local/bin/tmux-scroll that contains
# `scroll-replay`. So `tmux-scroll` is the binary every session actually runs, and a box that
# never gets a new one keeps whatever it first installed. Before this pin, three of the four
# running boxes had never moved: nextjs-dev installed only when the binary was missing, and the
# two jumpboxes had no Terraform path at all.
#
# The build is ejc3/tmux branch menu/reference @ a843d544 (tmux next-3.9), published as
# release tmux-scroll-a843d54 from a clean checkout and verified before publishing (tmux regress
# and render-parity suites, the memory gym on a sanitizer build, the real-terminal checks; all
# t-claude tests/*.zsh pass against it). aarch64 only for now:
# every running box is Ubuntu 24.04 aarch64. The x86 dev box keeps its current copy until an x86_64 asset is
# added to the release (the metal updater logs "no asset ... keeping current").
#
# Installing a new binary under a live tmux server is safe here: 3.7b, next-3.8 and next-3.9
# all speak client/server PROTOCOL_VERSION 8, and a running server keeps the binary it started
# with. Sessions move to the new build when their server restarts.
#
# To roll forward: publish a new release, change the tag and sha256 below, apply.
locals {
  tmux_scroll_tag            = "tmux-scroll-a843d54"
  tmux_scroll_sha256_aarch64 = "02d218b9a84cf79cc6bb91a604ccbe6604b5e84f79657431729d819b8257f272"

  # t-claude (github.com/ejc3/t-claude). The dev boxes and nextjs-dev follow its main branch
  # on every setup run by design (claude-remote-control.tf, dev-user-data.tf). The jumpboxes
  # take this exact commit, because a Terraform step can only re-run when its inputs change.
  tclaude_ref = "d0ee535af990e6304834690d66cd410e4ed03a00"

  admin_tmux_boxes = merge(
    var.enable_jumpbox ? { "jumpbox" = aws_instance.jumpbox[0].id } : {},
    var.enable_jumpbox_2 ? { "jumpbox-2" = aws_instance.jumpbox_2[0].id } : {},
  )
}

# The jumpboxes. Both ignore user_data changes and neither runs dev-selfupdate: an admin box
# must not host a service that re-runs a downloaded script as root (dev-selfupdate.tf). So this
# is a ONE-SHOT, run by whoever applies, that re-runs only when the pin, the installer or the
# instance changes.
#
# It goes through SSM Run Command rather than SSH: it works from either admin box, needs no
# key on disk, and it is the permitted admin -> admin direction. The installer runs as ubuntu,
# not root, and writes only ~/.local/bin/{tmux-scroll,nosync-wrap} and ~/.config/t-claude.zsh.
# ~/.local/bin is ahead of /usr/local/bin in PATH, and t-claude checks ~/.local/bin/tmux-scroll
# first, so the root-owned copies are left alone. The tmux tarball is checked against the
# sha256 above before anything is replaced.
resource "terraform_data" "admin_tmux_tclaude" {
  for_each = local.admin_tmux_boxes

  triggers_replace = {
    instance  = each.value
    tag       = local.tmux_scroll_tag
    sha256    = local.tmux_scroll_sha256_aarch64
    tclaude   = local.tclaude_ref
    installer = filesha256("${path.module}/scripts/admin-tmux-tclaude.sh")
    wrapper   = filesha256("${path.module}/scripts/ssm-admin-tmux-tclaude.sh")
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/scripts/ssm-admin-tmux-tclaude.sh ${each.value} ${var.aws_region}"
    environment = {
      TMUX_SCROLL_TAG    = local.tmux_scroll_tag
      TMUX_SCROLL_SHA256 = local.tmux_scroll_sha256_aarch64
      TCLAUDE_REF        = local.tclaude_ref
    }
  }
}

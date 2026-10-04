# user-agents.tf
#
# A starting set of user-level agent instructions for an account that has none, on every box
# whose setup script provisions user homes: both metal boxes, every account on nextjs-dev, and
# both admin boxes.
#
# THE TEXT lives in ONE place, scripts/user-agents.md. The boxes get that file verbatim and the
# docs point at it, so there is no second copy to drift.
#
# WHAT A BOX GETS. `user-agents-seed` (scripts/user-agents-seed.sh) creates, per account,
#   ~/.codex/AGENTS.md   the real file, which Codex reads
#   ~/.claude/CLAUDE.md  a symlink to it, which Claude Code reads
# and only when NEITHER path exists. An account that already has either one is left exactly as
# it is: never overwritten, appended to or re-linked. A re-run is therefore a no-op, and an
# edit made on the box afterwards is kept (the file is seeded once, not managed).
#
# HOW IT REACHES A BOX. It is part of each setup script, published to S3, so no instance is
# replaced or rebooted: every instance ignores user_data changes. A fresh box runs it at first
# boot. Running boxes take it the way they take any setup change: nextjs-dev through its
# setup-sync timer, the metal boxes through dev-selfupdate at their next boot, and an admin box
# only when its setup script is re-run by hand.
#
# The files travel base64-encoded (as pbox and codex-restart do), so neither Terraform nor the
# shell can interpolate anything inside them, and nothing is downloaded on the box.
locals {
  user_agents_seed_install = <<-INSTALL
install -d -m 755 /usr/local/share/user-agents
base64 -d > /usr/local/share/user-agents/AGENTS.md.new <<'USERAGENTSB64'
${base64encode(file("${path.module}/scripts/user-agents.md"))}
USERAGENTSB64
chmod 644 /usr/local/share/user-agents/AGENTS.md.new
mv -f /usr/local/share/user-agents/AGENTS.md.new /usr/local/share/user-agents/AGENTS.md
base64 -d > /usr/local/bin/user-agents-seed.new <<'USERAGENTSSEEDB64'
${base64encode(file("${path.module}/scripts/user-agents-seed.sh"))}
USERAGENTSSEEDB64
chmod 755 /usr/local/bin/user-agents-seed.new
mv -f /usr/local/bin/user-agents-seed.new /usr/local/bin/user-agents-seed
INSTALL

  # The boxes whose sessions all run as ubuntu: the metal boxes and the admin boxes. A failed
  # seed is only a warning: a box without the starter file is still a working box.
  user_agents_seed_ubuntu = <<-SEED
${local.user_agents_seed_install}
/usr/local/bin/user-agents-seed ubuntu || echo "WARNING: user-agents-seed failed for ubuntu"
SEED
}

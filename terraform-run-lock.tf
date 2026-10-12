# terraform-run-lock.tf
#
# One Terraform run at a time on each admin box (#408). scripts/terraform-run-lock.sh is installed AT
# /usr/local/bin/terraform (the path every shell already resolves; zsh caches command paths) with the real binary moved
# to /usr/local/lib/terraform-run-lock/terraform. init, plan, apply and the other state commands wait for one host lock
# (/var/lib/terraform-run-lock/lock) that the Stop hook shares; cheap commands pass straight through. Two plans at once
# ran the 8 GB jumpbox out of memory (2026-10-12).
#
# The jumpboxes ignore user_data changes, so the install is an SSM association (the permitted admin -> admin
# direction; its content is this repository's, nothing is fetched on the box). It runs when the wrapper or installer
# changes and daily, so an upgrade that puts a new real binary at /usr/local/bin/terraform is moved aside and the
# wrapper restored by the next run. A Terraform run already going keeps the file it started from: every replacement
# is a rename.
locals {
  terraform_run_lock_install = <<-EOT
    set -eu
    t=$(mktemp -d); trap 'rm -rf "$t"' EXIT
    cat > "$t/terraform" <<'TFWRAPPER'
    ${indent(4, chomp(file("${path.module}/scripts/terraform-run-lock.sh")))}
    TFWRAPPER
    cat > "$t/install.sh" <<'TFINSTALL'
    ${indent(4, chomp(file("${path.module}/scripts/terraform-run-lock-install.sh")))}
    TFINSTALL
    bash "$t/install.sh" "$t/terraform"
  EOT
}

resource "aws_ssm_association" "terraform_run_lock" {
  count = length(local.admin_tmux_boxes) > 0 ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "terraform-run-lock"
  schedule_expression = "rate(1 day)"
  max_concurrency     = "1"
  max_errors          = "1"

  targets {
    key    = "InstanceIds"
    values = values(local.admin_tmux_boxes)
  }

  parameters = {
    commands         = local.terraform_run_lock_install
    executionTimeout = "120"
  }
}

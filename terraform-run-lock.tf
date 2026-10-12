# terraform-run-lock.tf
#
# One Terraform run at a time on each admin box (#408). scripts/terraform-run-lock.sh is installed as
# /usr/local/sbin/terraform, ahead of the real /usr/local/bin/terraform on every PATH, and makes init, plan, apply and
# the other state commands wait for one host lock (/var/lib/terraform-run-lock/lock) that the Stop hook shares; cheap
# commands pass straight through. Two plans at once ran the 8 GB jumpbox out of memory (2026-10-12).
#
# The jumpboxes ignore user_data changes and run no downloaded script as root, so a running one gets the wrapper
# through this explicit SSM step (the permitted admin -> admin direction), re-run when the wrapper, the installer or
# the instance changes. The apply that runs it is itself under the old wrapper (or none, the first time); the new file
# replaces the old one atomically, so that run is unaffected.
resource "terraform_data" "admin_terraform_run_lock" {
  for_each = local.admin_tmux_boxes

  triggers_replace = {
    instance  = each.value
    wrapper   = filesha256("${path.module}/scripts/terraform-run-lock.sh")
    installer = filesha256("${path.module}/scripts/ssm-terraform-run-lock.sh")
  }

  provisioner "local-exec" {
    command = "bash ${path.module}/scripts/ssm-terraform-run-lock.sh ${each.value} ${var.aws_region}"
  }
}

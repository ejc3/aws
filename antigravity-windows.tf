# Colin's personal Windows account, reached from fcvm-arm through its existing pinned colin-pc SSH alias.
# A PC can be asleep at deploy time. Reapply the same per-user bootstrap when it is next reachable.
resource "aws_ssm_association" "antigravity_colin_pc" {
  count = var.enable_firecracker_instance ? 1 : 0

  name                = "AWS-RunShellScript"
  association_name    = "antigravity-colin-pc"
  schedule_expression = "rate(1 hour)"

  targets {
    key    = "InstanceIds"
    values = [aws_instance.firecracker_dev[0].id]
  }

  parameters = {
    executionTimeout = "600"
    commands         = <<-CMD
      cd /home/ubuntu || exit 1
      exec runuser -u ubuntu -- env HOME=/home/ubuntu PATH=/usr/local/bin:/usr/bin:/bin /bin/bash -s -- \
        '${textencodebase64(file("${path.module}/scripts/windows/antigravity-stdin.ps1"), "UTF-16LE")}' \
        '${filebase64("${path.module}/scripts/windows/antigravity-install.ps1")}' \
        '${filebase64("${path.module}/scripts/antigravity-models.md")}' <<'RECONCILE'
      ${chomp(file("${path.module}/scripts/antigravity-tunnel-status.sh"))}
      ${chomp(file("${path.module}/scripts/windows/antigravity-reconcile.sh"))}
      RECONCILE
    CMD
  }

  depends_on = [aws_ssm_association.fcvm_mac_ssh_clients]
}

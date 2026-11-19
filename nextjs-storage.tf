# Run after the EBS resize, and follow a replacement instance through its bootstrap.
# Changing the shared setup script would also restart unrelated live services.
resource "aws_ssm_association" "nextjs_storage" {
  count = var.enable_nextjs_ct && local.nextjs_ct_serving ? 1 : 0

  name                             = "AWS-RunShellScript"
  association_name                 = "nextjs-root-and-swap"
  schedule_expression              = "rate(1 day)"
  wait_for_success_timeout_seconds = 2400

  targets {
    key    = "InstanceIds"
    values = [aws_instance.nextjs_ct[0].id]
  }

  parameters = {
    commands         = <<-SH
      exec /bin/bash -s -- '${aws_instance.nextjs_ct[0].root_block_device[0].volume_id}' '${var.nextjs_ct_volume_size}' <<'STORAGE'
      ${file("${path.module}/scripts/nextjs-root-and-swap.sh")}
      STORAGE
    SH
    executionTimeout = "2100"
  }
}

# Temporary, non-credential acceptance fixtures restored from reviewed PR #69.
# Source preparation is not a deployment gate: first prove a real broker job's
# success, credential deletion before job startup, and automatic host termination.
# Revalidated 2026-09-12: the pinned Amazon ARM64 image remains available in west1.
# These deliberately do not use Role=github-runner: the autoscaler ignores them.
# Remove through a reviewed Terraform apply after the before/after IAM checks.
# RemoveAfter is an operator reminder, NOT an automatic expiry policy.
data "aws_ami" "runner_iam_canary" {
  count  = var.enable_github_runner ? 1 : 0
  owners = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.12.20260831.0-kernel-6.18-arm64"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

resource "aws_instance" "runner_iam_canary" {
  for_each = toset(var.enable_github_runner ? ["first", "second"] : [])

  ami                         = data.aws_ami.runner_iam_canary[0].id
  instance_type               = "t4g.micro"
  subnet_id                   = aws_subnet.runner[0].id
  vpc_security_group_ids      = [aws_security_group.runner[0].id]
  iam_instance_profile        = aws_iam_instance_profile.runner[0].name
  key_name                    = "fcvm-ec2"
  associate_public_ip_address = true

  # AL2023 includes AWS CLI v2. No bootstrap user data, repo checkout, registration
  # token, personal login, or CI job is installed on either test machine.
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 8
    encrypted             = true
    delete_on_termination = true
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  credit_specification {
    cpu_credits = "standard"
  }

  tags = {
    Name        = "runner-iam-canary-${each.key}"
    Role        = "runner-iam-canary"
    Purpose     = "runner-bootstrap-iam-canary"
    RemoveAfter = "2026-09-13"
  }

  depends_on = [aws_iam_role_policy.runner_bootstrap]
}

resource "aws_ssm_parameter" "runner_iam_canary" {
  for_each = aws_instance.runner_iam_canary

  name  = "/github-runner/bootstrap/security-canary-20260912-${each.key}"
  type  = "SecureString"
  tier  = "Standard"
  value = "security-canary-not-a-credential"
  tags = {
    InstanceArn = each.value.arn
    Purpose     = "runner-bootstrap-iam-canary"
    RemoveAfter = "2026-09-13"
  }
}

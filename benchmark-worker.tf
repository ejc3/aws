# One ephemeral Ohio benchmark host; the ENI and its two fixed egress addresses outlive
# its instances. Only the primary ARM operator can request a launch or inject an SSH key.
locals {
  benchmark_worker_enabled      = local.boot_race_driver_enabled
  benchmark_worker_name         = "benchmark-worker"
  benchmark_worker_vpc_id       = aws_vpc.ohio_pbox.id
  benchmark_worker_private_ipv4 = local.benchmark_worker_enabled ? aws_network_interface.benchmark_worker[0].private_ip : null
  benchmark_worker_ipv6_cidr    = local.benchmark_worker_enabled ? "${one(aws_network_interface.benchmark_worker[0].ipv6_addresses)}/128" : null
  benchmark_worker_public_ipv4  = local.benchmark_worker_enabled ? aws_eip.benchmark_worker[0].public_ip : null
  benchmark_worker_ec2          = "arn:aws:ec2:${local.boot_race_region}:${local.boot_race_account}"
  benchmark_worker_tags         = merge(local.boot_race_tag, { Name = local.benchmark_worker_name })
}

resource "aws_security_group" "benchmark_worker" {
  count       = local.benchmark_worker_enabled ? 1 : 0
  provider    = aws.ohio
  name        = local.benchmark_worker_name
  description = "Benchmark worker: SSH from the primary ARM management address only"
  vpc_id      = local.benchmark_worker_vpc_id

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["${aws_eip.firecracker_dev_next[0].public_ip}/32"]
  }

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = local.benchmark_worker_tags
}

resource "aws_network_interface" "benchmark_worker" {
  count              = local.benchmark_worker_enabled ? 1 : 0
  provider           = aws.ohio
  subnet_id          = aws_subnet.ohio_pbox.id
  security_groups    = [aws_security_group.benchmark_worker[0].id]
  ipv6_address_count = 1
  tags               = local.benchmark_worker_tags
}

resource "aws_eip" "benchmark_worker" {
  count             = local.benchmark_worker_enabled ? 1 : 0
  provider          = aws.ohio
  domain            = "vpc"
  network_interface = aws_network_interface.benchmark_worker[0].id
  tags              = local.benchmark_worker_tags
}

resource "aws_iam_role" "benchmark_worker" {
  count = local.benchmark_worker_enabled ? 1 : 0
  name  = local.benchmark_worker_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.benchmark_worker_tags
}

resource "aws_iam_instance_profile" "benchmark_worker" {
  count = local.benchmark_worker_enabled ? 1 : 0
  name  = local.benchmark_worker_name
  role  = aws_iam_role.benchmark_worker[0].name
}

resource "aws_launch_template" "benchmark_worker" {
  count         = local.benchmark_worker_enabled ? 1 : 0
  provider      = aws.ohio
  name          = local.benchmark_worker_name
  image_id      = var.parallel_box_ami_ohio
  instance_type = "c8gd.metal-24xl"

  # No static SSH key. Canonical Ubuntu already has EC2 Instance Connect, and
  # user data neither copies the dev-hop key nor publishes any private material.
  user_data = base64encode(<<-SH
    #!/bin/bash
    set -eu
    ${local.linux_ip_options_bootstrap}
    # Signature for the ARM client's strict SSH check. This is a public host key,
    # read through the authenticated EC2 console API, never trust-on-first-use.
    printf 'BENCHMARK_HOST_KEY %s\n' "$(awk '{print $1, $2}' /etc/ssh/ssh_host_ed25519_key.pub)" >/dev/console
    export DEBIAN_FRONTEND=noninteractive
    apt-get -o DPkg::Lock::Timeout=300 update
    ${local.pbox_local_nvme_setup}
    findmnt --mountpoint /mnt/nvme >/dev/null
    test -c /dev/kvm
    usermod -aG kvm ubuntu
    snap install aws-cli --classic
    install -m 0644 /dev/null /etc/fcvm-benchmark-worker
  SH
  )

  iam_instance_profile {
    name = aws_iam_instance_profile.benchmark_worker[0].name
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  instance_market_options {
    market_type = "spot"
    spot_options {
      spot_instance_type             = "one-time"
      instance_interruption_behavior = "terminate"
    }
  }

  network_interfaces {
    device_index          = 0
    network_interface_id  = aws_network_interface.benchmark_worker[0].id
    delete_on_termination = false
  }

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = 30
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  tag_specifications {
    resource_type = "instance"
    # The worker may assume the boot-race driver, but that role must not
    # be able to stop or image the worker via its Project=boot-race grants.
    tags = { Name = local.benchmark_worker_name, Project = "benchmark-worker" }
  }

  tag_specifications {
    resource_type = "volume"
    tags          = { Name = local.benchmark_worker_name, Project = "benchmark-worker" }
  }

  tags = local.benchmark_worker_tags
}

data "archive_file" "benchmark_worker" {
  type        = "zip"
  source_file = "${path.module}/benchmark-worker/control.py"
  output_path = "${path.module}/.terraform/benchmark-worker.zip"
}

resource "aws_cloudwatch_log_group" "benchmark_worker" {
  count             = local.benchmark_worker_enabled ? 1 : 0
  provider          = aws.ohio
  name              = "/aws/lambda/${local.benchmark_worker_name}"
  retention_in_days = 7
  tags              = local.benchmark_worker_tags
}

resource "aws_iam_role" "benchmark_worker_control" {
  count = local.benchmark_worker_enabled ? 1 : 0
  name  = "${local.benchmark_worker_name}-control"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.benchmark_worker_tags
}

resource "aws_iam_role_policy" "benchmark_worker_control" {
  count = local.benchmark_worker_enabled ? 1 : 0
  name  = local.benchmark_worker_name
  role  = aws_iam_role.benchmark_worker_control[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeNetworkInterfaces", "ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = "ec2:RunInstances"
        Resource = [
          "${local.benchmark_worker_ec2}:instance/*",
          "${local.benchmark_worker_ec2}:volume/*",
        ]
        Condition = {
          ArnEquals    = { "ec2:LaunchTemplate" = aws_launch_template.benchmark_worker[0].arn }
          StringEquals = { "aws:RequestTag/Name" = local.benchmark_worker_name }
        }
      },
      {
        Effect = "Allow"
        Action = "ec2:RunInstances"
        Resource = [
          aws_launch_template.benchmark_worker[0].arn,
          aws_network_interface.benchmark_worker[0].arn,
          aws_subnet.ohio_pbox.arn,
          aws_security_group.benchmark_worker[0].arn,
          "arn:aws:ec2:${local.boot_race_region}::image/${var.parallel_box_ami_ohio}",
        ]
        Condition = { ArnEquals = { "ec2:LaunchTemplate" = aws_launch_template.benchmark_worker[0].arn } }
      },
      {
        Effect    = "Allow"
        Action    = "ec2:CreateTags"
        Resource  = ["${local.benchmark_worker_ec2}:instance/*", "${local.benchmark_worker_ec2}:volume/*"]
        Condition = { StringEquals = { "ec2:CreateAction" = "RunInstances", "aws:RequestTag/Name" = local.benchmark_worker_name } }
      },
      {
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.benchmark_worker[0].arn
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
      {
        Effect    = "Allow"
        Action    = "ec2:TerminateInstances"
        Resource  = "${local.benchmark_worker_ec2}:instance/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Name" = local.benchmark_worker_name, "ec2:ResourceTag/Project" = "benchmark-worker" } }
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.benchmark_worker[0].arn}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "benchmark_worker" {
  count                          = local.benchmark_worker_enabled ? 1 : 0
  provider                       = aws.ohio
  function_name                  = local.benchmark_worker_name
  role                           = aws_iam_role.benchmark_worker_control[0].arn
  handler                        = "control.lambda_handler"
  runtime                        = "python3.12"
  filename                       = data.archive_file.benchmark_worker.output_path
  source_code_hash               = data.archive_file.benchmark_worker.output_base64sha256
  reserved_concurrent_executions = 1
  timeout                        = 30

  environment {
    variables = {
      ENI         = aws_network_interface.benchmark_worker[0].id
      TEMPLATE    = aws_launch_template.benchmark_worker[0].id
      VERSION     = tostring(aws_launch_template.benchmark_worker[0].latest_version)
      PUBLIC_IPV4 = local.benchmark_worker_public_ipv4
    }
  }

  tags       = local.benchmark_worker_tags
  depends_on = [aws_iam_role_policy.benchmark_worker_control]
}

resource "aws_cloudwatch_event_rule" "benchmark_worker_reaper" {
  count               = local.benchmark_worker_enabled ? 1 : 0
  provider            = aws.ohio
  name                = "benchmark-worker-reaper"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_lambda_permission" "benchmark_worker_reaper" {
  count         = local.benchmark_worker_enabled ? 1 : 0
  provider      = aws.ohio
  statement_id  = "BenchmarkWorkerReaper"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.benchmark_worker[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.benchmark_worker_reaper[0].arn
}

resource "aws_cloudwatch_event_target" "benchmark_worker_reaper" {
  count     = local.benchmark_worker_enabled ? 1 : 0
  provider  = aws.ohio
  rule      = aws_cloudwatch_event_rule.benchmark_worker_reaper[0].name
  target_id = "RetireBenchmarkWorker"
  arn       = aws_lambda_function.benchmark_worker[0].arn
  input     = jsonencode({ action = "reap" })

  retry_policy {
    maximum_retry_attempts       = 0
    maximum_event_age_in_seconds = 300
  }

  depends_on = [aws_lambda_permission.benchmark_worker_reaper]
}

resource "aws_iam_role" "benchmark_worker_operator" {
  count = local.benchmark_worker_enabled ? 1 : 0
  name  = "${local.benchmark_worker_name}-operator"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.dev_server.arn }
      Action    = "sts:AssumeRole"
      Condition = { ArnEquals = { "ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn } }
    }]
  })
  tags = local.benchmark_worker_tags
}

resource "aws_iam_role_policy" "benchmark_worker_operator" {
  count = local.benchmark_worker_enabled ? 1 : 0
  name  = local.benchmark_worker_name
  role  = aws_iam_role.benchmark_worker_operator[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.benchmark_worker[0].arn
      },
      {
        Effect    = "Allow"
        Action    = "ec2:GetConsoleOutput"
        Resource  = "${local.benchmark_worker_ec2}:instance/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Name" = local.benchmark_worker_name, "ec2:ResourceTag/Project" = "benchmark-worker" } }
      },
      {
        Effect   = "Allow"
        Action   = "ec2-instance-connect:SendSSHPublicKey"
        Resource = "${local.benchmark_worker_ec2}:instance/*"
        Condition = {
          StringEquals = {
            "ec2:osuser"              = "ubuntu"
            "aws:ResourceTag/Name"    = local.benchmark_worker_name
            "aws:ResourceTag/Project" = "benchmark-worker"
          }
        }
      },
    ]
  })
  # Give operators a launch button only after the automatic expiration is set.
  depends_on = [aws_cloudwatch_event_target.benchmark_worker_reaper]
}

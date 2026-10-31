# One shared, ephemeral M4 Pro build satellite in Oregon. Terraform provisions only
# the launch recipe; a reviewed admin-only macpbox up starts a separately approved
# 24-hour Dedicated Host lease. Ordinary applies never allocate or replace a Mac.
locals {
  macpbox_region = "us-west-2"
  macpbox_type   = "mac-m4pro.metal"
  macpbox_az     = "us-west-2b"
}

data "aws_vpc" "macpbox" {
  provider = aws.west2
  default  = true
}

data "aws_subnets" "macpbox" {
  provider = aws.west2
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.macpbox.id]
  }
  filter {
    name   = "availability-zone"
    values = [local.macpbox_az]
  }
}

# AWS stable macOS Golden Gate 27.0, release 2026.09.23 (CLT 27.0 included).
# Pin the reviewed image rather than silently moving a paid launch to a new OS.
data "aws_ami" "macpbox" {
  provider = aws.west2
  owners   = ["amazon"]
  filter {
    name   = "image-id"
    values = ["ami-0f7e2f24d6cd04436"]
  }
  filter {
    name   = "architecture"
    values = ["arm64_mac"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

resource "aws_key_pair" "macpbox" {
  provider   = aws.west2
  key_name   = "macpbox"
  public_key = aws_key_pair.parallel_box.public_key
  tags       = { Name = "macpbox", Purpose = "macpbox" }
}

resource "aws_security_group" "macpbox" {
  provider    = aws.west2
  name_prefix = "macpbox-"
  description = "Mac build satellite: SSH only; no public GUI or build server"
  vpc_id      = data.aws_vpc.macpbox.id

  ingress {
    description = "Public-key SSH for approved admin and dev-hop clients"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Name = "macpbox", Purpose = "macpbox" }
}

resource "aws_launch_template" "macpbox" {
  provider      = aws.west2
  name          = "macpbox"
  image_id      = data.aws_ami.macpbox.id
  instance_type = local.macpbox_type
  key_name      = aws_key_pair.macpbox.key_name

  iam_instance_profile {
    name = aws_iam_instance_profile.mac_instance.name
  }
  placement {
    tenancy = "host"
  }
  network_interfaces {
    device_index                = 0
    subnet_id                   = sort(data.aws_subnets.macpbox.ids)[0]
    security_groups             = [aws_security_group.macpbox.id]
    associate_public_ip_address = true
    delete_on_termination       = true
  }
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }
  block_device_mappings {
    device_name = data.aws_ami.macpbox.root_device_name
    ebs {
      volume_type           = "gp3"
      volume_size           = 200
      iops                  = 10000
      throughput            = 400
      encrypted             = true
      delete_on_termination = true
    }
  }

  # Only PUBLIC keys enter the recipe. The Mac receives no jumpbox/dev-hop private
  # key, personal login, VNC password, state credential, or administrative policy.
  user_data = base64encode(<<-MACINIT
#!/bin/bash
set -euo pipefail
umask 077
exec > /var/log/macpbox-bootstrap.log 2>&1
install -d -m 700 -o ec2-user -g staff /Users/ec2-user/.ssh
touch /Users/ec2-user/.ssh/authorized_keys
grep -qxF '${trimspace(tls_private_key.dev_hop.public_key_openssh)}' /Users/ec2-user/.ssh/authorized_keys || \
  printf '%s\n' '${trimspace(tls_private_key.dev_hop.public_key_openssh)}' >> /Users/ec2-user/.ssh/authorized_keys
chown ec2-user:staff /Users/ec2-user/.ssh/authorized_keys
chmod 600 /Users/ec2-user/.ssh/authorized_keys
install -d -m 700 -o ec2-user -g staff /Users/ec2-user/work
# AWS ships CLT/Homebrew. Do not open an interactive installer, change the selected
# Xcode, seed authentication, or guess which disk is safe to erase during bootstrap.
/usr/bin/xcode-select -p
/usr/bin/xcrun --find clang
/usr/bin/xcrun --find swift
/usr/bin/touch /var/db/macpbox-ready
MACINIT
  )

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "macpbox", Purpose = "macpbox" }
  }
  tag_specifications {
    resource_type = "volume"
    tags          = { Name = "macpbox-root", Purpose = "macpbox" }
  }
  tag_specifications {
    resource_type = "network-interface"
    tags          = { Name = "macpbox", Purpose = "macpbox" }
  }
  tags = { Name = "macpbox", Purpose = "macpbox" }
}

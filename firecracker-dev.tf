# Firecracker Development Instance
# ARM64 metal instance for Firecracker/KVM testing
# Cost: ~$1.36/hour for c6g.metal (stop when not in use!)
#
# IMPORTANT: NV2 nested virtualization requires a custom kernel with DSB patches.
# See CLAUDE.md for kernel rebuild instructions after adding new patches to fcvm.

variable "enable_firecracker_instance" {
  description = "Enable standalone Firecracker development instance"
  type        = bool
  default     = true # Enabled - instance is imported
}

variable "firecracker_instance_type" {
  description = "Instance type for Firecracker dev"
  type        = string
  default     = "c7gd.metal" # ARM64 Graviton3 metal + NVMe for nested virt
}

variable "firecracker_volume_size" {
  description = "Root volume size in GB; includes /home/ubuntu"
  type        = number
  default     = 400
}

variable "firecracker_key_name" {
  description = "SSH key pair name"
  type        = string
  default     = "fcvm-ec2"
}

# AMI for ARM64 Ubuntu - hardcoded to match imported instance
variable "firecracker_ami" {
  description = "AMI ID for Firecracker instance"
  type        = string
  default     = "ami-0094253710d975cfa" # Ubuntu 24.04 ARM64 (2025-12-12)
}

# ---------------------------------------------------------------------------------
# Moving the box to another Availability Zone
#
# Spot capacity for c7gd.metal comes and goes per AZ. On 2026-09-13 us-west-1a refused every
# start while us-west-1c had capacity at a third of the price. The root disk is AZ-bound, so a
# move is an image of the stopped box and a new instance built from it in the target AZ:
#
#   1. stop the box
#   2. set firecracker_move_from_instance_id to its instance ID (terraform output
#      firecracker_dev_instance_id) and firecracker_availability_zone to the target
#   3. merge, then apply from a fresh worktree
#
# The new instance is created before the old one is destroyed, so a launch refused for lack of
# capacity leaves the old box untouched. The Elastic IP, security group and IAM follow, and the
# SSH host keys come across in the image and are kept (local.firecracker_user_data). The old
# root volume survives termination (delete_on_termination = false) as a rollback copy; delete it
# once the new box checks out. A target AZ needs a subnet in local.subnet_ids_by_az (main.tf).
# ---------------------------------------------------------------------------------
variable "firecracker_availability_zone" {
  description = "AZ for fcvm-metal-arm; must be a key of local.subnet_ids_by_az"
  type        = string
  default     = "us-west-1c"

  validation {
    condition     = contains(["us-west-1a", "us-west-1c"], var.firecracker_availability_zone)
    error_message = "fcvm-metal-arm can only be placed in an AZ that has a subnet: us-west-1a or us-west-1c."
  }
}

variable "firecracker_move_from_instance_id" {
  description = "Instance whose root disk the box is built from after a move; empty boots var.firecracker_ami"
  type        = string
  default     = "i-0766472741714f88a" # 2026-09-13: us-west-1a -> us-west-1c
}

# Security group for Firecracker dev instance
resource "aws_security_group" "firecracker_dev" {
  count       = var.enable_firecracker_instance ? 1 : 0
  name_prefix = "${var.project_name}-firecracker-dev-sg-"
  description = "Security group for Firecracker development instance"
  vpc_id      = local.vpc_id

  lifecycle {
    create_before_destroy = true
  }

  # SSH access
  ingress {
    from_port        = 22
    to_port          = 22
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
    description      = "SSH access"
  }

  # Eternal Terminal (persistent SSH sessions)
  ingress {
    from_port        = 2022
    to_port          = 2022
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
    description      = "Eternal Terminal"
  }

  # All outbound traffic for package installs, etc.
  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = {
    Name = "${var.project_name}-firecracker-dev-sg"
  }
}

# Network interface with IPv6 /64 prefix for routed mode VMs
resource "aws_network_interface" "firecracker_dev" {
  count             = var.enable_firecracker_instance ? 1 : 0
  subnet_id         = local.subnet_ids_by_az[var.firecracker_availability_zone]
  security_groups   = [aws_security_group.firecracker_dev[0].id]
  ipv6_prefix_count = 1

  # The host always gets its own IPv6 address too, not just the /80 its VMs route. The ENI
  # created by the 2026-09-13 move came up without one, and cloud-init only turns on DHCPv6
  # for an interface that has an address when the box boots.
  ipv6_address_count = 1

  tags = {
    Name = "fcvm-metal-arm-eni"
  }

  lifecycle {
    create_before_destroy = true
  }
}

# Image of the box being moved. Keyed by the source instance ID rather than a reference to
# aws_instance.firecracker_dev, because the new instance is built from this image. If the box
# is still running, AWS reboots it to take a consistent image.
resource "aws_ami_from_instance" "firecracker_dev_move" {
  count              = var.enable_firecracker_instance && var.firecracker_move_from_instance_id != "" ? 1 : 0
  name               = "fcvm-metal-arm-root-${var.firecracker_move_from_instance_id}"
  source_instance_id = var.firecracker_move_from_instance_id

  timeouts {
    create = "3h"
  }

  tags = {
    Name = "fcvm-metal-arm-root-${var.firecracker_move_from_instance_id}"
  }
}

locals {
  # Multipart so cloud-init reads ssh_deletekeys before running the usual bootstrap script. A
  # new instance built from a moved disk otherwise regenerates its SSH host keys, and every
  # known_hosts entry for the box starts refusing it.
  firecracker_user_data = <<-USERDATA
    Content-Type: multipart/mixed; boundary="==FCVM-ARM=="
    MIME-Version: 1.0

    --==FCVM-ARM==
    Content-Type: text/cloud-config; charset="us-ascii"

    #cloud-config
    ssh_deletekeys: false

    --==FCVM-ARM==
    Content-Type: text/x-shellscript; charset="us-ascii"

    #!/bin/bash
    set -euxo pipefail
    # Install AWS CLI via snap (awscli package not available on Ubuntu 24.04)
    snap install aws-cli --classic
    aws s3 cp s3://ejc3-dev-scripts/user-data/arm.sh /tmp/user_data.sh
    chmod +x /tmp/user_data.sh && /tmp/user_data.sh
    --==FCVM-ARM==--
  USERDATA
}

# Firecracker dev instance
resource "aws_instance" "firecracker_dev" {
  count         = var.enable_firecracker_instance ? 1 : 0
  ami           = length(aws_ami_from_instance.firecracker_dev_move) > 0 ? aws_ami_from_instance.firecracker_dev_move[0].id : var.firecracker_ami
  instance_type = var.firecracker_instance_type
  key_name      = var.firecracker_key_name

  # Shutdown behaviour is deliberately not set: AWS refuses to modify it on spot instances
  # (that failed the create in #116), and a spot instance whose interruption behaviour is
  # stop already launches with stop.

  # Network configuration - uses explicit ENI for IPv6 /64 prefix delegation
  network_interface {
    network_interface_id = aws_network_interface.firecracker_dev[0].id
    device_index         = 0
  }

  # IAM role - restricted (SSM to runners only, not admin)
  iam_instance_profile = aws_iam_instance_profile.dev_server.name

  # Spot instance - ~70% cheaper than on-demand
  instance_market_options {
    market_type = "spot"
    spot_options {
      instance_interruption_behavior = "stop"
      spot_instance_type             = "persistent"
    }
  }

  # Root volume
  root_block_device {
    volume_size           = var.firecracker_volume_size
    volume_type           = "gp3"
    delete_on_termination = false
    iops                  = 3000
    throughput            = 125
    tags = {
      Name   = "firecracker-dev-root"
      Backup = "daily"
    }
  }

  # User data - bootstrap fetches full script from S3 (bypasses 16KB limit)
  # Full script defined in dev-user-data.tf -> aws_s3_object.arm_user_data
  user_data = base64encode(local.firecracker_user_data)

  # Monitoring
  monitoring = true # 1-minute detailed monitoring

  tags = {
    Name   = "fcvm-metal-arm"
    DevEBS = "true"
  }

  lifecycle {
    # Build the replacement before destroying the old box, so a move refused for spot
    # capacity leaves the old box intact.
    create_before_destroy = true
    # Prevent recreation for imported instance
    ignore_changes = [
      ami,
      user_data,
      user_data_base64,
      metadata_options,
      root_block_device[0].encrypted,
      root_block_device[0].kms_key_id,
    ]
  }
}

# Auto-stop handled by Lambda (dev-auto-stop-lambda.tf)
# CloudWatch alarms removed - they caused drift when instances were recreated

# ============================================
# Elastic IP for static address
# ============================================

resource "aws_eip" "firecracker_dev" {
  count  = var.enable_firecracker_instance ? 1 : 0
  domain = "vpc"

  tags = {
    Name = "fcvm-metal-arm-eip"
  }
}

resource "aws_eip_association" "firecracker_dev" {
  count         = var.enable_firecracker_instance ? 1 : 0
  instance_id   = aws_instance.firecracker_dev[0].id
  allocation_id = aws_eip.firecracker_dev[0].id
}

# Output the instance ID and connection command
output "firecracker_dev_instance_id" {
  description = "Instance ID of Firecracker dev instance"
  value       = var.enable_firecracker_instance ? aws_instance.firecracker_dev[0].id : null
}

output "firecracker_dev_availability_zone" {
  description = "AZ the Firecracker dev instance runs in"
  value       = var.enable_firecracker_instance ? aws_instance.firecracker_dev[0].availability_zone : null
}

output "firecracker_dev_public_ip" {
  description = "Public IP of Firecracker dev instance (Elastic IP)"
  value       = var.enable_firecracker_instance ? aws_eip.firecracker_dev[0].public_ip : null
}

output "firecracker_dev_ssh_command" {
  description = "Command to connect to Firecracker dev instance via SSH"
  value       = var.enable_firecracker_instance ? "ssh -i ~/.ssh/${var.firecracker_key_name} ubuntu@${aws_eip.firecracker_dev[0].public_ip}" : null
}

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
  description = "Root volume size in GB"
  type        = number
  default     = 300
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
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "SSH access"
  }

  # Eternal Terminal (persistent SSH sessions)
  ingress {
    from_port   = 2022
    to_port     = 2022
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Eternal Terminal"
  }

  # All outbound traffic for package installs, etc.
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-firecracker-dev-sg"
  }
}

# Firecracker dev instance
resource "aws_instance" "firecracker_dev" {
  count         = var.enable_firecracker_instance ? 1 : 0
  ami           = var.firecracker_ami
  instance_type = var.firecracker_instance_type
  key_name      = var.firecracker_key_name

  # Network configuration
  subnet_id                   = aws_subnet.subnet_a.id
  vpc_security_group_ids      = [aws_security_group.firecracker_dev[0].id]
  associate_public_ip_address = true

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
  user_data = base64encode(<<-BOOTSTRAP
#!/bin/bash
set -euxo pipefail
# Install AWS CLI via snap (awscli package not available on Ubuntu 24.04)
snap install aws-cli --classic
aws s3 cp s3://ejc3-dev-scripts/user-data/arm.sh /tmp/user_data.sh
chmod +x /tmp/user_data.sh && /tmp/user_data.sh
BOOTSTRAP
  )

  # Monitoring
  monitoring = true  # 1-minute detailed monitoring

  tags = {
    Name = "fcvm-metal-arm"
  }

  # Lifecycle - prevent recreation for imported instance
  lifecycle {
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

output "firecracker_dev_public_ip" {
  description = "Public IP of Firecracker dev instance (Elastic IP)"
  value       = var.enable_firecracker_instance ? aws_eip.firecracker_dev[0].public_ip : null
}

output "firecracker_dev_ssh_command" {
  description = "Command to connect to Firecracker dev instance via SSH"
  value       = var.enable_firecracker_instance ? "ssh -i ~/.ssh/${var.firecracker_key_name} ubuntu@${aws_eip.firecracker_dev[0].public_ip}" : null
}

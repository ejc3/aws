# x86 Development Instance
# Intel metal instance for x86-specific testing
# Cost: ~$0.77/hour spot for c5.metal

variable "enable_x86_dev_instance" {
  description = "Enable x86 development instance"
  type        = bool
  default     = true
}

variable "x86_dev_instance_type" {
  description = "Instance type for x86 dev"
  type        = string
  default     = "c5.metal" # 96 vCPU, 192GB RAM
}

variable "x86_dev_volume_size" {
  description = "Root volume size in GB"
  type        = number
  default     = 300
}

# AMI for x86 Ubuntu 24.04
data "aws_ami" "ubuntu_x86" {
  most_recent = true
  owners      = ["099720109477"] # Canonical

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# Security group for x86 dev instance (reuse firecracker_dev SG pattern)
resource "aws_security_group" "x86_dev" {
  count       = var.enable_x86_dev_instance ? 1 : 0
  name_prefix = "${var.project_name}-x86-dev-sg-"
  description = "Security group for x86 development instance"
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

  # All outbound traffic
  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_name}-x86-dev-sg"
  }
}

# x86 dev instance
resource "aws_instance" "x86_dev" {
  count         = var.enable_x86_dev_instance ? 1 : 0
  ami           = data.aws_ami.ubuntu_x86.id
  instance_type = var.x86_dev_instance_type
  key_name      = var.firecracker_key_name

  # Network configuration - same VPC as ARM dev
  subnet_id                   = aws_subnet.subnet_a.id
  vpc_security_group_ids      = [aws_security_group.x86_dev[0].id]
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
    volume_size           = var.x86_dev_volume_size
    volume_type           = "gp3"
    delete_on_termination = false
    iops                  = 3000
    throughput            = 125
    tags = {
      Name   = "x86-dev-root"
      Backup = "daily"
    }
  }

  # User data - bootstrap fetches full script from S3 (bypasses 16KB limit)
  # Full script defined in dev-user-data.tf -> aws_s3_object.x86_user_data
  user_data = base64encode(<<-BOOTSTRAP
#!/bin/bash
set -euxo pipefail
aws s3 cp s3://ejc3-dev-scripts/user-data/x86.sh /tmp/user_data.sh
chmod +x /tmp/user_data.sh && /tmp/user_data.sh
BOOTSTRAP
  )


  monitoring = false

  tags = {
    Name = "fcvm-metal-x86"
  }

  # Lifecycle - prevent recreation for imported instance
  lifecycle {
    ignore_changes = [
      ami,
      user_data,
      user_data_base64,
    ]
  }
}

# ============================================
# Auto-stop after 3 days idle
# ============================================

resource "aws_cloudwatch_metric_alarm" "x86_dev_idle" {
  count               = var.enable_x86_dev_instance ? 1 : 0
  alarm_name          = "x86-dev-idle-3d"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 72 # 72 x 1hr = 3 days
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 3600
  statistic           = "Average"
  threshold           = 5
  alarm_description   = "Stop x86 dev instance after 3 days idle"

  dimensions = {
    InstanceId = aws_instance.x86_dev[0].id
  }

  alarm_actions = ["arn:aws:automate:us-west-1:ec2:stop"]

  tags = {
    Name = "x86-dev-idle-3d"
  }
}

# ============================================
# Elastic IP for static address
# ============================================

resource "aws_eip" "x86_dev" {
  count  = var.enable_x86_dev_instance ? 1 : 0
  domain = "vpc"

  tags = {
    Name = "fcvm-metal-x86-eip"
  }
}

resource "aws_eip_association" "x86_dev" {
  count         = var.enable_x86_dev_instance ? 1 : 0
  instance_id   = aws_instance.x86_dev[0].id
  allocation_id = aws_eip.x86_dev[0].id
}

# Outputs
output "x86_dev_instance_id" {
  description = "Instance ID of x86 dev instance"
  value       = var.enable_x86_dev_instance ? aws_instance.x86_dev[0].id : null
}

output "x86_dev_public_ip" {
  description = "Public IP of x86 dev instance (Elastic IP)"
  value       = var.enable_x86_dev_instance ? aws_eip.x86_dev[0].public_ip : null
}

output "x86_dev_ssh_command" {
  description = "Command to connect to x86 dev instance via SSH"
  value       = var.enable_x86_dev_instance ? "ssh -i ~/.ssh/${var.firecracker_key_name} ubuntu@${aws_eip.x86_dev[0].public_ip}" : null
}

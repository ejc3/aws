# Jumpbox Instance
# Tiny ARM64 instance with AWS admin access for remote management
# Cost: ~$3/month for t4g.nano (can leave running)

variable "enable_jumpbox" {
  description = "Enable jumpbox instance with admin AWS access"
  type        = bool
  default     = true
}

# IAM role with admin access for AWS CLI operations
resource "aws_iam_role" "jumpbox_admin" {
  count = var.enable_jumpbox ? 1 : 0
  name  = "jumpbox-admin-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Name = "jumpbox-admin-role"
  }
}

# Attach AdministratorAccess for full AWS CLI access
resource "aws_iam_role_policy_attachment" "jumpbox_admin" {
  count      = var.enable_jumpbox ? 1 : 0
  role       = aws_iam_role.jumpbox_admin[0].name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# Attach SSM for remote access
resource "aws_iam_role_policy_attachment" "jumpbox_ssm" {
  count      = var.enable_jumpbox ? 1 : 0
  role       = aws_iam_role.jumpbox_admin[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Instance profile
resource "aws_iam_instance_profile" "jumpbox_admin" {
  count = var.enable_jumpbox ? 1 : 0
  name  = "jumpbox-admin-profile"
  role  = aws_iam_role.jumpbox_admin[0].name
}

# Jumpbox instance
resource "aws_instance" "jumpbox" {
  count         = var.enable_jumpbox ? 1 : 0
  ami           = var.firecracker_ami  # Same Ubuntu ARM64 AMI
  instance_type = "t4g.nano"
  key_name      = var.firecracker_key_name

  # Network - same subnet as firecracker dev instance
  subnet_id                   = aws_subnet.subnet_a.id
  vpc_security_group_ids      = [aws_security_group.firecracker_dev[0].id]
  associate_public_ip_address = true

  # Admin IAM role
  iam_instance_profile = aws_iam_instance_profile.jumpbox_admin[0].name

  # Minimal root volume
  root_block_device {
    volume_size           = 8
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags = {
    Name = "jumpbox"
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

# Outputs
output "jumpbox_instance_id" {
  description = "Instance ID of jumpbox"
  value       = var.enable_jumpbox ? aws_instance.jumpbox[0].id : null
}

output "jumpbox_public_ip" {
  description = "Public IP of jumpbox"
  value       = var.enable_jumpbox ? aws_instance.jumpbox[0].public_ip : null
}

output "jumpbox_ssh_command" {
  description = "SSH to jumpbox"
  value       = var.enable_jumpbox ? "ssh -i ~/.ssh/${var.firecracker_key_name} ubuntu@${aws_instance.jumpbox[0].public_ip}" : null
}

output "jumpbox_to_firecracker_command" {
  description = "SSH from jumpbox to firecracker dev instance"
  value       = var.enable_jumpbox && var.enable_firecracker_instance ? "ssh -J ubuntu@${aws_instance.jumpbox[0].public_ip} ubuntu@${aws_instance.firecracker_dev[0].private_ip}" : null
}

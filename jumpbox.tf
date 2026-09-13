# Jumpbox Instance
# ARM64 instance with AWS admin access for remote management
# Cost: ~$48/month for t4g.large on-demand

variable "enable_jumpbox" {
  description = "Enable jumpbox instance with admin AWS access"
  type        = bool
  default     = true
}

# Shared by both jumpbox and jumpbox-2 (jumpbox2.tf reuses this instance profile directly
# rather than keeping a parallel AdministratorAccess copy in sync by hand). Gating these on
# enable_jumpbox alone was a real bug caught by codex review of jumpbox-2's PR: with
# enable_jumpbox=false and enable_jumpbox_2=true, aws_iam_instance_profile.jumpbox_admin[0]
# does not exist, so jumpbox-2's reference to it fails to evaluate -- meaning the backup
# admin box could not survive, or even be created fresh, if the original jumpbox were ever
# disabled or lost. That defeats the entire purpose of having a second, independent admin
# box, so this needs BOTH toggles, not just the original one.
locals {
  jumpbox_admin_iam_needed = var.enable_jumpbox || var.enable_jumpbox_2
}

# IAM role with admin access for AWS CLI operations
resource "aws_iam_role" "jumpbox_admin" {
  count = local.jumpbox_admin_iam_needed ? 1 : 0
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
  count      = local.jumpbox_admin_iam_needed ? 1 : 0
  role       = aws_iam_role.jumpbox_admin[0].name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# Attach SSM for remote access
resource "aws_iam_role_policy_attachment" "jumpbox_ssm" {
  count      = local.jumpbox_admin_iam_needed ? 1 : 0
  role       = aws_iam_role.jumpbox_admin[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Instance profile
resource "aws_iam_instance_profile" "jumpbox_admin" {
  count = local.jumpbox_admin_iam_needed ? 1 : 0
  name  = "jumpbox-admin-profile"
  role  = aws_iam_role.jumpbox_admin[0].name
}

# Persistent EBS volume for /home/ubuntu
resource "aws_ebs_volume" "jumpbox_home" {
  count             = var.enable_jumpbox ? 1 : 0
  availability_zone = "us-west-1a"
  size              = 40
  type              = "gp3"

  tags = {
    Name = "jumpbox-home"
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Attach home volume to jumpbox
resource "aws_volume_attachment" "jumpbox_home" {
  count       = var.enable_jumpbox ? 1 : 0
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.jumpbox_home[0].id
  instance_id = aws_instance.jumpbox[0].id
}

# Jumpbox instance
resource "aws_instance" "jumpbox" {
  count         = var.enable_jumpbox ? 1 : 0
  ami           = var.firecracker_ami # Same Ubuntu ARM64 AMI
  instance_type = "t4g.large"
  key_name      = var.firecracker_key_name

  # Network - same subnet as firecracker dev instance
  subnet_id                   = aws_subnet.subnet_a.id
  vpc_security_group_ids      = [aws_security_group.firecracker_dev[0].id]
  associate_public_ip_address = true

  # Admin IAM role
  iam_instance_profile = aws_iam_instance_profile.jumpbox_admin[0].name

  # Root volume sized for packages, provider builds, and system caches
  root_block_device {
    volume_size           = 40
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags = {
    Name = "jumpbox"
  }

  # Same thin bootstrap as jumpbox-2 (jumpbox2.tf): fetch the shared admin-box script
  # (jumpbox2-user-data.tf) from S3 and run it. The running instance predates this and
  # ignores user_data, so it only takes effect if the jumpbox is rebuilt; a running jumpbox
  # converges by re-fetching and re-running user-data/jumpbox.sh.
  user_data = base64encode(<<-BOOTSTRAP
    #!/bin/bash
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y || true
    apt-get install -y unzip curl || true
    if ! command -v aws >/dev/null 2>&1; then
      curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip
      cd /tmp && unzip -qo awscliv2.zip && ./aws/install && rm -rf /tmp/aws /tmp/awscliv2.zip
    fi
    aws s3 cp s3://ejc3-dev-scripts/user-data/jumpbox.sh /tmp/user_data.sh --region us-west-1
    chmod +x /tmp/user_data.sh && /tmp/user_data.sh
  BOOTSTRAP
  )

  # The bootstrap names the S3 object by path, so make the ordering explicit (jumpbox2.tf
  # explains the race this prevents on a from-scratch apply).
  depends_on = [aws_s3_object.jumpbox_user_data]

  # Lifecycle - prevent recreation for imported instance
  lifecycle {
    ignore_changes = [
      ami,
      user_data,
      user_data_base64,
      instance_type,
    ]
  }
}

# Backup plan for jumpbox home volume
resource "aws_backup_plan" "jumpbox" {
  count = local.jumpbox_admin_iam_needed ? 1 : 0 # shared plan; see the comment above enable_jumpbox
  name  = "jumpbox-backup-plan"

  rule {
    rule_name         = "daily"
    target_vault_name = aws_backup_vault.ejc3_backup.name
    schedule          = "cron(0 5 * * ? *)"
    start_window      = 60
    completion_window = 120
    lifecycle {
      delete_after = 7
    }
  }

  rule {
    rule_name         = "weekly"
    target_vault_name = aws_backup_vault.ejc3_backup.name
    schedule          = "cron(0 5 ? * SUN *)"
    start_window      = 60
    completion_window = 120
    lifecycle {
      delete_after = 30
    }
    copy_action {
      destination_vault_arn = aws_backup_vault.ejc3_backup_dr.arn
      lifecycle {
        delete_after = 30
      }
    }
  }

  rule {
    rule_name         = "monthly"
    target_vault_name = aws_backup_vault.ejc3_backup.name
    schedule          = "cron(0 5 1 * ? *)"
    start_window      = 60
    completion_window = 300
    lifecycle {
      cold_storage_after = 30
      delete_after       = 365
    }
    copy_action {
      destination_vault_arn = aws_backup_vault.ejc3_backup_dr.arn
      lifecycle {
        cold_storage_after = 30
        delete_after       = 365
      }
    }
    copy_action {
      destination_vault_arn = aws_backup_vault.staging.arn
      lifecycle {
        cold_storage_after = 30
        delete_after       = 365
      }
    }
  }
}

# Backup selection for jumpbox home volume, and jumpbox-2's root volume (jumpbox2.tf) --
# same plan, same schedule and retention. compact() drops the jumpbox-2 entry cleanly if
# it is ever disabled, rather than passing an empty string AWS Backup would reject.
resource "aws_backup_selection" "jumpbox" {
  count        = local.jumpbox_admin_iam_needed ? 1 : 0
  name         = "jumpbox-home-volume"
  plan_id      = local.backup_recovery_cutover_enabled ? aws_backup_plan.processing["jumpbox"].id : aws_backup_plan.jumpbox[0].id
  iam_role_arn = "arn:aws:iam::928413605543:role/AWSBackupDefaultServiceRole"

  resources = compact([
    # aws_ebs_volume.jumpbox_home is still gated on enable_jumpbox alone (correctly -- it
    # is jumpbox-1-specific), so this reference has to stay conditional too: with
    # enable_jumpbox=false, jumpbox_home[0] does not exist either, and this whole
    # selection resource now exists whenever enable_jumpbox_2 alone is true.
    var.enable_jumpbox ? aws_ebs_volume.jumpbox_home[0].arn : "",
    var.enable_jumpbox_2 ? "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:volume/${aws_instance.jumpbox_2[0].root_block_device[0].volume_id}" : "",
  ])
  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = !local.backup_recovery_cutover_enabled || local.backup_recovery_cleanup_enabled
      error_message = "Verify temporary-copy cleanup before switching the jumpbox backup selection."
    }
  }
}

# ============================================
# Elastic IP for static address
# ============================================

resource "aws_eip" "jumpbox" {
  count  = var.enable_jumpbox ? 1 : 0
  domain = "vpc"

  tags = {
    Name = "jumpbox-eip"
  }
}

resource "aws_eip_association" "jumpbox" {
  count         = var.enable_jumpbox ? 1 : 0
  instance_id   = aws_instance.jumpbox[0].id
  allocation_id = aws_eip.jumpbox[0].id
}

# Outputs
output "jumpbox_instance_id" {
  description = "Instance ID of jumpbox"
  value       = var.enable_jumpbox ? aws_instance.jumpbox[0].id : null
}

output "jumpbox_public_ip" {
  description = "Public IP of jumpbox (Elastic IP)"
  value       = var.enable_jumpbox ? aws_eip.jumpbox[0].public_ip : null
}

output "jumpbox_ssh_command" {
  description = "SSH to jumpbox"
  value       = var.enable_jumpbox ? "ssh -i ~/.ssh/${var.firecracker_key_name} ubuntu@${aws_eip.jumpbox[0].public_ip}" : null
}

output "jumpbox_to_firecracker_command" {
  description = "SSH from jumpbox to firecracker dev instance"
  value       = var.enable_jumpbox && var.enable_firecracker_instance ? "ssh -J ubuntu@${aws_eip.jumpbox[0].public_ip} ubuntu@${aws_instance.firecracker_dev[0].private_ip}" : null
}

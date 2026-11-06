# Stages a same-AZ Spot donor for an authorized recovery of the existing ARM root.
# No production instance, primary ENI, root attachment, or Elastic IP is changed here.
variable "enable_firecracker_recovery_donor" {
  description = "Deliberately stage one isolated us-west-1a FCVM ARM Spot recovery donor"
  type        = bool
  default     = false
}

variable "firecracker_recovery_donor_type" {
  description = "Approved Graviton bare-metal type for the staged Spot donor; try one at a time"
  type        = string
  default     = "c7gd.metal"

  validation {
    condition     = contains(["c7gd.metal", "r7gd.metal", "m8gd.metal-24xl"], var.firecracker_recovery_donor_type)
    error_message = "The recovery donor must be c7gd.metal, r7gd.metal, or m8gd.metal-24xl."
  }
}

resource "aws_network_interface" "firecracker_recovery_donor" {
  count             = var.enable_firecracker_recovery_donor ? 1 : 0
  subnet_id         = local.subnet_ids_by_az["us-west-1a"]
  security_groups   = [aws_security_group.firecracker_dev[0].id]
  ipv6_prefix_count = 1
  private_ips_count = 1
  tags              = { Name = "fcvm-metal-arm-recovery-donor-eni" }

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = var.aws_region == "us-west-1" && var.enable_firecracker_instance
      error_message = "The donor must use the existing FCVM ARM network in us-west-1."
    }
  }
}

# As with the production ENI, the host address and delegated prefix must be
# allocated in separate calls. Give cloud-init a host address before first boot.
resource "terraform_data" "firecracker_recovery_donor_ipv6" {
  count            = var.enable_firecracker_recovery_donor ? 1 : 0
  triggers_replace = aws_network_interface.firecracker_recovery_donor[0].id

  provisioner "local-exec" {
    interpreter = ["/bin/bash", "-c"]
    command     = <<-SH
      set -euo pipefail
      ENI="${aws_network_interface.firecracker_recovery_donor[0].id}"
      N=$(aws ec2 describe-network-interfaces --region us-west-1 --network-interface-ids "$ENI" --query 'length(NetworkInterfaces[0].Ipv6Addresses)' --output text)
      if [ "$N" = "0" ]; then
        aws ec2 assign-ipv6-addresses --region us-west-1 --network-interface-id "$ENI" --ipv6-address-count 1 > /dev/null
      fi
    SH
  }
}

resource "aws_instance" "firecracker_recovery_donor" {
  count = var.enable_firecracker_recovery_donor ? 1 : 0
  # Canonical's stock Noble 24.04 arm64 UEFI gp3 image in us-west-1 (2026-10-23).
  # The donor must boot a clean temporary root, not a historical FCVM data image.
  ami                  = "ami-02b9b3bdf3f150286"
  instance_type        = var.firecracker_recovery_donor_type
  key_name             = var.firecracker_key_name
  iam_instance_profile = aws_iam_instance_profile.dev_server.name
  monitoring           = true

  network_interface {
    network_interface_id = aws_network_interface.firecracker_recovery_donor[0].id
    device_index         = 0
  }

  instance_market_options {
    market_type = "spot"
    spot_options {
      spot_instance_type             = "persistent"
      instance_interruption_behavior = "stop"
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_put_response_hop_limit = 2
    http_tokens                 = "required"
  }

  root_block_device {
    encrypted             = true
    volume_size           = 16
    volume_type           = "gp3"
    delete_on_termination = false
    tags                  = { Name = "fcvm-metal-arm-recovery-donor-root" }
  }

  # After the manual transplant, cloud-init sees the donor's instance ID on
  # the old disk. Keep that disk's SSH host keys and don't run a new setup script.
  user_data = <<-CLOUD
    #cloud-config
    ssh_deletekeys: false
  CLOUD

  tags       = { Name = "fcvm-metal-arm-recovery-donor" }
  depends_on = [terraform_data.firecracker_recovery_donor_ipv6]

  lifecycle {
    prevent_destroy = true
    # The type is chosen at launch only: the provider would stop an existing
    # donor before attempting an in-place change that EC2 forbids for Spot.
    # After transplant, also leave the production root's settings and tags alone.
    ignore_changes = [instance_type, root_block_device]
  }
}

output "firecracker_recovery_donor" {
  description = "Staged Spot donor identity; its Elastic IP and production root are not managed here"
  value = var.enable_firecracker_recovery_donor ? {
    instance_id       = aws_instance.firecracker_recovery_donor[0].id
    spot_request_id   = aws_instance.firecracker_recovery_donor[0].spot_instance_request_id
    instance_type     = aws_instance.firecracker_recovery_donor[0].instance_type
    availability_zone = aws_instance.firecracker_recovery_donor[0].availability_zone
    network_interface = aws_network_interface.firecracker_recovery_donor[0].id
    private_ip        = aws_network_interface.firecracker_recovery_donor[0].private_ip
    root_volume_id    = aws_instance.firecracker_recovery_donor[0].root_block_device[0].volume_id
  } : null
}

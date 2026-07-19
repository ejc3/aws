# mac-dev.tf
#
# TEMPORARY EC2 Mac dev box (mac2-m2pro.metal: 12 vCPU / 32 GB) for cmux builds.
#
# ⚠️  TWO THINGS TO KNOW:
#   1. us-west-1 offers NO Mac instance types, so this lives in us-west-2 (its own
#      provider alias + the default VPC there) — it is deliberately isolated from the
#      rest of the infrastructure.
#   2. EC2 Mac runs on a DEDICATED HOST with a 24-HOUR MINIMUM allocation that cannot
#      be released early, and the host bills continuously whether the instance is
#      running or stopped. TEARDOWN: set enable_mac_dev = false and apply (only after
#      the 24h window has elapsed).

# OFF BY DEFAULT on purpose: this box costs ~$1.56/hr with a 24h minimum that cannot
# be released early, so a plain `terraform apply` must never spin it up by accident.
# Enable it deliberately for a run:  terraform apply -var enable_mac_dev=true
variable "enable_mac_dev" {
  description = "Spin up the temporary EC2 Mac dev host. NOTE: ~$1.56/hr, 24h minimum billing."
  type        = bool
  default     = false
}

# IMPORTANT: this account only has Dedicated-Host QUOTA (5 each) for these three types
# in us-west-2 -- there is NO quota entry for mac-m4/mac-m4pro/mac-m4max/mac-m3ultra, so
# allocating those always fails (AWS reports it as Client.InsufficientHostCapacity, which
# is misleading). Check with:
#   aws service-quotas list-service-quotas --service-code ec2 --region us-west-2 \
#     --query "Quotas[?contains(QuotaName,'mac')]"
#   mac2-m2pro.metal    12 vCPU / 32 GB  (~$1.56/hr)   AZs: us-west-2a,2b,2c
#   mac2-m1ultra.metal  20 vCPU / 128 GB (~$5.00/hr)   AZs: us-west-2b,2c,2d
#   mac2.metal           8 vCPU / 16 GB  (~$0.65/hr)   AZs: us-west-2a,2b,2c,2d
# Raise a quota request if you want the M4/M3 generations.
variable "mac_instance_type" {
  description = "EC2 Mac instance type (must be one we hold dedicated-host quota for)"
  type        = string
  default     = "mac2-m2pro.metal"
}

variable "mac_ssh_public_key" {
  description = "Public key permitted to SSH into the Mac"
  type        = string
  default     = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFoub+bRUiFb0xVRX8x/zTxdhXFB7yhh5hwfCUucXj9a ejc3-aws"
}

provider "aws" {
  alias  = "mac"
  region = "us-west-2"
}

# Mac dedicated-host capacity is genuinely scarce and varies by AZ — us-west-2b returned
# Client.InsufficientHostCapacity for mac-m4pro.metal (2a did too for mac2-m2pro). The
# error message names the AZs that DO have capacity -- read it. Override with:
#   terraform apply -var enable_mac_dev=true -var mac_az=us-west-2c
variable "mac_az" {
  description = "AZ to allocate the Mac dedicated host in (capacity varies by AZ)"
  type        = string
  default     = "us-west-2b"
}

locals {
  mac_az = var.mac_az
}

data "aws_vpc" "mac_default" {
  provider = aws.mac
  default  = true
}

data "aws_subnets" "mac" {
  provider = aws.mac
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.mac_default.id]
  }
  filter {
    name   = "availability-zone"
    values = [local.mac_az]
  }
}

data "aws_ami" "macos" {
  provider    = aws.mac
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "architecture"
    values = ["arm64_mac"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
  filter {
    name   = "name"
    values = ["amzn-ec2-macos-*"]
  }
}

# The dedicated host — this is what starts the 24h billing clock.
resource "aws_ec2_host" "mac" {
  count             = var.enable_mac_dev ? 1 : 0
  provider          = aws.mac
  instance_type     = var.mac_instance_type
  availability_zone = local.mac_az
  auto_placement    = "off"
  tags = {
    Name      = "mac-dev-temp"
    Temporary = "true"
    Purpose   = "cmux-build-24h"
  }
}

resource "aws_key_pair" "mac" {
  count      = var.enable_mac_dev ? 1 : 0
  provider   = aws.mac
  key_name   = "ejc3-aws-mac"
  public_key = var.mac_ssh_public_key
  tags       = { Name = "ejc3-aws-mac", Temporary = "true" }
}

resource "aws_security_group" "mac" {
  count       = var.enable_mac_dev ? 1 : 0
  provider    = aws.mac
  name_prefix = "mac-dev-temp-"
  description = "Temporary EC2 Mac dev box: SSH + VNC"
  vpc_id      = data.aws_vpc.mac_default.id

  ingress {
    description = "SSH"
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

  tags = { Name = "mac-dev-temp", Temporary = "true" }
}

resource "aws_instance" "mac" {
  count                       = var.enable_mac_dev ? 1 : 0
  provider                    = aws.mac
  ami                         = data.aws_ami.macos.id
  instance_type               = var.mac_instance_type
  host_id                     = aws_ec2_host.mac[0].id
  tenancy                     = "host"
  key_name                    = aws_key_pair.mac[0].key_name
  subnet_id                   = data.aws_subnets.mac.ids[0]
  vpc_security_group_ids      = [aws_security_group.mac[0].id]
  associate_public_ip_address = true

  root_block_device {
    volume_size = 200 # room for Xcode + cmux build artifacts
    volume_type = "gp3"
  }

  tags = {
    Name      = "mac-dev-temp"
    Temporary = "true"
    Purpose   = "cmux-build-24h"
  }
}

# Persistent IP
resource "aws_eip" "mac" {
  count    = var.enable_mac_dev ? 1 : 0
  provider = aws.mac
  domain   = "vpc"
  tags     = { Name = "mac-dev-temp", Temporary = "true" }
}

resource "aws_eip_association" "mac" {
  count         = var.enable_mac_dev ? 1 : 0
  provider      = aws.mac
  instance_id   = aws_instance.mac[0].id
  allocation_id = aws_eip.mac[0].id
}

output "mac_dev_public_ip" {
  description = "Persistent public IP of the temporary Mac dev box"
  value       = var.enable_mac_dev ? aws_eip.mac[0].public_ip : null
}

output "mac_dev_ssh" {
  description = "SSH command for the temporary Mac dev box"
  value       = var.enable_mac_dev ? "ssh ec2-user@${aws_eip.mac[0].public_ip}" : null
}

output "mac_dev_host_id" {
  description = "Dedicated host id (24h minimum before it can be released)"
  value       = var.enable_mac_dev ? aws_ec2_host.mac[0].id : null
}

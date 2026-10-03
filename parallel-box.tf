# parallel-box.tf
#
# On-demand 192-core Graviton spot box for embarrassingly parallel work, with a
# persistent 300GB data disk that OUTLIVES the instance.
#
# WHY c8g.48xlarge: 192 vCPU is the ceiling for Graviton across every family (c8g/m8g/
# r8g/x8g all cap at 48xlarge), and c8g is the compute-optimized one, so it is the
# cheapest per core: ~$3.14/hr spot in us-west-1 = ~$0.016/core-hour, roughly half what
# c8i.96xlarge costs per PHYSICAL core. Graviton runs one thread per core, so 192 vCPU
# really is 192 cores -- unlike Intel, where 384 vCPU is 192 cores with SMT.
#
# WHY us-west-2d, NOT us-west-1 (where the rest of the infra lives): us-west-1 has only
# two AZs and is capacity-starved for 192-core instances. A real launch there failed
# across all 13 Graviton pools with Server.InsufficientInstanceCapacity, and its
# on-demand quota (155 vCPU) is below the 192 this needs, so even paying full price
# would fail. us-west-2d scores 9/10 on spot placement and the region already has
# 512 vCPU of both spot and on-demand quota. EBS is AZ-locked, so the volume pins the
# AZ -- deliberately, since AZ-roaming would require a slow snapshot restore on boot.
#
# LIFECYCLE: this file holds only what is DURABLE -- the volume (cheap: 300GB gp3 ~=
# $8/month), the security group and the key pair. The INSTANCE is not terraform's; it is
# launched from the launch template in parallel-box-launch.tf by scripts/parallel-box.sh
# and terminated either by that script or by the idle watchdog. Read the header of
# parallel-box-launch.tf for why the launch moved out of terraform.
#
#     scripts/parallel-box.sh up | down | status | ssh      (or `pbox ...` on a dev box)
#
# COST: the instance is the expensive part (~$3.14/hr). Down means $0 compute.

variable "parallel_box_az" {
  description = "AZ for the box AND its persistent volume. Changing this strands the volume."
  type        = string
  default     = "us-west-2d" # spot placement score 9/10 for 192-core Graviton
}

# Second alias for us-west-2. mac-dev.tf already declares one ("mac"), but that name is
# meaningless here and the Mac config is disabled; a distinct alias keeps the two
# unrelated us-west-2 workloads from sharing a name.
provider "aws" {
  alias  = "west2"
  region = "us-west-2"
}

variable "parallel_box_ami" {
  description = "Ubuntu 24.04 arm64 in us-west-2"
  type        = string
  default     = "ami-0d81b5e3fc6de11fe"
}

resource "aws_key_pair" "parallel_box" {
  provider   = aws.west2
  key_name   = "fcvm-ec2-west2"
  public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINwtXjjTCVgT9OR3qrnz3zDkV2GveuCBlWFXSOBG2joe fcvm-ec2"
  tags       = { Name = "fcvm-ec2-west2" }
}

# ---------------------------------------------------------------------------------
# The persistent disk. Deliberately NOT tied to the instance's lifecycle.
#
# prevent_destroy is the point of this resource: `terraform destroy`, a stray count
# change, or a careless -target must never be able to delete the work stored here. If
# you genuinely want it gone you have to edit this block first, which is the friction
# we want.
# ---------------------------------------------------------------------------------
resource "aws_ebs_volume" "parallel_work" {
  provider          = aws.west2
  availability_zone = var.parallel_box_az
  size              = 300 # was 100; 69G was in use. EBS grows online but never shrinks
  type              = "gp3"
  encrypted         = true

  tags = {
    Name    = "parallel-box-work"
    Purpose = "persistent scratch for the on-demand 192-core box"
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Dedicated SG rather than reusing the dev box's: that one is created with a count and
# disappears whenever enable_firecracker_instance is false, which would break this box
# for an unrelated reason.
resource "aws_security_group" "parallel_box" {
  provider    = aws.west2
  name_prefix = "parallel-box-"
  description = "On-demand parallel compute box: SSH only"
  vpc_id      = "vpc-0376811912470fdbe" # default VPC in us-west-2

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # ipv6_cidr_blocks matters even though nothing here is IPv6-first: the subnet assigns
  # IPv6 addresses and DNS returns AAAA records first, so without it every outbound v6
  # connection sits in SYN-SENT until TCP gives up. On nextjs-dev that cost 60-90s per
  # AWS CLI call for three weeks before anyone traced it.
  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = { Name = "parallel-box" }

  lifecycle {
    create_before_destroy = true
  }
}

output "parallel_box_work_volume" {
  description = "Persistent 300GB work volume (survives the instance)"
  value       = aws_ebs_volume.parallel_work.id
}

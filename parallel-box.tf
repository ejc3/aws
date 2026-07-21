# parallel-box.tf
#
# On-demand 192-core Graviton spot box for embarrassingly parallel work, with a
# persistent 100GB data disk that OUTLIVES the instance.
#
# WHY c8g.48xlarge: 192 vCPU is the ceiling for Graviton across every family (c8g/m8g/
# r8g/x8g all cap at 48xlarge), and c8g is the compute-optimized one, so it is the
# cheapest per core: ~$3.14/hr spot in us-west-1 = ~$0.016/core-hour, roughly half what
# c8i.96xlarge costs per PHYSICAL core. Graviton runs one thread per core, so 192 vCPU
# really is 192 cores -- unlike Intel, where 384 vCPU is 192 cores with SMT.
#
# WHY us-west-1a: EBS volumes are AZ-locked, so the persistent disk pins the instance's
# AZ. 1a is where the existing dev boxes and subnet already live, and c8g.48xlarge is
# offered there, so this reuses the VPC/subnet/key/SG instead of duplicating them.
#
# LIFECYCLE: the VOLUME always exists (cheap: 100GB gp3 ~= $8/month). The INSTANCE only
# exists while enable_parallel_box = true. Bring it up and down with:
#     scripts/parallel-box.sh up | down | status | ssh
#
# COST: the instance is the expensive part (~$3.14/hr). Down means $0 compute.

variable "enable_parallel_box" {
  description = "Run the 192-core Graviton spot box. ~$3.14/hr while up. Use scripts/parallel-box.sh."
  type        = bool
  default     = false
}

variable "parallel_box_type" {
  description = "Instance type. Must be arm64 and offered in the volume's AZ."
  type        = string
  default     = "c8g.48xlarge" # 192 cores / 384GB
}

variable "parallel_box_az" {
  description = "AZ for the box AND its persistent volume. Changing this strands the volume."
  type        = string
  default     = "us-west-1a"
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
  availability_zone = var.parallel_box_az
  size              = 100
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
  name_prefix = "parallel-box-"
  description = "On-demand parallel compute box: SSH only"
  vpc_id      = data.aws_vpc.selected.id

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

  tags = { Name = "parallel-box" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_instance" "parallel_box" {
  count = var.enable_parallel_box ? 1 : 0

  ami           = var.firecracker_ami # Ubuntu 24.04 ARM64, shared with the dev boxes
  instance_type = var.parallel_box_type
  key_name      = var.firecracker_key_name

  availability_zone      = var.parallel_box_az
  subnet_id              = "subnet-05c215519b2150ecd" # same subnet as the dev boxes
  vpc_security_group_ids = [aws_security_group.parallel_box.id]

  instance_market_options {
    market_type = "spot"
    spot_options {
      # Interruption is survivable: all work lives on the persistent volume, which is
      # detached rather than destroyed. Terminate (not stop) keeps this simple -- there
      # is no state on the root disk worth preserving.
      spot_instance_type             = "one-time"
      instance_interruption_behavior = "terminate"
    }
  }

  # Root is DISPOSABLE and recreated on every launch. Anything you care about belongs
  # on /mnt/work, which is the persistent volume.
  root_block_device {
    volume_size           = 30
    volume_type           = "gp3"
    delete_on_termination = true
  }

  user_data = <<-INIT
    #!/bin/bash
    set -uxo pipefail

    # Mount the persistent work volume.
    #
    # SAFETY: this must never reformat a disk that already holds data, and must never
    # touch the wrong disk. Two guards:
    #   1. Find the device by matching the EBS volume ID against the NVMe serial, rather
    #      than guessing /dev/nvme1n1 -- device order is not stable on Nitro.
    #   2. Only mkfs when blkid reports NO filesystem at all. No -f, ever.
    VOL_ID="${aws_ebs_volume.parallel_work.id}"
    SERIAL=$(echo "$VOL_ID" | tr -d '-')

    DEV=""
    for _ in $(seq 1 30); do
      DEV=$(lsblk -dn -o NAME,SERIAL 2>/dev/null | awk -v s="$SERIAL" '$2==s {print $1}' | head -1)
      [ -n "$DEV" ] && break
      sleep 2
    done

    if [ -z "$DEV" ]; then
      echo "FATAL: could not find the EBS volume $VOL_ID by serial; refusing to format anything" >&2
      exit 1
    fi
    DEV="/dev/$DEV"

    if ! blkid "$DEV" >/dev/null 2>&1; then
      echo "no filesystem on $DEV (first use) -- creating ext4"
      mkfs.ext4 -L parallel-work "$DEV"
    else
      echo "$DEV already has a filesystem -- mounting as-is, NOT formatting"
    fi

    mkdir -p /mnt/work
    mount "$DEV" /mnt/work
    chown ubuntu:ubuntu /mnt/work
    grep -q "$DEV" /etc/fstab || echo "$DEV /mnt/work ext4 defaults,nofail 0 2" >> /etc/fstab

    # Parallel-work basics. GNU parallel is the usual driver for this shape of job.
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y parallel build-essential git htop

    # Raise the file-descriptor ceiling: 192-way fan-out hits the 1024 default fast.
    echo "* soft nofile 1048576" >> /etc/security/limits.conf
    echo "* hard nofile 1048576" >> /etc/security/limits.conf

    echo "parallel-box ready: $(nproc) cores, /mnt/work mounted"
  INIT

  tags = {
    Name    = "parallel-box"
    Purpose = "on-demand embarrassingly-parallel compute"
  }
}

resource "aws_volume_attachment" "parallel_work" {
  count       = var.enable_parallel_box ? 1 : 0
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.parallel_work.id
  instance_id = aws_instance.parallel_box[0].id

  # Detach cleanly when the box goes away; the VOLUME itself is untouched.
  force_detach = true
}

output "parallel_box_ssh" {
  description = "SSH command for the on-demand parallel box"
  value       = var.enable_parallel_box ? "ssh -i ~/.ssh/fcvm-ec2 ubuntu@${aws_instance.parallel_box[0].public_ip}" : "down (scripts/parallel-box.sh up)"
}

output "parallel_box_work_volume" {
  description = "Persistent 100GB work volume (survives the instance)"
  value       = aws_ebs_volume.parallel_work.id
}

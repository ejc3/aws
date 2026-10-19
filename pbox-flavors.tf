# pbox-flavors.tf
#
# THE FLAVORS OF THE PARALLEL BOXES. `pbox up` launches only a flavor the caller names (owner, 2026-10-19: "the agent
# should be required to pick among flavors", "24 core minimum"): the shape of the work (what it is bound by) and its size.
# Each flavor is a list of instance types tried in order, because spot capacity differs from type to type and one type
# alone is often out.
#
#   compute   2 GiB per vCPU: CPU-bound work
#   general   4 GiB per vCPU
#   memory    8 GiB per vCPU: memory-bound work
#   storage   8 GiB per vCPU plus large local NVMe (/mnt/nvme, gone with the box): local-disk-bound work
#   kvm       bare metal, for /dev/kvm (fcvm, firecracker); the owner's slots only, never a member's
#
# Sizes are 32, 48, 96 and 192 vCPU. No Graviton c, m or r type has 24 vCPU (there is no 6xlarge), so 32 is the smallest
# and nothing is under the 24-vCPU floor. Everything is Graviton (arm64), because every pbox AMI is arm64. Every type is
# offered in us-east-2c, the home AZ (checked 2026-10-19 with describe-instance-type-offerings); a fallback AZ that does
# not offer a type skips it (us-west-1 has no i8g, i8ge or i4g and no 9th-generation types).
#
# scripts/parallel-box.sh carries the same menu (FLAVORS), and scripts/test-pbox-flavors.py keeps the two equal. The
# instance types a pbox may be launched as are exactly these: pbox-flavor-types below denies every other type to a
# launch named as one of the boxes.

locals {
  pbox_flavors = {
    "compute-32"  = ["c8g.8xlarge", "c9g.8xlarge", "c8gn.8xlarge", "c8gb.8xlarge", "c8gd.8xlarge", "c9gd.8xlarge", "c7g.8xlarge", "c7gd.8xlarge", "c7gn.8xlarge"]
    "compute-48"  = ["c8g.12xlarge", "c9g.12xlarge", "c8gn.12xlarge", "c8gb.12xlarge", "c8gd.12xlarge", "c9gd.12xlarge", "c7g.12xlarge", "c7gd.12xlarge", "c7gn.12xlarge"]
    "compute-96"  = ["c8g.24xlarge", "c9g.24xlarge", "c8gn.24xlarge", "c8gb.24xlarge", "c8gd.24xlarge", "c9gd.24xlarge"]
    "compute-192" = ["c8g.48xlarge", "c9g.48xlarge", "c8gn.48xlarge", "c8gb.48xlarge", "c8gd.48xlarge", "c9gd.48xlarge"]
    "general-32"  = ["m8g.8xlarge", "m9g.8xlarge", "m8gd.8xlarge", "m9gd.8xlarge", "m7g.8xlarge", "m7gd.8xlarge"]
    "general-48"  = ["m8g.12xlarge", "m9g.12xlarge", "m8gd.12xlarge", "m9gd.12xlarge", "m7g.12xlarge", "m7gd.12xlarge"]
    "general-96"  = ["m8g.24xlarge", "m9g.24xlarge", "m8gd.24xlarge", "m9gd.24xlarge"]
    "general-192" = ["m8g.48xlarge", "m9g.48xlarge", "m8gd.48xlarge", "m9gd.48xlarge"]
    "memory-32"   = ["r8g.8xlarge", "r9g.8xlarge", "r8gd.8xlarge", "r9gd.8xlarge", "r7g.8xlarge", "r7gd.8xlarge"]
    "memory-48"   = ["r8g.12xlarge", "r9g.12xlarge", "r8gd.12xlarge", "r9gd.12xlarge", "r7g.12xlarge", "r7gd.12xlarge"]
    "memory-96"   = ["r8g.24xlarge", "r9g.24xlarge", "r8gd.24xlarge", "r9gd.24xlarge"]
    "memory-192"  = ["r8g.48xlarge", "r9g.48xlarge", "r8gd.48xlarge", "r9gd.48xlarge"]
    "storage-32"  = ["i8g.8xlarge", "i4g.8xlarge"]
    "storage-48"  = ["i8g.12xlarge", "i8ge.12xlarge"]
    "storage-96"  = ["i8g.24xlarge", "i8ge.24xlarge"]
    "storage-192" = ["i8g.48xlarge", "i8ge.48xlarge"]
    "kvm-96"      = ["c8g.metal-24xl", "c8gd.metal-24xl", "m8g.metal-24xl", "m8gd.metal-24xl"]
    "kvm-192"     = ["c8g.metal-48xl", "c8gd.metal-48xl", "c9g.metal-48xl", "c9gd.metal-48xl", "m8g.metal-48xl", "m8gd.metal-48xl"]
  }

  # The owner's slots and fallback boxes may be any flavor; a member's box any but kvm.
  pbox_flavor_types = distinct(flatten(values(local.pbox_flavors)))
  member_pbox_types = distinct(flatten([for f, types in local.pbox_flavors : types if !startswith(f, "kvm-")]))

  # Instance-store NVMe (the storage flavors, and the types with a "d"): found by its model name, never by a guessed
  # /dev name, and blank at every launch (a pbox is one-time spot or on-demand, terminated and never stopped). One disk is
  # formatted and mounted at /mnt/nvme; several become one RAID0 first. A disk that holds any filesystem is never
  # formatted. Not in fstab: the disks do not outlive the instance. Runs after apt-get update (mdadm for RAID0).
  pbox_local_nvme_setup = <<-NVME
NVME_DISKS=$(lsblk -dn -o NAME,MODEL | awk '/Amazon EC2 NVMe Instance Storage/ {print "/dev/" $1}')
if [ -n "$NVME_DISKS" ]; then
  NVME_USED=""
  for d in $NVME_DISKS; do blkid "$d" >/dev/null 2>&1 && NVME_USED="$NVME_USED $d"; done
  if [ -n "$NVME_USED" ]; then
    echo "local NVMe:$NVME_USED already holds a filesystem -- NOT formatting, /mnt/nvme not set up"
  else
    set -- $NVME_DISKS
    NVME_DEV="$1"
    if [ "$#" -gt 1 ]; then
      command -v mdadm >/dev/null || apt-get install -y mdadm
      mdadm --create /dev/md/nvme --level=0 --raid-devices="$#" --run "$@" && NVME_DEV=/dev/md/nvme
    fi
    if [ "$NVME_DEV" = /dev/md/nvme ] || [ "$#" -eq 1 ]; then
      mkfs.ext4 -E nodiscard -L local-nvme "$NVME_DEV" && mkdir -p /mnt/nvme && mount "$NVME_DEV" /mnt/nvme && chown ubuntu:ubuntu /mnt/nvme
    fi
  fi
fi
  NVME
}

# ---------------------------------------------------------------------------------
# Exactly the flavors' instance types, for every launch of a pbox: the owner's slots (parallel-box-control), the fallback
# sites (parallel-box-fallback, whose boxes carry the same names) and the members' boxes (member-pbox-control). A Deny on
# the launch's Name tag rather than a condition in each Allow: an ec2:InstanceType condition exists only on the instance,
# so in an Allow it would need the instance split from its volume and ENI in each of the three policies, and the lists
# (about 1,500 characters) do not fit in parallel-box-control or member-pbox-control under IAM's 6,144. A Deny also holds
# whatever else the role is allowed. Its own managed policy, attached to both roles that launch pboxes.
# ---------------------------------------------------------------------------------
data "aws_iam_policy_document" "pbox_flavor_types" {
  statement {
    sid       = "OwnerPboxFlavorTypesOnly"
    effect    = "Deny"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:*:${local.parallel_box_account}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.parallel_box_names
    }

    condition {
      test     = "StringNotEquals"
      variable = "ec2:InstanceType"
      values   = local.pbox_flavor_types
    }
  }

  statement {
    sid       = "MemberPboxFlavorTypesOnly"
    effect    = "Deny"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:*:${local.parallel_box_account}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.member_pbox_names
    }

    condition {
      test     = "StringNotEquals"
      variable = "ec2:InstanceType"
      values   = local.member_pbox_types
    }
  }
}

resource "aws_iam_policy" "pbox_flavor_types" {
  name        = "pbox-flavor-types"
  description = "Every pbox (the owner's slots, fallback boxes, members' boxes) launches only as one of its flavors' instance types (pbox-flavors.tf)"
  policy      = data.aws_iam_policy_document.pbox_flavor_types.json
}

resource "aws_iam_role_policy_attachment" "dev_server_pbox_flavor_types" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.pbox_flavor_types.arn
}

resource "aws_iam_role_policy_attachment" "nextjs_dev_pbox_flavor_types" {
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.pbox_flavor_types.arn
}

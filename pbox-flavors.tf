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
#   kvm-gpu   bare metal with NVIDIA GPUs bound to vfio-pci, for GPU passthrough into fcvm microVMs; the owner's only
#
# Sizes are 32, 48, 96 and 192 vCPU. No Graviton c, m or r type has 24 vCPU (there is no 6xlarge), so 32 is the smallest
# and nothing is under the 24-vCPU floor. Everything is Graviton (arm64), the launch templates' AMI, except kvm-gpu-96.
# Every type but g5g.metal is offered in us-east-2c, the home AZ (checked 2026-10-19 with describe-instance-type-offerings);
# a fallback AZ that does not offer a type skips it (us-west-1 has no i8g, i8ge or i4g and no 9th-generation types).
#
# kvm-gpu-64 is g5g.metal alone: 64 vCPU, 128 GiB, two NVIDIA T4G, and the only Graviton metal type with a GPU. It is not
# offered in us-east-2 at all (us-west-2a/b/c and us-east-1a-d offer it, checked 2026-10-22), so it launches only at a
# fallback site, with a fresh work volume. Both GPU flavors imply --fallback (scripts/parallel-box.sh): home first, then
# every fallback region that offers the type, best spot placement score first. The G spot quota decides where one fits:
# 96 vCPU in us-east-2 and us-west-2 (below), 640 in us-west-1 and 64 in us-east-1 (checked 2026-10-22), so us-east-1 takes
# a kvm-gpu-64 but not a kvm-gpu-96, and us-west-1 offers only g4dn.metal.
#
# kvm-gpu-96 is g4dn.metal alone: 96 vCPU, 384 GiB, eight NVIDIA T4 and 1.8 TB of local NVMe, x86_64 (Intel), offered in
# us-east-2a/b/c (checked 2026-10-22), so it launches at home on the persistent volume. It is the one x86 type: the
# templates boot arm64, so `pbox` launches it with --image-id set to the site's x86_64 build of the same Ubuntu image
# (var.parallel_box_ami_ohio_x86 at home, each fallback site's x86 image elsewhere), published in /infra/parallel-box and
# pinned by exact ARN in parallel-box-control and parallel-box-fallback. Intel needs intel_iommu=on for VFIO, so its first
# boot puts that on the kernel command line and reboots once (pbox_gpu_iommu_reboot below).
#
# Both GPU flavors count against the G spot quota, not the Standard one (pbox_gpu_spot_vcpus below).
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
    "kvm-gpu-64"  = ["g5g.metal"]
    "kvm-gpu-96"  = ["g4dn.metal"]
  }

  # The owner's slots and fallback boxes may be any flavor; a member's box any but kvm.
  # The kvm-gpu flavors start with "kvm-" too, so the member exclusions below and in scripts/parallel-box.sh cover them.
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

  # GPU passthrough host (the kvm-gpu flavors): every NVIDIA PCI function (vendor 0x10de) goes to vfio-pci, so fcvm can hand
  # it to a microVM. A box without one skips all of it. The host never gets an NVIDIA driver: driverctl's override binds
  # vfio-pci now and again at every boot (its udev rule sets driver_override before a driver can probe), and vfio-pci is
  # loaded at boot. Graviton metal has an SMMUv3 the arm64 kernel enables by default, so no kernel parameter is needed; x86
  # metal gets intel_iommu=on iommu=pt from pbox_gpu_iommu_reboot, which runs earlier in the boot. For a rootless fcvm,
  # ubuntu may open the VFIO groups (group kvm, which `pbox up` adds it to on a kvm flavor) and pin a guest's memory for
  # DMA (memlock). /etc/pbox-gpu lists each function,
  # its driver and its IOMMU group. Runs after apt-get update (driverctl).
  pbox_gpu_vfio_setup = <<-GPU
GPU_DEVS=""
for d in /sys/bus/pci/devices/*; do
  [ "$(cat "$d/vendor")" = 0x10de ] && GPU_DEVS="$GPU_DEVS $(basename "$d")"
done
if [ -n "$GPU_DEVS" ]; then
  echo vfio-pci > /etc/modules-load.d/vfio-pci.conf
  modprobe vfio-pci
  apt-get install -y driverctl
  echo 'SUBSYSTEM=="vfio", GROUP="kvm", MODE="0660"' > /etc/udev/rules.d/70-vfio-kvm.rules
  printf 'ubuntu soft memlock unlimited\nubuntu hard memlock unlimited\n' > /etc/security/limits.d/90-vfio-memlock.conf
  udevadm control --reload
  for dev in $GPU_DEVS; do driverctl set-override "$dev" vfio-pci; done
  udevadm settle
  for dev in $GPU_DEVS; do
    drv=$(readlink "/sys/bus/pci/devices/$dev/driver")
    grp=$(readlink "/sys/bus/pci/devices/$dev/iommu_group")
    echo "$dev driver=$${drv##*/} iommu_group=$${grp##*/}"
  done > /etc/pbox-gpu
fi
  GPU

  # The x86 GPU host's one reboot (kvm-gpu-96). VFIO needs the Intel IOMMU, which the Ubuntu kernel leaves off. So on an
  # x86_64 box with an NVIDIA PCI function, whose kernel was not booted with intel_iommu=on: put it (and iommu=pt, so the
  # host's own devices skip translation) in a GRUB drop-in, run update-grub and reboot. cloud-init runs user data once per
  # instance, so its semaphore is removed first: the whole boot script runs again after the reboot, and this step then
  # finds intel_iommu=on and does nothing. It runs FIRST in the boot script, before the work volume, apt, the local NVMe and
  # pbox_gpu_vfio_setup, so nothing that is not idempotent has run when the box reboots. Once only: the marker records the
  # boot that asked for the reboot, and a box with a marker never reboots again (if the parameter did not take, the GPU
  # check in `pbox up` fails instead of the box rebooting in a loop). `pbox up` reads the marker to tell "rebooting now"
  # from "failed". An arm64 box, or one without an NVIDIA function, skips all of it.
  pbox_gpu_iommu_reboot = <<-IOMMU
if [ "$(uname -m)" = x86_64 ] && grep -qx 0x10de /sys/bus/pci/devices/*/vendor 2>/dev/null && ! grep -qw intel_iommu=on /proc/cmdline && [ ! -e /var/lib/pbox-iommu-reboot ]; then
  echo 'GRUB_CMDLINE_LINUX_DEFAULT="$GRUB_CMDLINE_LINUX_DEFAULT intel_iommu=on iommu=pt"' > /etc/default/grub.d/90-pbox-iommu.cfg
  update-grub
  cat /proc/sys/kernel/random/boot_id > /var/lib/pbox-iommu-reboot
  rm -f /var/lib/cloud/instance/sem/config_scripts_user
  echo "pbox: rebooting once for intel_iommu=on iommu=pt (GPU passthrough); this boot script runs again after it"
  systemctl reboot
  sleep 600
  exit 0
fi
  IOMMU
}

# ---------------------------------------------------------------------------------
# The G spot quota where the GPU flavors launch: us-east-2, the home site (kvm-gpu-96; the default there is 64 vCPU, too few
# for one g4dn.metal), and us-west-2 (kvm-gpu-64, which us-east-2 does not offer). L-3819A6DF = "All G and VT Spot Instance
# Requests", in vCPU; one box of the largest GPU flavor needs 96. The current value is read here at plan time (the metal
# boxes can read it too since ejc3/aws#588, but only an administrator may request more): when it already covers one box
# nothing is requested, otherwise the apply files a RequestServiceQuotaIncrease (see ohio_spot_standard in ohio-pbox.tf for
# how the provider reports a pending request). A launch past the quota fails with MaxSpotInstanceCountExceeded and pbox
# moves on to the next AZ or region. On demand (parallel_box_spot = false) is another quota, L-DB2E81BA, not raised here.
# ---------------------------------------------------------------------------------
locals {
  # One box of the largest GPU flavor: the size in its kvm-gpu-N name.
  pbox_gpu_spot_vcpus = max([for f in keys(local.pbox_flavors) : tonumber(trimprefix(f, "kvm-gpu-")) if startswith(f, "kvm-gpu-")]...)
}

data "aws_servicequotas_service_quota" "ohio_spot_g" {
  provider     = aws.ohio
  service_code = "ec2"
  quota_code   = "L-3819A6DF"
}

resource "aws_servicequotas_service_quota" "ohio_spot_g" {
  provider     = aws.ohio
  service_code = "ec2"
  quota_code   = "L-3819A6DF"
  value        = max(local.pbox_gpu_spot_vcpus, data.aws_servicequotas_service_quota.ohio_spot_g.value)
}

data "aws_servicequotas_service_quota" "west2_spot_g" {
  provider     = aws.west2
  service_code = "ec2"
  quota_code   = "L-3819A6DF"
}

resource "aws_servicequotas_service_quota" "west2_spot_g" {
  provider     = aws.west2
  service_code = "ec2"
  quota_code   = "L-3819A6DF"
  value        = max(local.pbox_gpu_spot_vcpus, data.aws_servicequotas_service_quota.west2_spot_g.value)
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
  # pbox cancels a spot request that is still open or active after its instance is gone: EC2 can leave a one-time request
  # active for half an hour after a terminate, counting against the spot quota (2026-10-22, MaxSpotInstanceCountExceeded on
  # the next GPU box). Cancelling a request never stops its instance. Here because both roles that launch pboxes hold this
  # policy and have no room for another (20 of 20 managed policies, inline at IAM's total).
  statement {
    sid       = "CancelStaleSpotRequests"
    effect    = "Allow"
    actions   = ["ec2:CancelSpotInstanceRequests"]
    resources = ["arn:aws:ec2:*:${local.parallel_box_account}:spot-instances-request/*"]
  }

  # Except the persistent dev boxes' requests: a persistent request cancelled while its box runs leaves a box that cannot
  # start again after its next stop.
  dynamic "statement" {
    for_each = length(local.pbox_persistent_spot_requests) > 0 ? [1] : []
    content {
      sid       = "NeverThePersistentBoxesRequests"
      effect    = "Deny"
      actions   = ["ec2:CancelSpotInstanceRequests"]
      resources = local.pbox_persistent_spot_requests
    }
  }

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

locals {
  # The persistent spot boxes' requests (fcvm-metal-arm and fcvm-metal-x86 in us-west-1, the I/O box in us-west-2), which no
  # dev box may cancel. Region-wildcarded: the request id alone names it.
  pbox_persistent_spot_requests = [
    for id in compact(concat(
      aws_instance.firecracker_dev[*].spot_instance_request_id,
      aws_instance.x86_dev[*].spot_instance_request_id,
      [aws_instance.io_box.spot_instance_request_id],
    )) : "arn:aws:ec2:*:${local.parallel_box_account}:spot-instances-request/${id}"
  ]
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

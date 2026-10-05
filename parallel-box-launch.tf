# parallel-box-launch.tf
#
# HOW THE PARALLEL BOXES ARE LAUNCHED, and why it is not `terraform apply`.
#
# The old path was: a dev box SSHes to the jumpbox with a forced-command key
# (pbox-key.tf, now deleted) and the jumpbox runs `terraform apply -target=...`. That
# existed because terraform state and the admin role live on the jumpbox, and a dev box
# holds neither. It also meant every dev box had a working credential pointed at the
# jumpbox -- the exact trust direction dev-hop-key.tf exists to prevent, on machines that
# run agents with --dangerously-skip-permissions.
#
# THE FIX is to stop launching with terraform. Terraform still owns everything DURABLE
# -- the work volumes, the security group, the key pair, and the launch template below,
# which carries the entire launch configuration (AMI, spot options, root device, network,
# instance profile, bootstrap). A dev box supplies only the one thing terraform cannot
# know ahead of time: which instance type has spot capacity right now. It calls
# ec2:RunInstances against this template with a tag-scoped policy, and nothing else.
#
# THIS IS NOT A NEW EXCEPTION TO "changes go through terraform". parallel-box-watchdog.tf
# already TERMINATES these boxes from a Lambda with ec2:TerminateInstances scoped by tag
# -- "this role can only ever terminate the parallel boxes, never a dev box". The
# instance side of this pair has been non-terraform for as long as the watchdog has
# existed; terraform's ownership of aws_instance.parallel_box was already fictional the
# moment the watchdog fired. This makes the launch side match the terminate side, so one
# mechanism owns the ephemeral instance end to end. AGENTS.md rule 5 asks that such
# exceptions be deliberate and documented rather than casual: this is the documentation.
#
# WHAT IS EPHEMERAL VS DURABLE, precisely:
#   durable  (terraform): work volume, SG, key pair, launch template, watchdog
#   ephemeral (pbox):     the instance itself, and its attachment to the work volume
#
# A drift detector that flags unmanaged instances WILL see a running parallel box. That
# is correct and expected -- it is unmanaged on purpose, exactly like a watchdog-launched
# replacement would have been. Allowlist tag Name=parallel-box / parallel-box-2 there.

locals {
  # Everything that differs between the two boxes. They are otherwise identical, which is
  # the point: a second box exists so two jobs can run without sharing one work disk.
  parallel_boxes = {
    "1" = {
      name = "parallel-box"
    }
    "2" = {
      name = "parallel-box-2"
    }
  }

  parallel_box_volume_arns = flatten([for r, site in local.parallel_box_sites : [for id in values(site.volumes) : "arn:aws:ec2:${r}:${local.parallel_box_account}:volume/${id}"]])
}

locals {
  # Where a box lives. The persistent work volume pins the AZ and therefore the region, so a site is everything that is
  # regional: AMI, subnet, security group, key pair and the volume of each box. There is one, us-east-2 (ohio-pbox.tf), where
  # the boxes moved from us-west-2 on 2026-10-05 (30% cheaper for the 192-core spot pool); the us-west-2 site, its volumes and
  # its templates are retired. The map form stays so a second site is an entry here, not a rewrite of the IAM below.
  parallel_box_sites = {
    "us-east-2" = {
      ami               = var.parallel_box_ami_ohio
      subnet_id         = aws_subnet.ohio_pbox.id
      security_group_id = aws_security_group.ohio_pbox.id
      key_name          = aws_key_pair.ohio_pbox.key_name
      volumes           = { for k, v in aws_ebs_volume.ohio_parallel_work : k => v.id }
    }
  }

  parallel_box_account = data.aws_caller_identity.current.account_id

  # One ARN pattern per site for the resources RunInstances creates, tags and manages.
  parallel_box_instance_arns   = [for r in keys(local.parallel_box_sites) : "arn:aws:ec2:${r}:${local.parallel_box_account}:instance/*"]
  parallel_box_eni_arns        = [for r in keys(local.parallel_box_sites) : "arn:aws:ec2:${r}:${local.parallel_box_account}:network-interface/*"]
  parallel_box_new_volume_arns = [for r in keys(local.parallel_box_sites) : "arn:aws:ec2:${r}:${local.parallel_box_account}:volume/*"]

  # Every pre-existing thing a launch REFERENCES, by exact ARN, across the sites.
  parallel_box_referenced_arns = flatten([
    for r, site in local.parallel_box_sites : [
      "arn:aws:ec2:${r}::image/${site.ami}",
      "arn:aws:ec2:${r}:${local.parallel_box_account}:subnet/${site.subnet_id}",
      "arn:aws:ec2:${r}:${local.parallel_box_account}:security-group/${site.security_group_id}",
      "arn:aws:ec2:${r}:${local.parallel_box_account}:key-pair/${site.key_name}",
    ]
  ])

  parallel_box_template_arns = [for lt in aws_launch_template.parallel_box_ohio : lt.arn]

  # The boot script of one box: authorize the dev hop key, find the work volume by its EBS serial and mount it
  # without ever formatting a disk that has a filesystem, then the shared NFS scratch. Keyed "<region>/<box>" because
  # the volume ID is the one regional input.
  parallel_box_user_data = {
    for key, vol in merge([for r, site in local.parallel_box_sites : { for box, id in site.volumes : "${r}/${box}" => id }]...) :
    key => base64encode(<<-INIT
        #!/bin/bash
        set -uxo pipefail

        # Authorize the dev-hop key (dev-hop-key.tf) for direct login. This box is launched
        # and used from a dev box, which holds no key to the jumpbox and no fcvm-ec2 key at
        # all -- dev_hop is the ONLY key those boxes carry that reaches another host, so it
        # has to be what this box trusts too.
        install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
        touch /home/ubuntu/.ssh/authorized_keys
        grep -qxF "${trimspace(tls_private_key.dev_hop.public_key_openssh)}" /home/ubuntu/.ssh/authorized_keys || \
          echo "${trimspace(tls_private_key.dev_hop.public_key_openssh)}" >> /home/ubuntu/.ssh/authorized_keys
        chmod 600 /home/ubuntu/.ssh/authorized_keys
        chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys

        # Mount the persistent work volume.
        #
        # The volume is attached by the CALLER after RunInstances returns (a launch template
        # cannot attach a pre-existing volume), so the device appears seconds after boot
        # rather than at boot. The retry loop below waits up to 120s for the serial to show
        # up -- doubled from the 60s that covered Nitro's own attach latency, because it now
        # also has to cover `aws ec2 wait instance-running` plus the attach call itself.
        #
        # SAFETY: this must never reformat a disk that already holds data, and must never
        # touch the wrong disk. Two guards:
        #   1. Find the device by matching the EBS volume ID against the NVMe serial, rather
        #      than guessing /dev/nvme1n1 -- device order is not stable on Nitro.
        #   2. Only mkfs when blkid reports NO filesystem at all. No -f, ever.
        VOL_ID="${vol}"
        SERIAL=$(echo "$VOL_ID" | tr -d '-')

        DEV=""
        for _ in $(seq 1 60); do
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

        # Shared bulk scratch/cache on the separate i8ge I/O box. This is an automount, so
        # the parallel box still boots cleanly when the I/O box is stopped.
        ${local.io_box_client_setup}

        # Raise the file-descriptor ceiling: 192-way fan-out hits the 1024 default fast.
        echo "* soft nofile 1048576" >> /etc/security/limits.conf
        echo "* hard nofile 1048576" >> /etc/security/limits.conf

        echo "parallel-box ready: $(nproc) cores, /mnt/work mounted"
    INIT
    )
  }
}

# ---------------------------------------------------------------------------------
# The launch configuration, moved verbatim from the aws_instance resources that used to
# live in parallel-box.tf / parallel-box2.tf.
#
# instance_type is deliberately ABSENT. It is the one field the caller supplies, because
# capacity is the hard part: a 192-core spot request pinned to one type scores 1/10 for
# fulfilment, so scripts/parallel-box.sh walks a list of pools until one answers.
# ---------------------------------------------------------------------------------
resource "aws_launch_template" "parallel_box_ohio" {
  provider = aws.ohio
  for_each = local.parallel_boxes

  name                   = each.value.name
  image_id               = var.parallel_box_ami_ohio # Ubuntu 24.04 arm64, us-east-2
  key_name               = aws_key_pair.ohio_pbox.key_name
  update_default_version = true

  iam_instance_profile {
    name = aws_iam_instance_profile.dev_ebs_only.name
  }

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
  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = 30
      volume_type           = "gp3"
      delete_on_termination = true
    }
  }

  # The subnet pins the AZ, and the AZ is pinned by the work volume: EBS is AZ-locked, so
  # the box must launch where its disk already is. Set here rather than passed by the
  # caller so a mistyped --subnet-id cannot strand an instance away from its disk.
  network_interfaces {
    subnet_id                   = aws_subnet.ohio_pbox.id # us-east-2c, the pbox VPC in ohio-pbox.tf
    security_groups             = [aws_security_group.ohio_pbox.id]
    associate_public_ip_address = true
    delete_on_termination       = true
  }

  # Tags must be applied AT LAUNCH, not afterwards: the IAM policy below authorizes
  # RunInstances only when the instance is born with the right Name, and the watchdog
  # finds its targets by that same tag. An untagged box would be both unauthorized and
  # invisible to the thing that stops it costing money.
  tag_specifications {
    resource_type = "instance"
    tags = {
      Name    = each.value.name
      Purpose = "on-demand embarrassingly-parallel compute"
      DevEBS  = "true"
    }
  }

  # The root volume carries the BOX's name, not "<name>-root". The IAM condition below
  # tests aws:RequestTag/Name against exactly the two box names, so any other value --
  # however sensible it reads -- is an unsatisfiable condition and a denied launch. It is
  # distinguishable from the work volume by tag Role, which nothing gates on.
  #
  # Deliberately no DevEBS=true here: that tag is what local.dev_ebs_policy keys on for
  # ec2:DeleteVolume, and there is no reason to hand out a delete grant for a disk that
  # already dies with the instance.
  tag_specifications {
    resource_type = "volume"
    tags = {
      Name = each.value.name
      Role = "root"
    }
  }

  # The ENI must be tagged too, and this is not cosmetic. RunInstances authorizes every
  # resource it creates, the IAM policy below gates instance/volume/network-interface on
  # aws:RequestTag/Name, and a condition on an untagged resource can never be satisfied.
  # Without this the launch fails with UnauthorizedOperation on network-interface/* --
  # observed live, and it fails for EVERY instance type, so it reads like a capacity
  # drought rather than a policy bug.
  tag_specifications {
    resource_type = "network-interface"
    tags = {
      Name = each.value.name
    }
  }

  tags = { Name = each.value.name }

  user_data = local.parallel_box_user_data["us-east-2/${each.key}"]
}

# ---------------------------------------------------------------------------------
# What a dev box is allowed to do, which is exactly: launch one of these two boxes,
# attach its own work disk, and kill it again.
#
# The condition structure matters and is easy to get wrong. RunInstances authorizes a
# LIST of resource types in one call -- image, subnet, security group, key pair, launch
# template, plus the instance/volume/ENI being created. A tag condition can only be
# satisfied by the resources that are actually being tagged, so putting
# aws:RequestTag/Name on a statement that also covers the AMI would deny the AMI and the
# whole call fails. Hence two statements: the created resources carry the tag condition,
# the referenced ones are pinned to specific ARNs instead.
# ---------------------------------------------------------------------------------
data "aws_iam_policy_document" "parallel_box_control" {
  # The instance, its root volume and its ENI: may only be created carrying a Name tag
  # that is one of the two boxes. This is the condition that makes "launch a 192-core
  # box" fail to generalize into "launch anything".
  statement {
    sid       = "RunTaggedParallelBoxOnly"
    effect    = "Allow"
    actions   = ["ec2:RunInstances"]
    resources = concat(local.parallel_box_instance_arns, local.parallel_box_new_volume_arns, local.parallel_box_eni_arns)

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = [for b in local.parallel_boxes : b.name]
    }

    # Only through the parallel-box launch templates. This role also holds gpu-box-control
    # (gpu-box.tf), and IAM authorizes each resource of a RunInstances call against the
    # UNION of both policies: without this pin, a template-less call tagged Name=parallel-box
    # could borrow the GPU box's x86 AMI and subnets and launch any x86 type, and a gpu-box
    # could borrow this policy's PassRole or its world-open security group.
    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.parallel_box_template_arns
    }
  }

  # The things the call REFERENCES rather than creates. Pinned to exact ARNs: this role
  # cannot launch from some other AMI, into some other subnet, or with some other
  # security group, because none of those ARNs are listed here.
  statement {
    sid       = "RunInstancesReferencedResources"
    effect    = "Allow"
    actions   = ["ec2:RunInstances"]
    resources = concat(local.parallel_box_referenced_arns, local.parallel_box_template_arns)

    # Same template pin as above, for the same union reason.
    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.parallel_box_template_arns
    }
  }

  # Tagging at launch. ec2:CreateAction pins this to RunInstances, so it cannot be used
  # to retag an existing resource into scope of some other tag-based policy.
  statement {
    sid       = "TagAtLaunchOnly"
    effect    = "Allow"
    actions   = ["ec2:CreateTags"]
    resources = concat(local.parallel_box_instance_arns, local.parallel_box_new_volume_arns, local.parallel_box_eni_arns)

    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances"]
    }
  }

  # Terminate, and stop/start for the occasional pause. Tag-scoped the same way the
  # watchdog's own policy is, so this can never reach a dev box or a jumpbox.
  statement {
    sid    = "ManageTaggedParallelBoxOnly"
    effect = "Allow"
    actions = [
      "ec2:TerminateInstances",
      "ec2:StopInstances",
      "ec2:StartInstances",
    ]
    resources = local.parallel_box_instance_arns

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Name"
      values   = [for b in local.parallel_boxes : b.name]
    }
  }

  # Attaching the work disk. Scoped to the two volume ARNs by IDENTITY, not by a DevEBS
  # tag: tagging them DevEBS=true would have swept them into local.dev_ebs_policy, which
  # grants ec2:DeleteVolume. These volumes carry prevent_destroy precisely because losing
  # one loses real work, so they must not be reachable by a delete grant.
  statement {
    sid    = "AttachWorkVolume"
    effect = "Allow"
    actions = [
      "ec2:AttachVolume",
      "ec2:DetachVolume",
    ]
    resources = local.parallel_box_volume_arns
  }

  statement {
    sid    = "AttachWorkVolumeToTaggedInstance"
    effect = "Allow"
    actions = [
      "ec2:AttachVolume",
      "ec2:DetachVolume",
    ]
    resources = local.parallel_box_instance_arns

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Name"
      values   = [for b in local.parallel_boxes : b.name]
    }
  }

  # The instance profile the box boots with. PassRole is the permission that most needs
  # pinning -- a wildcard here would let this role hand ANY role to a new instance and
  # escalate straight out of its own scope. One role ARN, and only to EC2.
  statement {
    sid       = "PassInstanceProfileRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.dev_ebs_only.arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }

  # Read-only lookups the script makes: find the running box, price the pools, locate the
  # volume's AZ. None of these accept resource-level scoping in IAM.
  statement {
    sid    = "DescribeForPbox"
    effect = "Allow"
    actions = [
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeInstanceStatus",
      "ec2:DescribeVolumes",
      "ec2:DescribeSpotPriceHistory",
      "ec2:DescribeLaunchTemplates",
      "ec2:DescribeLaunchTemplateVersions",
      "ec2:DescribeImages",
      "ec2:DescribeSubnets",
      "ec2:DescribeSecurityGroups",
    ]
    resources = ["*"]
  }
}

# ONE managed policy for both roles, not an inline policy on each. dev-server-role's inline policies total about 10,060 of the
# 10,240 characters IAM allows per role, so the policy that grew to cover two regions (about 4,100 characters) could not be
# put inline there: PutRolePolicy failed with LimitExceeded. A managed policy has its own 6,144 limit and does not count
# against the role's inline total.
resource "aws_iam_policy" "parallel_box_control" {
  name   = "parallel-box-control"
  policy = data.aws_iam_policy_document.parallel_box_control.json
}

# The metal boxes (fcvm-metal-arm, fcvm-metal-x86).
resource "aws_iam_role_policy_attachment" "dev_server_parallel_box" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.parallel_box_control.arn
}

# The shared Next.js box. It is a four-human box, so this is worth being explicit about:
# the grant is identical to the metal boxes' and is bounded by the tag conditions above.
# The worst a compromised account there can do with it is cost money on a box the
# watchdog kills after 30 minutes idle -- it cannot reach any other instance, cannot pass
# any other role, and cannot delete the work volumes.
resource "aws_iam_role_policy_attachment" "nextjs_dev_parallel_box" {
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.parallel_box_control.arn
}

output "parallel_box_launch_templates" {
  description = "Launch templates pbox runs instances from (terraform owns the config; pbox picks the instance type)"
  value       = { for k, lt in aws_launch_template.parallel_box_ohio : k => lt.name }
}

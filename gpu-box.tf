# gpu-box.tf
#
# THE ON-DEMAND GPU BOXES: up to four small NVIDIA instances (slots 1-4) for measuring
# browser games on real graphics hardware. Every dev box renders WebGL in software
# (SwiftShader), which says nothing about frame rate on a real GPU -- Colton Games' 36-player Starfall royale runs at
# ~0.1 fps there, and that number is useless for deciding whether it is playable.
#
# SAME SHAPE AS THE PARALLEL BOXES (parallel-box-launch.tf), deliberately: terraform owns
# the durable half -- the security group, the launch template and the IAM grant -- and a
# dev box launches and terminates the instance itself (`gbox up` / `gbox down`,
# scripts/gpu-box.sh) with a tag-scoped policy. It is the same documented exception to
# AGENTS.md rule 5, not a new one: the instance is ephemeral and never in state.
#
#   durable  (terraform): security group, launch templates, IAM grant, watchdog entries
#   ephemeral (gbox):     the instances and their disposable root disks
#
# THREE THINGS BOUND WHAT IT CAN COST, because every account on nextjs-dev has sudo and
# the grant is therefore box-wide:
#   1. the policy allows only the instance types in local.gpu_box_types (the smallest
#      NVIDIA sizes, ~$0.5-1/hr on demand), only the slot names in local.gpu_box_slots,
#      only the slots' templates;
#   2. parallel-box-watchdog.tf terminates each one after 30 minutes below 5% CPU;
#   3. a hard lifetime of var.gpu_box_max_hours, enforced twice: the watchdog terminates
#      it by LaunchTime (MAX_AGE_MINUTES), which nothing on the box can disarm, and the box
#      arms its own shutdown -h timer as a backup, so even a busy runaway test ends.
#
# Nothing on it persists. Tests copy their build in over SSH, run, and copy results out.
#
# FOUR SLOTS, so several projects' GPU jobs stop queueing behind one box. Each slot is its own
# box with its own Name tag -- slot 1 keeps the original "gpu-box", slots 2-4 are "gpu-box-2"
# .. "gpu-box-4" -- and its own launch template of the same name, exactly as the parallel
# boxes do it (one template per box, the Name baked into the template's tags). gbox therefore
# never passes a tag at launch: the template writes it, and the grant below accepts exactly
# these names, so `gbox up 3` needs no wider tagging right than `gbox up` does. Everything
# that bounds the cost is per box and keyed on the same names: the watchdog finds, idles out
# and lifetime-caps each one, and `gbox down N` terminates only the instances tagged with
# slot N's name.

variable "gpu_box_ami" {
  description = "AWS Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04), x86_64, us-west-2. The NVIDIA driver is preinstalled."
  type        = string
  default     = "ami-04f7da1c787a89267" # 20260918 build
}

variable "gpu_box_max_hours" {
  description = "Hard lifetime of the GPU box in hours: the watchdog terminates it this long after LaunchTime (the box's own shutdown timer is a backup)."
  type        = number
  default     = 4
}

locals {
  # Slot 1 keeps the original name, so every existing `gbox` caller and the box already
  # running are untouched; the others are suffixed, as parallel-box-2 is.
  gpu_box_name  = "gpu-box"
  gpu_box_slots = { for n in [1, 2, 3, 4] : tostring(n) => n == 1 ? local.gpu_box_name : "${local.gpu_box_name}-${n}" }
  gpu_box_names = [for k in sort(keys(local.gpu_box_slots)) : local.gpu_box_slots[k]]

  gpu_box_template_arns = [for k in sort(keys(local.gpu_box_slots)) : aws_launch_template.gpu_box[k].arn]

  # The tag keys the launch template writes, per created resource. IAM evaluates
  # aws:TagKeys for each resource of a RunInstances call separately (the untagged-ENI trap
  # in AGENTS.md is that rule biting), so each statement allows exactly its own keys and a
  # launch cannot smuggle in DevEBS=true or any key another tag-based policy trusts.
  gpu_box_tag_keys = {
    instance = ["Name", "Purpose"]
    volume   = ["Name", "Role"]
    eni      = ["Name"]
  }

  # The smallest NVIDIA sizes, cheapest first. gbox tries them in this order across every
  # default-VPC subnet until one launches; the IAM policy below allows exactly these.
  gpu_box_types = ["g4dn.xlarge", "g5.xlarge", "g6.xlarge", "g4dn.2xlarge"]

  # The four default-VPC subnets in us-west-2 (a, b, c, d). Unlike the parallel boxes
  # there is no AZ-locked work volume, so any AZ with capacity will do.
  gpu_box_subnets = [
    "subnet-047683926b94c92c7", # us-west-2a
    "subnet-00844231e2667deec", # us-west-2b
    "subnet-0346a0cc9fe6b928f", # us-west-2c
    "subnet-095349c0fcef8c47f", # us-west-2d
  ]
}

# SSH from nextjs-dev only (its Elastic IP), unlike the parallel boxes' 0.0.0.0/0: nothing
# else ever drives this box, and a test box running a browser has no business being
# reachable from the internet.
resource "aws_security_group" "gpu_box" {
  provider    = aws.west2
  name_prefix = "gpu-box-"
  description = "On-demand GPU test box: SSH from nextjs-dev only"
  vpc_id      = "vpc-0376811912470fdbe" # default VPC in us-west-2

  # aws_eip.nextjs_dev is counted on var.enable_nextjs_dev; with the box disabled there is
  # nobody to let in, and an ingress rule with no source would be invalid.
  dynamic "ingress" {
    for_each = aws_eip.nextjs_dev
    content {
      description = "SSH from nextjs-dev"
      from_port   = 22
      to_port     = 22
      protocol    = "tcp"
      cidr_blocks = ["${ingress.value.public_ip}/32"]
    }
  }

  # Egress is web, DNS and NTP only -- deliberately not "all". io-box.tf exports its shared
  # NFS scratch read-write to its own us-west-2d subnet (one of the subnets below), and this
  # box runs a browser on test pages; it has no business reaching port 2049 (or anything
  # else inside the fleet). Everything it needs -- apt, npm, Playwright's browser
  # downloads, the game build copied in over the inbound SSH -- is covered here. These
  # subnets assign no IPv6, so there is no v6 rule.
  dynamic "egress" {
    for_each = { https = ["tcp", 443], http = ["tcp", 80], dns-udp = ["udp", 53], dns-tcp = ["tcp", 53], ntp = ["udp", 123] }
    content {
      description = egress.key
      from_port   = egress.value[1]
      to_port     = egress.value[1]
      protocol    = egress.value[0]
      cidr_blocks = ["0.0.0.0/0"]
    }
  }

  tags = { Name = local.gpu_box_name }

  lifecycle {
    create_before_destroy = true
  }
}

# The whole launch configuration except two things gbox supplies at launch: the instance
# type and the subnet, because capacity is the part terraform cannot know. Neither is a
# free choice -- the IAM policy pins both to the lists above, and requires one of THESE
# templates. One template per slot, identical but for the Name it writes.
# IAM cannot pin the template's user_data, shutdown behaviour or termination protection
# against a launch-time override; the watchdog's LaunchTime lifetime is what bounds those.
resource "aws_launch_template" "gpu_box" {
  provider               = aws.west2
  for_each               = local.gpu_box_slots
  name                   = each.value
  image_id               = var.gpu_box_ami
  update_default_version = true

  # An OS-initiated shutdown TERMINATES, so the lifetime timer in user_data ends the box
  # instead of leaving a stopped instance and its disk behind.
  instance_initiated_shutdown_behavior = "terminate"

  # No instance profile: the box needs no AWS access at all, so there is no role to pass
  # and the grant below cannot hand any role to an instance.
  vpc_security_group_ids = [aws_security_group.gpu_box.id]

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  # Root is disposable. 100 GB rather than the AMI's 75 GB: browser builds, Playwright's
  # browsers and screenshots of a long test run live here for the box's short life.
  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = 100
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  # Tags at launch on all three created resources: the IAM conditions below test
  # aws:RequestTag/Name on the instance, its root volume AND its ENI, and the watchdog
  # finds the box by the same tag. See AGENTS.md "Hopping between dev servers" for the
  # untagged-ENI trap this avoids.
  tag_specifications {
    resource_type = "instance"
    tags = {
      Name    = each.value
      Purpose = "on-demand GPU browser performance tests"
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      Name = each.value
      Role = "root"
    }
  }

  tag_specifications {
    resource_type = "network-interface"
    tags = {
      Name = each.value
    }
  }

  tags = { Name = each.value }

  user_data = base64encode(<<-INIT
    #!/bin/bash
    set -uxo pipefail

    # The box's own copy of the hard lifetime, armed first so nothing below can delay it.
    # With the template's shutdown behaviour this terminates the instance. It is only a
    # backup: user_data runs once, so a reboot disarms it; the watchdog's LaunchTime check
    # (parallel-box-watchdog.tf MAX_AGE_MINUTES) is the lifetime that always holds.
    shutdown -h +${var.gpu_box_max_hours * 60} "${each.value} lifetime (${var.gpu_box_max_hours} h) reached"

    # Authorize the dev-hop key (dev-hop-key.tf), the only key nextjs-dev holds that
    # reaches another host -- exactly as the parallel boxes do.
    install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
    touch /home/ubuntu/.ssh/authorized_keys
    grep -qxF "${trimspace(tls_private_key.dev_hop.public_key_openssh)}" /home/ubuntu/.ssh/authorized_keys || \
      echo "${trimspace(tls_private_key.dev_hop.public_key_openssh)}" >> /home/ubuntu/.ssh/authorized_keys
    chmod 600 /home/ubuntu/.ssh/authorized_keys
    chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys

    # Proof the GPU is there, where gbox status can read it.
    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader > /etc/gpu-box-ready 2>&1 || \
      echo "nvidia-smi failed" > /etc/gpu-box-ready
  INIT
  )
}

# The single template that existed before the slots is slot 1's, under the same name.
moved {
  from = aws_launch_template.gpu_box
  to   = aws_launch_template.gpu_box["1"]
}

# ---------------------------------------------------------------------------------
# What nextjs-dev may do: launch the four tagged slot boxes from their four templates, in one
# of four subnets, as one of four small GPU types -- and terminate them. The statement split
# follows parallel-box-launch.tf and exists for the same reason: a condition only holds
# for the resources that carry the key it tests, so created resources, the instance's
# type, and referenced resources each need their own statement.
# ---------------------------------------------------------------------------------
data "aws_iam_policy_document" "gpu_box_control" {
  # EVERY RunInstances statement below requires one of these templates. The role also holds
  # the parallel boxes' grant (parallel-box-launch.tf), and IAM checks each resource of a call
  # against the union of both, so without the pin a template-less call could mix this
  # policy's AMI and subnets with that one's tags, PassRole or security group. The
  # parallel-box statements carry the mirror-image pin.

  # The instance: tagged with a slot's name, one of the allowed types, default tenancy,
  # IMDSv2, no instance profile, only the template's own tag keys. ec2:InstanceType and friends exist
  # only on the instance resource, so they cannot share a statement with the volume/ENI.
  statement {
    sid       = "RunGpuBoxInstanceOnly"
    effect    = "Allow"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:instance/*"]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.gpu_box_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.gpu_box_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.gpu_box_tag_keys.instance
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:InstanceType"
      values   = local.gpu_box_types
    }

    # NotEquals rather than Equals: it holds whether or not EC2 fills ec2:Tenancy in when the
    # template leaves tenancy unset, and still refuses a dedicated or host override.
    condition {
      test     = "StringNotEquals"
      variable = "ec2:Tenancy"
      values   = ["dedicated", "host"]
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:MetadataHttpTokens"
      values   = ["required"]
    }

    condition {
      test     = "Null"
      variable = "ec2:InstanceProfile"
      values   = ["true"]
    }
  }

  # Its root volume: the template's shape (gp3, encrypted, at most 100 GB) -- a launch-time
  # block-device override could otherwise add a huge io2 disk that outlives the box.
  statement {
    sid       = "RunGpuBoxRootVolume"
    effect    = "Allow"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:volume/*"]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.gpu_box_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.gpu_box_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.gpu_box_tag_keys.volume
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:VolumeType"
      values   = ["gp3"]
    }

    condition {
      test     = "NumericLessThanEquals"
      variable = "ec2:VolumeSize"
      values   = ["100"]
    }

    condition {
      test     = "Bool"
      variable = "ec2:Encrypted"
      values   = ["true"]
    }
  }

  # Its ENI: tagged with a slot's name.
  statement {
    sid       = "RunGpuBoxEni"
    effect    = "Allow"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:network-interface/*"]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.gpu_box_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.gpu_box_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.gpu_box_tag_keys.eni
    }
  }

  # What the call references, pinned to exact ARNs: this AMI, these subnets, this
  # security group, these templates. No key pair (the key arrives through user_data) and
  # no instance profile.
  statement {
    sid     = "RunGpuBoxReferencedResources"
    effect  = "Allow"
    actions = ["ec2:RunInstances"]
    resources = concat(
      [
        "arn:aws:ec2:us-west-2::image/${var.gpu_box_ami}",
        "arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:security-group/${aws_security_group.gpu_box.id}",
      ],
      local.gpu_box_template_arns,
      [for s in local.gpu_box_subnets : "arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:subnet/${s}"],
    )

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.gpu_box_template_arns
    }
  }

  # Tagging at launch only; ec2:CreateAction keeps it from retagging anything existing
  # into scope of this or any other tag-based policy.
  statement {
    sid     = "TagGpuBoxAtLaunchOnly"
    effect  = "Allow"
    actions = ["ec2:CreateTags"]
    resources = [
      "arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:instance/*",
      "arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:volume/*",
      "arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:network-interface/*",
    ]

    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances"]
    }
  }

  # Down. Terminate only: there is no state on the box worth a stop/start.
  statement {
    sid       = "TerminateTaggedGpuBoxOnly"
    effect    = "Allow"
    actions   = ["ec2:TerminateInstances"]
    resources = ["arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:instance/*"]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Name"
      values   = local.gpu_box_names
    }
  }

  # gbox skips subnets whose AZ does not offer a type before it tries to launch there.
  # Read-only, and not scopable to resources.
  statement {
    sid       = "DescribeForGbox"
    effect    = "Allow"
    actions   = ["ec2:DescribeInstances", "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeSubnets"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = ["us-west-2"]
    }
  }
}

# The shared Next.js box, where the games and their browser tests live. As with the
# parallel boxes, an instance-role grant reaches every account there (they all have
# sudo); the bounds above -- four types, four slot tags, the watchdog, the lifetime -- are
# what make that acceptable.
#
# A MANAGED policy, not inline: a role's inline policies share one 10,240-character limit
# (nextjs-dev-role's use roughly 6.6K of it today), and this grant would take most of what
# is left. A managed policy has its own 6,144-character budget.
resource "aws_iam_policy" "gpu_box_control" {
  name        = "gpu-box-control"
  description = "nextjs-dev: launch and terminate the tagged on-demand GPU test boxes (gpu-box.tf)"
  policy      = data.aws_iam_policy_document.gpu_box_control.json
}

resource "aws_iam_role_policy_attachment" "nextjs_dev_gpu_box" {
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.gpu_box_control.arn
}

output "gpu_box_launch_templates" {
  description = "Launch template per gbox slot (terraform owns the config; gbox picks type and subnet)"
  value       = { for k, lt in aws_launch_template.gpu_box : k => lt.name }
}

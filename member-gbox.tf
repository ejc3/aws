# member-gbox.tf
#
# EACH MEMBER'S GBOX: one small NVIDIA Linux box per person in local.member_box_people
# (member-boxes.tf), "gbox-<person>", for measuring browser games on real graphics hardware,
# in us-west-2 like the owner's four (gpu-box.tf, unchanged). A member runs `gbox up` /
# `gbox down` / `gbox ssh` from their own account: scripts/gpu-box.sh takes no slot number for a
# member and acts on their box only. Nothing on it persists, as with the owner's.
#
# THE SAME SHAPE AS THE OWNER'S, with these differences:
#   - ONE SUBNET AND A FIXED PRIVATE ADDRESS (local.member_gbox_ips): the us-west-2a default
#     subnet, which offers all four types, in an explicit CIDR reservation EC2 never assigns
#     on its own. The owner's boxes hop across four AZs for capacity; a member's stays put so
#     its address can be named in advance;
#   - REACHED PRIVATELY: the dev fleet's route table sends the whole us-west-2 default VPC over
#     the I/O box peering (main.tf), and that VPC's main route table sends the dev fleet back
#     (io-box.tf), so nextjs-dev and nextjs-ct reach the private address directly. Its own
#     security group admits SSH from the dev fleet's subnets only, nothing public;
#   - the person's own SSH key (member-boxes.tf), not the dev-hop key;
#   - the box's lifetime backstop is a timer it re-arms at every boot (member-boxes.tf), not a
#     one-shot `shutdown -h`; the watchdog's LaunchTime lifetime (var.gpu_box_max_hours) and its
#     30 idle minutes apply exactly as to the owner's.

locals {
  member_gbox_slots  = { for p in local.member_box_people : p => "gbox-${p}" }
  member_gbox_names  = [for k in sort(keys(local.member_gbox_slots)) : local.member_gbox_slots[k]]
  member_gbox_region = "us-west-2"

  # us-west-2a, one of local.gpu_box_subnets.
  member_gbox_subnet_id = local.gpu_box_subnets[0]

  # The /28 at offset 4064 of the subnet's /20 (clear of the addresses EC2 hands out first and of
  # the last, reserved one); person i has the third address + i.
  member_gbox_reserved_cidr = cidrsubnet(data.aws_subnet.member_gbox.cidr_block, 8, 254)
  member_gbox_ips           = { for p, i in local.member_box_index : p => cidrhost(local.member_gbox_reserved_cidr, 2 + i) }

  member_gbox_tag_keys = {
    instance = ["Name", "Purpose"]
    volume   = ["Name", "Role"]
    eni      = ["Name"]
  }

  member_gbox_account       = data.aws_caller_identity.current.account_id
  member_gbox_instance_arn  = "arn:aws:ec2:${local.member_gbox_region}:${local.member_gbox_account}:instance/*"
  member_gbox_volume_arn    = "arn:aws:ec2:${local.member_gbox_region}:${local.member_gbox_account}:volume/*"
  member_gbox_eni_arn       = "arn:aws:ec2:${local.member_gbox_region}:${local.member_gbox_account}:network-interface/*"
  member_gbox_template_arns = [for k in sort(keys(local.member_gbox_slots)) : aws_launch_template.member_gbox[k].arn]
}

data "aws_subnet" "member_gbox" {
  provider = aws.west2
  id       = local.member_gbox_subnet_id
}

resource "aws_ec2_subnet_cidr_reservation" "member_gbox" {
  provider         = aws.west2
  subnet_id        = data.aws_subnet.member_gbox.id
  cidr_block       = local.member_gbox_reserved_cidr
  reservation_type = "explicit"
  description      = "Fixed private addresses of the members' gboxes (member-gbox.tf)"
}

# SSH from the dev fleet's private subnets only (over the I/O box peering); egress as the
# owner's GPU box: web, DNS and NTP, never NFS or anything else in the fleet.
resource "aws_security_group" "member_gbox" {
  provider    = aws.west2
  name_prefix = "member-gbox-"
  description = "Member GPU test boxes: SSH from the dev fleet subnets only"
  vpc_id      = data.aws_subnet.member_gbox.vpc_id

  ingress {
    description = "SSH from the dev fleet"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [for s in local.dev_fleet_subnets : s.cidr_block]
  }

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

  tags = { Name = "member-gbox" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_launch_template" "member_gbox" {
  provider               = aws.west2
  for_each               = local.member_gbox_slots
  name                   = each.value
  image_id               = var.gpu_box_ami
  update_default_version = true

  instance_initiated_shutdown_behavior = "terminate"

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = 100
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  # A public address for outbound traffic only (the security group admits the fleet alone).
  network_interfaces {
    subnet_id                   = local.member_gbox_subnet_id
    private_ip_address          = local.member_gbox_ips[each.key]
    security_groups             = [aws_security_group.member_gbox.id]
    associate_public_ip_address = true
    delete_on_termination       = true
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = each.value, Purpose = "a member's GPU browser performance tests" }
  }

  tag_specifications {
    resource_type = "volume"
    tags          = { Name = each.value, Role = "root" }
  }

  tag_specifications {
    resource_type = "network-interface"
    tags          = { Name = each.value }
  }

  tags = { Name = each.value }

  user_data = base64encode(<<-INIT
    #!/bin/bash
    set -uxo pipefail

    ${local.linux_ip_options_bootstrap}

    HOURS=${var.gpu_box_max_hours}
    ${local.member_box_deadline_setup}

    # This person's key and no other (member-boxes.tf).
    install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
    touch /home/ubuntu/.ssh/authorized_keys
    grep -qxF "${trimspace(tls_private_key.member_box[each.key].public_key_openssh)}" /home/ubuntu/.ssh/authorized_keys || \
      echo "${trimspace(tls_private_key.member_box[each.key].public_key_openssh)}" >> /home/ubuntu/.ssh/authorized_keys
    chmod 600 /home/ubuntu/.ssh/authorized_keys
    chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys

    nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader > /etc/gpu-box-ready 2>&1 || \
      echo "nvidia-smi failed" > /etc/gpu-box-ready
  INIT
  )

  depends_on = [aws_ec2_subnet_cidr_reservation.member_gbox]
}

# ============================================================ what nextjs-dev may do
# gpu-box-control's shape for the members' templates, names and the one subnet. Every resource
# is per person, so a session policy can narrow it to one.
data "aws_iam_policy_document" "member_gbox_control" {
  statement {
    sid       = "RunMemberGboxInstance"
    actions   = ["ec2:RunInstances"]
    resources = [local.member_gbox_instance_arn]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_gbox_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.member_gbox_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.member_gbox_tag_keys.instance
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:InstanceType"
      values   = local.gpu_box_types
    }

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

  statement {
    sid       = "RunMemberGboxRootVolume"
    actions   = ["ec2:RunInstances"]
    resources = [local.member_gbox_volume_arn]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_gbox_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.member_gbox_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.member_gbox_tag_keys.volume
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

  statement {
    sid       = "RunMemberGboxEni"
    actions   = ["ec2:RunInstances"]
    resources = [local.member_gbox_eni_arn]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_gbox_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.member_gbox_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.member_gbox_tag_keys.eni
    }

    # The ENI must be the template's own: a --network-interfaces override (another address, the
    # fixed one dropped) makes this false. AWS's example policies use exactly this key on
    # network-interface/* to forbid overriding the template's network interfaces.
    condition {
      test     = "Bool"
      variable = "ec2:IsLaunchTemplateResource"
      values   = ["true"]
    }
  }

  statement {
    sid     = "RunMemberGboxReferencedResources"
    actions = ["ec2:RunInstances"]
    resources = concat(
      [
        "arn:aws:ec2:${local.member_gbox_region}::image/${var.gpu_box_ami}",
        "arn:aws:ec2:${local.member_gbox_region}:${local.member_gbox_account}:subnet/${local.member_gbox_subnet_id}",
        "arn:aws:ec2:${local.member_gbox_region}:${local.member_gbox_account}:security-group/${aws_security_group.member_gbox.id}",
      ],
      local.member_gbox_template_arns,
    )

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_gbox_template_arns
    }
  }

  statement {
    sid       = "TagMemberGboxAtLaunchOnly"
    actions   = ["ec2:CreateTags"]
    resources = [local.member_gbox_instance_arn, local.member_gbox_volume_arn, local.member_gbox_eni_arn]

    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances"]
    }
  }

  statement {
    sid       = "TerminateTaggedMemberGboxOnly"
    actions   = ["ec2:TerminateInstances"]
    resources = [local.member_gbox_instance_arn]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Name"
      values   = local.member_gbox_names
    }
  }

  statement {
    sid       = "ReadMemberBoxSshKeys"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.member_box_ssh_arns
  }

  statement {
    sid       = "DescribeForMemberGbox"
    actions   = ["ec2:DescribeInstances"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.member_gbox_region]
    }
  }
}

resource "aws_iam_policy" "member_gbox_control" {
  name        = "member-gbox-control"
  description = "nextjs-dev: launch and terminate the members' own GPU test boxes (member-gbox.tf)"
  policy      = data.aws_iam_policy_document.member_gbox_control.json
}

resource "aws_iam_role_policy_attachment" "member_gbox_control" {
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.member_gbox_control.arn
}

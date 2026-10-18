# member-pbox.tf
#
# EACH MEMBER'S PBOX: one many-core Graviton spot box per person in local.member_box_people
# (member-boxes.tf), "pbox-<person>", with a persistent 100 GB work volume of its own, in Ohio
# next to the owner's parallel boxes (ohio-pbox.tf: the VPC, subnet, security group and key
# pair are shared; nothing of the owner's four slots changes). A member runs `pbox up` /
# `pbox down` / `pbox ssh` from their own account: scripts/parallel-box.sh takes no slot number
# for a member and acts on their box only.
#
# THE SAME SHAPE AS THE OWNER'S (parallel-box-launch.tf): terraform owns the durable half, the
# script launches from the person's template with a tag-scoped grant and attaches the person's
# volume. Differences, all on the side of less:
#   - no instance profile: the owner's boxes boot with dev-ebs-only (DevEBS volumes); a member's
#     box needs no AWS access, so there is no role to pass;
#   - no NFS scratch: /srv/io is the owner's shared I/O box, and one person's box must not reach
#     data another's job wrote there;
#   - no fallback sites: the box has a fixed address and a home volume, nothing else;
#   - the person's own SSH key (member-boxes.tf), not the dev-hop key;
#   - spot only, IMDSv2, an encrypted root, and only the instance types in
#     local.member_pbox_types (the script's default list), all enforced by the grant;
#   - a hard lifetime of local.member_box_max_hours, enforced twice: the Ohio watchdog by
#     LaunchTime, and the box's own deadline timer. The owner's boxes have none (a long job is
#     their point); a member's also ends after 30 idle minutes, as the owner's do.
#
# ITS OWN SUBNET, 10.12.2.0/24 in the pbox VPC, with a route table that has the internet and
# nothing else: no route to the I/O box's peering and not in local.io_box_nfs_client_cidrs, so
# root on a member's box cannot reach the owner's NFS scratch (the owner's subnet, 10.12.1.0/24,
# has both). Its address is fixed per person (local.member_pbox_ips, an explicit reservation),
# but nothing in us-west-1 routes to Ohio, so the box is reached at its public address, exactly
# as the owner's are (SSH only, key only). A private path from nextjs-dev / nextjs-ct would be a
# new VPC peering, an owner call.

locals {
  member_pbox_slots  = { for p in local.member_box_people : p => "pbox-${p}" }
  member_pbox_names  = [for k in sort(keys(local.member_pbox_slots)) : local.member_pbox_slots[k]]
  member_pbox_region = "us-east-2" # the aws.ohio provider, where the shared pbox network is

  member_pbox_volume_size = 100
  member_pbox_root_size   = 30

  # The default TYPES list of scripts/parallel-box.sh (192 and 96 cores, Graviton, virtualized);
  # the grant allows exactly these. No metal: `pbox up kvm` is the owner's.
  member_pbox_types = [
    "c8g.48xlarge", "c8gb.48xlarge", "c8gd.48xlarge", "c8gn.48xlarge", "c9g.48xlarge", "c9gd.48xlarge", "m8g.48xlarge",
    "m8gd.48xlarge", "m9g.48xlarge", "m9gd.48xlarge", "i8g.48xlarge", "i8ge.48xlarge", "r8g.48xlarge", "r8gd.48xlarge",
    "c8g.24xlarge", "c8gb.24xlarge", "c8gd.24xlarge", "c8gn.24xlarge", "c9g.24xlarge", "c9gd.24xlarge", "m8g.24xlarge",
    "m8gd.24xlarge", "m9g.24xlarge", "m9gd.24xlarge", "i8g.24xlarge", "i8ge.24xlarge", "r8g.24xlarge", "r8gd.24xlarge",
  ]

  # .8/29 of the members' own pbox subnet, explicit; person i has .10 + i.
  member_pbox_reserved_cidr = cidrsubnet(aws_subnet.member_pbox.cidr_block, 5, 1)
  member_pbox_ips           = { for p, i in local.member_box_index : p => cidrhost(local.member_pbox_reserved_cidr, 2 + i) }

  member_pbox_tag_keys = {
    instance = ["Name", "Purpose"]
    volume   = ["Name", "Role"]
    eni      = ["Name"]
  }

  member_pbox_account       = data.aws_caller_identity.current.account_id
  member_pbox_instance_arn  = "arn:aws:ec2:${local.member_pbox_region}:${local.member_pbox_account}:instance/*"
  member_pbox_volume_arn    = "arn:aws:ec2:${local.member_pbox_region}:${local.member_pbox_account}:volume/*"
  member_pbox_eni_arn       = "arn:aws:ec2:${local.member_pbox_region}:${local.member_pbox_account}:network-interface/*"
  member_pbox_template_arns = [for k in sort(keys(local.member_pbox_slots)) : aws_launch_template.member_pbox[k].arn]
  member_pbox_work_arns     = [for k in sort(keys(local.member_pbox_slots)) : aws_ebs_volume.member_pbox_work[k].arn]
}

resource "aws_subnet" "member_pbox" {
  provider                = aws.ohio
  vpc_id                  = aws_vpc.ohio_pbox.id
  cidr_block              = "10.12.2.0/24"
  availability_zone       = local.ohio_pbox_az # the work volumes' AZ
  map_public_ip_on_launch = false              # the template asks for one, for outbound traffic

  tags = { Name = "member-pbox-subnet-ohio" }
}

# The internet and the VPC's implicit local route, nothing else. Never add the I/O box peer route,
# and never add this subnet to local.io_box_nfs_client_cidrs. No IPv6 on the subnet, so there is no
# v6 default route to need (the owner's subnet has both because it assigns v6 addresses).
resource "aws_route_table" "member_pbox" {
  provider = aws.ohio
  vpc_id   = aws_vpc.ohio_pbox.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.ohio_pbox.id
  }

  tags = { Name = "member-pbox-rt-ohio" }
}

resource "aws_route_table_association" "member_pbox" {
  provider       = aws.ohio
  subnet_id      = aws_subnet.member_pbox.id
  route_table_id = aws_route_table.member_pbox.id
}

resource "aws_ec2_subnet_cidr_reservation" "member_pbox" {
  provider         = aws.ohio
  subnet_id        = aws_subnet.member_pbox.id
  cidr_block       = local.member_pbox_reserved_cidr
  reservation_type = "explicit"
  description      = "Fixed private addresses of the members' pboxes (member-pbox.tf)"
}

# One per person, in the shared subnet's AZ (EBS is AZ-locked). Losing one loses that person's work.
resource "aws_ebs_volume" "member_pbox_work" {
  provider          = aws.ohio
  for_each          = local.member_pbox_slots
  availability_zone = local.ohio_pbox_az
  size              = local.member_pbox_volume_size
  type              = "gp3"
  encrypted         = true

  tags = { Name = "${each.value}-work", Purpose = "persistent scratch for a member's many-core box" }

  lifecycle {
    prevent_destroy = true
  }
}

locals {
  # The boot script: the lifetime backstop first, the person's key, then the work volume, found
  # by its EBS serial and formatted only when it holds no filesystem at all (the owner's boxes'
  # rule, parallel-box-launch.tf).
  member_pbox_user_data = {
    for p, name in local.member_pbox_slots : p => <<-INIT
      #!/bin/bash
      set -uxo pipefail

      HOURS=${local.member_box_max_hours}
      ${local.member_box_deadline_setup}

      # This person's key and no other (member-boxes.tf); EC2 adds the admin key pair's.
      install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
      touch /home/ubuntu/.ssh/authorized_keys
      grep -qxF "${trimspace(tls_private_key.member_box[p].public_key_openssh)}" /home/ubuntu/.ssh/authorized_keys || \
        echo "${trimspace(tls_private_key.member_box[p].public_key_openssh)}" >> /home/ubuntu/.ssh/authorized_keys
      chmod 600 /home/ubuntu/.ssh/authorized_keys
      chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys

      # The work volume, attached by `pbox up` after the instance is running.
      SERIAL=$(echo "${aws_ebs_volume.member_pbox_work[p].id}" | tr -d '-')
      DEV=""
      for _ in $(seq 1 60); do
        DEV=$(lsblk -dn -o NAME,SERIAL 2>/dev/null | awk -v s="$SERIAL" '$2==s {print $1}' | head -1)
        [ -n "$DEV" ] && break
        sleep 2
      done
      if [ -z "$DEV" ]; then
        echo "FATAL: the work volume is not attached; refusing to format anything" >&2
        exit 1
      fi
      DEV="/dev/$DEV"
      if ! blkid "$DEV" >/dev/null 2>&1; then
        mkfs.ext4 -L parallel-work "$DEV"
      fi
      mkdir -p /mnt/work
      mount "$DEV" /mnt/work
      chown ubuntu:ubuntu /mnt/work
      grep -q "$DEV" /etc/fstab || echo "$DEV /mnt/work ext4 defaults,nofail 0 2" >> /etc/fstab

      export DEBIAN_FRONTEND=noninteractive
      apt-get update
      apt-get install -y parallel build-essential git htop
      echo "* soft nofile 1048576" >> /etc/security/limits.conf
      echo "* hard nofile 1048576" >> /etc/security/limits.conf
      echo "${name} ready: $(nproc) cores, /mnt/work mounted"
    INIT
  }
}

resource "aws_launch_template" "member_pbox" {
  provider = aws.ohio
  for_each = local.member_pbox_slots

  name                   = each.value
  image_id               = var.parallel_box_ami_ohio
  key_name               = aws_key_pair.ohio_pbox.key_name
  update_default_version = true

  # The lifetime timer powers the box off; that must end the instance.
  instance_initiated_shutdown_behavior = "terminate"

  instance_market_options {
    market_type = "spot"
    spot_options {
      spot_instance_type             = "one-time"
      instance_interruption_behavior = "terminate"
    }
  }

  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = local.member_pbox_root_size
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  network_interfaces {
    subnet_id                   = aws_subnet.member_pbox.id
    private_ip_address          = local.member_pbox_ips[each.key]
    security_groups             = [aws_security_group.ohio_pbox.id]
    associate_public_ip_address = true
    delete_on_termination       = true
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = each.value, Purpose = "a member's many-core box" }
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

  user_data = base64encode(local.member_pbox_user_data[each.key])

  depends_on = [aws_ec2_subnet_cidr_reservation.member_pbox, aws_route_table_association.member_pbox]
}

# ============================================================ what nextjs-dev may do
# The owner's parallel-box-control, narrowed: these templates, these names, the listed types,
# spot, no instance profile, the person's own volume and key. Every RunInstances statement pins
# these templates (the role holds the other box grants too, and IAM checks each resource of a
# call against the union). Every resource is per person, so a session policy can narrow it.
data "aws_iam_policy_document" "member_pbox_control" {
  statement {
    sid       = "RunMemberPboxInstance"
    actions   = ["ec2:RunInstances"]
    resources = [local.member_pbox_instance_arn]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_pbox_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.member_pbox_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.member_pbox_tag_keys.instance
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:InstanceType"
      values   = local.member_pbox_types
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

    condition {
      test     = "StringEquals"
      variable = "ec2:InstanceMarketType"
      values   = ["spot"]
    }
  }

  statement {
    sid       = "RunMemberPboxRootVolume"
    actions   = ["ec2:RunInstances"]
    resources = [local.member_pbox_volume_arn]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_pbox_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.member_pbox_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.member_pbox_tag_keys.volume
    }

    condition {
      test     = "StringEquals"
      variable = "ec2:VolumeType"
      values   = ["gp3"]
    }

    condition {
      test     = "NumericLessThanEquals"
      variable = "ec2:VolumeSize"
      values   = [tostring(local.member_pbox_root_size)]
    }

    condition {
      test     = "Bool"
      variable = "ec2:Encrypted"
      values   = ["true"]
    }
  }

  statement {
    sid       = "RunMemberPboxEni"
    actions   = ["ec2:RunInstances"]
    resources = [local.member_pbox_eni_arn]

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_pbox_template_arns
    }

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.member_pbox_names
    }

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "aws:TagKeys"
      values   = local.member_pbox_tag_keys.eni
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
    sid     = "RunMemberPboxReferencedResources"
    actions = ["ec2:RunInstances"]
    resources = concat(
      [
        "arn:aws:ec2:${local.member_pbox_region}::image/${var.parallel_box_ami_ohio}",
        "arn:aws:ec2:${local.member_pbox_region}:${local.member_pbox_account}:subnet/${aws_subnet.member_pbox.id}",
        "arn:aws:ec2:${local.member_pbox_region}:${local.member_pbox_account}:security-group/${aws_security_group.ohio_pbox.id}",
        "arn:aws:ec2:${local.member_pbox_region}:${local.member_pbox_account}:key-pair/${aws_key_pair.ohio_pbox.key_name}",
      ],
      local.member_pbox_template_arns,
    )

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.member_pbox_template_arns
    }
  }

  statement {
    sid       = "TagMemberPboxAtLaunchOnly"
    actions   = ["ec2:CreateTags"]
    resources = [local.member_pbox_instance_arn, local.member_pbox_volume_arn, local.member_pbox_eni_arn]

    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances"]
    }
  }

  statement {
    sid       = "TerminateTaggedMemberPboxOnly"
    actions   = ["ec2:TerminateInstances"]
    resources = [local.member_pbox_instance_arn]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Name"
      values   = local.member_pbox_names
    }
  }

  # The work volumes by identity, to an instance tagged as a member pbox. No DetachVolume:
  # terminating detaches it.
  statement {
    sid       = "AttachMemberPboxWorkVolume"
    actions   = ["ec2:AttachVolume"]
    resources = local.member_pbox_work_arns
  }

  statement {
    sid       = "AttachToTaggedMemberPboxOnly"
    actions   = ["ec2:AttachVolume"]
    resources = [local.member_pbox_instance_arn]

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Name"
      values   = local.member_pbox_names
    }
  }

  statement {
    sid       = "ReadMemberBoxSshKeys"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = local.member_box_ssh_arns
  }

  statement {
    sid = "DescribeForMemberPbox"
    actions = [
      "ec2:DescribeInstances",
      "ec2:DescribeVolumes",
      "ec2:DescribeSpotPriceHistory",
      "ec2:DescribeInstanceTypes",
      "ec2:DescribeLaunchTemplateVersions",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [local.member_pbox_region]
    }
  }
}

resource "aws_iam_policy" "member_pbox_control" {
  name        = "member-pbox-control"
  description = "nextjs-dev: launch, attach the work volume of, and terminate the members' own many-core spot boxes (member-pbox.tf)"
  policy      = data.aws_iam_policy_document.member_pbox_control.json
}

resource "aws_iam_role_policy_attachment" "member_pbox_control" {
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.member_pbox_control.arn
}

output "member_pbox_work_volumes" {
  description = "Persistent work volume per member pbox (survives every instance), in us-east-2"
  value       = { for k, v in aws_ebs_volume.member_pbox_work : k => v.id }
}

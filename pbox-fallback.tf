# pbox-fallback.tf
#
# FALLBACK SITES for the parallel boxes: where `pbox up N FLAVOR --fallback` (scripts/parallel-box.sh) launches when the home AZ
# (ohio-pbox.tf, local.ohio_pbox_az) has no capacity for any instance type of the flavor. One site per US region
# (modules/pbox-fallback-site), covering every AZ of that region except the home AZ.
#
# A FALLBACK BOX STARTS WITH AN EMPTY WORK VOLUME. The persistent work volumes are AZ-locked at home and are not copied
# here (the owner's call: "Fresh is fine"). The launch template gives the box a fresh gp3 /mnt/work that is created and
# deleted with it, so nothing on a fallback box outlives it, and the shared NFS scratch (/srv/io, us-west-2) is not
# mounted: there is no peering to these VPCs.
#
# Plain `pbox up N FLAVOR` does not use any of this. It launches at home as before and, when the home AZ is out of capacity,
# exits with a distinct status that names --fallback, so the caller (often an agent) decides whether a box with an empty
# disk somewhere else is acceptable.
#
# The IAM for these launches is its own managed policy (parallel-box-fallback): the home policy is near the 6,144
# characters a managed policy may hold once a second region is in it (parallel-box-launch.tf).

locals {
  # Region => the site's /16 (not 10.0, 10.1, 10.11 or 10.12, the other VPCs) and the AZs a box may launch in.
  pbox_fallback_regions = {
    "us-east-1" = { cidr = "10.13.0.0/16", azs = ["us-east-1a", "us-east-1b", "us-east-1c", "us-east-1d", "us-east-1e", "us-east-1f"] }
    "us-east-2" = { cidr = "10.14.0.0/16", azs = [for az in ["us-east-2a", "us-east-2b", "us-east-2c"] : az if az != local.ohio_pbox_az] }
    "us-west-1" = { cidr = "10.15.0.0/16", azs = ["us-west-1a", "us-west-1c"] }
    "us-west-2" = { cidr = "10.16.0.0/16", azs = ["us-west-2a", "us-west-2b", "us-west-2c", "us-west-2d"] }
  }

  # The same Ubuntu build as var.parallel_box_ami_ohio, by name, so every region boots the same image.
  pbox_fallback_ami_name = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-20260923"

  # Every box launches from the site's one template, named by the caller. The fresh work disk is as big as the largest
  # home volume: it exists only while the box does, so the size costs cents.
  pbox_fallback_template_name = "parallel-box-fallback"
  pbox_fallback_work_size     = max([for v in values(local.ohio_pbox_volumes) : v.size]...)

  # The boot script of a fallback box: the home one (parallel-box-launch.tf) without the persistent volume and the NFS
  # scratch. The work disk is the launch template's second EBS volume, found as the EBS disk that does not hold /, and
  # it is new, so the mkfs guard (format only a disk with no filesystem at all) always formats it.
  pbox_fallback_user_data = base64encode(<<-INIT
    #!/bin/bash
    set -uxo pipefail

    # Authorize the dev-hop key (dev-hop-key.tf), as on the home boxes.
    install -d -m 700 -o ubuntu -g ubuntu /home/ubuntu/.ssh
    touch /home/ubuntu/.ssh/authorized_keys
    grep -qxF "${trimspace(tls_private_key.dev_hop.public_key_openssh)}" /home/ubuntu/.ssh/authorized_keys || \
      echo "${trimspace(tls_private_key.dev_hop.public_key_openssh)}" >> /home/ubuntu/.ssh/authorized_keys
    chmod 600 /home/ubuntu/.ssh/authorized_keys
    chown ubuntu:ubuntu /home/ubuntu/.ssh/authorized_keys

    # The fresh work volume: the EBS disk that is not the root disk. Instance-store NVMe (the *gd types) has another
    # model name and is left alone.
    ROOT_DISK=$(lsblk -no PKNAME "$(findmnt -no SOURCE /)")
    DEV=""
    for _ in $(seq 1 60); do
      DEV=$(lsblk -dn -o NAME,MODEL | awk -v r="$ROOT_DISK" '$1 != r && /Amazon Elastic Block Store/ {print $1}' | head -1)
      [ -n "$DEV" ] && break
      sleep 2
    done
    if [ -z "$DEV" ]; then
      echo "FATAL: no work volume found besides the root disk; refusing to format anything" >&2
      exit 1
    fi
    DEV="/dev/$DEV"
    if ! blkid "$DEV" >/dev/null 2>&1; then
      echo "fresh work volume $DEV -- creating ext4"
      mkfs.ext4 -L parallel-work "$DEV"
    else
      echo "$DEV already has a filesystem -- mounting as-is, NOT formatting"
    fi
    mkdir -p /mnt/work
    mount "$DEV" /mnt/work
    chown ubuntu:ubuntu /mnt/work
    grep -q "$DEV" /etc/fstab || echo "$DEV /mnt/work ext4 defaults,nofail 0 2" >> /etc/fstab
    echo "fallback site: /mnt/work is a fresh volume that is deleted with this box; /srv/io is not mounted" > /mnt/work/FALLBACK-BOX-README

    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y parallel build-essential git htop
    ${local.pbox_local_nvme_setup}
    ${local.pbox_gpu_vfio_setup}

    echo "* soft nofile 1048576" >> /etc/security/limits.conf
    echo "* hard nofile 1048576" >> /etc/security/limits.conf

    echo "parallel-box ready (fallback site): $(nproc) cores, fresh /mnt/work mounted"
  INIT
  )

  pbox_fallback_common = {
    ami_name              = local.pbox_fallback_ami_name
    template_name         = local.pbox_fallback_template_name
    work_size             = local.pbox_fallback_work_size
    user_data_b64         = local.pbox_fallback_user_data
    instance_profile_name = aws_iam_instance_profile.dev_ebs_only.name
    spot                  = var.parallel_box_spot
    key_name              = "fcvm-ec2-pbox-fallback"
    public_key            = aws_key_pair.ohio_pbox.public_key
    flow_log_destination  = "${aws_s3_bucket.security_audit.arn}/vpc-flow"
  }

  # A watchdog runs in us-east-1 and us-west-1 only: us-west-2 has parallel-box-watchdog.tf's, which already watches
  # these names, and us-east-2 has ohio-pbox.tf's.
  pbox_fallback_watchdog = {
    enabled  = true
    role_arn = aws_iam_role.parallel_watchdog.arn
    zip_path = data.archive_file.parallel_watchdog.output_path
    zip_hash = data.archive_file.parallel_watchdog.output_base64sha256
    environment = {
      IDLE_MINUTES  = tostring(var.parallel_box_idle_minutes)
      IDLE_CPU_PCT  = tostring(var.parallel_box_idle_cpu_pct)
      TAG_NAMES     = join(",", local.parallel_box_names)
      IO_THRESHOLDS = local.parallel_watchdog_io_thresholds
      SNS_REGION    = var.aws_region
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
    }
  }
  pbox_no_watchdog = merge(local.pbox_fallback_watchdog, { enabled = false })
}

module "pbox_fallback_us_east_1" {
  source                = "./modules/pbox-fallback-site"
  providers             = { aws = aws.dr }
  name                  = "us-east-1"
  cidr_block            = local.pbox_fallback_regions["us-east-1"].cidr
  availability_zones    = local.pbox_fallback_regions["us-east-1"].azs
  ami_name              = local.pbox_fallback_common.ami_name
  template_name         = local.pbox_fallback_common.template_name
  work_size             = local.pbox_fallback_common.work_size
  user_data_b64         = local.pbox_fallback_common.user_data_b64
  instance_profile_name = local.pbox_fallback_common.instance_profile_name
  spot                  = local.pbox_fallback_common.spot
  key_name              = local.pbox_fallback_common.key_name
  public_key            = local.pbox_fallback_common.public_key
  flow_log_destination  = local.pbox_fallback_common.flow_log_destination
  watchdog              = local.pbox_fallback_watchdog
  depends_on            = [aws_s3_bucket_policy.security_audit]
}

module "pbox_fallback_us_east_2" {
  source                = "./modules/pbox-fallback-site"
  providers             = { aws = aws.ohio }
  name                  = "us-east-2"
  cidr_block            = local.pbox_fallback_regions["us-east-2"].cidr
  availability_zones    = local.pbox_fallback_regions["us-east-2"].azs
  ami_name              = local.pbox_fallback_common.ami_name
  template_name         = local.pbox_fallback_common.template_name
  work_size             = local.pbox_fallback_common.work_size
  user_data_b64         = local.pbox_fallback_common.user_data_b64
  instance_profile_name = local.pbox_fallback_common.instance_profile_name
  spot                  = local.pbox_fallback_common.spot
  key_name              = local.pbox_fallback_common.key_name
  public_key            = local.pbox_fallback_common.public_key
  flow_log_destination  = local.pbox_fallback_common.flow_log_destination
  watchdog              = local.pbox_no_watchdog
  depends_on            = [aws_s3_bucket_policy.security_audit]
}

module "pbox_fallback_us_west_1" {
  source                = "./modules/pbox-fallback-site"
  name                  = "us-west-1"
  cidr_block            = local.pbox_fallback_regions["us-west-1"].cidr
  availability_zones    = local.pbox_fallback_regions["us-west-1"].azs
  ami_name              = local.pbox_fallback_common.ami_name
  template_name         = local.pbox_fallback_common.template_name
  work_size             = local.pbox_fallback_common.work_size
  user_data_b64         = local.pbox_fallback_common.user_data_b64
  instance_profile_name = local.pbox_fallback_common.instance_profile_name
  spot                  = local.pbox_fallback_common.spot
  key_name              = local.pbox_fallback_common.key_name
  public_key            = local.pbox_fallback_common.public_key
  flow_log_destination  = local.pbox_fallback_common.flow_log_destination
  watchdog              = local.pbox_fallback_watchdog
  depends_on            = [aws_s3_bucket_policy.security_audit]
}

module "pbox_fallback_us_west_2" {
  source                = "./modules/pbox-fallback-site"
  providers             = { aws = aws.west2 }
  name                  = "us-west-2"
  cidr_block            = local.pbox_fallback_regions["us-west-2"].cidr
  availability_zones    = local.pbox_fallback_regions["us-west-2"].azs
  ami_name              = local.pbox_fallback_common.ami_name
  template_name         = local.pbox_fallback_common.template_name
  work_size             = local.pbox_fallback_common.work_size
  user_data_b64         = local.pbox_fallback_common.user_data_b64
  instance_profile_name = local.pbox_fallback_common.instance_profile_name
  spot                  = local.pbox_fallback_common.spot
  key_name              = local.pbox_fallback_common.key_name
  public_key            = local.pbox_fallback_common.public_key
  flow_log_destination  = local.pbox_fallback_common.flow_log_destination
  watchdog              = local.pbox_no_watchdog
  depends_on            = [aws_s3_bucket_policy.security_audit]
}

locals {
  pbox_fallback_sites = {
    "us-east-1" = module.pbox_fallback_us_east_1
    "us-east-2" = module.pbox_fallback_us_east_2
    "us-west-1" = module.pbox_fallback_us_west_1
    "us-west-2" = module.pbox_fallback_us_west_2
  }

  # What `pbox` needs to launch at a fallback site, published in /infra/parallel-box (ohio-pbox.tf): the template name,
  # and region => AZ => subnet.
  pbox_fallback_pointer = {
    template = local.pbox_fallback_template_name
    regions  = { for r, s in local.pbox_fallback_sites : r => { subnets = s.subnets } }
  }

  pbox_fallback_template_arns = [for s in values(local.pbox_fallback_sites) : s.template_arn]

  # The resources a launch creates, in any US region: the launch-template pin (only the four fallback templates, which
  # exist only at the fallback sites) is what confines them, and listing each region separately would cost about a
  # thousand of the policy's 6,144 characters.
  pbox_fallback_created_arns = [
    "arn:aws:ec2:us-*:${local.parallel_box_account}:instance/*",
    "arn:aws:ec2:us-*:${local.parallel_box_account}:volume/*",
    "arn:aws:ec2:us-*:${local.parallel_box_account}:network-interface/*",
  ]
  pbox_fallback_instance_arns = ["arn:aws:ec2:us-*:${local.parallel_box_account}:instance/*"]
  pbox_fallback_referenced_arns = flatten([
    for r, s in local.pbox_fallback_sites : concat(
      [
        "arn:aws:ec2:${r}::image/${s.ami_id}",
        "arn:aws:ec2:${r}:${local.parallel_box_account}:security-group/${s.security_group_id}",
        "arn:aws:ec2:${r}:${local.parallel_box_account}:key-pair/${s.key_name}",
      ],
      [for id in values(s.subnets) : "arn:aws:ec2:${r}:${local.parallel_box_account}:subnet/${id}"],
    )
  ])
}

# ---------------------------------------------------------------------------------
# What a dev box may do at the fallback sites: the home policy's shape (parallel-box-launch.tf), for these templates.
# There is no volume attach (the work disk comes with the launch) and no PassRole (the home policy's grant of the one
# role, to EC2 only, covers every region).
# ---------------------------------------------------------------------------------
data "aws_iam_policy_document" "parallel_box_fallback" {
  # The instance and its volumes and ENI: only with one of the box names, and only through a fallback template.
  statement {
    sid       = "RunTaggedFallbackBoxOnly"
    effect    = "Allow"
    actions   = ["ec2:RunInstances"]
    resources = local.pbox_fallback_created_arns

    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Name"
      values   = local.parallel_box_names
    }

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.pbox_fallback_template_arns
    }
  }

  # The AMI, subnets, security group and key pair a launch references, by exact ARN.
  statement {
    sid       = "RunFallbackReferencedResources"
    effect    = "Allow"
    actions   = ["ec2:RunInstances"]
    resources = concat(local.pbox_fallback_referenced_arns, local.pbox_fallback_template_arns)

    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = local.pbox_fallback_template_arns
    }
  }

  statement {
    sid       = "TagFallbackAtLaunchOnly"
    effect    = "Allow"
    actions   = ["ec2:CreateTags"]
    resources = local.pbox_fallback_created_arns

    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances"]
    }
  }

  statement {
    sid    = "ManageTaggedFallbackBoxOnly"
    effect = "Allow"
    actions = [
      "ec2:TerminateInstances",
      "ec2:StopInstances",
      "ec2:StartInstances",
    ]
    resources = local.pbox_fallback_instance_arns

    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Name"
      values   = local.parallel_box_names
    }
  }
}

resource "aws_iam_policy" "parallel_box_fallback" {
  name   = "parallel-box-fallback"
  policy = data.aws_iam_policy_document.parallel_box_fallback.json
}

resource "aws_iam_role_policy_attachment" "dev_server_parallel_box_fallback" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.parallel_box_fallback.arn
}

resource "aws_iam_role_policy_attachment" "nextjs_dev_parallel_box_fallback" {
  role       = aws_iam_role.nextjs_dev.name
  policy_arn = aws_iam_policy.parallel_box_fallback.arn
}

# us-west-1 already runs a watchdog under exactly these names, left from the July 2026 parallel box and in no state.
# Adopt it rather than collide with it (CreateFunction would fail, and PutRule would silently overwrite its rule):
# the apply then gives it this module's code, environment and the shared role.
import {
  to = module.pbox_fallback_us_west_1.aws_lambda_function.watchdog[0]
  id = "parallel-box-watchdog"
}

import {
  to = module.pbox_fallback_us_west_1.aws_cloudwatch_event_rule.watchdog[0]
  id = "parallel-box-watchdog"
}

import {
  to = module.pbox_fallback_us_west_1.aws_cloudwatch_event_target.watchdog[0]
  id = "parallel-box-watchdog/parallel-box-watchdog"
}

import {
  to = module.pbox_fallback_us_west_1.aws_lambda_permission.watchdog[0]
  id = "parallel-box-watchdog/AllowExecutionFromCloudWatch"
}

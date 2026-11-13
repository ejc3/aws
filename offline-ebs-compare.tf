# Opt-in lab for two exact detached recovery volumes in the staging recovery region.
# The lab owns only its isolated network, analyzer and attachments; the evidence
# volumes are data sources and remain intact when the lab is removed.
variable "offline_ebs_compare" {
  description = "One-shot offline comparison of two tagged encrypted recovery volumes in the staging recovery account. Null keeps the lab absent."
  type = object({
    incident        = string
    pre_volume      = string
    post_volume     = string
    volume_size_gib = number
    window_start    = string
    window_end      = string
    hostname_sha256 = optional(string)
  })
  default = null

  validation {
    condition = var.offline_ebs_compare == null ? true : (
      can(regex("^[a-z0-9][a-z0-9-]{2,31}$", var.offline_ebs_compare.incident)) &&
      can(regex("^vol-[a-f0-9]{17}$", var.offline_ebs_compare.pre_volume)) &&
      can(regex("^vol-[a-f0-9]{17}$", var.offline_ebs_compare.post_volume)) &&
      var.offline_ebs_compare.pre_volume != var.offline_ebs_compare.post_volume &&
      var.offline_ebs_compare.volume_size_gib >= 100 &&
      var.offline_ebs_compare.volume_size_gib <= 2048 &&
      floor(var.offline_ebs_compare.volume_size_gib) == var.offline_ebs_compare.volume_size_gib &&
      can(regex("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$", var.offline_ebs_compare.window_start)) &&
      can(regex("^\\d{4}-\\d{2}-\\d{2}T\\d{2}:\\d{2}:\\d{2}Z$", var.offline_ebs_compare.window_end)) &&
      try(timecmp(var.offline_ebs_compare.window_start, var.offline_ebs_compare.window_end) < 0, false) &&
      try(timecmp(timeadd(var.offline_ebs_compare.window_start, "24h"), var.offline_ebs_compare.window_end) >= 0, false) &&
      (var.offline_ebs_compare.hostname_sha256 == null ? true : can(regex("^[a-f0-9]{64}$", var.offline_ebs_compare.hostname_sha256)))
    )
    error_message = "Specify a safe incident label, two distinct volume IDs, integral 100–2048 GiB size, UTC window no longer than 24 hours, and optionally a lowercase hostname SHA-256."
  }
}

locals {
  offline_ebs_compare_enabled = var.offline_ebs_compare != null
  offline_ebs_compare_volumes = local.offline_ebs_compare_enabled ? {
    before = var.offline_ebs_compare.pre_volume
    after  = var.offline_ebs_compare.post_volume
  } : {}
  offline_ebs_compare_userdata = local.offline_ebs_compare_enabled ? templatefile("${path.module}/scripts/offline-ebs-compare-boot.sh.tftpl", {
    analysis     = file("${path.module}/scripts/offline-ebs-compare.py")
    pre_volume   = var.offline_ebs_compare.pre_volume
    post_volume  = var.offline_ebs_compare.post_volume
    volume_bytes = var.offline_ebs_compare.volume_size_gib * 1073741824
    config_base64 = base64encode(jsonencode({
      window_start_utc = var.offline_ebs_compare.window_start
      window_end_utc   = var.offline_ebs_compare.window_end
      hostname_sha256  = var.offline_ebs_compare.hostname_sha256
    }))
  }) : null
  offline_ebs_compare_tags = { Purpose = "offline-ebs-compare" }
}

# This is the official Ubuntu 24.04 ARM64 server AMI in us-east-1. It is
# pinned to a reviewed image and checked against Canonical's owner.
data "aws_ami" "offline_ebs_compare" {
  provider = aws.staging_dr
  count    = local.offline_ebs_compare_enabled ? 1 : 0
  owners   = ["099720109477"]
  filter {
    name   = "image-id"
    values = ["ami-0f6435163a86ffc8c"]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

data "aws_ebs_volume" "offline_ebs_compare" {
  provider = aws.staging_dr
  for_each = local.offline_ebs_compare_volumes
  filter {
    name   = "volume-id"
    values = [each.value]
  }
  filter {
    # On the only enabling plan, reject anything already attached. For
    # cleanup set the input back to null; these lookups are then absent.
    name   = "status"
    values = ["available"]
  }
  filter {
    name   = "tag:Incident"
    values = [var.offline_ebs_compare.incident]
  }
  filter {
    name   = "tag:EvidencePhase"
    values = [each.key]
  }
}

resource "aws_vpc" "offline_ebs_compare" {
  provider                         = aws.staging_dr
  count                            = local.offline_ebs_compare_enabled ? 1 : 0
  cidr_block                       = "10.252.192.0/28"
  enable_dns_support               = false
  enable_dns_hostnames             = false
  assign_generated_ipv6_cidr_block = false
  tags                             = local.offline_ebs_compare_tags
}

resource "aws_subnet" "offline_ebs_compare" {
  provider                        = aws.staging_dr
  count                           = local.offline_ebs_compare_enabled ? 1 : 0
  vpc_id                          = aws_vpc.offline_ebs_compare[0].id
  cidr_block                      = aws_vpc.offline_ebs_compare[0].cidr_block
  availability_zone               = data.aws_ebs_volume.offline_ebs_compare["before"].availability_zone
  map_public_ip_on_launch         = false
  assign_ipv6_address_on_creation = false
  tags                            = local.offline_ebs_compare_tags
}

resource "aws_security_group" "offline_ebs_compare" {
  provider    = aws.staging_dr
  count       = local.offline_ebs_compare_enabled ? 1 : 0
  vpc_id      = aws_vpc.offline_ebs_compare[0].id
  description = "No ingress or egress for offline evidence inspection"
  ingress     = []
  egress      = []
  tags        = local.offline_ebs_compare_tags
}

resource "aws_instance" "offline_ebs_compare" {
  provider                             = aws.staging_dr
  count                                = local.offline_ebs_compare_enabled ? 1 : 0
  ami                                  = data.aws_ami.offline_ebs_compare[0].id
  instance_type                        = "t4g.medium"
  subnet_id                            = aws_subnet.offline_ebs_compare[0].id
  associate_public_ip_address          = false
  vpc_security_group_ids               = [aws_security_group.offline_ebs_compare[0].id]
  instance_initiated_shutdown_behavior = "stop"
  user_data_base64                     = base64encode(local.offline_ebs_compare_userdata)

  metadata_options {
    # Trusted cloud-init reads launch user data over IMDSv2. The script
    # disables all non-loopback interfaces before mounting any evidence.
    # No instance profile or key pair is supplied.
    http_endpoint               = "enabled"
    http_protocol_ipv6          = "disabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    instance_metadata_tags      = "disabled"
  }

  root_block_device {
    encrypted             = true
    volume_type           = "gp3"
    volume_size           = 12
    delete_on_termination = true
  }

  lifecycle {
    precondition {
      condition = alltrue([
        for v in values(data.aws_ebs_volume.offline_ebs_compare) :
        v.encrypted && v.volume_type == "gp3" && !v.multi_attach_enabled &&
        v.size == var.offline_ebs_compare.volume_size_gib &&
        v.availability_zone == data.aws_ebs_volume.offline_ebs_compare["before"].availability_zone
      ])
      error_message = "Both tagged recovery volumes must be detached encrypted gp3, match the specified size, and be in the same staging recovery AZ."
    }
    precondition {
      condition     = length(base64encode(local.offline_ebs_compare_userdata)) <= 21844
      error_message = "User data must stay under the EC2 raw 16 KiB limit."
    }
  }

  tags = merge(local.offline_ebs_compare_tags, { Name = "offline-ebs-compare" })
}

resource "aws_volume_attachment" "offline_ebs_compare" {
  provider    = aws.staging_dr
  for_each    = local.offline_ebs_compare_volumes
  device_name = each.key == "before" ? "/dev/sdf" : "/dev/sdg"
  volume_id   = data.aws_ebs_volume.offline_ebs_compare[each.key].volume_id
  instance_id = aws_instance.offline_ebs_compare[0].id
}

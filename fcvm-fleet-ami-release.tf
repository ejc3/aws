# A versioned software release pointer. Terraform owns the launch templates;
# the image-only publisher advances this non-secret value under the backend lock.
resource "aws_ssm_parameter" "fcvm_ami_release" {
  provider    = aws.fcvm_fleet_east1
  name        = "/fcvm-fleet/ami-release"
  description = "Validated Fleet AMI pair and previous release; image-only publisher owns the value"
  type        = "String"
  value = jsonencode({
    version      = 1
    generation   = 0
    source_sha   = ""
    build_id     = ""
    guard_sha256 = filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")
    host_amis    = var.fcvm_fleet_host_amis
    tile_amis    = var.fcvm_fleet_tile_amis
    versions     = {}
    previous     = null
  })
  lifecycle {
    ignore_changes  = [value]
    prevent_destroy = true
  }
}

# Every published host and tile image is also copied into these regions, so agents'
# GPU boxes and other work can run outside the staging fleet's region and quotas. The
# copies stay out of host_amis and tile_amis: no copy region gets launch templates or
# fleet hosts. Never the compute region (us-east-1).
locals {
  fcvm_ami_copy_regions = ["us-west-2"]
}

# The ids of the active pair in the compute region and of its copies in each copy
# region, written by the publisher after each publication (`replicate-active-release`
# copies the active pair on demand). Tools read it; nothing launches from it.
resource "aws_ssm_parameter" "fcvm_ami_copies" {
  provider    = aws.fcvm_fleet_east1
  name        = "${aws_ssm_parameter.fcvm_ami_release.name}-copies"
  description = "Region-by-region ids of the active Fleet AMI pair and its copies; image-only publisher owns the value"
  type        = "String"
  value       = jsonencode({ version = 1, generation = 0, build_id = "", source_sha = "", images = {} })
  lifecycle {
    ignore_changes = [value]
  }
}

data "aws_ssm_parameter" "fcvm_ami_release" {
  provider        = aws.fcvm_fleet_east1
  count           = var.fcvm_ami_pipeline_enabled ? 1 : 0
  name            = aws_ssm_parameter.fcvm_ami_release.name
  with_decryption = false
  lifecycle {
    postcondition {
      condition = (
        try(jsondecode(self.value).version, 0) == 1 &&
        try(jsondecode(self.value).guard_sha256, "") == filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")
      )
      error_message = "The Fleet AMI release schema or guard digest is incompatible; keep the current release and review the candidate."
    }
  }
}

locals {
  # SSM marks every value sensitive, including this explicitly non-secret image
  # manifest. Only the AMI maps are declassified so resource keys stay usable.
  fcvm_active_host_amis = var.fcvm_ami_pipeline_enabled ? nonsensitive(jsondecode(data.aws_ssm_parameter.fcvm_ami_release[0].value).host_amis) : var.fcvm_fleet_host_amis
  fcvm_active_tile_amis = var.fcvm_ami_pipeline_enabled ? nonsensitive(jsondecode(data.aws_ssm_parameter.fcvm_ami_release[0].value).tile_amis) : var.fcvm_fleet_tile_amis
  fcvm_ami_templates    = module.fcvm_fleet_east1.image_templates
  # Manually verified legacy images, not sanitized CI bases. Keep this catalog
  # additive: moving a tag's resource_id revokes the old image's launch permission,
  # including an active or rollback release. Preserve the original resource keys.
  fcvm_ami_bootstrap = {
    "host/x86_64"          = { image = "ami-00fb7a2e8b89e2446", kind = "host" }
    "tile/x86_64"          = { image = "ami-0edfc91ca2e651db9", kind = "tile" }
    "host/x86_64/38d63412" = { image = "ami-0d94bb60fbf180b7f", kind = "host" }
    "tile/x86_64/2f861183" = { image = "ami-0a9804d3fc86cd87e", kind = "tile" }
    "host/x86_64/2414e101" = { image = "ami-05234f17eb73f6a34", kind = "host" }
    "tile/x86_64/2414e101" = { image = "ami-0a76a813745a28952", kind = "tile" }
    "host/x86_64/46f2d8bc" = { image = "ami-0653ffbb5ec723d2f", kind = "host" }
    "tile/x86_64/46f2d8bc" = { image = "ami-00578e249371ea9fe", kind = "tile" }
    "host/x86_64/a70ad25c" = { image = "ami-0129863cf5498d43b", kind = "host" }
    "tile/x86_64/a70ad25c" = { image = "ami-05dd36badf188758b", kind = "tile" }
    "host/x86_64/a5bae67b" = { image = "ami-051db4be56ae7f6b0", kind = "host" }
    "tile/x86_64/a5bae67b" = { image = "ami-0ce935fe1f66fb10f", kind = "tile" }
    "host/x86_64/04915961" = { image = "ami-0e7a6e72f1dc8f4f4", kind = "host" }
    "host/x86_64/3b597cf9" = { image = "ami-017c2fffe2dd0c176", kind = "host" }
    "tile/x86_64/3b597cf9" = { image = "ami-058b6cec0b53ddf4d", kind = "tile" }
    "host/x86_64/95217caa" = { image = "ami-0620224a0a4dd168e", kind = "host" }
    "host/x86_64/a4a0bd3e" = { image = "ami-04c3294f21cb88942", kind = "host" }
    "host/x86_64/92a26793" = { image = "ami-09ac99f69700ec306", kind = "host" }
    "tile/x86_64/92a26793" = { image = "ami-094ca9c62a284a389", kind = "tile" }
    "host/x86_64/343b2287" = { image = "ami-05923927f810a2ce9", kind = "host" }
  }
}

locals {
  # Reviewed images built by hand in the build region (us-west-2 since #807). Terraform tags
  # the original validated there; the publisher's reviewed action copies it to us-east-1 with
  # its tags (as it copies a factory candidate) and releases the copy, and its record names
  # the original as the build region's image. Additive, like the catalog above.
  #
  # fb2f5c36: built from ejc3/fcvm main fb2f5c36, revised from the f4c57dc3 image (itself built from the 343b2287 one,
  # never released): everything in 343b2287's, plus fcvm's host kernel instead of Ubuntu's (ejc3/fcvm#1700, 6.18.60),
  # a browser's first show after a phone retake in about 0.2 s instead of 2.6 to 3.2 s (ejc3/fcvm#1724), a held
  # phone's stamp read as the whole screen (ejc3/fcvm#1729, ejc3/fcvm#1730), the container frozen across a snapshot
  # boundary (ejc3/fcvm#1705), working-set replay in first-touch order (ejc3/fcvm#1538, ejc3/fcvm#1704,
  # ejc3/fcvm#1664), routed restore with one ip -batch (ejc3/fcvm#1702) and the recipe's metal reboot and prep fixes
  # (ejc3/fcvm#1716, ejc3/fcvm#1736). The IP-options guard is installed by prep and its publication proof passed.
  #
  # 853eb034: built from ejc3/fcvm main 853eb034, revised from the fb2f5c36 image: everything in fb2f5c36's, plus
  # Windows mode's image and bridge (ejc3/fcvm#1742, ejc3/fcvm#1743, ejc3/fcvm#1778; the image is a Windows Server
  # 2025 evaluation install, rebuilt before its evaluation runs out), Cloud Hypervisor restores with memory mapped
  # from the snapshot (ejc3/fcvm#1728), and the other changes on fcvm main from fb2f5c36 to 853eb034.
  #
  # 0e910245: built from ejc3/fcvm main 0e910245, revised from the 853eb034 image: everything in 853eb034's, plus
  # Minecraft restores that keep the bots connected (ejc3/fcvm#1824: restore to first chunk 2.0 s to 0.49 s), the
  # level particles decode fix (ejc3/fcvm#1822), the servers' scheduled reset (ejc3/fcvm#1776), the recipe's wait for
  # the boot's store restore and its base-store cleanup (ejc3/fcvm#1826, ejc3/fcvm#1827), cached-download.sh's quiet
  # stdout (ejc3/fcvm#1829), and the other changes on fcvm main from 853eb034 to 0e910245.
  #
  # cad0ddad: built from ejc3/fcvm main cad0ddad, revised from the 0e910245 image: everything in 0e910245's, plus
  # Minecraft view sockets on the host's IPv4 address (ejc3/fcvm#1833), the Windows bridges on an IPv6-enabled podman
  # network (ejc3/fcvm#1837), the bridges' unauthenticated /still route removed (ejc3/fcvm#1839), and the other changes
  # on fcvm main from 0e910245 to cad0ddad.
  #
  # 293325bd: built from ejc3/fcvm main 293325bd, revised from the cad0ddad image: everything in cad0ddad's, plus the
  # public Minecraft join limits (ejc3/fcvm#1844: per-source connection and rate limits on the host, Pumpkin from the
  # ejc3/Pumpkin fork with a login timeout), the declared port ranges and in-host viewer acceptance (ejc3/fcvm#1841,
  # #1847, #1848), the bake's pruning of untagged images (ejc3/fcvm#1857), and the other changes on fcvm main from
  # cad0ddad to 293325bd.
  fcvm_ami_build_region_reviewed = {
    "host/x86_64/fb2f5c36" = { image = "ami-0d176763f9f33416e", kind = "host", name = "fcvm-fleet-host-x86_64-fb2f5c36-202611072303" }
    "host/x86_64/853eb034" = { image = "ami-0925206c2b9eac0fe", kind = "host", name = "fcvm-fleet-host-x86_64-853eb034-202611091050" }
    "host/x86_64/0e910245" = { image = "ami-0c1f87eea5c86c172", kind = "host", name = "fcvm-fleet-host-x86_64-0e910245-202611121727" }
    "host/x86_64/cad0ddad" = { image = "ami-05c06ef8ae037b10a", kind = "host", name = "fcvm-fleet-host-x86_64-cad0ddad-202611131412" }
    "host/x86_64/293325bd" = { image = "ami-0784a83b0e32e6601", kind = "host", name = "fcvm-fleet-host-x86_64-293325bd-202611131727" }
  }
}

resource "aws_ec2_tag" "fcvm_ami_build_region_kind" {
  provider    = aws.fcvm_fleet_west2
  for_each    = local.fcvm_ami_build_region_reviewed
  resource_id = each.value.image
  key         = "FleetImageKind"
  value       = each.value.kind
}

resource "aws_ec2_tag" "fcvm_ami_build_region_validated" {
  provider    = aws.fcvm_fleet_west2
  for_each    = local.fcvm_ami_build_region_reviewed
  resource_id = each.value.image
  key         = "FleetImageValidated"
  value       = "true"
}

# The publisher's us-east-1 copy of each of those images, once it has made one (none before).
# Potato mode is chosen by the boot image's id, so the host bootstrap names each copy; the
# reviewed action refuses until it does. Bound to the original by the copy's description and
# names (the publisher's regional_copy: the original's name, then -copy-2 and -copy-3).
data "aws_ami_ids" "fcvm_ami_build_region_copies" {
  provider = aws.fcvm_fleet_east1
  for_each = local.fcvm_ami_build_region_reviewed
  owners   = ["self"]
  filter {
    name   = "name"
    values = [each.value.name, "${each.value.name}-copy-2", "${each.value.name}-copy-3"]
  }
  filter {
    name   = "description"
    values = ["copy of ${each.value.image} from us-west-2"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
  filter {
    name   = "tag:FleetImageValidated"
    values = ["true"]
  }
}

resource "aws_ec2_tag" "fcvm_ami_bootstrap_kind" {
  provider    = aws.fcvm_fleet_east1
  for_each    = local.fcvm_ami_bootstrap
  resource_id = each.value.image
  key         = "FleetImageKind"
  value       = each.value.kind
}

resource "aws_ec2_tag" "fcvm_ami_bootstrap_validated" {
  provider    = aws.fcvm_fleet_east1
  for_each    = local.fcvm_ami_bootstrap
  resource_id = each.value.image
  key         = "FleetImageValidated"
  value       = "true"
}

# The existing coordinator inline policy is already at its aggregate size limit.
# Keep image authorization separate; every other launch/profile/network grant
# stays in its existing exact-resource statement.
resource "aws_iam_policy" "fcvm_ami_validated_images" {
  provider    = aws.fcvm_fleet
  name        = "fcvm-fleet-validated-images"
  description = "Launch only owned private validated images with the reviewed guard and matching Fleet kind"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ValidatedTileImagesOnly"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = [for region in local.fcvm_fleet_regions : "arn:aws:ec2:${region}::image/*"]
        Condition = {
          StringEquals = {
            "ec2:Owner"                           = local.fcvm_fleet_account_id
            "ec2:ResourceTag/Purpose"             = "fcvm-fleet"
            "ec2:ResourceTag/FleetImageKind"      = "tile"
            "ec2:ResourceTag/FleetImageValidated" = "true"
            "ec2:ResourceTag/GuardHelperSHA256"   = filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")
          }
          Bool    = { "ec2:Public" = "false" }
          ArnLike = { "ec2:LaunchTemplate" = local.fcvm_fleet_tile_template_condition }
        }
      },
      {
        Sid      = "ValidatedHostImagesOnly"
        Effect   = "Allow"
        Action   = ["ec2:RunInstances", "ec2:CreateFleet"]
        Resource = [for region in local.fcvm_fleet_regions : "arn:aws:ec2:${region}::image/*"]
        Condition = {
          StringEquals = {
            "ec2:Owner"                           = local.fcvm_fleet_account_id
            "ec2:ResourceTag/Purpose"             = "fcvm-fleet"
            "ec2:ResourceTag/FleetImageKind"      = "host"
            "ec2:ResourceTag/FleetImageValidated" = "true"
            "ec2:ResourceTag/GuardHelperSHA256"   = filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")
          }
          Bool            = { "ec2:Public" = "false" }
          ArnLikeIfExists = { "ec2:LaunchTemplate" = local.fcvm_fleet_host_template_condition }
        }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "fcvm_ami_validated_images" {
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_coordinator_task.name
  policy_arn = aws_iam_policy.fcvm_ami_validated_images.arn
}

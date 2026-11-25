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

# The renders host image's release pointer (fcvm-fleet-ami-pipeline.tf, the renders track): only the renders publication
# writes it, and the pair's never reads or writes it. renders_amis names the build region's original (the next renders
# build's base) and the release region's copy (the renders host template's image): { region = { arm64 = "ami-..." } }.
# Empty until the first renders publication; the renders host class exists only once it names an image.
resource "aws_ssm_parameter" "fcvm_ami_renders_release" {
  provider    = aws.fcvm_fleet_east1
  name        = "/fcvm-fleet/ami-release-renders"
  description = "Validated Fleet renders host AMI and previous release; the image-only publisher owns the value"
  type        = "String"
  value = jsonencode({
    version      = 1
    track        = "renders"
    generation   = 0
    source_sha   = ""
    build_id     = ""
    guard_sha256 = filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")
    renders_amis = {}
    versions     = {}
    build        = null
    previous     = null
  })
  lifecycle {
    ignore_changes  = [value]
    prevent_destroy = true
  }
}

# Every published host and tile image is also copied into these regions: the fixed ones, so
# agents' GPU boxes and other work can run outside the staging fleet's region and quotas, and
# every other region staging lists for its GPU hosts (fcvm-fleet.tf, local.fcvm_fleet_host_regions),
# so each build lands in every listed region with no hand step. Never the compute region
# (us-east-1). The copies stay out of the release pointer's host_amis and tile_amis, which stay
# the compute region's: a listed copy region's host template boots the copy record's host image
# instead (local.fcvm_fleet_host_amis below), and the publisher moves that template onto each
# release's copy once it has recorded it (publish.py, follow_copies). No copy region gets tiles.
locals {
  fcvm_ami_fixed_copy_regions = ["us-west-2"]
  fcvm_ami_copy_regions = distinct(concat(local.fcvm_ami_fixed_copy_regions,
  [for region in local.fcvm_fleet_host_regions.stage : region if region != local.fcvm_ami_region]))
}

# The ids of the active pair in the compute region and of its copies in each copy
# region, written by the publisher after each publication (`replicate-active-release`
# copies the active pair on demand). Tools read it, and a listed host region's host
# template boots its host image there.
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

# Keep apply-time guards in the root. Terraform 1.10.3 cannot expand an ephemeral inside an
# absent module instance. Only the launch templates depend on the verified version.
ephemeral "aws_ssm_parameter" "fcvm_ami_release_guard" {
  provider        = aws.fcvm_fleet_east1
  count           = var.fcvm_ami_pipeline_enabled ? 1 : 0
  arn             = aws_ssm_parameter.fcvm_ami_release.arn
  with_decryption = false
  lifecycle {
    postcondition {
      condition     = self.version == data.aws_ssm_parameter.fcvm_ami_release[0].version
      error_message = "The Fleet AMI release changed after planning; create a fresh plan."
    }
  }
}

# The copy record, read the way the release pointer is: the host AMI of every other region staging
# lists for its GPU hosts comes from it. Only well-formed fleet-region image ids are accepted.
data "aws_ssm_parameter" "fcvm_ami_copies" {
  provider        = aws.fcvm_fleet_east1
  count           = var.fcvm_ami_pipeline_enabled ? 1 : 0
  name            = aws_ssm_parameter.fcvm_ami_copies.name
  with_decryption = false
  lifecycle {
    postcondition {
      condition = (
        try(jsondecode(self.value).version, 0) == 1 &&
        try(alltrue([for region, kinds in jsondecode(self.value).images : contains(local.fcvm_fleet_regions, region) && alltrue([
          for kind, image in kinds : contains(["host", "tile"], kind) && can(regex("^ami-[0-9a-f]{17}$", image))
        ])]), false)
      )
      error_message = "The Fleet AMI copy record's schema is incompatible; keep the current host templates and review the record."
    }
  }
}

# A saved plan made before the publisher last wrote the copy record would put a copy region's
# host template back on an older copy: refuse it, as the release guard does for the pointer.
ephemeral "aws_ssm_parameter" "fcvm_ami_copies_guard" {
  provider        = aws.fcvm_fleet_east1
  count           = var.fcvm_ami_pipeline_enabled ? 1 : 0
  arn             = aws_ssm_parameter.fcvm_ami_copies.arn
  with_decryption = false
  lifecycle {
    postcondition {
      condition     = self.version == data.aws_ssm_parameter.fcvm_ami_copies[0].version
      error_message = "The Fleet AMI copy record changed after planning; create a fresh plan."
    }
  }
}

# The renders pointer, read the way the release pointer is: the renders host class's image (fcvm-fleet.tf) comes from it,
# and from nothing in the pair's pointer. renders_amis is { region = { arm64 = "ami-..." } }: the build region's original
# and the release region's copy, which the publisher's renders track writes; only fleet regions, the arm64 key and exact
# image ids are accepted.
data "aws_ssm_parameter" "fcvm_ami_renders_release" {
  provider        = aws.fcvm_fleet_east1
  count           = var.fcvm_ami_pipeline_enabled ? 1 : 0
  name            = aws_ssm_parameter.fcvm_ami_renders_release.name
  with_decryption = false
  lifecycle {
    postcondition {
      condition = (
        try(jsondecode(self.value).version, 0) == 1 &&
        try(jsondecode(self.value).track, "") == "renders" &&
        try(jsondecode(self.value).guard_sha256, "") == filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py") &&
        try(alltrue([for region, arches in jsondecode(self.value).renders_amis : contains(local.fcvm_fleet_regions, region) && length(arches) == 1 && contains(keys(arches), "arm64") && alltrue([
          for arch, image in arches : can(regex("^ami-[0-9a-f]{17}$", image))
        ])]), false)
      )
      error_message = "The Fleet renders AMI release schema or guard digest is incompatible; keep the current renders template and review the release."
    }
  }
}

# A saved plan made before the publisher last wrote the renders pointer would put the renders template back on an older
# image: refuse it, as the release guard does for the pair's.
ephemeral "aws_ssm_parameter" "fcvm_ami_renders_release_guard" {
  provider        = aws.fcvm_fleet_east1
  count           = var.fcvm_ami_pipeline_enabled ? 1 : 0
  arn             = aws_ssm_parameter.fcvm_ami_renders_release.arn
  with_decryption = false
  lifecycle {
    postcondition {
      condition     = self.version == data.aws_ssm_parameter.fcvm_ami_renders_release[0].version
      error_message = "The Fleet renders AMI release changed after planning; create a fresh plan."
    }
  }
}

locals {
  # The renders host class's images (fcvm-fleet.tf): {} until the renders track's first publication, and while the
  # pipeline is disabled. With {} the class has no launch template, group, role or policy and no coordinator variables.
  fcvm_active_renders_amis = var.fcvm_ami_pipeline_enabled ? nonsensitive(jsondecode(data.aws_ssm_parameter.fcvm_ami_renders_release[0].value).renders_amis) : {}
}

locals {
  # SSM marks every value sensitive, including this explicitly non-secret image
  # manifest. Only the AMI maps are declassified so resource keys stay usable.
  fcvm_active_host_amis = var.fcvm_ami_pipeline_enabled ? nonsensitive(jsondecode(data.aws_ssm_parameter.fcvm_ami_release[0].value).host_amis) : var.fcvm_fleet_host_amis
  fcvm_active_tile_amis = var.fcvm_ami_pipeline_enabled ? nonsensitive(jsondecode(data.aws_ssm_parameter.fcvm_ami_release[0].value).tile_amis) : var.fcvm_fleet_tile_amis
  # The publisher's copy of the live host image in each region of the copy record (never a tile: tiles stay in us-east-1).
  fcvm_ami_copy_host_amis = var.fcvm_ami_pipeline_enabled ? {
    for region, kinds in nonsensitive(jsondecode(data.aws_ssm_parameter.fcvm_ami_copies[0].value).images) : region => kinds.host if can(kinds.host)
  } : {}
  # Each fleet region's GPU host AMIs, which give it a host template (modules/fcvm-fleet-region, gpu_amis): the compute
  # region's from the release pointer, as always; another region's only while staging lists it for its GPU hosts, and
  # then its copy of the live host image, once the publisher has recorded one there.
  fcvm_fleet_host_amis = { for region in local.fcvm_fleet_regions : region => (
    region == local.fcvm_ami_region || !var.fcvm_ami_pipeline_enabled ? lookup(local.fcvm_active_host_amis, region, {}) : (
      contains(local.fcvm_fleet_host_regions.stage, region) && contains(keys(local.fcvm_ami_copy_host_amis), region)
      ? { x86_64 = local.fcvm_ami_copy_host_amis[region] } : {}
    )
  ) }
  # The templates the publisher releases into: the compute region's four, then the host template of each other region
  # staging lists, which it moves onto that region's copy after each replication (publish.py, follow_copies). Only these
  # templates' versions and defaults are the publisher's to change (fcvm-fleet-ami-pipeline.tf).
  fcvm_ami_compute_templates = module.fcvm_fleet_east1.image_templates
  fcvm_ami_copy_templates = flatten([for region in local.fcvm_fleet_host_regions.stage : [
    for template in local.fcvm_fleet_regional[region].image_templates : template if template.kind == "host"
  ] if region != local.fcvm_ami_region && contains(keys(local.fcvm_fleet_regional), region)])
  fcvm_ami_templates = concat(local.fcvm_ami_compute_templates, local.fcvm_ami_copy_templates)
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

# A region newly listed for GPU hosts gets its host template only once the copy record names a host
# image there: the publisher copies into it from its next publication on (COPY_REGIONS follows the
# list), or at once with `replicate-active-release`. Until then this warns rather than fails.
check "fcvm_fleet_listed_host_regions_have_a_copy" {
  assert {
    condition = !var.fcvm_ami_pipeline_enabled || alltrue([
      for region in local.fcvm_fleet_host_regions.stage : region == local.fcvm_ami_region || contains(keys(local.fcvm_ami_copy_host_amis), region)
    ])
    error_message = "A region in local.fcvm_fleet_host_regions.stage has no copy of the live host image yet, so no host template: invoke the publisher's replicate-active-release (or wait for the next publication), then apply again."
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
  # c12ad070: built from ejc3/fcvm main c12ad070, revised from an unreleased a8245c00 image: everything in cad0ddad's,
  # plus fcvm's host kernel with the EC2 network driver built in (ejc3/fcvm#1874), so a first boot finds its network
  # interface before cloud-init's first stage looks for one, the phone's GPU retake beside the bring-up
  # (ejc3/fcvm#1872), the bake's hugepage-prefilled check on the phone clone (ejc3/fcvm#1871), the public Minecraft
  # join limits (ejc3/fcvm#1844), the GPU restore path (ejc3/fcvm#1850, #1855, #1867, #1869, #1875), and the other
  # changes on fcvm main from cad0ddad to c12ad070.
  fcvm_ami_build_region_reviewed = {
    "host/x86_64/fb2f5c36" = { image = "ami-0d176763f9f33416e", kind = "host", name = "fcvm-fleet-host-x86_64-fb2f5c36-202611072303" }
    "host/x86_64/853eb034" = { image = "ami-0925206c2b9eac0fe", kind = "host", name = "fcvm-fleet-host-x86_64-853eb034-202611091050" }
    "host/x86_64/0e910245" = { image = "ami-0c1f87eea5c86c172", kind = "host", name = "fcvm-fleet-host-x86_64-0e910245-202611121727" }
    "host/x86_64/cad0ddad" = { image = "ami-05c06ef8ae037b10a", kind = "host", name = "fcvm-fleet-host-x86_64-cad0ddad-202611131412" }
    "host/x86_64/c12ad070" = { image = "ami-0c2656522631215d1", kind = "host", name = "fcvm-fleet-host-x86_64-c12ad070-202611141114" }
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

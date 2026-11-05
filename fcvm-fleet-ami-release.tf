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

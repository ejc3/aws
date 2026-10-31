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
  fcvm_active_host_amis = var.fcvm_ami_pipeline_enabled ? jsondecode(data.aws_ssm_parameter.fcvm_ami_release[0].value).host_amis : var.fcvm_fleet_host_amis
  fcvm_active_tile_amis = var.fcvm_ami_pipeline_enabled ? jsondecode(data.aws_ssm_parameter.fcvm_ami_release[0].value).tile_amis : var.fcvm_fleet_tile_amis
  fcvm_ami_templates    = module.fcvm_fleet_east1.image_templates
  # These two pre-pipeline images already passed actual hardware/stream/guard
  # verification. They are eligible for the transition, not sanitized CI bases.
  fcvm_ami_bootstrap = merge(
    { for arch, image in lookup(var.fcvm_fleet_host_amis, "us-east-1", {}) : "host/${arch}" => { image = image, kind = "host" } if image != "" },
    { for arch, image in lookup(var.fcvm_fleet_tile_amis, "us-east-1", {}) : "tile/${arch}" => { image = image, kind = "tile" } if image != "" }
  )
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

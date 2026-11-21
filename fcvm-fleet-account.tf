# fcvm-fleet-account.tf
#
# A member account of its own for fcvm's GPU VM wall (ejc3/aws#604; design: ejc3/fcvm#1136, docs/fleet/ARCHITECTURE.md),
# built from here as the constellation account is (constellation-account.tf). The fleet runs always-on spot GPU hosts that
# serve the public internet (TCP 443) with workloads viewers can steer, so it is contained by an account boundary: nothing
# in this (management) account or any other member account trusts anything in fcvm-fleet; no VPC peering, no shared
# subnets, no resource policy names it. Owner, 2026-10-22: "its good make it so".
#
# - Regions: us-east-2, us-west-2 and us-east-1 (local.fcvm_fleet_regions), each a provider alias below. A service control
#   policy on the OU denies every regional action elsewhere, denies leaving the organization, and denies IAM users and
#   access keys (roles only).
# - Applied two ways: the fleet's infrastructure from here (fcvm-fleet.tf, a jumpbox through OrganizationAccountAccessRole),
#   and fcvm's gpubox tools from fcvm-metal-arm through `fcvm-fleet-terraform` (an allow list for gpubox's calls only, no
#   secret, one-hour sessions), which trusts dev-server-role pinned to fcvm-arm's instance, as constellation's.
# - CI cannot assume a role in this account. Its management-account role may publish to the coordinator's ECR repository
#   and invoke the image-only release function (fcvm-fleet-ci.tf). Only that function assumes the scoped release role;
#   neither has Terraform-state or reusable-credential access.
# - Quotas (ejc3/aws#605, #913): 480 vCPU of G spot in us-east-1 (production's two hosts and a replacement, staging's
#   host and an image-factory build host); one region is enough.
#
# First apply: the provider aliases need the account id, so apply the account first
# (-target=aws_organizations_account.fcvm_fleet), then everything.

locals {
  fcvm_fleet_regions         = ["us-east-2", "us-west-2", "us-east-1"]
  fcvm_fleet_home_region     = "us-east-2"
  fcvm_fleet_account_id      = aws_organizations_account.fcvm_fleet.id
  fcvm_fleet_terraform_role  = "arn:aws:iam::${aws_organizations_account.fcvm_fleet.id}:role/fcvm-fleet-terraform"
  fcvm_fleet_access_role_arn = "arn:aws:iam::${aws_organizations_account.fcvm_fleet.id}:role/OrganizationAccountAccessRole"
  # G and VT spot vCPUs in us-east-1 (#605, #913): production's two awake g4dn.metal hosts (192), the replacement a
  # Spot interruption notice launches while the reclaimed host drains (96), staging's host (96) and an image-factory
  # build host (96).
  fcvm_fleet_spot_g_vcpus = 480
  # On-demand G vCPUs in us-east-1 (ejc3/fcvm#1257): the wall's host (96) and its replacement while the old one shuts
  # down (96), the EC2 tiles started on demand (8 x 4), and the gpubox test hosts and AMI builders that share the
  # region (96 each, four at once). At 384 the wall's host was refused with VcpuLimitExceeded for 13 minutes on two
  # wakes of 2026-11-05 while two gpuboxes, an AMI builder and the tiles held the quota.
  fcvm_fleet_on_demand_g_vcpus = 768
}

resource "aws_organizations_organizational_unit" "fcvm_fleet" {
  name      = "FcvmFleet"
  parent_id = data.aws_organizations_organization.current.roots[0].id
  tags      = { Purpose = "fcvm-fleet", Managed = "terraform" }
}

resource "aws_organizations_account" "fcvm_fleet" {
  name                       = "fcvm-fleet"
  email                      = local.fcvm_fleet_account_email
  parent_id                  = aws_organizations_organizational_unit.fcvm_fleet.id
  role_name                  = "OrganizationAccountAccessRole"
  iam_user_access_to_billing = "DENY"
  close_on_deletion          = false

  lifecycle {
    # A change of name or email replaces the account; guard against an accidental replace or destroy.
    prevent_destroy = true
    ignore_changes  = [role_name, iam_user_access_to_billing]
  }

  tags = { Purpose = "fcvm-fleet", Managed = "terraform" }
}

provider "aws" {
  alias  = "fcvm_fleet"
  region = "us-east-2"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.fcvm_fleet.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "fcvm_fleet_west2"
  region = "us-west-2"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.fcvm_fleet.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "fcvm_fleet_east1"
  region = "us-east-1"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.fcvm_fleet.id}:role/OrganizationAccountAccessRole"
  }
}

# ------------------------------------------------------------------------------------------------ the OU's guardrails
locals {
  fcvm_fleet_scp = {
    Version = "2012-10-17"
    Statement = [
      {
        # The global services stay usable from any console region (the same list as the Dolphin OU's policy).
        Sid       = "DenyOutsideTheFleetRegions"
        Effect    = "Deny"
        NotAction = local.dolphin_region_scp_global_actions
        Resource  = "*"
        Condition = { StringNotEquals = { "aws:RequestedRegion" = local.fcvm_fleet_regions } }
      },
      {
        Sid      = "DenyLeavingTheOrganization"
        Effect   = "Deny"
        Action   = "organizations:LeaveOrganization"
        Resource = "*"
      },
      {
        # Roles only: no IAM user, no long-lived access key, no console password, in this account.
        Sid      = "DenyIamUsersAndKeys"
        Effect   = "Deny"
        Action   = ["iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile", "iam:UpdateLoginProfile"]
        Resource = "*"
      },
      {
        # aws:RequestedRegion is the endpoint, not where a bucket goes.
        Sid      = "DenyBucketsOutsideTheFleetRegions"
        Effect   = "Deny"
        Action   = "s3:CreateBucket"
        Resource = "*"
        Condition = {
          StringNotEquals = { "s3:LocationConstraint" = local.fcvm_fleet_regions }
          Null            = { "s3:LocationConstraint" = "false" }
        }
      },
    ]
  }
}

resource "aws_organizations_policy" "fcvm_fleet_guardrails" {
  name        = "fcvm-fleet-guardrails"
  description = "fcvm-fleet OU: the fleet's three regions only, no leaving the organization, no IAM users or access keys (fcvm-fleet-account.tf)"
  type        = "SERVICE_CONTROL_POLICY"
  content     = jsonencode(local.fcvm_fleet_scp)
  depends_on  = [aws_organizations_organization.security]
}

resource "aws_organizations_policy_attachment" "fcvm_fleet_guardrails" {
  policy_id = aws_organizations_policy.fcvm_fleet_guardrails.id
  target_id = aws_organizations_organizational_unit.fcvm_fleet.id
}

resource "aws_s3_account_public_access_block" "fcvm_fleet" {
  provider                = aws.fcvm_fleet
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ------------------------------------------------------------------------------------------------ state bucket
resource "aws_s3_bucket" "fcvm_fleet_terraform_state" {
  provider = aws.fcvm_fleet
  bucket   = "fcvm-fleet-terraform-state-${local.fcvm_fleet_account_id}"
  tags     = { Name = "fcvm-fleet-terraform-state", Managed = "terraform" }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "fcvm_fleet_terraform_state" {
  provider = aws.fcvm_fleet
  bucket   = aws_s3_bucket.fcvm_fleet_terraform_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "fcvm_fleet_terraform_state" {
  provider                = aws.fcvm_fleet
  bucket                  = aws_s3_bucket.fcvm_fleet_terraform_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "fcvm_fleet_terraform_state" {
  provider = aws.fcvm_fleet
  bucket   = aws_s3_bucket.fcvm_fleet_terraform_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "fcvm_fleet_terraform_state" {
  provider = aws.fcvm_fleet
  bucket   = aws_s3_bucket.fcvm_fleet_terraform_state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# State can hold secret values: its objects are for the apply role and the management account's access role only.
resource "aws_s3_bucket_policy" "fcvm_fleet_terraform_state" {
  provider = aws.fcvm_fleet
  bucket   = aws_s3_bucket.fcvm_fleet_terraform_state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.fcvm_fleet_terraform_state.arn, "${aws_s3_bucket.fcvm_fleet_terraform_state.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "StateObjectsForAppliersOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = "${aws_s3_bucket.fcvm_fleet_terraform_state.arn}/*"
        Condition = { ArnNotLike = { "aws:PrincipalArn" = [local.fcvm_fleet_terraform_role, local.fcvm_fleet_access_role_arn] } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.fcvm_fleet_terraform_state]
}

# ------------------------------------------------------------------------------------------------ apply role
resource "aws_iam_role" "fcvm_fleet_terraform" {
  provider             = aws.fcvm_fleet
  name                 = "fcvm-fleet-terraform"
  description          = "Applies fcvm's fleet Terraform. Assumable only from fcvm-metal-arm's instance (see ejc3/aws fcvm-fleet-account.tf)."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "FcvmArmOnly"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.dev_server.arn }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = local.constellation_applier_instance_arn } }
    }]
  })
  tags = { Name = "fcvm-fleet-terraform", Managed = "terraform" }
}

# What fcvm-arm does in this account: fcvm's fleet/tools/gpubox.sh (and g-admission.sh) launch, drive and end
# short-lived GPU boxes. Nothing else. The account also holds the fleet's certificates and tokens, staging's and
# production's, so the role is an allow list for exactly those calls (ejc3/aws#922), not AdministratorAccess: an
# administrator can reach any secret by reading it, by passing a role that can, or by changing what a role-holding
# workload runs (a launch template, an AMI pointer, a host through Run Command).
locals {
  fcvm_fleet_gpubox_purpose = "fcvm-fleet-gpubox" # gpubox.sh PURPOSE
  fcvm_fleet_gpubox_regions = ["us-west-2", "us-east-1"]
}

resource "aws_iam_role_policy" "fcvm_fleet_terraform_gpubox" {
  provider = aws.fcvm_fleet
  name     = "gpubox-only"
  role     = aws_iam_role.fcvm_fleet_terraform.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Look"
        Effect = "Allow"
        Action = ["ec2:DescribeImages", "ec2:DescribeInstances", "ec2:DescribeInstanceTypeOfferings", "ec2:DescribeInstanceTypes",
        "ec2:DescribeSecurityGroups", "ec2:DescribeSubnets", "ssm:DescribeInstanceInformation", "ssm:GetCommandInvocation", "servicequotas:GetServiceQuota"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.fcvm_fleet_gpubox_regions } }
      },
      {
        Sid       = "ReadTheImageRelease"
        Effect    = "Allow"
        Action    = "ssm:GetParameter"
        Resource  = [for name in ["ami-release", "ami-release-copies"] : "arn:aws:ssm:*:${local.fcvm_fleet_account_id}:parameter/fcvm-fleet/${name}"]
        Condition = { StringEquals = { "aws:RequestedRegion" = local.fcvm_fleet_gpubox_regions } }
      },
      {
        Sid       = "LaunchOnlyGpuboxes"
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = "arn:aws:ec2:*:${local.fcvm_fleet_account_id}:instance/*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.fcvm_fleet_gpubox_regions, "aws:RequestTag/Purpose" = local.fcvm_fleet_gpubox_purpose } }
      },
      {
        Sid    = "WhatALaunchUses"
        Effect = "Allow"
        Action = "ec2:RunInstances"
        Resource = ["arn:aws:ec2:*::image/*", "arn:aws:ec2:*:${local.fcvm_fleet_account_id}:subnet/*",
          "arn:aws:ec2:*:${local.fcvm_fleet_account_id}:security-group/*", "arn:aws:ec2:*:${local.fcvm_fleet_account_id}:network-interface/*",
        "arn:aws:ec2:*:${local.fcvm_fleet_account_id}:volume/*", "arn:aws:ec2:*:${local.fcvm_fleet_account_id}:spot-instances-request/*"]
        Condition = { StringEquals = { "aws:RequestedRegion" = local.fcvm_fleet_gpubox_regions } }
      },
      {
        Sid      = "TagAtLaunch"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = "arn:aws:ec2:*:${local.fcvm_fleet_account_id}:instance/*"
        Condition = {
          StringEquals                = { "ec2:CreateAction" = "RunInstances", "aws:RequestedRegion" = local.fcvm_fleet_gpubox_regions }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Name", "Purpose", "Owner", "LeaseUntil", "Market", "fcvm-admission"] }
        }
      },
      {
        Sid      = "ExtendOnlyGpuboxLeases"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = "arn:aws:ec2:*:${local.fcvm_fleet_account_id}:instance/*"
        Condition = {
          StringEquals                = { "ec2:ResourceTag/Purpose" = local.fcvm_fleet_gpubox_purpose, "aws:RequestedRegion" = local.fcvm_fleet_gpubox_regions }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["LeaseUntil"] }
        }
      },
      {
        Sid       = "EndOnlyGpuboxes"
        Effect    = "Allow"
        Action    = "ec2:TerminateInstances"
        Resource  = "arn:aws:ec2:*:${local.fcvm_fleet_account_id}:instance/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = local.fcvm_fleet_gpubox_purpose, "aws:RequestedRegion" = local.fcvm_fleet_gpubox_regions } }
      },
      {
        Sid       = "RunCommandsOnlyOnGpuboxes"
        Effect    = "Allow"
        Action    = "ssm:SendCommand"
        Resource  = "arn:aws:ec2:*:${local.fcvm_fleet_account_id}:instance/*"
        Condition = { StringEquals = { "ssm:resourceTag/Purpose" = local.fcvm_fleet_gpubox_purpose, "aws:RequestedRegion" = local.fcvm_fleet_gpubox_regions } }
      },
      {
        Sid       = "TheShellDocument"
        Effect    = "Allow"
        Action    = "ssm:SendCommand"
        Resource  = "arn:aws:ssm:*::document/AWS-RunShellScript"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.fcvm_fleet_gpubox_regions } }
      },
      {
        Sid       = "PassOnlyTheGpuboxRole"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.fcvm_fleet_gpubox.arn
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
      {
        # copy's staging objects (signed URLs carry this role's permissions) and the admission lock (g-admission.sh).
        Sid      = "TheGpuboxPrefix"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.fcvm_fleet_assets.arn}/gpubox/*"
      },
      {
        # Already outside the allow list above; denied by name so a later broad grant cannot reopen them.
        Sid    = "NeverSecretsIamOrWhatWorkloadsRun"
        Effect = "Deny"
        Action = ["secretsmanager:*", "kms:Decrypt", "iam:Create*", "iam:Put*", "iam:Attach*", "iam:Update*", "iam:Delete*", "iam:Detach*",
        "iam:AddRoleToInstanceProfile", "ssm:PutParameter", "ssm:StartSession", "ec2:CreateLaunchTemplateVersion", "ec2:ModifyLaunchTemplate", "ec2:ModifyInstanceAttribute"]
        Resource = "*"
      },
    ]
  })
}

# The instance role of a gpubox: Session Manager and the release artifacts a host image fetches at boot, and no secret
# (the fleet's host role reads the certificate and the agents' Access token; a gpubox needs neither).
resource "aws_iam_role" "fcvm_fleet_gpubox" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-gpubox"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "fcvm-fleet-gpubox", Managed = "terraform" }
}

resource "aws_iam_role_policy" "fcvm_fleet_gpubox" {
  provider = aws.fcvm_fleet
  name     = "gpubox"
  role     = aws_iam_role.fcvm_fleet_gpubox.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # ManagedInstanceCore grants GetParameter and GetParameters on "*". Keep SSM command delivery
      # and AWS's public AMI parameters while excluding other account parameters from the effective grant.
      {
        Sid    = "DenyUnrelatedParameters"
        Effect = "Deny"
        Action = "ssm:GetParameter*"
        NotResource = [
          "arn:aws:ssm:*::parameter/aws/service/*",
          "arn:aws:ssm:us-east-1:${local.fcvm_fleet_account_id}:parameter/fcvm-fleet/ami-release",
          "arn:aws:ssm:us-east-1:${local.fcvm_fleet_account_id}:parameter/fcvm-fleet/ami-release-copies",
        ]
      },
      { Sid = "ReadReleaseArtifacts", Effect = "Allow", Action = "s3:GetObject", Resource = "${aws_s3_bucket.fcvm_fleet_assets.arn}/*" },
      { Sid = "ListReleaseArtifacts", Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.fcvm_fleet_assets.arn },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_gpubox_ssm" {
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_gpubox.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "fcvm_fleet_gpubox" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-gpubox"
  role     = aws_iam_role.fcvm_fleet_gpubox.name
}

# Every G launch in us-east-1 by this role goes through fcvm's admission (fleet/tools/g-admission.sh), which
# leaves the wall's GPU host its room in the account's G vCPU quotas and tags the launch
# fcvm-admission=wall-reserve. A launch without the tag is a copy of gpubox.sh or build-ami.sh
# that predates the admission: on 2026-11-07 one took the on-demand quota the wall's host
# needed, in the second the wall woke, and the staging smoke got no fcvm tile. Only the wall's
# host region: the reserve is for the quota the wall's host shares there. The coordinator and
# the image factory launch with their own roles and are not affected.
resource "aws_iam_role_policy" "fcvm_fleet_terraform_g_admission" {
  provider = aws.fcvm_fleet
  name     = "g-launches-through-the-wall-reserve"
  role     = aws_iam_role.fcvm_fleet_terraform.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "DenyUnadmittedGLaunch"
      Effect   = "Deny"
      Action   = "ec2:RunInstances"
      Resource = "arn:aws:ec2:*:*:instance/*"
      Condition = {
        StringEquals    = { "aws:RequestedRegion" = "us-east-1" }
        StringLike      = { "ec2:InstanceType" = ["g*", "vt*"] }
        StringNotEquals = { "aws:RequestTag/fcvm-admission" = "wall-reserve" }
      }
    }]
  })
}

# What fcvm-arm reads to find the account (no credentials): /infra/fcvm-fleet, granted in assume-constellation-terraform
# (constellation-account.tf) with the same instance pin, as dev-server-role has no room for another policy.
resource "aws_ssm_parameter" "fcvm_fleet" {
  name        = "/infra/fcvm-fleet"
  description = "fcvm-fleet member account: id, regions, state bucket, apply role (fcvm-fleet-account.tf); no credentials"
  type        = "String"
  value = jsonencode({
    account_id     = local.fcvm_fleet_account_id
    regions        = local.fcvm_fleet_regions
    home_region    = local.fcvm_fleet_home_region
    state_bucket   = aws_s3_bucket.fcvm_fleet_terraform_state.bucket
    terraform_role = aws_iam_role.fcvm_fleet_terraform.arn
  })
}

# ------------------------------------------------------------------------------------------------ quotas (#605)
# us-east-1 only (owner, 2026-10-22: "I am good as long as there is one"). The new account started at 0 in every region;
# us-east-1 granted 192, us-west-2's case closed at 0 and us-east-2's was still open. The other two requests are dropped from
# Terraform without touching AWS (a quota cannot be lowered or a request withdrawn here), so plans stop re-filing them.
removed {
  from = aws_servicequotas_service_quota.fcvm_fleet_spot_g_east2
  lifecycle {
    destroy = false
  }
}

removed {
  from = aws_servicequotas_service_quota.fcvm_fleet_spot_g_west2
  lifecycle {
    destroy = false
  }
}

data "aws_servicequotas_service_quota" "fcvm_fleet_spot_g_east1" {
  provider     = aws.fcvm_fleet_east1
  service_code = "ec2"
  quota_code   = "L-3819A6DF"
}

resource "aws_servicequotas_service_quota" "fcvm_fleet_spot_g_east1" {
  provider     = aws.fcvm_fleet_east1
  service_code = "ec2"
  quota_code   = "L-3819A6DF"
  value        = max(local.fcvm_fleet_spot_g_vcpus, data.aws_servicequotas_service_quota.fcvm_fleet_spot_g_east1.value)
}

data "aws_servicequotas_service_quota" "fcvm_fleet_on_demand_g_east1" {
  provider     = aws.fcvm_fleet_east1
  service_code = "ec2"
  quota_code   = "L-DB2E81BA"
}

resource "aws_servicequotas_service_quota" "fcvm_fleet_on_demand_g_east1" {
  provider     = aws.fcvm_fleet_east1
  service_code = "ec2"
  quota_code   = "L-DB2E81BA"
  value        = max(local.fcvm_fleet_on_demand_g_vcpus, data.aws_servicequotas_service_quota.fcvm_fleet_on_demand_g_east1.value)
}

output "fcvm_fleet" {
  description = "The fcvm-fleet member account (no credentials); also in /infra/fcvm-fleet"
  value = {
    account_id     = local.fcvm_fleet_account_id
    regions        = local.fcvm_fleet_regions
    state_bucket   = aws_s3_bucket.fcvm_fleet_terraform_state.bucket
    terraform_role = aws_iam_role.fcvm_fleet_terraform.arn
  }
}

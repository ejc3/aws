# astryx-account.tf
#
# A member account of its own for Astryx sync (the sync server of ejc3/astryx-swift: Postgres, zero-cache and the sync API),
# built from here as the constellation account is (constellation-account.tf). Its Terraform is the private repository
# ejc3/astryx-infra, which applies inside this account only: an account, its state bucket and apply role are created by
# this repository's reviewed Terraform from a jumpbox, never by that repository.
#
# Who may apply ejc3/astryx-infra: the same two places as constellation. The apply role `astryx-terraform`
# (AdministratorAccess inside the astryx account, one-hour sessions) trusts dev-server-role pinned to fcvm-arm's instance
# with ec2:SourceInstanceARN (fcvm-x86 shares the role and cannot assume it), granted in assume-constellation-terraform
# because dev-server-role has no room for another policy; and nextjs-dev-role, unpinned (the owner's nextjs-ct container,
# granted by nextjs-dev-fcvm-parity.tf; every member container's session denies sts:AssumeRole). Never a wildcard over member
# accounts, never OrganizationAccountAccessRole, never organizations:* for a dev box.
#
# Guardrails: an SCP on the Astryx OU, as on the fcvm-fleet OU: us-west-1 only (global services exempt), no leaving the
# organization, no IAM users or access keys. Nothing in this (management) account or any other member account trusts the
# astryx account.
#
# No CI credential, as with constellation: GitHub holds no AWS role for this account (state and configuration reads can
# carry secret values). astryx-infra's CI validates without credentials. First apply: the provider alias below needs the
# account id, so apply the account first (-target=aws_organizations_account.astryx), then everything.

locals {
  astryx_regions            = [var.aws_region]
  astryx_account_id         = aws_organizations_account.astryx.id
  astryx_terraform_role_arn = "arn:aws:iam::${aws_organizations_account.astryx.id}:role/astryx-terraform"
  astryx_access_role_arn    = "arn:aws:iam::${aws_organizations_account.astryx.id}:role/OrganizationAccountAccessRole"
}

resource "aws_organizations_organizational_unit" "astryx" {
  name      = "Astryx"
  parent_id = data.aws_organizations_organization.current.roots[0].id
  tags      = { Purpose = "astryx", Managed = "terraform" }
}

resource "aws_organizations_account" "astryx" {
  name                       = "astryx"
  email                      = local.astryx_account_email
  parent_id                  = aws_organizations_organizational_unit.astryx.id
  role_name                  = "OrganizationAccountAccessRole"
  iam_user_access_to_billing = "DENY"
  close_on_deletion          = false

  lifecycle {
    # A change of name or email replaces the account; guard against an accidental replace or destroy.
    prevent_destroy = true
    ignore_changes  = [role_name, iam_user_access_to_billing]
  }

  tags = { Purpose = "astryx", Managed = "terraform" }
}

provider "aws" {
  alias  = "astryx"
  region = var.aws_region
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.astryx.id}:role/OrganizationAccountAccessRole"
  }
}

# ------------------------------------------------------------------------------------------------ the OU's guardrails
locals {
  astryx_scp = {
    Version = "2012-10-17"
    Statement = [
      {
        # The global services stay usable from any console region (the same list as the Dolphin OU's policy).
        Sid       = "DenyOutsideTheAstryxRegions"
        Effect    = "Deny"
        NotAction = local.dolphin_region_scp_global_actions
        Resource  = "*"
        Condition = { StringNotEquals = { "aws:RequestedRegion" = local.astryx_regions } }
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
        Sid      = "DenyBucketsOutsideTheAstryxRegions"
        Effect   = "Deny"
        Action   = "s3:CreateBucket"
        Resource = "*"
        Condition = {
          StringNotEquals = { "s3:LocationConstraint" = local.astryx_regions }
          Null            = { "s3:LocationConstraint" = "false" }
        }
      },
    ]
  }
}

resource "aws_organizations_policy" "astryx_guardrails" {
  name        = "astryx-guardrails"
  description = "Astryx OU: us-west-1 only, no leaving the organization, no IAM users or access keys (astryx-account.tf)"
  type        = "SERVICE_CONTROL_POLICY"
  content     = jsonencode(local.astryx_scp)
  depends_on  = [aws_organizations_organization.security]
}

resource "aws_organizations_policy_attachment" "astryx_guardrails" {
  policy_id = aws_organizations_policy.astryx_guardrails.id
  target_id = aws_organizations_organizational_unit.astryx.id
}

resource "aws_s3_account_public_access_block" "astryx" {
  provider                = aws.astryx
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ------------------------------------------------------------------------------------------------ state bucket
resource "aws_s3_bucket" "astryx_terraform_state" {
  provider = aws.astryx
  bucket   = "astryx-terraform-state-${local.astryx_account_id}"
  tags     = { Name = "astryx-terraform-state", Managed = "terraform" }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "astryx_terraform_state" {
  provider = aws.astryx
  bucket   = aws_s3_bucket.astryx_terraform_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "astryx_terraform_state" {
  provider                = aws.astryx
  bucket                  = aws_s3_bucket.astryx_terraform_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "astryx_terraform_state" {
  provider = aws.astryx
  bucket   = aws_s3_bucket.astryx_terraform_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "astryx_terraform_state" {
  provider = aws.astryx
  bucket   = aws_s3_bucket.astryx_terraform_state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# State can hold secret values: its objects are for the apply role and the management account's access role only.
resource "aws_s3_bucket_policy" "astryx_terraform_state" {
  provider = aws.astryx
  bucket   = aws_s3_bucket.astryx_terraform_state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.astryx_terraform_state.arn, "${aws_s3_bucket.astryx_terraform_state.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "StateObjectsForAppliersOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = "${aws_s3_bucket.astryx_terraform_state.arn}/*"
        Condition = { ArnNotLike = { "aws:PrincipalArn" = [local.astryx_terraform_role_arn, local.astryx_access_role_arn] } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.astryx_terraform_state]
}

# ------------------------------------------------------------------------------------------------ apply role
resource "aws_iam_role" "astryx_terraform" {
  provider             = aws.astryx
  name                 = "astryx-terraform"
  description          = "Applies ejc3/astryx-infra. Assumable only from fcvm-metal-arm's instance and the owner's nextjs-ct container (see ejc3/aws astryx-account.tf)."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "FcvmArmOnly"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.dev_server.arn }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = local.constellation_applier_instance_arn } }
      }, {
      # The owner's nextjs-ct container, as for constellation-terraform: its session carries no ec2:SourceInstanceARN,
      # and every member container's session denies sts:AssumeRole (nextjs-ct-user-data.tf's OwnerOnlyRoles).
      Sid       = "OwnersNextjsContainer"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.nextjs_dev.arn }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = "astryx-terraform", Managed = "terraform" }
}

resource "aws_iam_role_policy_attachment" "astryx_terraform_admin" {
  provider   = aws.astryx
  role       = aws_iam_role.astryx_terraform.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# ------------------------------------------------------------------------------------------------ pointer
locals {
  # What ejc3/astryx-infra's backend and provider name, and what its staging service is given by astryx-stage.tf. No
  # credentials: ids, names and Access's audience tag, which is not a secret.
  astryx_pointer = {
    account_id     = local.astryx_account_id
    region         = var.aws_region
    state_bucket   = aws_s3_bucket.astryx_terraform_state.bucket
    terraform_role = aws_iam_role.astryx_terraform.arn
    stage          = local.astryx_stage_pointer
  }
}

output "astryx_terraform" {
  description = "What ejc3/astryx-infra's backend and provider name (no credentials); also in /infra/astryx"
  value       = local.astryx_pointer
}

# The same, where the appliers can read it (fcvm-arm through assume-constellation-terraform, pinned to its instance; the
# owner's container through nextjs-dev-fcvm-parity): a dev box cannot read this repository's state.
#   aws ssm get-parameter --region us-west-1 --name /infra/astryx --query Parameter.Value --output text
resource "aws_ssm_parameter" "astryx" {
  name        = "/infra/astryx"
  description = "Astryx sync's account, state bucket, apply role and staging settings (astryx-account.tf); no credentials"
  type        = "String"
  tier        = "Standard"
  value       = jsonencode(local.astryx_pointer)
}

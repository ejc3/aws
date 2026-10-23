# constellation-account.tf
#
# A member account of its own for constellation (the private repository ejc3/constellation, its issue #8), built from here
# as the Dolphin account is (dolphin-account.tf, dolphin-bootstrap.tf), not by that repository's own bootstrap: an account,
# its Terraform state bucket and apply role are created by this repository's reviewed Terraform from a jumpbox, and
# constellation's Terraform then applies inside its own account only. constellation must not manage anything below.
#
# Who may apply constellation: fcvm-metal-arm only, where its checkout lives. The apply role `constellation-terraform`
# (AdministratorAccess inside the constellation account, one-hour sessions) trusts dev-server-role pinned to fcvm-arm's
# instance with ec2:SourceInstanceARN, so fcvm-x86, which shares the role, cannot assume it; and dev-server-role's grant
# here names exactly that one role ARN. Never a wildcard over member accounts, never OrganizationAccountAccessRole, never
# organizations:*: those would be admin of every member account (staging, Dolphin) from a dev box.
#
# BOX-WIDE GRANT. Agents run on fcvm-arm with full permissions, so anything there can apply in the constellation account.
# What bounds it is the account boundary: nothing in this (management) account trusts anything in the constellation account.
#
# No CI credential, as with the Dolphin account: GitHub holds no AWS role for this account. Terraform state and the
# configuration APIs (Lambda environment, task definitions, user data) can carry secret values, and any same-repository pull
# request could run a job that reads them. constellation's CI validates without credentials; plans run on fcvm-arm with the
# apply role. First apply: the provider alias below needs the account id, so apply the account first
# (-target=aws_organizations_account.constellation), then everything.

resource "aws_organizations_organizational_unit" "constellation" {
  name      = "Constellation"
  parent_id = data.aws_organizations_organization.current.roots[0].id
  tags      = { Purpose = "constellation", Managed = "terraform" }
}

resource "aws_organizations_account" "constellation" {
  name                       = "constellation"
  email                      = local.constellation_account_email
  parent_id                  = aws_organizations_organizational_unit.constellation.id
  role_name                  = "OrganizationAccountAccessRole"
  iam_user_access_to_billing = "DENY"
  close_on_deletion          = false

  lifecycle {
    # A change of name or email replaces the account; guard against an accidental replace or destroy.
    prevent_destroy = true
    ignore_changes  = [role_name, iam_user_access_to_billing]
  }

  tags = { Purpose = "constellation", Managed = "terraform" }
}

provider "aws" {
  alias  = "constellation"
  region = var.aws_region
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.constellation.id}:role/OrganizationAccountAccessRole"
  }
}

locals {
  constellation_account_id         = aws_organizations_account.constellation.id
  constellation_terraform_role_arn = "arn:aws:iam::${aws_organizations_account.constellation.id}:role/constellation-terraform"
  # fcvm-arm's instance, the one box that may apply constellation. fcvm-arm is optional (enable_firecracker_instance); while
  # it is off this is an ARN no instance has, so the role cannot be assumed at all rather than the plan failing.
  constellation_applier_instance_arn = try(aws_instance.firecracker_dev[0].arn, "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:instance/fcvm-arm-not-running")
}

resource "aws_s3_account_public_access_block" "constellation" {
  provider                = aws.constellation
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ------------------------------------------------------------------------------------------------ state bucket
resource "aws_s3_bucket" "constellation_terraform_state" {
  provider = aws.constellation
  bucket   = "constellation-terraform-state-${local.constellation_account_id}"
  tags     = { Name = "constellation-terraform-state", Managed = "terraform" }
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "constellation_terraform_state" {
  provider = aws.constellation
  bucket   = aws_s3_bucket.constellation_terraform_state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "constellation_terraform_state" {
  provider                = aws.constellation
  bucket                  = aws_s3_bucket.constellation_terraform_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "constellation_terraform_state" {
  provider = aws.constellation
  bucket   = aws_s3_bucket.constellation_terraform_state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "constellation_terraform_state" {
  provider = aws.constellation
  bucket   = aws_s3_bucket.constellation_terraform_state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# State can hold secret values: its objects are for the apply role and the management account's access role only.
resource "aws_s3_bucket_policy" "constellation_terraform_state" {
  provider = aws.constellation
  bucket   = aws_s3_bucket.constellation_terraform_state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.constellation_terraform_state.arn, "${aws_s3_bucket.constellation_terraform_state.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "StateObjectsForAppliersOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = "${aws_s3_bucket.constellation_terraform_state.arn}/*"
        Condition = {
          ArnNotLike = {
            "aws:PrincipalArn" = [
              local.constellation_terraform_role_arn,
              "arn:aws:iam::${local.constellation_account_id}:role/OrganizationAccountAccessRole",
            ]
          }
        }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.constellation_terraform_state]
}

# ------------------------------------------------------------------------------------------------ apply role
resource "aws_iam_role" "constellation_terraform" {
  provider             = aws.constellation
  name                 = "constellation-terraform"
  description          = "Applies ejc3/constellation. Assumable only from fcvm-metal-arm's instance and the owner's nextjs-ct container (see ejc3/aws constellation-account.tf)."
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
      # The owner's nextjs-ct container (nextjs-dev-fcvm-parity.tf). Its session is nextjs-dev-role assumed again by
      # nextjs-ct-imds, which carries no ec2:SourceInstanceARN; every member container's session denies sts:AssumeRole
      # (nextjs-ct-user-data.tf's OwnerOnlyRoles), as for the Dolphin apply role.
      Sid       = "OwnersNextjsContainer"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.nextjs_dev.arn }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = "constellation-terraform", Managed = "terraform" }
}

resource "aws_iam_role_policy_attachment" "constellation_terraform_admin" {
  provider   = aws.constellation
  role       = aws_iam_role.constellation_terraform.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# The one grant on the management-account side: fcvm-arm may assume exactly that role, and nothing else. A managed policy,
# not an inline one: dev-server-role's inline policies are at IAM's 10,240-byte total (the first apply was refused with
# LimitExceeded), and managed policies count against a separate limit (20 per role here).
resource "aws_iam_policy" "dev_server_constellation_terraform" {
  name = "assume-constellation-terraform"
  # The description is immutable (a change replaces the attached policy); it also grants dolphin-terraform (below).
  description = "fcvm-arm may assume constellation-terraform in the constellation account and read /infra/constellation, and nothing else (constellation-account.tf)."
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AssumeTheConstellationApplyRoleOnly"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Resource  = aws_iam_role.constellation_terraform.arn
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = local.constellation_applier_instance_arn } }
      }, {
      # What the role ARN, the state bucket and Lattice staging's Access settings are (/infra/constellation, below): the
      # box cannot read this repository's state, and nothing else of it is written anywhere the box can read.
      Sid       = "ReadTheConstellationPointerOnly"
      Effect    = "Allow"
      Action    = "ssm:GetParameter"
      Resource  = aws_ssm_parameter.constellation.arn
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = local.constellation_applier_instance_arn } }
      }, {
      # Apply dolphin-labs-hq/dolphin-aws (dolphin-bootstrap.tf). Here because dev-server-role has no room for another
      # policy: its inline policies are at IAM's 10,240-character total and it holds 20 of 20 managed ones.
      Sid       = "AssumeTheDolphinApplyRoleOnly"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Resource  = aws_iam_role.dolphin_terraform.arn
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = local.constellation_applier_instance_arn } }
      }, {
      # Apply in the fcvm-fleet account (fcvm-fleet-account.tf), and read where it is (/infra/fcvm-fleet). Same pin.
      Sid       = "AssumeTheFcvmFleetApplyRoleOnly"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Resource  = aws_iam_role.fcvm_fleet_terraform.arn
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = local.constellation_applier_instance_arn } }
      }, {
      Sid       = "ReadTheFcvmFleetPointerOnly"
      Effect    = "Allow"
      Action    = "ssm:GetParameter"
      Resource  = aws_ssm_parameter.fcvm_fleet.arn
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = local.constellation_applier_instance_arn } }
      }, {
      # Apply ejc3/astryx-infra in the astryx account (astryx-account.tf), and read where it is (/infra/astryx). Same pin.
      Sid       = "AssumeTheAstryxApplyRoleOnly"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Resource  = aws_iam_role.astryx_terraform.arn
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = local.constellation_applier_instance_arn } }
      }, {
      Sid       = "ReadTheAstryxPointerOnly"
      Effect    = "Allow"
      Action    = "ssm:GetParameter"
      Resource  = aws_ssm_parameter.astryx.arn
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = local.constellation_applier_instance_arn } }
      }, {
      # opencode's abliteration.ai key (dev-ai-services.tf), on both metal boxes: no instance pin.
      Sid      = "ReadTheAbliterationApiKey"
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = aws_secretsmanager_secret.abliteration_api_key.arn
    }]
  })
  tags = { Name = "assume-constellation-terraform", Managed = "terraform" }
}

resource "aws_iam_role_policy_attachment" "dev_server_constellation_terraform" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.dev_server_constellation_terraform.arn
}

locals {
  # What ejc3/constellation's backend and provider name, and what its Lattice staging service is given by
  # constellation-lattice-stage.tf, and its remote MCP Worker by constellation-mcp-stage.tf. No credentials: ids, names and Access's audience tag, which is not a secret.
  constellation_pointer = {
    account_id     = local.constellation_account_id
    region         = var.aws_region
    state_bucket   = aws_s3_bucket.constellation_terraform_state.bucket
    terraform_role = aws_iam_role.constellation_terraform.arn
    lattice_stage  = local.lattice_stage_pointer
    mcp_stage      = local.constellation_mcp_stage_pointer
  }
}

output "constellation_terraform" {
  description = "What ejc3/constellation's backend and provider name (no credentials); also in /infra/constellation"
  value       = local.constellation_pointer
}

# The same, where fcvm-arm can read it (the grant above, pinned to its instance): a dev box cannot read this repository's
# state (AGENTS.md), so without this the box that applies constellation could not learn the role ARN it is cleared to assume.
#   aws ssm get-parameter --region us-west-1 --name /infra/constellation --query Parameter.Value --output text
resource "aws_ssm_parameter" "constellation" {
  name        = "/infra/constellation"
  description = "constellation's account, state bucket, apply role and Lattice staging settings (constellation-account.tf); no credentials"
  type        = "String"
  tier        = "Standard"
  value       = jsonencode(local.constellation_pointer)
}

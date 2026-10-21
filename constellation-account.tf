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
# CI plans read-only: `constellation-ci-plan` trusts GitHub OIDC for ejc3/constellation's pull_request workflows only
# (ReadOnlyAccess there, state objects readable, secret values denied). The repository variables its plan job reads are set
# below. First apply: the provider alias below needs the account id, so apply the account first
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
  constellation_repository         = "constellation"
  constellation_terraform_role_arn = "arn:aws:iam::${aws_organizations_account.constellation.id}:role/constellation-terraform"
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

# State can hold secret values. Writes are for the apply role and the management account's access role only; reads also
# for the CI plan role, which plans pull requests.
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
        Sid       = "StateWritesForAppliersOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
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
      {
        Sid       = "StateReadsForAppliersAndCiPlan"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource  = "${aws_s3_bucket.constellation_terraform_state.arn}/*"
        Condition = {
          ArnNotLike = {
            "aws:PrincipalArn" = [
              local.constellation_terraform_role_arn,
              "arn:aws:iam::${local.constellation_account_id}:role/constellation-ci-plan",
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
  description          = "Applies ejc3/constellation. Assumable only from fcvm-metal-arm's instance (see ejc3/aws constellation-account.tf)."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "FcvmArmOnly"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.dev_server.arn }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn } }
    }]
  })
  tags = { Name = "constellation-terraform", Managed = "terraform" }
}

resource "aws_iam_role_policy_attachment" "constellation_terraform_admin" {
  provider   = aws.constellation
  role       = aws_iam_role.constellation_terraform.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# The one grant on the management-account side: fcvm-arm may assume exactly that role, and nothing else.
resource "aws_iam_role_policy" "dev_server_constellation_terraform" {
  name = "assume-constellation-terraform"
  role = aws_iam_role.dev_server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AssumeTheConstellationApplyRoleOnly"
      Effect    = "Allow"
      Action    = "sts:AssumeRole"
      Resource  = aws_iam_role.constellation_terraform.arn
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn } }
    }]
  })
}

# ------------------------------------------------------------------------------------------------ CI plan role
resource "aws_iam_openid_connect_provider" "constellation_github" {
  provider       = aws.constellation
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  tags           = { Name = "github-actions", Managed = "terraform" }
}

resource "aws_iam_role" "constellation_ci_plan" {
  provider             = aws.constellation
  name                 = "constellation-ci-plan"
  description          = "Read-only terraform plan for ejc3/constellation's pull requests (GitHub OIDC)."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "ConstellationPullRequestsOnly"
      Effect    = "Allow"
      Action    = "sts:AssumeRoleWithWebIdentity"
      Principal = { Federated = aws_iam_openid_connect_provider.constellation_github.arn }
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:ejc3/${local.constellation_repository}:pull_request"
        }
      }
    }]
  })
  tags = { Name = "constellation-ci-plan", Managed = "terraform" }
}

resource "aws_iam_role_policy_attachment" "constellation_ci_plan_read" {
  provider   = aws.constellation
  role       = aws_iam_role.constellation_ci_plan.name
  policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"
}

# A plan reads configuration and the state, never a secret value.
resource "aws_iam_role_policy" "constellation_ci_plan_state" {
  provider = aws.constellation
  name     = "state-read-no-secrets"
  role     = aws_iam_role.constellation_ci_plan.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadState"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [aws_s3_bucket.constellation_terraform_state.arn, "${aws_s3_bucket.constellation_terraform_state.arn}/*"]
      },
      {
        Sid      = "NoSecretValues"
        Effect   = "Deny"
        Action   = ["secretsmanager:GetSecretValue", "ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath", "kms:Decrypt"]
        Resource = "*"
      },
    ]
  })
}

# ------------------------------------------------------------------------------------------------ the repository's variables
resource "github_actions_variable" "constellation_ci_plan_role" {
  provider      = github.agents_md
  repository    = local.constellation_repository
  variable_name = "AWS_CI_PLAN_ROLE_ARN"
  value         = aws_iam_role.constellation_ci_plan.arn
}

resource "github_actions_variable" "constellation_state_bucket" {
  provider      = github.agents_md
  repository    = local.constellation_repository
  variable_name = "AWS_STATE_BUCKET"
  value         = aws_s3_bucket.constellation_terraform_state.bucket
}

output "constellation_terraform" {
  description = "What ejc3/constellation's backend and provider name (no credentials)"
  value = {
    account_id       = local.constellation_account_id
    region           = var.aws_region
    state_bucket     = aws_s3_bucket.constellation_terraform_state.bucket
    terraform_role   = aws_iam_role.constellation_terraform.arn
    ci_plan_role_arn = aws_iam_role.constellation_ci_plan.arn
  }
}

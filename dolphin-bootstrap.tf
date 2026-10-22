# dolphin-bootstrap.tf
#
# What the Dolphin account's own Terraform (the private repository dolphin-labs-hq/dolphin-aws) needs before it can run
# `terraform init`: a state bucket in the Dolphin account and the role it applies with. Both are built from here, through
# OrganizationAccountAccessRole, because something has to exist first. dolphin-aws must not manage either of them.
#
# Applying dolphin-aws happens on nextjs-dev and fcvm-metal-arm, never in CI: nextjs-dev's instance role, and dev-server-role
# from fcvm-arm's instance only (owner, 2026-10-22: "i want fcvm to have rights and ejc3 on dolphin"), may assume
# `dolphin-terraform` (AdministratorAccess inside the Dolphin account, one-hour sessions) and nothing else in that account.
#
# BOX-WIDE GRANT. An instance-role grant is a grant to the whole box. Every account on nextjs-dev has full passwordless
# sudo and agents run there, so ANYONE ON THAT BOX CAN APPLY IN THE DOLPHIN ACCOUNT (the member containers' session policies
# deny sts:AssumeRole, but the host and the retired box hold the raw role); on fcvm-arm every agent runs as ubuntu. What bounds it is the account
# boundary: the role is admin of the Dolphin account only, and nothing in this (management) account trusts anything in
# the Dolphin account. See AGENTS.md, "The Dolphin account".

provider "aws" {
  alias  = "dolphin"
  region = var.aws_region
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

locals {
  dolphin_account_id         = aws_organizations_account.dolphin.id
  dolphin_state_bucket       = "dolphin-terraform-state-${aws_organizations_account.dolphin.id}"
  dolphin_terraform_role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/dolphin-terraform"
  # The owner's Identity Center administrator sessions in the Dolphin account (dolphin-identity-center.tf). The path
  # carries the Identity Center region on newer instances, so the wildcard spans it.
  dolphin_sso_admin_role_arns = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/aws-reserved/sso.amazonaws.com/*AWSReservedSSO_AdministratorAccess_*"
}

resource "aws_s3_account_public_access_block" "dolphin" {
  provider                = aws.dolphin
  account_id              = local.dolphin_account_id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

# ------------------------------------------------------------------------------------------------ state bucket
# S3 native locking (use_lockfile in dolphin-aws's backend): no DynamoDB table.
resource "aws_s3_bucket" "dolphin_terraform_state" {
  provider = aws.dolphin
  bucket   = local.dolphin_state_bucket
  tags     = { Name = local.dolphin_state_bucket, Purpose = "dolphin-aws terraform state", Managed = "terraform" }

  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_versioning" "dolphin_terraform_state" {
  provider = aws.dolphin
  bucket   = aws_s3_bucket.dolphin_terraform_state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "dolphin_terraform_state" {
  provider                = aws.dolphin
  bucket                  = aws_s3_bucket.dolphin_terraform_state.id
  block_public_acls       = true
  ignore_public_acls      = true
  block_public_policy     = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "dolphin_terraform_state" {
  provider = aws.dolphin
  bucket   = aws_s3_bucket.dolphin_terraform_state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_ownership_controls" "dolphin_terraform_state" {
  provider = aws.dolphin
  bucket   = aws_s3_bucket.dolphin_terraform_state.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

# State can hold secret values, so its objects are for the appliers only: the apply role, the owner's Identity Center
# administrator sessions and the management account's access role. A read-only session (Steve's DolphinReadOnly
# includes s3:Get*) is refused whatever its identity policy says.
resource "aws_s3_bucket_policy" "dolphin_terraform_state" {
  provider = aws.dolphin
  bucket   = aws_s3_bucket.dolphin_terraform_state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.dolphin_terraform_state.arn, "${aws_s3_bucket.dolphin_terraform_state.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "StateObjectsForAppliersOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = "${aws_s3_bucket.dolphin_terraform_state.arn}/*"
        Condition = {
          ArnNotLike = {
            "aws:PrincipalArn" = [
              local.dolphin_terraform_role_arn,
              local.dolphin_sso_admin_role_arns,
              "arn:aws:iam::${local.dolphin_account_id}:role/OrganizationAccountAccessRole",
            ]
          }
        }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.dolphin_terraform_state]
}

# ------------------------------------------------------------------------------------------------ apply role
resource "aws_iam_role" "dolphin_terraform" {
  provider             = aws.dolphin
  name                 = "dolphin-terraform"
  description          = "Applies dolphin-labs-hq/dolphin-aws. Assumable only by nextjs-dev's instance role and fcvm-metal-arm's (box-wide; see ejc3/aws dolphin-bootstrap.tf)."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "NextjsDevOnly"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.nextjs_dev.arn }
      Action    = "sts:AssumeRole"
      }, {
      # dev-server-role is fcvm-x86's too: pinned to fcvm-arm's instance, as constellation-terraform's trust is.
      Sid       = "FcvmArmOnly"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.dev_server.arn }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = local.constellation_applier_instance_arn } }
    }]
  })
  tags = { Name = "dolphin-terraform", Managed = "terraform" }
}

resource "aws_iam_role_policy_attachment" "dolphin_terraform_admin" {
  provider   = aws.dolphin
  role       = aws_iam_role.dolphin_terraform.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# The one grant on the management-account side: nextjs-dev may assume exactly that role, and nothing else.
resource "aws_iam_role_policy" "nextjs_dev_dolphin_terraform" {
  name = "assume-dolphin-terraform"
  role = aws_iam_role.nextjs_dev.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "AssumeTheDolphinApplyRoleOnly"
      Effect   = "Allow"
      Action   = "sts:AssumeRole"
      Resource = aws_iam_role.dolphin_terraform.arn
    }]
  })
}

output "dolphin_terraform" {
  description = "What dolphin-labs-hq/dolphin-aws's backend and provider name"
  value = {
    account_id = local.dolphin_account_id
    region     = var.aws_region
    bucket     = aws_s3_bucket.dolphin_terraform_state.bucket
    role_arn   = aws_iam_role.dolphin_terraform.arn
  }
}

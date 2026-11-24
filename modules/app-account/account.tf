locals {
  account_id         = aws_organizations_account.this.id
  terraform_role_arn = "arn:aws:iam::${aws_organizations_account.this.id}:role/${var.app}-terraform"
  access_role_arn    = "arn:aws:iam::${aws_organizations_account.this.id}:role/OrganizationAccountAccessRole"
}

resource "aws_organizations_organizational_unit" "this" {
  name      = var.ou_name
  parent_id = var.organization_root_id
  tags      = { Purpose = var.app, Managed = "terraform" }
}

resource "aws_organizations_account" "this" {
  name                       = var.app
  email                      = var.email
  parent_id                  = aws_organizations_organizational_unit.this.id
  role_name                  = "OrganizationAccountAccessRole"
  iam_user_access_to_billing = "DENY"
  close_on_deletion          = false
  lifecycle {
    # A change of name or email replaces the account; guard against an accidental replace or destroy.
    prevent_destroy = true
    ignore_changes  = [role_name, iam_user_access_to_billing]
  }
  tags = { Purpose = var.app, Managed = "terraform" }
}

# ------------------------------------------------------------------------------------------ guardrails
resource "aws_organizations_policy" "guardrails" {
  name        = "${var.app}-guardrails"
  description = "${var.ou_name} OU: ${var.region} only, no leaving the organization, no IAM users or access keys (modules/app-account)"
  type        = "SERVICE_CONTROL_POLICY"
  content = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyOutsideTheAppsRegion"
        Effect    = "Deny"
        NotAction = var.global_actions
        Resource  = "*"
        Condition = { StringNotEquals = { "aws:RequestedRegion" = [var.region] } }
      },
      {
        Sid      = "DenyLeavingTheOrganization"
        Effect   = "Deny"
        Action   = "organizations:LeaveOrganization"
        Resource = "*"
      },
      {
        Sid      = "DenyIamUsersAndKeys"
        Effect   = "Deny"
        Action   = ["iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile", "iam:UpdateLoginProfile"]
        Resource = "*"
      },
      {
        # aws:RequestedRegion is the endpoint, not where a bucket goes.
        Sid      = "DenyBucketsOutsideTheAppsRegion"
        Effect   = "Deny"
        Action   = "s3:CreateBucket"
        Resource = "*"
        Condition = {
          StringNotEquals = { "s3:LocationConstraint" = [var.region] }
          Null            = { "s3:LocationConstraint" = "false" }
        }
      },
    ]
  })
}

resource "aws_organizations_policy_attachment" "guardrails" {
  policy_id = aws_organizations_policy.guardrails.id
  target_id = aws_organizations_organizational_unit.this.id
}

resource "aws_s3_account_public_access_block" "this" {
  provider                = aws.app
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ------------------------------------------------------------------------------------------ state bucket
resource "aws_s3_bucket" "state" {
  provider = aws.app
  bucket   = "${var.app}-terraform-state-${local.account_id}"
  tags     = { Name = "${var.app}-terraform-state", Managed = "terraform" }
  lifecycle {
    prevent_destroy = true
  }
}
resource "aws_s3_bucket_versioning" "state" {
  provider = aws.app
  bucket   = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}
resource "aws_s3_bucket_public_access_block" "state" {
  provider                = aws.app
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  provider = aws.app
  bucket   = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
resource "aws_s3_bucket_ownership_controls" "state" {
  provider = aws.app
  bucket   = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}
# State can hold secret values: its objects are for the apply role and the access role only.
resource "aws_s3_bucket_policy" "state" {
  provider = aws.app
  bucket   = aws_s3_bucket.state.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "StateObjectsForAppliersOnly"
        Effect    = "Deny"
        Principal = "*"
        Action    = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion"]
        Resource  = "${aws_s3_bucket.state.arn}/*"
        Condition = { ArnNotLike = { "aws:PrincipalArn" = [local.terraform_role_arn, local.access_role_arn] } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.state]
}

# ------------------------------------------------------------------------------------------ apply role
resource "aws_iam_role" "terraform" {
  provider             = aws.app
  name                 = "${var.app}-terraform"
  description          = "Applies ${var.app}'s Terraform in this account. Assumable only from fcvm-metal-arm's instance and the owner's nextjs-ct container (ejc3/aws modules/app-account)."
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "FcvmArmOnly"
      Effect    = "Allow"
      Principal = { AWS = var.appliers.dev_server_role_arn }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "ec2:SourceInstanceARN" = var.appliers.applier_instance_arn } }
      }, {
      Sid       = "OwnersNextjsContainer"
      Effect    = "Allow"
      Principal = { AWS = var.appliers.nextjs_dev_role_arn }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = "${var.app}-terraform", Managed = "terraform" }
}
resource "aws_iam_role_policy_attachment" "terraform_admin" {
  provider   = aws.app
  role       = aws_iam_role.terraform.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# ------------------------------------------------------------------------------------------ budget
resource "aws_budgets_budget" "monthly" {
  provider     = aws.app
  name         = "${var.app}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_email]
  }
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_email]
  }
}

# ------------------------------------------------------------------------------------------ pointer
locals {
  # What the app's repository's backend and provider name (no credentials; Access's audience tag is not a secret).
  pointer = {
    account_id     = local.account_id
    region         = var.region
    state_bucket   = aws_s3_bucket.state.bucket
    terraform_role = aws_iam_role.terraform.arn
    stage          = var.stage == null ? null : local.stage_pointer
  }
}
#   aws ssm get-parameter --region <region> --name /infra/<app> --query Parameter.Value --output text
resource "aws_ssm_parameter" "pointer" {
  name        = "/infra/${var.app}"
  description = "${var.app}'s account, state bucket, apply role and staging settings (modules/app-account); no credentials"
  type        = "String"
  tier        = "Standard"
  value       = jsonencode(local.pointer)
}

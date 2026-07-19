# dev-staging-bootstrap.tf
#
# Wire the dev-staging member account so terraform + GitHub Actions can deploy into it
# and it can receive cross-account backup copies (the offsite copy that survives even a
# main-account compromise).

# Cross-account provider: assume OrganizationAccountAccessRole in dev-staging.
provider "aws" {
  alias  = "staging"
  region = var.aws_region
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dev_staging[0].id}:role/OrganizationAccountAccessRole"
  }
}

data "aws_caller_identity" "staging" {
  provider = aws.staging
}

# --- GitHub Actions OIDC + deploy role IN dev-staging (for promote-through-staging CI) ---
resource "aws_iam_openid_connect_provider" "github_staging" {
  provider        = aws.staging
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

resource "aws_iam_role" "github_actions_staging" {
  provider = aws.staging
  name     = "github-actions-terraform"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github_staging.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com" }
        StringLike   = { "token.actions.githubusercontent.com:sub" = "repo:ejc3/aws:*" }
      }
    }]
  })
}

# Staging is an isolated verify account: full deploy rights here are fine and are the point.
resource "aws_iam_role_policy_attachment" "github_actions_staging_admin" {
  provider   = aws.staging
  role       = aws_iam_role.github_actions_staging.name
  policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"
}

# --- Cross-account backup copy target in dev-staging ---
resource "aws_backup_vault" "staging" {
  provider = aws.staging
  name     = "ejc3-backup"
  tags     = { Name = "ejc3-backup", Managed = "terraform", Purpose = "cross-account-copy-target" }
}

resource "aws_backup_vault_policy" "staging" {
  provider          = aws.staging
  backup_vault_name = aws_backup_vault.staging.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AllowCopyFromMainAccount"
      Effect    = "Allow"
      Principal = { AWS = "arn:aws:iam::928413605543:root" }
      Action    = "backup:CopyIntoBackupVault"
      Resource  = "*"
    }]
  })
}

# Enable cross-account backup at the org level (management account = main).
resource "aws_backup_global_settings" "main" {
  global_settings = {
    "isCrossAccountBackupEnabled" = "true"
  }
}

output "dev_staging_github_role_arn" {
  description = "GitHub Actions terraform role in dev-staging"
  value       = aws_iam_role.github_actions_staging.arn
}

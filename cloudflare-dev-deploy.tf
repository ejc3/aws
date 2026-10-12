# cloudflare-dev-deploy.tf
#
# A Cloudflare token the metal dev boxes may read, so an agent there can ship a Workers app end to
# end: create its D1 database, run its first deploy, and install the token as the app repo's
# CLOUDFLARE_API_TOKEN so the repo's CI deploys every merge after that (owner, 2026-10-12: the dev
# box should be doing all the deploys).
#
#   cloudflare-dev-deploy-token   Secrets Manager (us-west-1), the raw token string.
#                                 Permissions: Workers Scripts Write, D1 Write and Account Settings
#                                 Read on this one account. No zones, DNS, Access, tunnels or tokens.
#
#   export CLOUDFLARE_API_TOKEN=$(aws secretsmanager get-secret-value --region us-west-1 \
#     --secret-id cloudflare-dev-deploy-token --query SecretString --output text)
#
# Separate from cloudflare-workers-deploy-token (workers-deploy.tf), which stays administration-only
# and is what the site repos' Actions hold. Readers: the administration set and dev-server-role (the
# metal boxes). Not nextjs-dev-role: every account there has full sudo. Not IP-pinned: the same token
# is installed into GitHub for each app's CI, and GitHub's runners have no stable address.
#
# REACH: Workers Scripts Write is account-wide (Cloudflare has no per-script scope), so this token can
# replace or delete any Worker in the account, the `<site>-stage` copies and colton-games-prod included.
#
# Same shape as workers-deploy.tf: a typed cloudflare_account_token minted through the token_minter
# provider alias, its value written to the container by Terraform (so it is in state; dev boxes cannot
# read state). Each pinned permission group id is checked against its name at plan time. ROTATE with
# `terraform apply -replace=cloudflare_account_token.dev_deploy`, then re-install it in each app repo.

resource "aws_secretsmanager_secret" "cloudflare_dev_deploy_token" {
  name                    = "cloudflare-dev-deploy-token"
  description             = "Cloudflare API token (Workers Scripts Write, D1 Write, Account Settings Read; account ${var.cloudflare_account_id}) the metal dev boxes use to create and deploy Workers apps. Written by Terraform from cloudflare_account_token.dev_deploy; see cloudflare-dev-deploy.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "cloudflare-dev-deploy-token", Project = "dev" }
}

locals {
  cloudflare_dev_deploy_readers = [aws_iam_role.dev_server.arn]

  # Permission group ids by their exact Cloudflare names (GET /accounts/<id>/tokens/permission_groups),
  # pinned by scripts/test-cloudflare-dev-deploy.py and checked against the live names at plan time.
  cloudflare_dev_deploy_groups = {
    "Workers Scripts Write" = "e086da7e2179491d91ee5f35b3ca210a"
    "D1 Write"              = "09b2857d1c31407795e75e3fed8617a1"
    "Account Settings Read" = "c1fde68c7bcc44588cbb6ddbc16d6480"
  }
}

resource "aws_secretsmanager_secret_policy" "cloudflare_dev_deploy_token" {
  secret_arn = aws_secretsmanager_secret.cloudflare_dev_deploy_token.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheMetalBoxesCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.cloudflare_dev_deploy_token.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.cloudflare_dev_deploy_readers) } }
    }]
  })
}

data "cloudflare_account_permission_group" "dev_deploy" {
  for_each            = local.cloudflare_dev_deploy_groups
  provider            = cloudflare.token_minter
  account_id          = var.cloudflare_account_id
  permission_group_id = each.value
}

resource "cloudflare_account_token" "dev_deploy" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "dev-deploy-terraform"
  policies = [{
    effect            = "allow"
    permission_groups = [for id in values(local.cloudflare_dev_deploy_groups) : { id = id }]
    resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
  }]

  lifecycle {
    precondition {
      condition     = alltrue([for name, group in data.cloudflare_account_permission_group.dev_deploy : group.name == name])
      error_message = "A pinned permission group id in local.cloudflare_dev_deploy_groups does not carry the name it is listed under."
    }
  }
}

resource "aws_secretsmanager_secret_version" "cloudflare_dev_deploy_token" {
  secret_id     = aws_secretsmanager_secret.cloudflare_dev_deploy_token.id
  secret_string = cloudflare_account_token.dev_deploy.value
}

data "aws_iam_policy_document" "cloudflare_dev_deploy_read" {
  statement {
    sid       = "ReadTheCloudflareDevDeployToken"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.cloudflare_dev_deploy_token.arn]
  }
}

# A managed policy, not inline: dev-server-role's inline policies are within a few hundred
# characters of IAM's 10,240 limit (dev-instance-common.tf).
resource "aws_iam_policy" "cloudflare_dev_deploy_read" {
  name        = "dev-cloudflare-dev-deploy-read"
  description = "Metal dev boxes: read the Cloudflare Workers and D1 deploy token (cloudflare-dev-deploy-token) and nothing else"
  policy      = data.aws_iam_policy_document.cloudflare_dev_deploy_read.json
}

resource "aws_iam_role_policy_attachment" "cloudflare_dev_deploy_read" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.cloudflare_dev_deploy_read.arn
}

output "cloudflare_dev_deploy_token_secret" {
  description = "Secrets Manager name of the dev boxes' Workers and D1 deploy token (us-west-1). Written by Terraform from cloudflare_account_token.dev_deploy."
  value       = aws_secretsmanager_secret.cloudflare_dev_deploy_token.name
}

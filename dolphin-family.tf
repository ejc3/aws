# dolphin-family.tf
#
# Dolphin Family (dolphin-labs-hq/dolphin-family): families' boards on the wall and the phone, the family's data
# end-to-end encrypted. Its service is one Cloudflare Worker with a Durable Object per household and a D1 database,
# deployed by that repository's CI on merge to main as `dolphin-family-stage` (adopted into workers-stage.tf after
# its first deploy, with its URLs off until then). The same shape as the Dolphin relay (dolphin-relay.tf):
#
#   - Its own deploy token: Workers Scripts Write, D1 Write and Account Settings Read. The shared workers_deploy token
#     has no D1 permission, and the deploy creates the database (by name, once) and runs its migrations.
#   - The token lives ONLY in a protected GitHub environment, `cloudflare-deploy`, in dolphin-family, whose deployment
#     policy admits `main` alone, as for dolphin-labs and dolphin-films (dolphin-labs-ci-logs.tf). Workers Scripts
#     Write cannot be scoped to one Worker, so the environment's branch rule is what keeps it to reviewed code. No dev
#     box can read it.
#
# Values in state: the deploy token, as with the other tokens Terraform mints (workers-deploy.tf); state is
# administration-only. ROTATE: `terraform apply -replace=cloudflare_account_token.dolphin_family_deploy`; Terraform
# rewrites the container and the environment secret, and the next deploy picks it up.

locals {
  dolphin_family_repository  = "dolphin-family"
  dolphin_family_environment = "cloudflare-deploy"
}

resource "cloudflare_account_token" "dolphin_family_deploy" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "dolphin-family-deploy"
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.workers_scripts_write }, { id = local.d1_write }, { id = local.account_settings_read }]
    resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
  }]
}

# A record of the token for administrators (rotation checks, a manual deploy in an emergency). Never readable by a dev
# box, like the relay's.
resource "aws_secretsmanager_secret" "dolphin_family_deploy_token" {
  name                    = "dolphin-family/deploy-token"
  description             = "Cloudflare token dolphin-family deploys with (Workers Scripts Write, D1 Write, Account Settings Read). Written by Terraform; dolphin-family.tf. Administration only."
  recovery_window_in_days = 7
  tags                    = { Name = "dolphin-family/deploy-token", Project = "dolphin-family" }
}

resource "aws_secretsmanager_secret_policy" "dolphin_family_deploy_token" {
  secret_arn = aws_secretsmanager_secret.dolphin_family_deploy_token.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.dolphin_family_deploy_token.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

resource "aws_secretsmanager_secret_version" "dolphin_family_deploy_token" {
  secret_id     = aws_secretsmanager_secret.dolphin_family_deploy_token.id
  secret_string = cloudflare_account_token.dolphin_family_deploy.value
}

# Only a job running on `main` may enter this environment, so only reviewed code gets the token. The administration
# app's provider on dolphin-labs-hq (dolphin-labs-ci-logs.tf), which has environments, secrets and variables write.
resource "github_repository_environment" "dolphin_family" {
  provider    = github.dolphin_labs_ci
  repository  = local.dolphin_family_repository
  environment = local.dolphin_family_environment
  # GitHub stores true for a private repository whatever is sent; the main-only branch policy is what holds (the
  # probe recorded in dolphin-relay.tf, 2026-10-12).
  can_admins_bypass = true
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "dolphin_family_main" {
  provider       = github.dolphin_labs_ci
  repository     = local.dolphin_family_repository
  environment    = github_repository_environment.dolphin_family.environment
  branch_pattern = "main"
}

resource "github_actions_environment_secret" "dolphin_family_cloudflare_token" {
  provider        = github.dolphin_labs_ci
  repository      = local.dolphin_family_repository
  environment     = github_repository_environment.dolphin_family.environment
  secret_name     = "CLOUDFLARE_API_TOKEN"
  plaintext_value = cloudflare_account_token.dolphin_family_deploy.value
}

resource "github_actions_environment_variable" "dolphin_family_account" {
  provider      = github.dolphin_labs_ci
  repository    = local.dolphin_family_repository
  environment   = github_repository_environment.dolphin_family.environment
  variable_name = "CLOUDFLARE_ACCOUNT_ID"
  value         = var.cloudflare_account_id
}

output "dolphin_family" {
  description = "Where dolphin-family's deploy credential lives (no values)."
  value = {
    github_environment  = "dolphin-labs-hq/${local.dolphin_family_repository}:${local.dolphin_family_environment}"
    deploy_token_secret = aws_secretsmanager_secret.dolphin_family_deploy_token.name
  }
}

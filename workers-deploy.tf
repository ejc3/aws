# workers-deploy.tf
#
# The credential the sites' GitHub Actions use to deploy a second copy of themselves to
# Cloudflare Workers (OpenNext) in the fleet's account. One token for every site repo:
#
#   cloudflare-workers-deploy-token   Secrets Manager (us-west-1), the raw token string.
#                                     Permissions: Workers Scripts Write and Account Settings
#                                     Read on this one account, nothing else (no zones, no DNS,
#                                     no Access, no tunnels).
#
# The token is a typed Terraform resource (cloudflare_account_token), minted through an aliased
# provider that reads the account-owned `cloudflare-account-token` ephemerally (see AGENTS.md,
# "Minting tokens"), and its value is written to the secret by Terraform. Both live in state, as the
# Workers Builds deploy token does (issue #22); dev boxes cannot read state. Issue #16 rules out
# curl and local-exec for Cloudflare resources, so there is no mint script. ROTATE with
# `terraform apply -replace=cloudflare_account_token.workers_deploy`, then re-run
# `scripts/workers-deploy-secret.sh OWNER/REPO` for each site repo except dolphin-labs and dolphin-films, whose
# copies are environment secrets the same apply rewrites (dolphin-labs-ci-logs.tf; the script refuses those repos). The token is deliberately not
# IP-pinned: GitHub's runners have no stable address.
#
# WHERE THE VALUE GOES: a GitHub Actions secret named CLOUDFLARE_API_TOKEN in each site repo
# (`scripts/workers-deploy-secret.sh OWNER/REPO`). That is a deploy-capable credential in
# GitHub, chosen by the owner on 2026-10-06 over Cloudflare Workers Builds (which needs a
# browser authorization per repository owner). Its reach is bounded to Workers scripts in
# this account; it cannot touch DNS, Access or the tunnels that protect the dev boxes.
#
# The Workers are STAGING copies: `<site>-stage`, behind Cloudflare Access (workers-stage.tf).
# Production stays on Vercel.

resource "aws_secretsmanager_secret" "cloudflare_workers_deploy_token" {
  name                    = "cloudflare-workers-deploy-token"
  description             = "Cloudflare API token (Workers Scripts Write + Account Settings Read, account ${var.cloudflare_account_id}) the site repos' GitHub Actions use to deploy Workers. Written by Terraform from cloudflare_account_token.workers_deploy; see workers-deploy.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "cloudflare-workers-deploy-token", Project = "workers-deploy" }
}

# Administrators only, whatever an identity policy elsewhere says: a dev box has no reason
# to deploy a Worker, and nextjs-dev gives every account sudo.
resource "aws_secretsmanager_secret_policy" "cloudflare_workers_deploy_token" {
  secret_arn = aws_secretsmanager_secret.cloudflare_workers_deploy_token.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.cloudflare_workers_deploy_token.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

locals {
  # Permission group ids (GET /accounts/<id>/tokens/permission_groups), pinned by scripts/test-workers-deploy.py.
  workers_scripts_write = "e086da7e2179491d91ee5f35b3ca210a"
  account_settings_read = "c1fde68c7bcc44588cbb6ddbc16d6480"

  workers_deploy_policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.workers_scripts_write }, { id = local.account_settings_read }]
    resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
  }]
}

# The account-owned token that may create other account tokens. It pre-exists Terraform (cold bootstrap) and is read
# ephemerally, so it is never in state.
ephemeral "aws_secretsmanager_secret_version" "cloudflare_account_token" {
  secret_id = "cloudflare-account-token"
}

provider "cloudflare" {
  alias = "token_minter"
  # The stored secret ends in a newline; the provider accepts only token characters.
  api_token = trimspace(ephemeral.aws_secretsmanager_secret_version.cloudflare_account_token.secret_string)
}

resource "cloudflare_account_token" "workers_deploy" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "workers-deploy-terraform"
  policies   = local.workers_deploy_policies
}

resource "aws_secretsmanager_secret_version" "cloudflare_workers_deploy_token" {
  secret_id     = aws_secretsmanager_secret.cloudflare_workers_deploy_token.id
  secret_string = cloudflare_account_token.workers_deploy.value
}

output "cloudflare_workers_deploy_token_secret" {
  description = "Secrets Manager name of the Workers deploy token (us-west-1). Written by Terraform from cloudflare_account_token.workers_deploy."
  value       = aws_secretsmanager_secret.cloudflare_workers_deploy_token.name
}

# ============================================================ where the token lives in GitHub
# Each site repo's deploy job takes this token from a `cloudflare-deploy` environment admitted to `main` only, never
# from a repository secret: a repository secret is readable by a workflow run on any branch, and Workers Scripts Write
# reaches every Worker in the account, production included (dolphin-relay.tf), so the branch rule is the control.
# dolphin-labs and dolphin-films are in dolphin-labs-ci-logs.tf (#452, #455); these are the rest (ejc3/aws#466; fcvm, for
# the fcvm-fleet-stage Worker at stage.fcvm.dev, ejc3/aws#627: main only, like the others, never a feature branch;
# constellation, for the constellation-mcp-stage Worker, constellation-mcp-stage.tf). Once a
# repo's deploy job names the environment, its repository secret is deleted. Rotating workers_deploy rewrites every one
# on the next apply. ts-api is not here: it deploys to its own Cloudflare account with its own token.
locals {
  # repo name => the ejc3 repos, through the administration agent's GitHub App on ejc3 (agents-md.tf's provider).
  workers_deploy_environments_ejc3 = toset(["imagine", "remote-claw", "fcvm", "constellation"])
}

resource "github_repository_environment" "workers_deploy_ejc3" {
  provider          = github.agents_md
  for_each          = local.workers_deploy_environments_ejc3
  repository        = each.key
  environment       = "cloudflare-deploy"
  can_admins_bypass = true
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "workers_deploy_ejc3_main" {
  provider       = github.agents_md
  for_each       = local.workers_deploy_environments_ejc3
  repository     = each.key
  environment    = github_repository_environment.workers_deploy_ejc3[each.key].environment
  branch_pattern = "main"
}

resource "github_actions_environment_secret" "workers_deploy_ejc3" {
  provider        = github.agents_md
  for_each        = local.workers_deploy_environments_ejc3
  repository      = each.key
  environment     = github_repository_environment.workers_deploy_ejc3[each.key].environment
  secret_name     = "CLOUDFLARE_API_TOKEN"
  plaintext_value = cloudflare_account_token.workers_deploy.value
}

# dolphin-maps, through the same App's dolphin-labs-hq installation (dolphin-labs-ci-logs.tf's provider).
resource "github_repository_environment" "workers_deploy_dolphin_maps" {
  provider          = github.dolphin_labs_ci
  repository        = "dolphin-maps"
  environment       = "cloudflare-deploy"
  can_admins_bypass = true
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "workers_deploy_dolphin_maps_main" {
  provider       = github.dolphin_labs_ci
  repository     = "dolphin-maps"
  environment    = github_repository_environment.workers_deploy_dolphin_maps.environment
  branch_pattern = "main"
}

resource "github_actions_environment_secret" "workers_deploy_dolphin_maps" {
  provider        = github.dolphin_labs_ci
  repository      = "dolphin-maps"
  environment     = github_repository_environment.workers_deploy_dolphin_maps.environment
  secret_name     = "CLOUDFLARE_API_TOKEN"
  plaintext_value = cloudflare_account_token.workers_deploy.value
}

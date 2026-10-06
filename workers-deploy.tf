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
# `scripts/workers-deploy-secret.sh OWNER/REPO` for each site repo. The token is deliberately not
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

# The token the first version of this file minted with curl (scripts/workers-deploy-token.sh, since removed). Adopted so
# a follow-up change can revoke it through Terraform instead of by hand. Its value is unknown to state; nothing reads it.
import {
  to = cloudflare_account_token.workers_deploy_legacy
  id = "${var.cloudflare_account_id}/1fab0b6a37d726771b3314ee40bd89b8"
}

resource "cloudflare_account_token" "workers_deploy_legacy" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "workers-deploy"
  policies   = local.workers_deploy_policies

  lifecycle {
    ignore_changes = all
  }
}

output "cloudflare_workers_deploy_token_secret" {
  description = "Secrets Manager name of the Workers deploy token (us-west-1). Written by Terraform from cloudflare_account_token.workers_deploy."
  value       = aws_secretsmanager_secret.cloudflare_workers_deploy_token.name
}

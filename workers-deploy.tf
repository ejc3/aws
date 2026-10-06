# workers-deploy.tf
#
# The credential the sites' GitHub Actions use to deploy a second copy of themselves to
# Cloudflare Workers (OpenNext) in the fleet's account. One token for every site repo:
#
#   cloudflare-workers-deploy-token   Secrets Manager (us-west-1), the raw token string.
#                                     Permissions: Workers Scripts Write and Account Settings
#                                     Read on this one account, nothing else (no zones, no DNS,
#                                     no Access, no tunnels). Value set out of band.
#
# Terraform owns only the container and who may read it. It never reads the value, so the
# token is not in state. `scripts/workers-deploy-token.sh` mints it from the account-owned
# `cloudflare-account-token` (see AGENTS.md, "Minting tokens"), stores it, and revokes the
# one it replaces, so a rotation is one command. The token is deliberately not IP-pinned:
# GitHub's runners have no stable address.
#
# WHERE THE VALUE GOES: a GitHub Actions secret named CLOUDFLARE_API_TOKEN in each site repo
# (`scripts/workers-deploy-secret.sh OWNER/REPO`). That is a deploy-capable credential in
# GitHub, chosen by the owner on 2026-10-06 over Cloudflare Workers Builds (which needs a
# browser authorization per repository owner). Its reach is bounded to Workers scripts in
# this account; it cannot touch DNS, Access or the tunnels that protect the dev boxes.
#
# The Workers are STAGING copies: `<site>-stage`, behind Cloudflare Access, running with the
# site's NON-PRODUCTION credentials. Production stays on Vercel.

resource "aws_secretsmanager_secret" "cloudflare_workers_deploy_token" {
  name                    = "cloudflare-workers-deploy-token"
  description             = "Cloudflare API token (Workers Scripts Write + Account Settings Read, account ${var.cloudflare_account_id}) the site repos' GitHub Actions use to deploy Workers. Value set by scripts/workers-deploy-token.sh; see workers-deploy.tf."
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

output "cloudflare_workers_deploy_token_secret" {
  description = "Secrets Manager name of the Workers deploy token (us-west-1). Value set by scripts/workers-deploy-token.sh."
  value       = aws_secretsmanager_secret.cloudflare_workers_deploy_token.name
}

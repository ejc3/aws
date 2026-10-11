# github-admin-tokens.tf
#
# GitHub tokens for the administration agent alone (owner, 2026-10-10: "i want you and only you to have access to a
# pat"), so repository administration (runner tokens, webhooks, Actions secrets and variables, environments) needs no
# browser. One fine-grained token per resource owner, because a fine-grained token has exactly one:
#
#   github-admin-token/dolphin-labs-hq   all of the organization's repositories
#   github-admin-token/ejc3              all of the user's repositories
#
# Readers: the jumpboxes' one role (jumpbox-admin-role, shared by jumpbox and jumpbox-2) and nothing else, enforced by
# a resource policy that denies everyone else whatever an identity policy says. Deliberately NOT the shared
# administration list (local.games_mp_admin_principals): it also matches a person's Identity Center
# AdministratorAccess session, and these are the agent's tokens. The account root can still replace the policy (break
# glass), and Terraform applies from a jumpbox, so the containers stay manageable. Terraform owns only the
# containers; the values are minted in the owner's browser session and put without being printed (they cannot be
# created by API). Each expires after at most 366 days, the organization's limit; mint again before then.
locals {
  github_admin_token_owners  = ["dolphin-labs-hq", "ejc3"]
  github_admin_token_readers = [aws_iam_role.jumpbox_admin[0].arn]
}

resource "aws_secretsmanager_secret" "github_admin_token" {
  for_each                = toset(local.github_admin_token_owners)
  name                    = "github-admin-token/${each.value}"
  description             = "Fine-grained GitHub token for the administration agent, every repository of ${each.value}. Administration only; value minted by hand. See github-admin-tokens.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "github-admin-token/${each.value}", Project = "dev" }
}

resource "aws_secretsmanager_secret_policy" "github_admin_token" {
  for_each   = aws_secretsmanager_secret.github_admin_token
  secret_arn = each.value.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTheJumpboxRoleCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = each.value.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.github_admin_token_readers } }
    }]
  })
}

# The administration agent's GitHub App (owner, 2026-10-11: "setup a github app"), so GitHub tokens are minted by API
# and expire on their own (an hour) instead of living for a year. `github-app/aws-admin` holds JSON: app_id, slug,
# client_id, private_key (PEM), webhook_secret, and installations {owner: installation_id}. scripts/github-app-token.py
# turns it into an installation token for one owner. Same readers as the tokens above: the jumpboxes' role only.
resource "aws_secretsmanager_secret" "github_app_admin" {
  name                    = "github-app/aws-admin"
  description             = "The administration agent's GitHub App credentials (JSON: app id, private key, installations). Jumpbox role only. See github-admin-tokens.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "github-app/aws-admin", Project = "dev" }
}

resource "aws_secretsmanager_secret_policy" "github_app_admin" {
  secret_arn = aws_secretsmanager_secret.github_app_admin.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTheJumpboxRoleCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.github_app_admin.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.github_admin_token_readers } }
    }]
  })
}

# The administration agent's Vercel token (owner, 2026-10-11: "vercel should be headless too"): a full-account token for
# the owner's Vercel account, so Vercel tokens, environment variables and projects are managed by API with no browser. A full-account token
# can create further tokens (POST /v3/user/tokens), so this is the one Vercel credential minted by hand. Raw token string;
# readers: the jumpboxes' role only, as above.
resource "aws_secretsmanager_secret" "vercel_admin_token" {
  name                    = "vercel-admin-token"
  description             = "Full-account Vercel token for the administration agent (the owner's Vercel account). Jumpbox role only. See github-admin-tokens.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "vercel-admin-token", Project = "dev" }
}

resource "aws_secretsmanager_secret_policy" "vercel_admin_token" {
  secret_arn = aws_secretsmanager_secret.vercel_admin_token.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyTheJumpboxRoleCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.vercel_admin_token.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.github_admin_token_readers } }
    }]
  })
}

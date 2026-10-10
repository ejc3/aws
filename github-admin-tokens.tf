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

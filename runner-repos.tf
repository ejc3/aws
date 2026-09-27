# runner-repos.tf
#
# Self-hosted runners for repos other than ejc3/fcvm. Today the runner system serves exactly
# one repo on bare metal; CoderColton/colton-games and dolphin-labs-hq/dolphin-labs are being
# added on ordinary x86 spot instances (their jobs need no KVM).
#
# STEP 1 (this file, for now): each repo's controller token. A runner registration token needs
# repo ADMIN, so each repo's owner mints a fine-grained token limited to that one repo with
# Administration RW (register, list, remove runners) and Webhooks RW (the workflow_job hook):
#   CoderColton/colton-games       minted by CoderColton (ejc3 has write, not admin there)
#   dolphin-labs-hq/dolphin-labs   minted by ejc3 as org admin
#
# CONTAINER ONLY, deliberately not an SSM parameter. Terraform creates the secret and never a
# version, so it never reads the value and the token cannot land in state. (An
# aws_ssm_parameter with `value` + ignore_changes still refreshes the DECRYPTED value into
# state; /github-runner/pat has that flaw, see GITHUB-RUNNERS.md.) Set the value out of band,
# never on argv:
#   printf %s "$TOKEN" | aws secretsmanager put-secret-value --region us-west-1 \
#     --secret-id github-runner/repo-pat/<owner>/<repo> --secret-string file:///dev/stdin
#
# Only administration and the runner Lambda role may read it. The runner INSTANCE role has no
# Secrets Manager access at all, so a CI job cannot read it either.
locals {
  runner_extra_repos = ["CoderColton/colton-games", "dolphin-labs-hq/dolphin-labs"]
}

resource "aws_secretsmanager_secret" "github_runner_repo_pat" {
  for_each = var.enable_github_runner ? toset(local.runner_extra_repos) : toset([])

  name                    = "github-runner/repo-pat/${each.value}"
  description             = "Fine-grained GitHub token for ${each.value} only: Administration RW + Webhooks RW (runner controller). Set by hand."
  recovery_window_in_days = 7
  tags                    = { Name = "github-runner-repo-pat", Managed = "terraform" }
}

resource "aws_secretsmanager_secret_policy" "github_runner_repo_pat" {
  for_each = aws_secretsmanager_secret.github_runner_repo_pat

  secret_arn = each.value.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheRunnerControllerCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = each.value.arn
      Condition = {
        ArnNotLike = {
          # local.games_mp_admin_principals (games-multiplayer.tf) is the account's standard
          # administration set: root, the jumpbox admin role, SSO administrators.
          "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.runner_lambda[0].arn])
        }
      }
    }]
  })
}

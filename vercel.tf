# Vercel, managed from Terraform.
#
# Colton Games (prod) lives in the Vercel team `coltons-projects-7f9a4e8b`, so the token this
# stack uses is a DEDICATED API token for that team, not anyone's CLI login. Personal Vercel
# logins belong to one Unix user and are never copied or seeded from here (AGENTS.md).
#
# Stage 1 (this file): only the Secrets Manager container. Its payload is filled once, outside
# Terraform, the same way `cloudflare-workers-builds-control-token` is, so the value never
# enters state. Stage 2 adds the provider, which reads the payload ephemerally, and the
# domain/redirect resources. Doing it in two applies is what keeps a plan from failing on a
# secret that has no version yet.

resource "aws_secretsmanager_secret" "vercel_api_token" {
  name                    = "vercel-api-token"
  description             = "Vercel API token for Terraform-managed Vercel projects and domains"
  recovery_window_in_days = 30
  tags                    = { Name = "vercel-api-token", Managed = "terraform" }

  lifecycle {
    prevent_destroy = true
  }
}

# Same shape as the tunnel connector-token secrets: only administration may read it. A dev
# box holding this could redirect or take down a production site.
resource "aws_secretsmanager_secret_policy" "vercel_api_token" {
  secret_arn = aws_secretsmanager_secret.vercel_api_token.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.vercel_api_token.arn
      Condition = {
        ArnNotLike = {
          "aws:PrincipalArn" = [
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
            aws_iam_role.jumpbox_admin[0].arn,
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*",
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AdministratorAccess_*",
          ]
        }
      }
    }]
  })
}

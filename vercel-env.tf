# vercel-env.tf
#
# Terraform writes the Vercel environments of the sites whose variables were set by hand (dolphin-labs, imagine,
# remote-claw, nest-step) from AWS, so AWS is the one source for every site on both clouds (the standing rule in
# AGENTS.md, "Sites also deploying to Cloudflare Workers"). This first part is the Vercel API token for the owner's
# personal team (imagine, remote-claw), which no Terraform provider here could reach: an API token scoped to that one
# team, minted by API with vercel-admin-token (github-admin-tokens.tf), never anyone's CLI login.
resource "aws_secretsmanager_secret" "vercel_api_token_ejc3" {
  name                    = "vercel-api-token-ejc3"
  description             = "Vercel API token for the owner's personal team (imagine, remote-claw): Terraform writes those projects' environments with it. Minted by API; see vercel-env.tf."
  recovery_window_in_days = 30
  tags                    = { Name = "vercel-api-token-ejc3", Project = "vercel-env" }

  lifecycle {
    prevent_destroy = true
  }
}

# Administration only, whatever an identity policy elsewhere says: a dev box holding this could change or take down the sites.
resource "aws_secretsmanager_secret_policy" "vercel_api_token_ejc3" {
  secret_arn = aws_secretsmanager_secret.vercel_api_token_ejc3.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.vercel_api_token_ejc3.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

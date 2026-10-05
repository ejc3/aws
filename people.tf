# people.tf: where people's email addresses live, so they are not in git.
#
# Some configuration is a person's address and nothing else: who the Cloudflare Access policies let through to the dev sites, who
# owns the browser manager, the root address of the staging AWS account. They used to be literals in tracked files. They are one
# Secrets Manager secret instead, `people/addresses` in us-west-1, which this file creates as an empty container and the
# administration set fills out of band, as with every other secret here (JSON on stdin, never a file in a repo):
#
#   read -rs T; printf %s "$T" | aws secretsmanager put-secret-value --region us-west-1 \
#     --secret-id people/addresses --secret-string file:///dev/stdin; unset T
#   {"owner": "...", "family": ["...", "..."], "staging_account": "..."}
#
# Terraform reads it into locals (local.people, and the names the configuration already used) wherever an address is needed.
# Every machine that plans (both jumpboxes, the Stop hook) has the administration role and can read it; a dev box cannot plan
# and the resource policy denies it the value. The value is in Terraform state like the other secrets Terraform reads. Never
# write an address into a file, a comment, a test, a commit message or a pull request: use an example.com one.
resource "aws_secretsmanager_secret" "people" {
  name                    = "people/addresses"
  description             = "People's email addresses used by configuration (JSON: owner, family, staging_account). Value set out of band; see people.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "people/addresses", Project = "dev" }
}

# Administration only, whatever an identity policy elsewhere says.
resource "aws_secretsmanager_secret_policy" "people" {
  secret_arn = aws_secretsmanager_secret.people.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.people.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

# vercel-env-secrets.tf
#
# AWS keeps a record of every Vercel project's environment, so Vercel is not the only holder of a site's credentials.
# One Secrets Manager container per project per Vercel target, `vercel-env/<site>/<target>` (JSON: variable name -> value),
# for the sites that have variables:
#
#   colton-games   development preview production      dolphin-labs   development preview production
#   dolphin-films  preview production
#   nest-step      development preview production      imagine        preview production
#   remote-claw    preview production                  ts-api         preview production
#
# (dolphin-films' variables are written by Terraform from dolphin-films/{prod,nonprod}/* and people/addresses,
# dolphin-films-vercel.tf: those stay the source and its records here are mirrors, as for colton-games' accounts.)
#
# Terraform owns only the containers and who may read them (administration only); it never reads a value. Vercel never
# returns a variable of type `sensitive` once saved, so the values are captured by `scripts/vercel-env-capture.py`, which
# deploys a throwaway route that reads the variables at run time, behind Vercel Deployment Protection, and deletes the
# deployment afterwards. Where Terraform ALSO writes a variable into Vercel from another container (colton-games'
# accounts, colton-games-accounts.tf), that container stays the source and this one is a mirror of what Vercel holds.
# The development target is never sensitive, and nest-step has no sensitive variable, so those are read with `vercel env pull`.

locals {
  vercel_env_targets = {
    "colton-games"  = ["development", "preview", "production"]
    "dolphin-films" = ["preview", "production"]
    "dolphin-labs"  = ["development", "preview", "production"]
    "imagine"       = ["preview", "production"]
    "nest-step"     = ["development", "preview", "production"]
    "remote-claw"   = ["preview", "production"]
    "ts-api"        = ["preview", "production"]
  }
  vercel_env_records = toset(flatten([for site, targets in local.vercel_env_targets : [for t in targets : "${site}/${t}"]]))
}

resource "aws_secretsmanager_secret" "vercel_env" {
  for_each = local.vercel_env_records

  name                    = "vercel-env/${each.key}"
  description             = "Record of the Vercel environment variables of ${each.key} (JSON: name -> value). Captured with scripts/vercel-env-capture.py; see vercel-env-secrets.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "vercel-env/${each.key}", Project = "vercel-env" }
}

resource "aws_secretsmanager_secret_policy" "vercel_env" {
  for_each = local.vercel_env_records

  secret_arn = aws_secretsmanager_secret.vercel_env[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.vercel_env[each.key].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

output "vercel_env_records" {
  description = "Secrets Manager names of the Vercel environment records (us-west-1), by site and target. Values are captured with scripts/vercel-env-capture.py."
  value       = sort([for r in local.vercel_env_records : aws_secretsmanager_secret.vercel_env[r].name])
}

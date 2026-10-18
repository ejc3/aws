# vercel-env-secrets.tf
#
# AWS keeps every Vercel project's environment, so Vercel is not the only holder of a site's credentials. For imagine,
# remote-claw and nest-step these containers are the SOURCE: vercel-env.tf writes the projects from them (change a value
# here and apply). For the rest they are still records captured from Vercel, as described below.
# One Secrets Manager container per project per Vercel target, `vercel-env/<site>/<target>` (JSON: variable name -> value),
# for the sites that have variables:
#
#   colton-games   development preview production      nest-step      development preview production
#   imagine        preview production                  remote-claw    preview production
#   ts-api         preview production
#
# THE DOLPHIN SITES' CONTAINERS (local.vercel_env_dolphin) outlive their Vercel team, which is being deleted (2026-10-18,
# ejc3/aws #503). They are no longer records of Vercel and nothing captures into them:
#   dolphin-labs   development preview production   vercel-env/dolphin-labs/production is the source the production
#                  Worker dolphin-labs-prod's secrets are loaded from (`wrangler secret bulk`; AGENTS.md, "Which
#                  production is behind Cloudflare"): change a value there, then load it into the Worker.
#   dolphin-films  preview production               the last copy of what that Vercel project held. dolphin-films-prod's
#                  secrets come from dolphin-films/{prod,nonprod}/* (dolphin-films.tf) and people/addresses instead.
# Their names and Terraform addresses (aws_secretsmanager_secret.vercel_env["dolphin-labs/production"], ...) are unchanged.
#
# Terraform owns only the containers and who may read them (administration only); it never reads a value. Vercel never
# returns a variable of type `sensitive` once saved, so the values are captured by `scripts/vercel-env-capture.py`, which
# deploys a throwaway route that reads the variables at run time, behind Vercel Deployment Protection, and deletes the
# deployment afterwards. Where Terraform ALSO writes a variable into Vercel from another container (colton-games'
# accounts, colton-games-accounts.tf), that container stays the source and this one is a mirror of what Vercel holds.
# The development target is never sensitive, and nest-step has no sensitive variable, so those are read with `vercel env pull`.

locals {
  vercel_env_targets = {
    "colton-games" = ["development", "preview", "production"]
    "imagine"      = ["preview", "production"]
    "nest-step"    = ["development", "preview", "production"]
    "remote-claw"  = ["preview", "production"]
    "ts-api"       = ["preview", "production"]
  }
  vercel_env_records = toset(flatten([for site, targets in merge(local.vercel_env_targets, local.vercel_env_dolphin) : [for t in targets : "${site}/${t}"]]))

  # Kept after the Dolphin sites left Vercel, and not capturable (no Vercel project any more): see the header.
  vercel_env_dolphin = {
    "dolphin-films" = ["preview", "production"]
    "dolphin-labs"  = ["development", "preview", "production"]
  }
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

# workers-stage-secrets.tf
#
# The runtime secrets of the staging Workers (workers-stage.tf), kept in AWS. A Worker's secrets are write-only in
# Cloudflare (nothing can read one back), so the Worker cannot be the record: one Secrets Manager container per site
# holds the whole set as JSON (variable name -> value), and `scripts/workers-stage-secrets.sh SITE` loads it into the
# Worker `<site>-stage`. Rebuilding a Worker, or rotating a value, is: edit the container, run the script.
#
#   workers-stage/dolphin-labs   workers-stage/dolphin-films   workers-stage/imagine
#   workers-stage/remote-claw    workers-stage/nest-step       workers-stage/colton-games
#
# Terraform owns only the containers and who may read them (administration only: a dev box has no reason to hold
# another site's credentials, and nextjs-dev gives every account sudo). It never reads a value, so none is in state.
# Values are put out of band by an administrator:
#
#   aws secretsmanager put-secret-value --region us-west-1 --secret-id workers-stage/<site> \
#     --secret-string file:///dev/stdin < <a 0600 JSON file, or a pipe>
#
# The staging copies use NON-production values where the site has them (their own OAuth client, the non-production
# database) and fresh stage-only random secrets where a value only has to be unguessable. Vercel never returns a
# variable of type `sensitive`, so those cannot be copied from there: take them from the real source, or make new ones.

locals {
  workers_stage_secret_sites = toset(["colton-games", "dolphin-films", "dolphin-labs", "imagine", "nest-step", "remote-claw"])
}

resource "aws_secretsmanager_secret" "workers_stage" {
  for_each = local.workers_stage_secret_sites

  name                    = "workers-stage/${each.key}"
  description             = "Runtime secrets of the ${each.key}-stage Worker (JSON: variable name -> value). Value set out of band and loaded with scripts/workers-stage-secrets.sh; see workers-stage-secrets.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "workers-stage/${each.key}", Project = "workers-stage" }
}

resource "aws_secretsmanager_secret_policy" "workers_stage" {
  for_each = local.workers_stage_secret_sites

  secret_arn = aws_secretsmanager_secret.workers_stage[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.workers_stage[each.key].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

output "workers_stage_secrets" {
  description = "Secrets Manager names of the staging Workers' runtime secrets (us-west-1). Values are set out of band; load one with scripts/workers-stage-secrets.sh SITE."
  value       = { for site, secret in aws_secretsmanager_secret.workers_stage : site => secret.name }
}

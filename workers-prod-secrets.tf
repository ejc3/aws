# workers-prod-secrets.tf
#
# The runtime secrets of the PRODUCTION Worker colton-games-prod (cloudflare-production.tf), kept in AWS. A Worker's secrets
# are write-only in Cloudflare, so AWS is the record. Production values never reach staging and staging values never reach
# production: these containers are not workers-stage/*, and scripts/workers-prod-secrets.sh loads them into `<site>-prod`
# only.
#
# Two containers, because the values have two owners:
#
#   workers-prod/colton-games/terraform   written HERE. Every value Terraform makes or already holds, and the SAME value it
#                                         writes to the Vercel production target, so the cutover changes no session, cookie,
#                                         join-token key or admin list: AUTH_SECRET, AUTH_GOOGLE_*, SITE_ADMIN_EMAILS (and the
#                                         push keys once prod/push is in the gate) from colton-games-accounts.tf; ACCOUNT_SAVES;
#                                         the multiplayer set from games-multiplayer-bringup.tf (MP_*, CRON_SECRET); plus what
#                                         only the Worker needs: AUTH_URL, the declared environment, and the two IAM users' keys
#                                         (picture checks, colton-games-moderation.tf; the launcher, cloudflare-production.tf),
#                                         because a Worker cannot present a Vercel OIDC token.
#   workers-prod/colton-games             put out of band by an administrator: what Terraform does not hold. The Supabase
#                                         project came from the Vercel integration, so SUPABASE_URL and SUPABASE_SECRET_KEY are
#                                         Vercel production's own values, and SKYHOOK_LEADERBOARD_SECRET is production's
#                                         hand-set signing key (its record is vercel-env/colton-games/production). Put it from
#                                         stdin or a 0600 file, never a command line:
#                                           aws secretsmanager put-secret-value --region us-west-1 \
#                                             --secret-id workers-prod/colton-games --secret-string file:///dev/stdin < <file>
#
# The loader merges the two and refuses a name that is in both, an empty value or a placeholder. Load after the Worker's own
# deploy has finished: `scripts/workers-prod-secrets.sh colton-games`. A Terraform-made value that rotates (a -replace, a new
# join-token generation) is in the managed container after the apply; re-run the loader then, as Vercel needs a redeploy.
#
# Both containers are administration only. The managed one's value is in Terraform state, like every value it is made from.

locals {
  # Names Vercel production has that the Worker does not get: the Vercel OIDC role (the Worker uses the IAM user's keys
  # instead, and the lobby refuses keys and a role together on Vercel only) and Vercel's own AWS_REGION pin.
  colton_games_prod_worker_vercel_only = ["MP_LAUNCH_ROLE_ARN", "AWS_REGION"]

  # Sensitive. Name -> value, the whole managed set.
  colton_games_prod_worker_env = merge(
    {
      for id, entry in local.colton_games_accounts_vercel_env :
      entry.key => local.colton_games_accounts_vercel_values[id] if entry.target == "production"
    },
    contains(var.colton_games_account_saves_targets, "production") ? { ACCOUNT_SAVES = "on" } : {},
    {
      for id, entry in local.games_mp_vercel_env :
      split("/", id)[0] => (entry.sensitive ? local.games_mp_vercel_secret_values[id] : entry.value)
      if contains(entry.targets, "production") && !contains(local.colton_games_prod_worker_vercel_only, split("/", id)[0])
    },
    {
      # Sign-in builds its callback address from this, never from a forwarded Host header. cc-games.org is canonical
      # (vercel-cc-games.tf) and its callback is already registered on the production Google client.
      AUTH_URL = "https://cc-games.org"
      # The records' namespace (the games repo's lib/server/environment.ts). Vercel production has it as a variable set
      # by hand; here it is a constant.
      SKYHOOK_LEADERBOARD_ENVIRONMENT  = "production"
      MP_LAUNCH_AWS_ACCESS_KEY_ID      = aws_iam_access_key.games_mp_launcher_worker.id
      MP_LAUNCH_AWS_SECRET_ACCESS_KEY  = aws_iam_access_key.games_mp_launcher_worker.secret
      MODERATION_AWS_ACCESS_KEY_ID     = aws_iam_access_key.colton_games_moderation_worker_prod.id
      MODERATION_AWS_SECRET_ACCESS_KEY = aws_iam_access_key.colton_games_moderation_worker_prod.secret
      MODERATION_AWS_REGION            = local.moderation_region
    },
  )

  # What the out-of-band container must hold (the loader checks it), so the two never overlap.
  colton_games_prod_worker_out_of_band = ["SUPABASE_URL", "SUPABASE_SECRET_KEY", "SKYHOOK_LEADERBOARD_SECRET"]

  workers_prod_secret_containers = {
    "colton-games"           = "Runtime secrets of the colton-games-prod Worker that Terraform does not hold (JSON: SUPABASE_URL, SUPABASE_SECRET_KEY, SKYHOOK_LEADERBOARD_SECRET). Value set out of band; see workers-prod-secrets.tf."
    "colton-games/terraform" = "Runtime secrets of the colton-games-prod Worker that Terraform makes (JSON: variable name -> value). Written by Terraform; loaded with scripts/workers-prod-secrets.sh. See workers-prod-secrets.tf."
  }
}

resource "aws_secretsmanager_secret" "workers_prod" {
  for_each = local.workers_prod_secret_containers

  name                    = "workers-prod/${each.key}"
  description             = each.value
  recovery_window_in_days = 7
  tags                    = { Name = "workers-prod/${each.key}", Project = "workers-prod" }
}

resource "aws_secretsmanager_secret_policy" "workers_prod" {
  for_each = local.workers_prod_secret_containers

  secret_arn = aws_secretsmanager_secret.workers_prod[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.workers_prod[each.key].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

resource "aws_secretsmanager_secret_version" "workers_prod_colton_games" {
  secret_id     = aws_secretsmanager_secret.workers_prod["colton-games/terraform"].id
  secret_string = jsonencode(local.colton_games_prod_worker_env)

  lifecycle {
    # Production's own scope, never another environment's.
    precondition {
      condition     = local.colton_games_prod_worker_env["MP_ENV"] == "production" && local.colton_games_prod_worker_env["SKYHOOK_LEADERBOARD_ENVIRONMENT"] == "production"
      error_message = "The colton-games-prod Worker must get MP_ENV and SKYHOOK_LEADERBOARD_ENVIRONMENT = production (workers-prod-secrets.tf)."
    }
    precondition {
      condition     = alltrue([for k, v in local.colton_games_prod_worker_env : can(regex("^[A-Z][A-Z0-9_]*$", k)) && v != null && trimspace(v) != ""])
      error_message = "Every colton-games-prod Worker variable needs a name in capitals and a non-empty value (workers-prod-secrets.tf)."
    }
    precondition {
      condition     = length(setintersection(keys(local.colton_games_prod_worker_env), local.colton_games_prod_worker_out_of_band)) == 0
      error_message = "A name Terraform writes is also one the out-of-band container holds (workers-prod-secrets.tf)."
    }
  }
}

output "workers_prod_secrets" {
  description = "Secrets Manager names of the production Worker's runtime secrets (us-west-1): the out-of-band one an administrator puts, the one Terraform writes, and the names each holds."
  value = {
    out_of_band = { name = aws_secretsmanager_secret.workers_prod["colton-games"].name, names = local.colton_games_prod_worker_out_of_band }
    terraform   = { name = aws_secretsmanager_secret.workers_prod["colton-games/terraform"].name, names = sort(nonsensitive(keys(local.colton_games_prod_worker_env))) }
  }
}

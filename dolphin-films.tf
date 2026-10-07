# dolphin-films.tf
#
# AWS half of dolphin-films (dolphin-labs-hq/dolphin-films, private): a Next.js app at
# films/web on Vercel, Google sign-in through Auth.js, a per-user store in Turso (libSQL),
# and offline asset builders that run on the metal dev boxes. The app reads nothing from AWS
# at run time, so there is no Vercel OIDC role here (imagine.tf has one because its web app
# invokes a Lambda). What this file owns:
#
#   Secrets Manager containers, one per environment and kind (JSON, values set out of band):
#     dolphin-films/prod/auth          AUTH_SECRET, AUTH_GOOGLE_ID, AUTH_GOOGLE_SECRET
#     dolphin-films/prod/turso         TURSO_DATABASE_URL, TURSO_AUTH_TOKEN
#     dolphin-films/nonprod/auth       the same names, for preview deployments and local dev
#     dolphin-films/nonprod/turso
#   and who may read them:
#     prod/*      the administration set only. Terraform writes production's values from these
#                 into the Vercel project (dolphin-films-vercel.tf).
#     nonprod/*   the administration set and dev-server-role (the metal boxes, where the app is
#                 developed and the builders run). Not nextjs-dev-role: every account on that
#                 box has sudo, so a grant there is box-wide.
#
# Production and non-production never share a credential: separate containers, a separate
# Google OAuth client, a separate AUTH_SECRET and a separate database with its own token, and
# no dev box can read a production value whatever an identity policy elsewhere says.
#
# TURSO: TWO KINDS OF CREDENTIAL. The platform token (turso/api-token, dev-ai-services.tf)
# belongs to the account: it creates and deletes databases and mints their tokens. The site
# never sees it; it is not one of its variables and nothing here grants it. What the site
# gets is per database: that database's URL and a token for that database alone
# (TURSO_DATABASE_URL, TURSO_AUTH_TOKEN), one pair per environment, in the two turso
# containers. The databases are made once with the platform token from a metal dev box
# (which may read it); an administrator puts each pair, since no dev box may write a
# container or read production's. The site applies its own migrations when it deploys, so
# nothing here runs one.
#
# WHAT IS NOT HERE, because it already exists or is not managed from this repo:
#   CI runners     runner-repos.tf and runner-app.tf serve the repo (label `dolphin`).
#   The builders   read browserbase/credentials and games/elevenlabs-api-key, which
#                  dev-server-role already may (dev-ai-services.tf), and call Claude on Amazon
#                  Bedrock through the same role (dev-instance-common.tf, BedrockRuntimeInvoke:
#                  anthropic.* and the account's inference profiles). So there is no LLM API
#                  key and no new Bedrock statement.
#   Turso          the two databases (one per environment) are created with the platform
#                  token, not by Terraform: no Turso resource is managed in this repo.
#   Vercel         the project was made by hand; its environment is written by Terraform from
#                  these containers and the people secret, with a token of its own for the
#                  dolphin-labs team (dolphin-films-vercel.tf). No address is written in this
#                  repository.
#   DNS            yourfantasymovie.com (yourfantasymovie.tf): DNS records here, the Vercel project
#                  `dolphin-films` (team dolphin-labs) and its domains made with the Vercel CLI as the owner.
#
# BRING-UP, once, in this order:
#   1. Apply. The four containers exist and are empty.
#   2. Create two Google OAuth clients (Web application), one per environment. Redirect URI:
#      https://<the environment's domain>/api/auth/callback/google (for non-production also
#      http://localhost:<port>/api/auth/callback/google).
#   3. On a metal dev box, with the platform token: make sure each environment's database
#      exists, and mint a token for each. Write each pair (TURSO_DATABASE_URL,
#      TURSO_AUTH_TOKEN) to a 0600 JSON file for an administrator; never print it.
#   4. An administrator puts each value, never on a command line, in a repo file or a commit:
#        aws secretsmanager put-secret-value --region us-west-1 \
#          --secret-id dolphin-films/<env>/<kind> --secret-string file://<a 0600 JSON file>
#      then delete the file. `openssl rand -base64 33` makes an AUTH_SECRET.
#   5. Bring up dolphin-films-vercel.tf (its header lists the steps): Terraform writes Production
#      from prod/* and Preview from nonprod/*. The next deploy applies the site's migrations.
#
# On a metal box, an agent exports one secret's variables without printing them:
#   eval "$(aws secretsmanager get-secret-value --region us-west-1 --secret-id dolphin-films/nonprod/auth \
#     --query SecretString --output text | jq -r 'to_entries[] | "export \(.key)=\(.value|@sh)"')"

locals {
  dolphin_films_auth_keys  = ["AUTH_SECRET", "AUTH_GOOGLE_ID", "AUTH_GOOGLE_SECRET"]
  dolphin_films_turso_keys = ["TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"]

  # Who may read a non-production secret besides the administration set.
  dolphin_films_nonprod_readers = [aws_iam_role.dev_server.arn]

  # <environment>/<kind> => the variable names its JSON holds and who else may read it.
  dolphin_films_secrets = {
    "prod/auth"     = { keys = local.dolphin_films_auth_keys, readers = [] }
    "prod/turso"    = { keys = local.dolphin_films_turso_keys, readers = [] }
    "nonprod/auth"  = { keys = local.dolphin_films_auth_keys, readers = local.dolphin_films_nonprod_readers }
    "nonprod/turso" = { keys = local.dolphin_films_turso_keys, readers = local.dolphin_films_nonprod_readers }
  }
}

resource "aws_secretsmanager_secret" "dolphin_films" {
  for_each = local.dolphin_films_secrets

  name                    = "dolphin-films/${each.key}"
  description             = "dolphin-films ${each.key} (JSON: ${join(", ", each.value.keys)}). Value set out of band; see dolphin-films.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "dolphin-films/${each.key}", Project = "dolphin-films" }
}

# Nobody but the administration set and that environment's readers may read it, whatever an
# identity policy elsewhere says. For prod/* the reader list is empty.
resource "aws_secretsmanager_secret_policy" "dolphin_films" {
  for_each = local.dolphin_films_secrets

  secret_arn = aws_secretsmanager_secret.dolphin_films[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndThisEnvironmentsReadersCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.dolphin_films[each.key].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, each.value.readers) } }
    }]
  })
}

data "aws_iam_policy_document" "dolphin_films_nonprod_read" {
  statement {
    sid       = "ReadTheDolphinFilmsNonProductionSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for key, secret in aws_secretsmanager_secret.dolphin_films : secret.arn if startswith(key, "nonprod/")]
  }
}

# A managed policy, not inline: dev-server-role's inline policies are within a few hundred
# characters of IAM's 10,240 limit (dev-instance-common.tf).
resource "aws_iam_policy" "dolphin_films_nonprod_read" {
  name        = "dev-dolphin-films-nonprod-read"
  description = "Metal dev boxes: read the dolphin-films non-production secrets (dolphin-films/nonprod/*) and nothing else"
  policy      = data.aws_iam_policy_document.dolphin_films_nonprod_read.json
}

resource "aws_iam_role_policy_attachment" "dolphin_films_nonprod_read" {
  role       = aws_iam_role.dev_server.name
  policy_arn = aws_iam_policy.dolphin_films_nonprod_read.arn
}

# -------------------------------------------------------------------------------------
# Outputs
# -------------------------------------------------------------------------------------

output "dolphin_films_secrets" {
  description = "Secrets Manager names of the dolphin-films credentials (us-west-1), by environment and kind. Values are set out of band."
  value       = { for key, secret in aws_secretsmanager_secret.dolphin_films : key => secret.name }
}

# What Terraform writes to the Vercel project: output dolphin_films_vercel_written
# (dolphin-films-vercel.tf).

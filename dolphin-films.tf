# dolphin-films.tf
#
# AWS half of dolphin-films (dolphin-labs-hq/dolphin-films, private): a Next.js app at
# films/web on Vercel, Google sign-in through Auth.js, a per-user store in Supabase Postgres,
# and offline asset builders that run on the metal dev boxes. The app reads nothing from AWS
# at run time, so there is no Vercel OIDC role here (imagine.tf has one because its web app
# invokes a Lambda). What this file owns:
#
#   Secrets Manager containers, one per environment and kind (JSON, values set out of band):
#     dolphin-films/prod/auth          AUTH_SECRET, AUTH_GOOGLE_ID, AUTH_GOOGLE_SECRET
#     dolphin-films/prod/supabase      SUPABASE_URL, SUPABASE_SECRET_KEY
#     dolphin-films/nonprod/auth       the same names, for preview deployments and local dev
#     dolphin-films/nonprod/supabase
#   and who may read them:
#     prod/*      the administration set only. Production's values live in the Vercel project;
#                 these are the record they are set from, since Vercel never shows a sensitive
#                 value again.
#     nonprod/*   the administration set and dev-server-role (the metal boxes, where the app is
#                 developed and the builders run). Not nextjs-dev-role: every account on that
#                 box has sudo, so a grant there is box-wide.
#
# Production and non-production never share a credential: separate containers, a separate
# Google OAuth client, a separate AUTH_SECRET and a separate Supabase key, and no dev box can
# read a production value whatever an identity policy elsewhere says.
#
# WHAT IS NOT HERE, because it already exists or is not managed from this repo:
#   CI runners     runner-repos.tf and runner-app.tf serve the repo (label `dolphin`).
#   The builders   read browserbase/credentials and games/elevenlabs-api-key, which
#                  dev-server-role already may (dev-ai-services.tf), and call Claude on Amazon
#                  Bedrock through the same role (dev-instance-common.tf, BedrockRuntimeInvoke:
#                  anthropic.* and the account's inference profiles). So there is no LLM API
#                  key and no new Bedrock statement.
#   Supabase       projects are created by hand; no Supabase project is managed in this repo
#                  (colton-games' came from the Vercel integration).
#   Vercel         the project and its environment are set by hand, as for imagine. The Vercel
#                  provider's token here (vercel.tf) reaches the colton-games team only.
#   DNS            yourfantasymovie.com (yourfantasymovie.tf): DNS records here, the Vercel project
#                  `dolphin-films` (team dolphin-labs) and its domains made with the Vercel CLI as the owner.
#
# BRING-UP, once, in this order:
#   1. Apply. The four containers exist and are empty.
#   2. Create two Google OAuth clients (Web application), one per environment. Redirect URI:
#      https://<the environment's domain>/api/auth/callback/google (for non-production also
#      http://localhost:<port>/api/auth/callback/google).
#   3. Create the Supabase project or projects and take each one's URL and secret key.
#   4. Put each value, never on a command line, in a repo file or a commit:
#        aws secretsmanager put-secret-value --region us-west-1 \
#          --secret-id dolphin-films/<env>/<kind> --secret-string file://<a 0600 JSON file>
#      then delete the file. `openssl rand -base64 33` makes an AUTH_SECRET.
#   5. Set the Vercel project's environment from the dolphin_films_vercel_env output:
#      Production from prod/*, Preview from nonprod/*, every variable marked sensitive.
#
# On a metal box, an agent exports one secret's variables without printing them:
#   eval "$(aws secretsmanager get-secret-value --region us-west-1 --secret-id dolphin-films/nonprod/auth \
#     --query SecretString --output text | jq -r 'to_entries[] | "export \(.key)=\(.value|@sh)"')"

locals {
  dolphin_films_auth_keys     = ["AUTH_SECRET", "AUTH_GOOGLE_ID", "AUTH_GOOGLE_SECRET"]
  dolphin_films_supabase_keys = ["SUPABASE_URL", "SUPABASE_SECRET_KEY"]

  # Who may read a non-production secret besides the administration set.
  dolphin_films_nonprod_readers = [aws_iam_role.dev_server.arn]

  # <environment>/<kind> => the variable names its JSON holds and who else may read it.
  dolphin_films_secrets = {
    "prod/auth"        = { keys = local.dolphin_films_auth_keys, readers = [] }
    "prod/supabase"    = { keys = local.dolphin_films_supabase_keys, readers = [] }
    "nonprod/auth"     = { keys = local.dolphin_films_auth_keys, readers = local.dolphin_films_nonprod_readers }
    "nonprod/supabase" = { keys = local.dolphin_films_supabase_keys, readers = local.dolphin_films_nonprod_readers }
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

# Names only. Terraform holds none of these values and sets nothing in Vercel.
output "dolphin_films_vercel_env" {
  description = "What to set by hand in the dolphin-films Vercel project: per Vercel environment, each secret and the variable names its JSON holds"
  value = {
    for target, env in { production = "prod", preview = "nonprod" } : target => {
      for key, secret in local.dolphin_films_secrets : aws_secretsmanager_secret.dolphin_films[key].name => secret.keys if startswith(key, "${env}/")
    }
  }
}

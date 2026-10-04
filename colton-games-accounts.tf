# colton-games-accounts.tf
#
# What the colton-games site (CoderColton/colton-games, the Vercel project of
# vercel-cc-games.tf) needs outside its own repository for accounts: optional Google sign-in,
# site admins, and browser push. Sign-in stays off in an environment until that environment
# has AUTH_GOOGLE_ID, AUTH_GOOGLE_SECRET and AUTH_SECRET; everything else builds on sign-in.
#
# Production and non-production never share a credential, so a session, an admin grant or a
# push subscription made in one means nothing in the other:
#
#   Secrets Manager containers, one per environment and kind (JSON, values set out of band):
#     colton-games/prod/auth       AUTH_GOOGLE_ID, AUTH_GOOGLE_SECRET, SITE_ADMIN_EMAILS
#     colton-games/prod/push       NEXT_PUBLIC_VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_SUBJECT
#     colton-games/nonprod/auth    the same names, for previews and the dev boxes
#     colton-games/nonprod/push
#   and who may read them:
#     prod/*      the administration set only. No dev box can read a production value,
#                 whatever an identity policy elsewhere says.
#     nonprod/*   the administration set, dev-server-role and nextjs-dev-role: the same two
#                 dev roles as the other games secrets (dev-ai-services.tf), because the games
#                 are developed on those boxes. Every account on nextjs-dev has sudo, so that
#                 grant is box-wide, as with the ElevenLabs key.
#
# The value of each secret is one JSON object with those keys, every one a string:
#   AUTH_GOOGLE_ID, AUTH_GOOGLE_SECRET   that environment's own Google OAuth client
#   SITE_ADMIN_EMAILS                    <comma-separated addresses of the site admins>; no
#                                        repository holds the list
#   NEXT_PUBLIC_VAPID_PUBLIC_KEY,
#   VAPID_PRIVATE_KEY                    that environment's own pair from
#                                        `npx web-push generate-vapid-keys`
#   VAPID_SUBJECT                        an https:// address of the site (or a mailto: one)
# Put it from a file only you can read, never on a command line, in a repo file or a commit:
#   aws secretsmanager put-secret-value --region us-west-1 \
#     --secret-id colton-games/<env>/<kind> --secret-string file://<a 0600 JSON file>
# then remove the file (`shred -u`).
#
# On a dev box, an agent exports a non-production secret's variables without printing them:
#   eval "$(aws secretsmanager get-secret-value --region us-west-1 --secret-id colton-games/nonprod/auth \
#     --query SecretString --output text | jq -r 'to_entries[] | "export \(.key)=\(.value|@sh)"')"
#
# VERCEL. Terraform writes each secret's keys to the colton-games project as sensitive
# variables, Production from prod/* and Preview from nonprod/*, one variable per target and
# never one spanning both. Nothing goes to Development. The values pass through Terraform
# state, like the other games secrets (dev-ai-services.tf, games-multiplayer-bringup.tf):
# state is readable by administration only, the same boundary as the secrets, and Terraform
# is pinned to 1.10.3, so write-only arguments (1.11) are not available. That includes the
# admin address list. NEXT_PUBLIC_VAPID_PUBLIC_KEY is the one name the browser sees: a push
# subscription needs the public key in the page, and it is public by design.
#
# FIRST APPLY. Reading a secret that has no value yet fails the whole plan, for every file in
# this repo. So var.colton_games_accounts_ready is a checked-in gate that starts empty:
# Terraform reads only the secrets it names and writes only their variables. With it empty a
# plan passes over empty containers and the site keeps sign-in off. (enable_runner_app_webhooks
# does the same job for the runner tokens, runner-app.tf.) Change the default in a commit,
# never with -var: a later plan without the flag would propose deleting the variables.
#
# BRING-UP, in this order (README.md, "Colton Games accounts", has the commands):
#   1. Apply with the gate empty. The four containers exist and are empty; nothing is in Vercel.
#   2. Create two Google OAuth clients (Web application), one per environment. Redirect URIs
#      are https://<hostname>/api/auth/callback/google for every hostname that serves the
#      site: production's on one client, the non-production ones on the other.
#   3. Put prod/auth and nonprod/auth (above).
#   4. Add "prod/auth" and "nonprod/auth" to the gate's default, commit, plan, apply. The plan
#      adds four variables per environment and changes nothing else.
#   5. Redeploy: a deployment reads its environment when it is built.
#   Push is the same later: make a key pair per environment, put */push, add both names.
#   Account saves come after sign-in works and after the games repository's saves migration
#   has arrived (games-mp-migrate applies it when it merges to main): then name the target in
#   var.colton_games_account_saves_targets (below).
# A value changed later is one put-secret-value, one apply and a redeploy. A value that is
# missing, is in the wrong key, or is the same in both environments fails the plan with the
# secret's name.
#
# ACCOUNT SAVES. The site keeps a signed-in player's saved games in their account only where
# ACCOUNT_SAVES is exactly `on` (lib/saves in the games repository); unset is off. It is a
# switch, not a secret, so it has no container: var.colton_games_account_saves_targets names
# the Vercel targets that get the variable, and it starts empty. Production is the one to
# name. The site trusts no preview address for saves, so the switch does nothing on Preview.
#
# WHAT IS NOT HERE, because it cannot be or already exists:
#   Google OAuth clients   Google has no API for Web application clients (cloudflare.tf).
#   Preview sign-in        Google accepts no wildcard redirect URI, and a preview's address is
#                          new with every deploy. So on a Vercel deployment the site offers
#                          sign-in only at its registered hostnames (production's): a preview
#                          shows no sign-in button and its auth routes answer 404, whatever
#                          Preview's variables hold.
#                          TODO(owner): if previews should offer sign-in, they need a stable
#                          hostname (a vercel_project_domain with a git_branch here) that the
#                          site also lists.
#   Supabase               the project came from the Vercel integration and is not managed in
#                          this repo. Production and Preview already hold its URL and server key
#                          and SKYHOOK_LEADERBOARD_ENVIRONMENT, so the accounts features add no
#                          database variable. Their migrations are applied by hand: games-mp-migrate
#                          runs only the multiplayer ones (games-multiplayer/bringup.py).
#   The dev boxes          read nonprod/* themselves and make their own AUTH_SECRET
#                          (`openssl rand -base64 33`); Preview's never leaves Vercel and state.

locals {
  colton_games_accounts_auth_keys = ["AUTH_GOOGLE_ID", "AUTH_GOOGLE_SECRET", "SITE_ADMIN_EMAILS"]
  colton_games_accounts_push_keys = ["NEXT_PUBLIC_VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"]

  # Who may read a non-production secret besides the administration set.
  colton_games_accounts_nonprod_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]

  # <environment>/<kind> => the variable names its JSON holds and who else may read it.
  colton_games_accounts_secrets = {
    "prod/auth"    = { keys = local.colton_games_accounts_auth_keys, readers = [] }
    "prod/push"    = { keys = local.colton_games_accounts_push_keys, readers = [] }
    "nonprod/auth" = { keys = local.colton_games_accounts_auth_keys, readers = local.colton_games_accounts_nonprod_readers }
    "nonprod/push" = { keys = local.colton_games_accounts_push_keys, readers = local.colton_games_accounts_nonprod_readers }
  }
}

resource "aws_secretsmanager_secret" "colton_games_accounts" {
  for_each = local.colton_games_accounts_secrets

  name                    = "colton-games/${each.key}"
  description             = "colton-games accounts, ${each.key} (JSON: ${join(", ", each.value.keys)}). Value set out of band; see colton-games-accounts.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "colton-games/${each.key}", Project = "games" }
}

# Nobody but the administration set and that environment's readers may read it, whatever an
# identity policy elsewhere says. For prod/* the reader list is empty.
resource "aws_secretsmanager_secret_policy" "colton_games_accounts" {
  for_each = local.colton_games_accounts_secrets

  secret_arn = aws_secretsmanager_secret.colton_games_accounts[each.key].arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndThisEnvironmentsReadersCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.colton_games_accounts[each.key].arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, each.value.readers) } }
    }]
  })
}

data "aws_iam_policy_document" "colton_games_accounts_nonprod_read" {
  statement {
    sid       = "ReadTheColtonGamesNonProductionAccountsSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [for key, secret in aws_secretsmanager_secret.colton_games_accounts : secret.arn if startswith(key, "nonprod/")]
  }
}

# A managed policy, not inline: dev-server-role's inline policies are within a few hundred
# characters of IAM's 10,240 limit (dev-instance-common.tf).
resource "aws_iam_policy" "colton_games_accounts_nonprod_read" {
  name        = "dev-colton-games-accounts-nonprod-read"
  description = "Dev boxes: read the colton-games non-production accounts secrets (colton-games/nonprod/*) and nothing else"
  policy      = data.aws_iam_policy_document.colton_games_accounts_nonprod_read.json
}

resource "aws_iam_role_policy_attachment" "colton_games_accounts_nonprod_read" {
  for_each   = { dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }
  role       = each.value
  policy_arn = aws_iam_policy.colton_games_accounts_nonprod_read.arn
}

# -------------------------------------------------------------------------------------
# The session-signing secret
# -------------------------------------------------------------------------------------

# AUTH_SECRET signs the session cookie and keys the admin mark inside it. Generated here, one
# per environment, and written to Vercel only: that is how this project's other cookie-signing
# secret is made (MP_COOKIE_SECRET, games-multiplayer-bringup.tf). Nobody has to invent, type
# or copy it, the two environments cannot be given the same one, and it is long enough by
# construction (the site refuses fewer than 32 characters). It is in state, like that one.
# Replacing it signs everyone out of that environment and nothing else: the site stores no
# session, and nothing in the database is keyed by it.
#   terraform apply -replace='random_password.colton_games_auth_secret["production"]'
resource "random_password" "colton_games_auth_secret" {
  for_each = toset(["production", "preview"])
  length   = 64
  special  = false
}

# -------------------------------------------------------------------------------------
# Vercel: each ready secret's variables, Production from prod and Preview from nonprod
# -------------------------------------------------------------------------------------

variable "colton_games_accounts_ready" {
  description = "The colton-games accounts secrets that have a value, as <env>/<kind> (prod/auth, prod/push, nonprod/auth, nonprod/push). Terraform reads only these and writes only their variables to Vercel. Starts empty; add a name to this default, in a commit, once its value is put (colton-games-accounts.tf)."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for name in var.colton_games_accounts_ready : contains(keys(local.colton_games_accounts_secrets), name)])
    error_message = "colton_games_accounts_ready may name only prod/auth, prod/push, nonprod/auth and nonprod/push."
  }
}

data "aws_secretsmanager_secret_version" "colton_games_accounts" {
  for_each  = var.colton_games_accounts_ready
  secret_id = aws_secretsmanager_secret.colton_games_accounts[each.key].id
}

locals {
  colton_games_accounts_vercel_target = { prod = "production", nonprod = "preview" }
  colton_games_accounts_other_target  = { production = "preview", preview = "production" }
  colton_games_accounts_target_env    = { for env, target in local.colton_games_accounts_vercel_target : target => env }

  # "<KEY>/<target>" => the secret and JSON key its value comes from. Built from the gate and
  # the table above only, never from a value: for_each keys must not be sensitive. An auth
  # secret brings its environment's AUTH_SECRET with it, so the three variables that turn
  # sign-in on arrive together.
  colton_games_accounts_vercel_env = {
    for entry in flatten([
      for name in var.colton_games_accounts_ready : [
        for key in concat(local.colton_games_accounts_secrets[name].keys, endswith(name, "/auth") ? ["AUTH_SECRET"] : []) :
        { secret = name, key = key, target = local.colton_games_accounts_vercel_target[split("/", name)[0]] }
      ]
    ]) : "${entry.key}/${entry.target}" => entry
  }

  # Sensitive from here on. A value that is not a JSON object, or lacks a key, becomes "" and
  # the precondition below refuses the plan by name; Terraform never quotes the value.
  colton_games_accounts_payload = {
    for name, version in data.aws_secretsmanager_secret_version.colton_games_accounts :
    name => try(jsondecode(version.secret_string), {})
  }

  colton_games_accounts_vercel_values = {
    for id, entry in local.colton_games_accounts_vercel_env : id => (
      entry.key == "AUTH_SECRET"
      ? random_password.colton_games_auth_secret[entry.target].result
      : trimspace(try(tostring(local.colton_games_accounts_payload[entry.secret][entry.key]), ""))
    )
  }

  # What a value must look like, by variable name; any other must only be non-empty.
  # NEXT_PUBLIC_VAPID_PUBLIC_KEY is compiled into the page, so a private key pasted in its
  # place would be published: the two shapes cannot match the same text (65 and 32 bytes of
  # URL-safe base64). The client id's shape catches the id and the secret swapped.
  colton_games_accounts_shapes = {
    AUTH_GOOGLE_ID               = "^[0-9A-Za-z_-]+\\.apps\\.googleusercontent\\.com$"
    NEXT_PUBLIC_VAPID_PUBLIC_KEY = "^B[A-Za-z0-9_-]{86}$"
    VAPID_PRIVATE_KEY            = "^[A-Za-z0-9_-]{43}$"
    VAPID_SUBJECT                = "^(https://|mailto:)\\S+$"
  }

  # Credentials an environment must have to itself. The admin list and the push subject may
  # be the same in both. (A missing value is the first precondition's to report, and the
  # other environment's is "" until its secret is ready.)
  colton_games_accounts_distinct_keys = ["AUTH_GOOGLE_ID", "AUTH_GOOGLE_SECRET", "NEXT_PUBLIC_VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"]
}

# Env changes reach a deployment only when it is built: the next production deploy, and new
# previews. Vercel refuses a second variable with the same key on a target, so none of these
# names may also be set by hand in the project.
resource "vercel_project_environment_variable" "colton_games_accounts" {
  for_each = local.colton_games_accounts_vercel_env

  team_id    = var.vercel_team_id
  project_id = local.colton_games_vercel_project_id
  key        = split("/", each.key)[0]
  value      = local.colton_games_accounts_vercel_values[each.key]
  target     = [each.value.target]
  sensitive  = true
  comment    = "colton-games accounts; managed by ejc3/aws colton-games-accounts.tf"

  lifecycle {
    precondition {
      condition     = can(regex(lookup(local.colton_games_accounts_shapes, each.value.key, "\\S"), local.colton_games_accounts_vercel_values[each.key]))
      error_message = "Secret colton-games/${each.value.secret}: ${each.value.key} is missing or is not what that key holds. Put the whole JSON again (colton-games-accounts.tf lists the keys)."
    }

    precondition {
      condition = (
        !contains(local.colton_games_accounts_distinct_keys, each.value.key) ||
        local.colton_games_accounts_vercel_values[each.key] == "" ||
        local.colton_games_accounts_vercel_values[each.key] != lookup(
          local.colton_games_accounts_vercel_values, "${each.value.key}/${local.colton_games_accounts_other_target[each.value.target]}", ""
        )
      )
      error_message = "${each.value.key} is the same in production and non-production. Each environment has its own Google OAuth client and its own push key pair (colton-games-accounts.tf)."
    }
  }
}

# -------------------------------------------------------------------------------------
# Account saves: the site's own switch, per target
# -------------------------------------------------------------------------------------

variable "colton_games_account_saves_targets" {
  description = "Vercel targets of the colton-games project where account saves are switched on (ACCOUNT_SAVES=on): production, preview, or both. Starts empty, which is off everywhere. Add a target to this default, in a commit, after that target's sign-in works and the saves migration is applied (colton-games-accounts.tf)."
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for target in var.colton_games_account_saves_targets : contains(values(local.colton_games_accounts_vercel_target), target)])
    error_message = "colton_games_account_saves_targets may name only production and preview."
  }
}

# Not sensitive: the value is the word `on`, and anyone can see whether the site offers saves.
# One variable per target, so each is switched on by itself. Saves belong to a signed-in
# account, so a target cannot be named before its auth secret is in the gate above.
resource "vercel_project_environment_variable" "colton_games_account_saves" {
  for_each = var.colton_games_account_saves_targets

  team_id    = var.vercel_team_id
  project_id = local.colton_games_vercel_project_id
  key        = "ACCOUNT_SAVES"
  value      = "on"
  target     = [each.key]
  sensitive  = false
  comment    = "colton-games account saves switch; managed by ejc3/aws colton-games-accounts.tf"

  lifecycle {
    precondition {
      condition     = contains(var.colton_games_accounts_ready, "${local.colton_games_accounts_target_env[each.key]}/auth")
      error_message = "Account saves need sign-in: add ${local.colton_games_accounts_target_env[each.key]}/auth to colton_games_accounts_ready before naming ${each.key} in colton_games_account_saves_targets."
    }
  }
}

# -------------------------------------------------------------------------------------
# Outputs
# -------------------------------------------------------------------------------------

output "colton_games_accounts_secrets" {
  description = "Secrets Manager names of the colton-games accounts credentials (us-west-1), by environment and kind. Values are set out of band."
  value       = { for key, secret in aws_secretsmanager_secret.colton_games_accounts : key => secret.name }
}

# Names only.
output "colton_games_accounts_vercel_env" {
  description = "The accounts variables Terraform writes to the colton-games Vercel project now, per target (names only). A target is empty until var.colton_games_accounts_ready names its secrets or var.colton_games_account_saves_targets names it."
  value = {
    for target in values(local.colton_games_accounts_vercel_target) :
    target => sort(concat(
      [for entry in local.colton_games_accounts_vercel_env : entry.key if entry.target == target],
      contains(var.colton_games_account_saves_targets, target) ? ["ACCOUNT_SAVES"] : [],
    ))
  }
}

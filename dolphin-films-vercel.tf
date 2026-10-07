# dolphin-films-vercel.tf
#
# Terraform writes the dolphin-films Vercel project's environment (project `dolphin-films`, team
# dolphin-labs, root directory films/web), as colton-games-accounts.tf does for colton-games:
# Production from the prod/* containers of dolphin-films.tf and Preview from nonprod/*, one
# sensitive variable per target, never one spanning both, nothing to Development. The values
# pass through Terraform state like the other secrets Terraform reads; state is readable by
# administration only, the same boundary as the containers.
#
#   AUTH_SECRET, AUTH_GOOGLE_ID, AUTH_GOOGLE_SECRET   from <env>/auth
#   TURSO_DATABASE_URL, TURSO_AUTH_TOKEN              from <env>/turso
#   FILMS_SEED_EMAILS, FILMS_ADMIN_EMAILS             from people/addresses (people.tf), key
#                                                     dolphin_films: {"prod": {"seeds": [...],
#                                                     "admins": [...]}, "nonprod": {...}}
#
# The address lists are in no container of this site and in no file of this repository: they
# are the `dolphin_films` key of the people secret, written with their environment's auth
# secret. An empty list writes no variable (Vercel refuses an empty value; the site reads an
# unset one as nobody). Production must have both lists: with the invite gate shut and nobody
# seeded, nobody could get in, so the plan refuses it.
#
# THE TOKEN. The Vercel provider of vercel-cc-games.tf holds a token scoped to the colton-games
# team, which cannot reach dolphin-labs. This file has its own provider, alias dolphin_labs,
# whose token is the container `vercel-api-token-dolphin-labs` (administration only): an API
# token made in Vercel for the dolphin-labs team, never anyone's CLI login.
#
# THE GATE. Reading a secret that has no value fails every plan of this repository, so
# var.dolphin_films_vercel_ready names the containers that have one, and starts empty. While it
# is empty nothing here reads a value or the token, and no provider is configured. Once it names
# a container, the token must have a value too. Change it in a commit, never with -var: a later
# plan without the flag would propose deleting every variable.
#
# BRING-UP, once, in this order:
#   1. Apply this file with the gate empty: the token's container exists.
#   2. In Vercel, as the owner: Account Settings > Tokens > Create, scope dolphin-labs. Put it:
#        read -rs T; printf %s "$T" | aws secretsmanager put-secret-value --region us-west-1 \
#          --secret-id vercel-api-token-dolphin-labs --secret-string file:///dev/stdin; unset T
#   3. Add the dolphin_films key to people/addresses (people.tf says how to put that secret).
#   4. Set the gate to all four containers in a commit, plan and apply. Expect up to fourteen to
#      add: seven variables for each of production and preview.
#   5. Redeploy. The production deploy that follows is the first thing to reach production's
#      database, and it applies the site's migrations (dolphin-films.tf).

variable "dolphin_films_vercel_team_id" {
  description = "Vercel team that owns the dolphin-films project (dolphin-labs)"
  type        = string
  default     = "team_4TayirAcbgetrOUgYXghMXJH"
}

variable "dolphin_films_vercel_ready" {
  description = "The dolphin-films containers that have a value, as <env>/<kind> (prod/auth, prod/turso, nonprod/auth, nonprod/turso). Terraform reads only these, and the dolphin-labs Vercel token once any is named, and writes only their variables to Vercel. It started empty; it names all four once the token and the people key are in place. Change it in a commit, never with -var (dolphin-films-vercel.tf)."
  type        = set(string)
  default     = ["prod/auth", "prod/turso", "nonprod/auth", "nonprod/turso"] # every container has a value, and so has the token

  validation {
    condition     = alltrue([for name in var.dolphin_films_vercel_ready : contains(keys(local.dolphin_films_secrets), name)])
    error_message = "dolphin_films_vercel_ready may name only prod/auth, prod/turso, nonprod/auth and nonprod/turso."
  }
}

resource "aws_secretsmanager_secret" "dolphin_films_vercel_api_token" {
  name                    = "vercel-api-token-dolphin-labs"
  description             = "Vercel API token for the dolphin-labs team: Terraform writes the dolphin-films project's environment with it. Value set out of band; see dolphin-films-vercel.tf."
  recovery_window_in_days = 30
  tags                    = { Name = "vercel-api-token-dolphin-labs", Project = "dolphin-films" }

  lifecycle {
    prevent_destroy = true
  }
}

# Administration only, whatever an identity policy elsewhere says: a dev box holding this could
# change or take down the site.
resource "aws_secretsmanager_secret_policy" "dolphin_films_vercel_api_token" {
  secret_arn = aws_secretsmanager_secret.dolphin_films_vercel_api_token.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.dolphin_films_vercel_api_token.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

ephemeral "aws_secretsmanager_secret_version" "dolphin_films_vercel_api_token" {
  count     = length(var.dolphin_films_vercel_ready) > 0 ? 1 : 0
  secret_id = aws_secretsmanager_secret.dolphin_films_vercel_api_token.id
}

# `team` is not set on the provider, as in vercel-cc-games.tf: each resource passes team_id.
# With the gate empty no resource uses this provider, so it is never configured.
provider "vercel" {
  alias     = "dolphin_labs"
  api_token = one(ephemeral.aws_secretsmanager_secret_version.dolphin_films_vercel_api_token[*].secret_string)
}

data "aws_secretsmanager_secret_version" "dolphin_films" {
  for_each  = var.dolphin_films_vercel_ready
  secret_id = aws_secretsmanager_secret.dolphin_films[each.key].id
}

locals {
  dolphin_films_vercel_project_id = "prj_2uGgqW9bD402J5kpuclLLKUBVl3e"
  dolphin_films_vercel_target     = { prod = "production", nonprod = "preview" }
  dolphin_films_other_target      = { production = "preview", preview = "production" }

  # Sensitive on purpose (they are written to Vercel as sensitive variables). Absent until the
  # key is added to the people secret; then each environment's two lists.
  dolphin_films_people = {
    for env in keys(local.dolphin_films_vercel_target) : env => {
      FILMS_SEED_EMAILS  = try(tolist(local.people.dolphin_films[env].seeds), [])
      FILMS_ADMIN_EMAILS = try(tolist(local.people.dolphin_films[env].admins), [])
    }
  }

  # What an auth container brings besides its own keys: the lists of its environment that are
  # not empty. Only whether a list is empty leaves the sensitive input; for_each keys must not
  # be sensitive.
  dolphin_films_auth_extras = {
    for env, lists in local.dolphin_films_people : env => [for name, addresses in lists : name if nonsensitive(length(addresses) > 0)]
  }

  # "<KEY>/<target>" => the container and key its value comes from. Built from the gate and the
  # tables only, never from a value.
  dolphin_films_vercel_env = {
    for entry in flatten([
      for name in var.dolphin_films_vercel_ready : [
        for key in concat(local.dolphin_films_secrets[name].keys, endswith(name, "/auth") ? local.dolphin_films_auth_extras[split("/", name)[0]] : []) :
        { secret = name, key = key, target = local.dolphin_films_vercel_target[split("/", name)[0]] }
      ]
    ]) : "${entry.key}/${entry.target}" => entry
  }

  # Sensitive from here on. A value that is not a JSON object, or lacks a key, becomes "" and a
  # precondition refuses the plan by name; Terraform never quotes the value.
  dolphin_films_payload = {
    for name, version in data.aws_secretsmanager_secret_version.dolphin_films : name => try(jsondecode(version.secret_string), {})
  }

  dolphin_films_vercel_values = {
    for id, entry in local.dolphin_films_vercel_env : id => (
      startswith(entry.key, "FILMS_")
      ? join(",", local.dolphin_films_people[split("/", entry.secret)[0]][entry.key])
      : trimspace(try(tostring(local.dolphin_films_payload[entry.secret][entry.key]), ""))
    )
  }

  # What a value must look like, by variable name; any other must only be non-empty. The client
  # id's shape catches the id and the secret swapped; the URL's catches the URL and the token.
  dolphin_films_shapes = {
    AUTH_SECRET        = "^\\S{32,}$"
    AUTH_GOOGLE_ID     = "^[0-9A-Za-z_-]+\\.apps\\.googleusercontent\\.com$"
    TURSO_DATABASE_URL = "^libsql://[a-z0-9.-]+$"
    FILMS_SEED_EMAILS  = "^[^\\s@,]+@[^\\s@,]+\\.[^\\s@,]+(,[^\\s@,]+@[^\\s@,]+\\.[^\\s@,]+)*$"
    FILMS_ADMIN_EMAILS = "^[^\\s@,]+@[^\\s@,]+\\.[^\\s@,]+(,[^\\s@,]+@[^\\s@,]+\\.[^\\s@,]+)*$"
  }

  # Credentials an environment must have to itself. The address lists may be the same in both.
  dolphin_films_distinct_keys = ["AUTH_SECRET", "AUTH_GOOGLE_ID", "AUTH_GOOGLE_SECRET", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"]
}

# Env changes reach a deployment only when it is built. Vercel refuses a second variable with the
# same key on a target, so none of these names may also be set by hand in the project.
resource "vercel_project_environment_variable" "dolphin_films" {
  provider = vercel.dolphin_labs
  for_each = local.dolphin_films_vercel_env

  team_id    = var.dolphin_films_vercel_team_id
  project_id = local.dolphin_films_vercel_project_id
  key        = split("/", each.key)[0]
  value      = local.dolphin_films_vercel_values[each.key]
  target     = [each.value.target]
  sensitive  = true
  comment    = "dolphin-films; managed by ejc3/aws dolphin-films-vercel.tf"

  lifecycle {
    precondition {
      condition     = can(regex(lookup(local.dolphin_films_shapes, each.value.key, "\\S"), local.dolphin_films_vercel_values[each.key]))
      error_message = "${startswith(each.value.key, "FILMS_") ? "people/addresses dolphin_films" : "Secret dolphin-films/${each.value.secret}"}: ${each.value.key} is missing or is not what that key holds (dolphin-films-vercel.tf lists the keys)."
    }

    precondition {
      condition = (
        !contains(local.dolphin_films_distinct_keys, each.value.key) ||
        local.dolphin_films_vercel_values[each.key] == "" ||
        local.dolphin_films_vercel_values[each.key] != lookup(
          local.dolphin_films_vercel_values, "${each.value.key}/${local.dolphin_films_other_target[each.value.target]}", ""
        )
      )
      error_message = "${each.value.key} is the same in production and non-production. Each environment has its own Google client, AUTH_SECRET and database (dolphin-films.tf)."
    }

    # Production with nobody seeded or nobody to run it: the gate would keep everyone out.
    precondition {
      condition     = each.value.secret != "prod/auth" || each.value.key != "AUTH_GOOGLE_ID" || length(local.dolphin_films_auth_extras["prod"]) == 2
      error_message = "prod/auth is in dolphin_films_vercel_ready but people/addresses has no dolphin_films.prod seeds or admins: with the invite gate shut, nobody could get in (dolphin-films-vercel.tf)."
    }
  }
}

# Names only.
output "dolphin_films_vercel_written" {
  description = "The variables Terraform writes to the dolphin-films Vercel project now, per target (names only). Empty until var.dolphin_films_vercel_ready names a container."
  value = {
    for target in values(local.dolphin_films_vercel_target) :
    target => sort([for entry in local.dolphin_films_vercel_env : entry.key if entry.target == target])
  }
}

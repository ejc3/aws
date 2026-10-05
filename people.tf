# people.tf: where people's email addresses live, so they are not in git.
#
# Some configuration is a person's address and nothing else: who the Cloudflare Access policies let through to the dev sites, who
# owns the browser manager, the root address of the staging AWS account. They used to be literals in tracked files. They are one
# Secrets Manager secret instead, `people/addresses` in us-west-1, which this file creates as an empty container and the
# administration set fills out of band, as with every other secret here (JSON on stdin, never a file in a repo):
#
#   read -rs T; printf %s "$T" | aws secretsmanager put-secret-value --region us-west-1 \
#     --secret-id people/addresses --secret-string file:///dev/stdin; unset T
#   {"owner": "...", "family": ["...", "..."], "staging_account": "...", "colton_games_site_admins": {"prod": [...], "nonprod": [...]}}
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

data "aws_secretsmanager_secret_version" "people" {
  secret_id = aws_secretsmanager_secret.people.id
}

locals {
  people = jsondecode(data.aws_secretsmanager_secret_version.people.secret_string)

  # The names the configuration used before the addresses left git (variables or locals), so call sites change only their prefix.
  #
  # nonsensitive() on purpose. These were plain literals, and the secret's value arrives marked sensitive; consumers then plan an
  # in-place "update" for the mark alone, and the dev-staging account's update makes every value that depends on its id unknown
  # (one such plan wanted to replace the security modules' IAM roles). Unmarked, an unchanged address plans no change at all.
  # The cost: if an address DOES change, the plan prints it. Plans stay on the jumpbox and in the Stop hook's log.
  dev_allowed_emails    = nonsensitive(local.people.family)          # Cloudflare Access: who may reach every *.cc-games.dev host
  browser_manager_owner = nonsensitive(local.people.owner)           # the browser manager's single owner
  dev_staging_email     = nonsensitive(local.people.staging_account) # root email of the dev-staging member account
}

# Fail the plan, naming the key and never the value, if the secret is missing, empty, or still lacks a key. An empty Access
# allowlist or a blank account email must not reach an apply.
resource "terraform_data" "people_shape" {
  input = sha256(data.aws_secretsmanager_secret_version.people.secret_string)

  lifecycle {
    precondition {
      condition     = can(local.people.owner) && can(local.people.family) && can(local.people.staging_account) && can(local.people.colton_games_site_admins.prod) && can(local.people.colton_games_site_admins.nonprod)
      error_message = "The people/addresses secret must be JSON with the keys owner, family, staging_account and colton_games_site_admins {prod, nonprod} (see people.tf)."
    }
    precondition {
      condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", local.people.owner)) && can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", local.people.staging_account))
      error_message = "people/addresses: owner and staging_account must each be one email address."
    }
    precondition {
      condition     = length(local.people.family) > 0 && alltrue([for e in local.people.family : can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", e))])
      error_message = "people/addresses: family must be a non-empty list of email addresses (it is the Cloudflare Access allowlist)."
    }
  }
}

# dolphin-labs-ci-logs.tf
#
# A read-only Workers Logs token for dolphin-labs-hq/dolphin-labs' GitHub Actions (dolphin-labs#583). After every
# production deploy, that repository's cloudflare-stage.yml runs its Playwright suite against gamesworthwatching.com and
# then reads the production Worker's (dolphin-labs-prod) Workers Logs for the same window: any exceededCpu,
# exceededMemory or exception outcome fails the run. On 2026-10-15 the Worker answered Error 1102 (exceededCpu, #435)
# while every check that existed stayed green. Until this secret exists the log step skips with a notice.
#
#   CLOUDFLARE_OBSERVABILITY_TOKEN   secret of the `production-check` environment in dolphin-labs-hq/dolphin-labs.
#                                    Workers Observability Read on this account and nothing else: it can query logs,
#                                    and cannot deploy, change, tail or read any zone. Cloudflare scopes this
#                                    permission to the account, not to one Worker, so it reads EVERY Worker's logs
#                                    (colton-games-prod's too), which is why only main may hold it. Checked
#                                    2026-10-16: a token with this one group answers the telemetry query (HTTP 200).
#
# An environment secret, admitted to the `main` branch only (as dolphin-relay.tf does for the relay's deploy token),
# rather than a repository secret: Workers Logs carry request paths and visitors' coarse location, and a repository
# secret is readable by every workflow run on any branch. The job that reads it (`e2e (Production)`) runs only on main.
#
# The token's value is in state, as with the other tokens Terraform mints (workers-deploy.tf); state is administration
# only, and no Secrets Manager copy is kept: nothing but that job needs it. ROTATE with
# `terraform apply -replace=cloudflare_account_token.dolphin_labs_ci_logs`; the next apply rewrites the GitHub secret.

locals {
  dolphin_labs_ci_repository  = "dolphin-labs"
  dolphin_labs_ci_environment = "production-check"
}

resource "cloudflare_account_token" "dolphin_labs_ci_logs" {
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "dolphin-labs-ci-logs"
  policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.cloudflare_observability_account_groups.workers_observability_read }]
    resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })
  }]
}

# The administration agent's GitHub App on dolphin-labs-hq (environments, secrets and variables write), the same
# credentials dolphin-relay.tf reads ephemerally; its key never reaches state.
provider "github" {
  alias = "dolphin_labs_ci"
  owner = "dolphin-labs-hq"
  app_auth {
    id              = tostring(local.dolphin_relay_github_app.app_id)
    installation_id = tostring(local.dolphin_relay_github_app.installations["dolphin-labs-hq"])
    pem_file        = local.dolphin_relay_github_app.private_key
  }
}

resource "github_repository_environment" "dolphin_labs_production_check" {
  provider    = github.dolphin_labs_ci
  repository  = local.dolphin_labs_ci_repository
  environment = local.dolphin_labs_ci_environment
  # GitHub stores true for a private repository whatever is sent (dolphin-relay.tf); the branch policy is what holds.
  can_admins_bypass = true
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "dolphin_labs_production_check_main" {
  provider       = github.dolphin_labs_ci
  repository     = local.dolphin_labs_ci_repository
  environment    = github_repository_environment.dolphin_labs_production_check.environment
  branch_pattern = "main"
}

resource "github_actions_environment_secret" "dolphin_labs_ci_logs" {
  provider        = github.dolphin_labs_ci
  repository      = local.dolphin_labs_ci_repository
  environment     = github_repository_environment.dolphin_labs_production_check.environment
  secret_name     = "CLOUDFLARE_OBSERVABILITY_TOKEN"
  plaintext_value = cloudflare_account_token.dolphin_labs_ci_logs.value
}

# The Workers deploy token (workers-deploy.tf) for dolphin-labs' cloudflare-stage.yml deploy jobs, as a secret of a
# `cloudflare-deploy` environment admitted to `main` only. It was a repository secret, set by
# scripts/workers-deploy-secret.sh, and so readable by a workflow run on any branch. Workers Scripts Write reaches every
# Worker in the account, production included, and cannot be scoped to one (dolphin-relay.tf), so the branch rule is
# the control. Once the deploy jobs name the environment, the repository secret is deleted. Rotating workers_deploy
# (workers-deploy.tf) rewrites this secret on the next apply.
resource "github_repository_environment" "dolphin_labs_cloudflare_deploy" {
  provider          = github.dolphin_labs_ci
  repository        = local.dolphin_labs_ci_repository
  environment       = "cloudflare-deploy"
  can_admins_bypass = true
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "dolphin_labs_cloudflare_deploy_main" {
  provider       = github.dolphin_labs_ci
  repository     = local.dolphin_labs_ci_repository
  environment    = github_repository_environment.dolphin_labs_cloudflare_deploy.environment
  branch_pattern = "main"
}

resource "github_actions_environment_secret" "dolphin_labs_cloudflare_deploy" {
  provider        = github.dolphin_labs_ci
  repository      = local.dolphin_labs_ci_repository
  environment     = github_repository_environment.dolphin_labs_cloudflare_deploy.environment
  secret_name     = "CLOUDFLARE_API_TOKEN"
  plaintext_value = cloudflare_account_token.workers_deploy.value
}

# The same for dolphin-films (dolphin-labs-hq/dolphin-films, films-cloudflare-stage.yml): its staging deploy job takes the
# token from a main-only `cloudflare-deploy` environment, and its production job from the `cloudflare-production`
# environment it already uses (main only; it holds production's Turso pair and is not managed here), so the
# repository secret can be deleted. One environment per job, which is why production's copy goes into its own.
resource "github_repository_environment" "dolphin_films_cloudflare_deploy" {
  provider          = github.dolphin_labs_ci
  repository        = "dolphin-films"
  environment       = "cloudflare-deploy"
  can_admins_bypass = true
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "dolphin_films_cloudflare_deploy_main" {
  provider       = github.dolphin_labs_ci
  repository     = "dolphin-films"
  environment    = github_repository_environment.dolphin_films_cloudflare_deploy.environment
  branch_pattern = "main"
}

resource "github_actions_environment_secret" "dolphin_films_cloudflare_deploy" {
  provider        = github.dolphin_labs_ci
  repository      = "dolphin-films"
  environment     = github_repository_environment.dolphin_films_cloudflare_deploy.environment
  secret_name     = "CLOUDFLARE_API_TOKEN"
  plaintext_value = cloudflare_account_token.workers_deploy.value
}

resource "github_actions_environment_secret" "dolphin_films_cloudflare_production" {
  provider        = github.dolphin_labs_ci
  repository      = "dolphin-films"
  environment     = "cloudflare-production"
  secret_name     = "CLOUDFLARE_API_TOKEN"
  plaintext_value = cloudflare_account_token.workers_deploy.value
}

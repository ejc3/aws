# dolphin-labs-stage-check.tf
#
# A Cloudflare Access service token for dolphin-labs-hq/dolphin-labs' staging check (dolphin-labs#844). After its
# cloudflare-stage.yml deploys dolphin-labs-stage, the same job reads every top-level page of that Worker through Access
# with this token (web/scripts/worker-smoke.mjs) and deploy-production waits for it, so a Worker that builds and deploys
# but throws on every request (Cloudflare error 1101) stops at staging instead of reaching gamesworthwatching.com.
# The check also verifies the anonymous sign-in redirect. The app pins its HTTPS credential destination to the host below.
#
#   STAGE_ACCESS_CLIENT_ID, STAGE_ACCESS_CLIENT_SECRET
#     secrets of the `cloudflare-deploy` environment in dolphin-labs-hq/dolphin-labs (dolphin-labs-ci-logs.tf), which
#     admits `main` only: the job that reads them is the staging deploy, and it holds that environment already.
#
# Hostname-level Access takes precedence over the Worker-native gate. The exact hostname gets the same three
# existing policies and the CI token. Preview URLs and sibling staging Workers retain their Worker-level gate.
# https://developers.cloudflare.com/workers/configuration/cloudflare-access/#understand-access-hierarchy
# The token lives in Terraform state and the main-only GitHub environment. Rotate it with
# terraform apply -replace=cloudflare_zero_trust_access_service_token.dolphin_labs_stage_check

resource "cloudflare_zero_trust_access_service_token" "dolphin_labs_stage_check" {
  account_id = var.cloudflare_account_id
  name       = "dolphin-labs-stage-check"
  duration   = "8760h"
}

resource "cloudflare_zero_trust_access_policy" "dolphin_labs_stage_check" {
  account_id       = var.cloudflare_account_id
  name             = "Dolphin Labs staging check"
  decision         = "non_identity"
  session_duration = "24h"

  include = [{
    service_token = {
      token_id = cloudflare_zero_trust_access_service_token.dolphin_labs_stage_check.id
    }
  }]
}

resource "cloudflare_zero_trust_access_application" "dolphin_labs_stage_check" {
  account_id       = var.cloudflare_account_id
  name             = "Dolphin Labs staging"
  type             = "self_hosted"
  domain           = "dolphin-labs-stage.cc-games.workers.dev"
  session_duration = "24h"

  policies = [
    { id = cloudflare_zero_trust_access_policy.cc_games_allowed.id, precedence = 1 },
    { id = cloudflare_zero_trust_access_policy.cc_games_service.id, precedence = 2 },
    { id = cloudflare_zero_trust_access_policy.workers_stage_agents.id, precedence = 3 },
    { id = cloudflare_zero_trust_access_policy.dolphin_labs_stage_check.id, precedence = 4 },
  ]
}

resource "github_actions_environment_secret" "dolphin_labs_stage_check_id" {
  provider        = github.dolphin_labs_ci
  repository      = local.dolphin_labs_ci_repository
  environment     = github_repository_environment.dolphin_labs_cloudflare_deploy.environment
  secret_name     = "STAGE_ACCESS_CLIENT_ID"
  plaintext_value = cloudflare_zero_trust_access_service_token.dolphin_labs_stage_check.client_id

  depends_on = [cloudflare_zero_trust_access_application.dolphin_labs_stage_check]
}

resource "github_actions_environment_secret" "dolphin_labs_stage_check_secret" {
  provider        = github.dolphin_labs_ci
  repository      = local.dolphin_labs_ci_repository
  environment     = github_repository_environment.dolphin_labs_cloudflare_deploy.environment
  secret_name     = "STAGE_ACCESS_CLIENT_SECRET"
  plaintext_value = cloudflare_zero_trust_access_service_token.dolphin_labs_stage_check.client_secret

  depends_on = [cloudflare_zero_trust_access_application.dolphin_labs_stage_check]
}

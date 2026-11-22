# constellation-stage-smoke.tf
#
# A Cloudflare Access service token for constellation's lights-stage live smoke (ejc3/aws#989). After deploy-lights-web
# deploys lights-stage, the same job walks the live site through Access with this token, from a photo to the bag
# (apps/lights-web/scripts/stage-smoke.mjs), and fails closed without it: the owner's rule that a feature is done only
# when it works on the user's path, checked automatically.
#
#   STAGE_ACCESS_CLIENT_ID, STAGE_ACCESS_CLIENT_SECRET (secrets), LIGHTS_STAGE_URL (a variable)
#     in constellation's `cloudflare-deploy` environment (workers-deploy.tf), which admits `main` only: the job that
#     reads them is the staging deploy, and it holds that environment already.
#
# As dolphin-labs-stage-check.tf: hostname-level Access takes precedence over the Worker-native gate, so the exact
# hostname gets the same three policies as the shared staging application plus the CI token. Preview URLs and the
# sibling staging Workers keep their Worker-level gate, which never admits this token.
# https://developers.cloudflare.com/workers/configuration/cloudflare-access/#understand-access-hierarchy
# The token lives in Terraform state and the main-only GitHub environment. Rotate between deploys with
# terraform apply -replace=cloudflare_zero_trust_access_service_token.lights_stage_smoke

locals {
  lights_stage_hostname = "lights-stage.${cloudflare_workers_subdomain.cc_games.subdomain}.workers.dev"
}

resource "cloudflare_zero_trust_access_service_token" "lights_stage_smoke" {
  account_id = var.cloudflare_account_id
  name       = "constellation-lights-stage-smoke"
  duration   = "8760h"
}

resource "cloudflare_zero_trust_access_policy" "lights_stage_smoke" {
  account_id       = var.cloudflare_account_id
  name             = "Lights staging smoke"
  decision         = "non_identity"
  session_duration = "24h"

  include = [{
    service_token = {
      token_id = cloudflare_zero_trust_access_service_token.lights_stage_smoke.id
    }
  }]
}

resource "cloudflare_zero_trust_access_application" "lights_stage_smoke" {
  account_id       = var.cloudflare_account_id
  name             = "Lights staging"
  type             = "self_hosted"
  domain           = local.lights_stage_hostname
  session_duration = "24h"

  policies = [
    { id = cloudflare_zero_trust_access_policy.cc_games_allowed.id, precedence = 1 },
    { id = cloudflare_zero_trust_access_policy.cc_games_service.id, precedence = 2 },
    { id = cloudflare_zero_trust_access_policy.workers_stage_agents.id, precedence = 3 },
    { id = cloudflare_zero_trust_access_policy.lights_stage_smoke.id, precedence = 4 },
  ]
}

resource "github_actions_environment_secret" "lights_stage_smoke_id" {
  provider        = github.agents_md
  repository      = "constellation"
  environment     = github_repository_environment.workers_deploy_ejc3["constellation"].environment
  secret_name     = "STAGE_ACCESS_CLIENT_ID"
  plaintext_value = cloudflare_zero_trust_access_service_token.lights_stage_smoke.client_id

  depends_on = [cloudflare_zero_trust_access_application.lights_stage_smoke]
}

resource "github_actions_environment_secret" "lights_stage_smoke_secret" {
  provider        = github.agents_md
  repository      = "constellation"
  environment     = github_repository_environment.workers_deploy_ejc3["constellation"].environment
  secret_name     = "STAGE_ACCESS_CLIENT_SECRET"
  plaintext_value = cloudflare_zero_trust_access_service_token.lights_stage_smoke.client_secret

  depends_on = [cloudflare_zero_trust_access_application.lights_stage_smoke]
}

# Not a credential: the page the smoke walks.
resource "github_actions_environment_variable" "lights_stage_smoke_url" {
  provider      = github.agents_md
  repository    = "constellation"
  environment   = github_repository_environment.workers_deploy_ejc3["constellation"].environment
  variable_name = "LIGHTS_STAGE_URL"
  value         = "https://${local.lights_stage_hostname}/"
}

# constellation-stage-smoke.tf
#
# constellation's deploy-lights-web job walks the live lights-stage Worker after every deploy, through the staging
# Workers' Access application (workers-stage.tf), from a photo to the bag (apps/lights-web/scripts/stage-smoke.mjs):
# the owner's rule that a feature is done only when it works on the user's path, checked automatically and failing
# closed (ejc3/aws#989). CI gets a token of its own, not a copy of the agents' (workers-stage-agents), so either can be
# revoked alone. It lives only in constellation's main-only `cloudflare-deploy` environment (workers-deploy.tf).
# Like every staging-Workers token it opens the staging Workers, none in production.

resource "cloudflare_zero_trust_access_service_token" "workers_stage_constellation_ci" {
  account_id = var.cloudflare_account_id
  name       = "constellation-ci-stage-smoke"
  duration   = "8760h"

  lifecycle {
    create_before_destroy = true
  }
}

resource "cloudflare_zero_trust_access_policy" "workers_stage_constellation_ci" {
  account_id       = var.cloudflare_account_id
  name             = "staging Workers: constellation main-only CI"
  decision         = "non_identity"
  session_duration = "24h"
  include = [{
    service_token = { token_id = cloudflare_zero_trust_access_service_token.workers_stage_constellation_ci.id }
  }]
}

resource "github_actions_environment_secret" "constellation_stage_smoke" {
  provider    = github.agents_md
  for_each    = toset(["STAGE_ACCESS_CLIENT_ID", "STAGE_ACCESS_CLIENT_SECRET"])
  repository  = "constellation"
  environment = github_repository_environment.workers_deploy_ejc3["constellation"].environment
  secret_name = each.key
  plaintext_value = {
    STAGE_ACCESS_CLIENT_ID     = cloudflare_zero_trust_access_service_token.workers_stage_constellation_ci.client_id
    STAGE_ACCESS_CLIENT_SECRET = cloudflare_zero_trust_access_service_token.workers_stage_constellation_ci.client_secret
  }[each.key]
}

# Not a credential: the staging URL the smoke walks, from the Worker's name and the account's workers.dev subdomain.
resource "github_actions_environment_variable" "constellation_stage_smoke" {
  provider      = github.agents_md
  repository    = "constellation"
  environment   = github_repository_environment.workers_deploy_ejc3["constellation"].environment
  variable_name = "LIGHTS_STAGE_URL"
  value         = "https://lights-stage.${cloudflare_workers_subdomain.cc_games.subdomain}.workers.dev/"
}

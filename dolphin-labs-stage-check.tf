# dolphin-labs-stage-check.tf
#
# A Cloudflare Access service token for dolphin-labs-hq/dolphin-labs' staging check (dolphin-labs#844). After its
# cloudflare-stage.yml deploys dolphin-labs-stage, the same job reads every top-level page of that Worker through Access
# with this token (web/scripts/worker-smoke.mjs) and deploy-production waits for it, so a Worker that builds and deploys
# but throws on every request (Cloudflare error 1101) stops at staging instead of reaching gamesworthwatching.com.
# Without the token, the same job also requires / to redirect to Access's sign-in, the proof that staging is gated.
# Until these secrets exist that step warns and skips.
#
#   STAGE_ACCESS_CLIENT_ID, STAGE_ACCESS_CLIENT_SECRET
#     secrets of the `cloudflare-deploy` environment in dolphin-labs-hq/dolphin-labs (dolphin-labs-ci-logs.tf), which
#     admits `main` only: the job that reads them is the staging deploy, and it holds that environment already.
#
# Admitted by a non_identity policy on the staging-Workers Access application (workers-stage.tf) and nothing else, so it
# opens the `<site>-stage` Workers only: a read of staging pages, as the agents' token there allows. One token of its own
# rather than the agents' (workers-stage-agents/access), so revoking either touches nothing else. Its value is in state,
# as the other Terraform-minted CI credentials' are (dolphin-labs-ci-logs.tf); no Secrets Manager copy is kept, since
# nothing but that job needs it. The names avoid the workers_stage prefix that scripts/test-workers-stage-secrets.py
# keeps free of Terraform-held values. ROTATE with
# `terraform apply -replace=cloudflare_zero_trust_access_service_token.dolphin_labs_stage_check`; the next apply rewrites
# both GitHub secrets.

resource "cloudflare_zero_trust_access_service_token" "dolphin_labs_stage_check" {
  account_id = var.cloudflare_account_id
  name       = "dolphin-labs-stage-check"
  duration   = "8760h"
}

resource "cloudflare_zero_trust_access_policy" "dolphin_labs_stage_check" {
  account_id       = var.cloudflare_account_id
  name             = "staging Workers: dolphin-labs stage check"
  decision         = "non_identity"
  session_duration = "24h"

  include = [{
    service_token = {
      token_id = cloudflare_zero_trust_access_service_token.dolphin_labs_stage_check.id
    }
  }]
}

resource "github_actions_environment_secret" "dolphin_labs_stage_check_id" {
  provider        = github.dolphin_labs_ci
  repository      = local.dolphin_labs_ci_repository
  environment     = github_repository_environment.dolphin_labs_cloudflare_deploy.environment
  secret_name     = "STAGE_ACCESS_CLIENT_ID"
  plaintext_value = cloudflare_zero_trust_access_service_token.dolphin_labs_stage_check.client_id
}

resource "github_actions_environment_secret" "dolphin_labs_stage_check_secret" {
  provider        = github.dolphin_labs_ci
  repository      = local.dolphin_labs_ci_repository
  environment     = github_repository_environment.dolphin_labs_cloudflare_deploy.environment
  secret_name     = "STAGE_ACCESS_CLIENT_SECRET"
  plaintext_value = cloudflare_zero_trust_access_service_token.dolphin_labs_stage_check.client_secret
}

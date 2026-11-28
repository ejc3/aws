# constellation-lights-hub-release.tf
#
# The Lights hub updates itself from signed releases (constellation docs/LIGHTS-DESIGN.md decision 52;
# docs/LIGHTS.md "The hub updates itself"). constellation's lights-hub workflow:
#
#   - builds the hub for every pull request and push, compiling in the release key's PUBLIC half
#     from the LIGHTS_HUB_UPDATE_KEY_PEM repository variable (public, not a secret);
#   - signs each release's manifest in its `release` job only, which runs in the `lights-hub-release`
#     environment: that environment admits `main` alone and is the only place LIGHTS_HUB_SIGNING_KEY
#     (the private half) lives, so no pull-request job can read it;
#   - builds the Lights Hub card image with the site the hub pairs with, LIGHTS_HUB_SITE (a variable).
#
# The key lives in Terraform state and the main-only environment. A new key means every hub built
# before it stops taking updates, so it is not rotated casually:
#   terraform apply -replace=tls_private_key.lights_hub_release
resource "tls_private_key" "lights_hub_release" {
  algorithm = "ED25519"
}

resource "github_repository_environment" "lights_hub_release" {
  provider          = github.agents_md
  repository        = "constellation"
  environment       = "lights-hub-release"
  can_admins_bypass = false
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "lights_hub_release_main" {
  provider       = github.agents_md
  repository     = "constellation"
  environment    = github_repository_environment.lights_hub_release.environment
  branch_pattern = "main"
}

resource "github_actions_environment_secret" "lights_hub_signing_key" {
  provider        = github.agents_md
  repository      = "constellation"
  environment     = github_repository_environment.lights_hub_release.environment
  secret_name     = "LIGHTS_HUB_SIGNING_KEY"
  plaintext_value = tls_private_key.lights_hub_release.private_key_pem
}

# Not a credential: the public half every hub build compiles in to check a release's signature.
resource "github_actions_variable" "lights_hub_update_key" {
  provider      = github.agents_md
  repository    = "constellation"
  variable_name = "LIGHTS_HUB_UPDATE_KEY_PEM"
  value         = tls_private_key.lights_hub_release.public_key_pem
}

# Not a credential: the site a Lights Hub card pairs with (staging until the production site exists).
resource "github_actions_variable" "lights_hub_site" {
  provider      = github.agents_md
  repository    = "constellation"
  variable_name = "LIGHTS_HUB_SITE"
  value         = "https://${local.lights_stage_hostname}/"
}

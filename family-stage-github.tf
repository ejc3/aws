# The Family release job uses its own GitHub environment. The separate AWS trust
# policy also checks refs/heads/main when STS exchanges the job's OIDC token.
resource "github_repository_environment" "family_stage_api" {
  provider    = github.agents_md
  repository  = "constellation"
  environment = "family-stage"

  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "family_stage_api_main" {
  provider       = github.agents_md
  repository     = github_repository_environment.family_stage_api.repository
  environment    = github_repository_environment.family_stage_api.environment
  branch_pattern = "main"
}

resource "github_actions_environment_variable" "family_stage_api_role" {
  provider      = github.agents_md
  repository    = github_repository_environment.family_stage_api.repository
  environment   = github_repository_environment.family_stage_api.environment
  variable_name = "FAMILY_STAGE_DEPLOY_ROLE_ARN"
  value         = aws_iam_role.family_stage_api_ci.arn
}

# Adopt the environment and its main-only rule already configured in GitHub.
import {
  to = github_repository_environment.family_stage_api
  id = "constellation:family-stage"
}

import {
  to = github_repository_environment_deployment_policy.family_stage_api_main
  id = "constellation:family-stage:68590535"
}

import {
  to = github_actions_environment_variable.family_stage_api_role
  id = "constellation:family-stage:FAMILY_STAGE_DEPLOY_ROLE_ARN"
}

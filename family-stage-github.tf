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

# The same environment in dolphin-labs-hq/dolphin-labs, where Family's API deploy moves
# (.github/workflows/family-deploy-api.yml; docs/MONOREPO-MOVE.md there, phase 5), through the
# dolphin-labs-hq installation (github.dolphin_labs_ci, dolphin-labs-ci-logs.tf). Limited to main, holding
# the role's ARN. constellation's environment above stays until phase 8 removes it with its trust subject.
resource "github_repository_environment" "dolphin_labs_family_stage_api" {
  provider    = github.dolphin_labs_ci
  repository  = local.dolphin_labs_ci_repository
  environment = "family-stage"
  # GitHub stores true for a private repository whatever is sent (dolphin-relay.tf); the branch policy is what holds.
  can_admins_bypass = true
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "dolphin_labs_family_stage_api_main" {
  provider       = github.dolphin_labs_ci
  repository     = local.dolphin_labs_ci_repository
  environment    = github_repository_environment.dolphin_labs_family_stage_api.environment
  branch_pattern = "main"
}

resource "github_actions_environment_variable" "dolphin_labs_family_stage_api_role" {
  provider      = github.dolphin_labs_ci
  repository    = local.dolphin_labs_ci_repository
  environment   = github_repository_environment.dolphin_labs_family_stage_api.environment
  variable_name = "FAMILY_STAGE_DEPLOY_ROLE_ARN"
  value         = aws_iam_role.family_stage_api_ci.arn
}

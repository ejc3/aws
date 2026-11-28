# Family's TestFlight uploads from CI (ejc3/aws#968): the role that .github/workflows/testflight-family.yml in
# dolphin-labs-hq/dolphin-labs assumes to read the App Store Connect key (games-worth-watching/app-store-connect,
# dev-ai-services.tf) and nothing else. Family moved into that repository (its docs/MONOREPO-MOVE.md, phase 5):
# the organization's Mac runners (label ios-pool) serve it, and a user-owned repository cannot use them. The job
# builds Family from the exact main commit, signs it with Apple's cloud signing and uploads it for external testers.
#
# The workflow path is pinned before the file exists on dolphin-labs main: phase 5 lands a probe there
# (workflow_dispatch, `aws sts get-caller-identity`), and phase 7 replaces it with the real upload.
#
# TRUST: that workflow, on main, in the `testflight-family` GitHub environment, and nothing else.
#   - sub is GitHub's immutable form with the environment (repo and owner ids, so a renamed or recreated repo
#     cannot assume it), and ref pins main.
#   - job_workflow_ref narrows main to the one workflow file: another workflow on main gets nothing.
#   - The environment itself is limited to main (below); this trust does not rely on that alone.
resource "aws_iam_role" "dolphin_labs_family_testflight" {
  name                 = "dolphin-labs-family-testflight"
  description          = "Assumed by dolphin-labs-hq/dolphin-labs testflight-family.yml (main, environment testflight-family) via GitHub OIDC: read the App Store Connect key"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud"              = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub"              = "repo:dolphin-labs-hq@316189183/dolphin-labs@1330391842:environment:testflight-family"
          "token.actions.githubusercontent.com:ref"              = "refs/heads/main"
          "token.actions.githubusercontent.com:job_workflow_ref" = "dolphin-labs-hq/dolphin-labs/.github/workflows/testflight-family.yml@refs/heads/main"
        }
      }
    }]
  })
  tags = { Name = "dolphin-labs-family-testflight", Project = "dolphin-labs" }
}

resource "aws_iam_role_policy_attachment" "dolphin_labs_family_testflight_read" {
  role       = aws_iam_role.dolphin_labs_family_testflight.name
  policy_arn = aws_iam_policy.app_store_connect_read.arn
}

# The `testflight-family` environment in dolphin-labs, through the dolphin-labs-hq installation
# (github.dolphin_labs_ci, dolphin-labs-ci-logs.tf), limited to main. The role's variable is
# FAMILY_TESTFLIGHT_ROLE_ARN, not AWS_TESTFLIGHT_ROLE_ARN: that name is a repository variable there, read by
# GWW's testflight.yml for its own role (dolphin-labs-testflight.tf), and an environment variable would shadow it.
# The region is the literal us-west-1 in the workflow. FAMILY_API and FAMILY_ZERO_URL both point at Family's one
# staging host (family-account.tf, stage.hostname): the API on its paths, zero-cache on the rest.
locals {
  family_stage_url = "https://family-sync-stage.dolphin-labs.dev"
  family_testflight_variables = {
    FAMILY_TESTFLIGHT_ROLE_ARN = aws_iam_role.dolphin_labs_family_testflight.arn
    FAMILY_BUNDLE_ID           = "dev.dolphinlabs.family"
    FAMILY_API                 = local.family_stage_url
    FAMILY_ZERO_URL            = local.family_stage_url
  }
}

resource "github_repository_environment" "dolphin_labs_testflight_family" {
  provider    = github.dolphin_labs_ci
  repository  = local.dolphin_labs_ci_repository
  environment = "testflight-family"
  # GitHub stores true for a private repository whatever is sent (dolphin-relay.tf); the branch policy is what holds.
  can_admins_bypass = true
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "dolphin_labs_testflight_family_main" {
  provider       = github.dolphin_labs_ci
  repository     = local.dolphin_labs_ci_repository
  environment    = github_repository_environment.dolphin_labs_testflight_family.environment
  branch_pattern = "main"
}

resource "github_actions_environment_variable" "dolphin_labs_testflight_family" {
  for_each      = local.family_testflight_variables
  provider      = github.dolphin_labs_ci
  repository    = local.dolphin_labs_ci_repository
  environment   = github_repository_environment.dolphin_labs_testflight_family.environment
  variable_name = each.key
  value         = each.value
}

# No review contact here: Family's beta review contact is set in App Store Connect itself (the app record's
# beta app review details), and the workflow does not send one.

output "dolphin_labs_family_testflight" {
  description = "Variables of the testflight-family environment in dolphin-labs-hq/dolphin-labs (no credentials)"
  value       = local.family_testflight_variables
}

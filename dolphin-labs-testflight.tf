# dolphin-labs TestFlight uploads from CI: the role .github/workflows/testflight.yml in
# dolphin-labs-hq/dolphin-labs assumes to read the App Store Connect key
# (games-worth-watching/app-store-connect, dev-ai-services.tf) and nothing else. The job runs on the
# CI VM on the owner's Mac (apps/native/scripts/ci-vm.sh) and builds, signs (Apple's cloud signing,
# with the key) and uploads the iPhone app when its inputs change.
#
# TRUST: workflow testflight.yml, on the main branch of dolphin-labs-hq/dolphin-labs, and nothing else.
#   - sub is GitHub's immutable form (local.dolphin_labs_main_sub, bedrock-haiku.tf): a pull request
#     (refs/pull/N/merge) or another branch cannot assume it.
#   - job_workflow_ref narrows main to the one workflow file: another workflow on main gets nothing.
#   - The job declares no GitHub environment; adding one changes sub and needs this trust changed with it.
resource "aws_iam_role" "dolphin_labs_testflight" {
  name        = "dolphin-labs-testflight"
  description = "Assumed by dolphin-labs-hq/dolphin-labs testflight.yml (main) via GitHub OIDC: read the App Store Connect key"
  # An archive and upload take well under an hour.
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
          "token.actions.githubusercontent.com:sub"              = local.dolphin_labs_main_sub
          "token.actions.githubusercontent.com:job_workflow_ref" = "dolphin-labs-hq/dolphin-labs/.github/workflows/testflight.yml@refs/heads/main"
        }
      }
    }]
  })
  tags = { Name = "dolphin-labs-testflight", Project = "dolphin-labs" }
}

resource "aws_iam_role_policy_attachment" "dolphin_labs_testflight_read" {
  role       = aws_iam_role.dolphin_labs_testflight.name
  policy_arn = aws_iam_policy.app_store_connect_read.arn
}

output "dolphin_labs_testflight" {
  description = "Actions variables for dolphin-labs-hq/dolphin-labs's TestFlight workflow (testflight.yml)"
  value = {
    AWS_TESTFLIGHT_REGION   = "us-west-1"
    AWS_TESTFLIGHT_ROLE_ARN = aws_iam_role.dolphin_labs_testflight.arn
  }
}

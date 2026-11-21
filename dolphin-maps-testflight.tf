# dolphin-maps TestFlight uploads from CI: the role .github/workflows/ios.yml in dolphin-labs-hq/dolphin-maps assumes
# to read the App Store Connect key (games-worth-watching/app-store-connect, dev-ai-services.tf) and nothing else. The
# job runs on the organization's Mac runner pool (the Mac mini's CI VM, label `mini`; README, "The CI VM's runners"):
# it runs the iPhone app's UI tours and, on a manual run on main with `upload: testflight`, archives, signs (Apple's
# cloud signing, with the key) and uploads the commit the tours passed (apps/ios/scripts/testflight.sh).
#
# TRUST: workflow ios.yml, on the main branch of dolphin-labs-hq/dolphin-maps, and nothing else.
#   - sub is GitHub's immutable form (local.dolphin_maps_loop_sub, dolphin-maps-loop.tf): a pull request
#     (refs/pull/N/merge) or another branch, which agents dispatch ios.yml on for their tours, cannot assume it.
#   - job_workflow_ref narrows main to the one workflow file: another workflow on main gets nothing.
#   - The job declares no GitHub environment; adding one changes sub and needs this trust changed with it.
resource "aws_iam_role" "dolphin_maps_testflight" {
  name        = "dolphin-maps-testflight"
  description = "Assumed by dolphin-labs-hq/dolphin-maps ios.yml (main) via GitHub OIDC: read the App Store Connect key"
  # The key is read once, before the archive; the role's session only needs to outlast that.
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
          "token.actions.githubusercontent.com:sub"              = local.dolphin_maps_loop_sub
          "token.actions.githubusercontent.com:job_workflow_ref" = "dolphin-labs-hq/dolphin-maps/.github/workflows/ios.yml@refs/heads/main"
        }
      }
    }]
  })
  tags = { Name = "dolphin-maps-testflight", Project = "dolphin-maps" }
}

resource "aws_iam_role_policy_attachment" "dolphin_maps_testflight_read" {
  role       = aws_iam_role.dolphin_maps_testflight.name
  policy_arn = aws_iam_policy.app_store_connect_read.arn
}

output "dolphin_maps_testflight" {
  description = "Repository variables for dolphin-labs-hq/dolphin-maps's iOS workflow (ios.yml)"
  value = {
    AWS_TESTFLIGHT_REGION   = var.aws_region
    AWS_TESTFLIGHT_ROLE_ARN = aws_iam_role.dolphin_maps_testflight.arn
  }
}

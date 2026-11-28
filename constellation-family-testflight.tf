# Family's TestFlight uploads from CI (ejc3/aws#968): the role .github/workflows/testflight-family.yml in
# ejc3/constellation assumes to read the App Store Connect key (games-worth-watching/app-store-connect,
# dev-ai-services.tf) and nothing else. The job runs on the organization's Mac runners (label ios-pool), builds
# Family from the exact main commit, signs it with Apple's cloud signing and uploads it for external testers.
#
# TRUST: that workflow, on main, in the `testflight-family` GitHub environment, and nothing else.
#   - sub is GitHub's immutable form with the environment (repo and owner ids, so a renamed or recreated repo
#     cannot assume it), and ref pins main.
#   - job_workflow_ref narrows main to the one workflow file: another workflow on main gets nothing.
#   - The environment itself is limited to main in the repository's settings (a deployment branch policy);
#     this trust does not rely on that alone.
resource "aws_iam_role" "constellation_family_testflight" {
  name                 = "constellation-family-testflight"
  description          = "Assumed by ejc3/constellation testflight-family.yml (main, environment testflight-family) via GitHub OIDC: read the App Store Connect key"
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
          "token.actions.githubusercontent.com:sub"              = "repo:ejc3@1694850/constellation@1427792887:environment:testflight-family"
          "token.actions.githubusercontent.com:ref"              = "refs/heads/main"
          "token.actions.githubusercontent.com:job_workflow_ref" = "ejc3/constellation/.github/workflows/testflight-family.yml@refs/heads/main"
        }
      }
    }]
  })
  tags = { Name = "constellation-family-testflight", Project = "constellation" }
}

resource "aws_iam_role_policy_attachment" "constellation_family_testflight_read" {
  role       = aws_iam_role.constellation_family_testflight.name
  policy_arn = aws_iam_policy.app_store_connect_read.arn
}

output "constellation_family_testflight" {
  description = "Variable for the testflight-family environment of ejc3/constellation (no credentials)"
  value = {
    AWS_TESTFLIGHT_ROLE_ARN = aws_iam_role.constellation_family_testflight.arn
  }
}

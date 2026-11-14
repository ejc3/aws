# The private kids' iPhone repo uses the same Apple cloud signing key as the sports
# app, but gets its own role. Match both immutable repository IDs and the exact
# upload workflow on main. The upload job does not use a GitHub environment.
resource "aws_iam_role" "kid_sports_highlights_testflight" {
  name                 = "kid-sports-highlights-testflight"
  description          = "Read the App Store Connect key from kid-sports-highlights testflight.yml on main"
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
          "token.actions.githubusercontent.com:sub"              = "repo:dolphin-labs-hq@316189183/kid-sports-highlights@1450475895:ref:refs/heads/main"
          "token.actions.githubusercontent.com:job_workflow_ref" = "dolphin-labs-hq/kid-sports-highlights/.github/workflows/testflight.yml@refs/heads/main"
        }
      }
    }]
  })
  tags = { Name = "kid-sports-highlights-testflight", Project = "kid-sports-highlights" }
}

resource "aws_iam_role_policy_attachment" "kid_sports_highlights_testflight_read" {
  role       = aws_iam_role.kid_sports_highlights_testflight.name
  policy_arn = aws_iam_policy.app_store_connect_read.arn
}

output "kid_sports_highlights_testflight" {
  description = "Repository variables for dolphin-labs-hq/kid-sports-highlights TestFlight uploads"
  value = {
    AWS_TESTFLIGHT_ROLE_ARN     = aws_iam_role.kid_sports_highlights_testflight.arn
    AWS_TESTFLIGHT_REGION       = var.aws_region
    APP_STORE_CONNECT_SECRET_ID = aws_secretsmanager_secret.app_store_connect.name
  }
}

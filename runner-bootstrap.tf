# Additive producer stage. Grant before changing the controller or removing the
# old runner PAT grant; publishing a new user-data document is a later stage.
resource "aws_iam_role_policy" "runner_bootstrap" {
  count = var.enable_github_runner ? 1 : 0
  name  = "consume-instance-bound-bootstrap"
  role  = aws_iam_role.runner[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ConsumeOwnBootstrapCredential"
      Effect   = "Allow"
      Action   = ["ssm:GetParameter", "ssm:DeleteParameter"]
      Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
      Condition = {
        StringEquals = { "ssm:resourceTag/InstanceArn" = "$${ec2:SourceInstanceARN}" }
        Null         = { "ec2:SourceInstanceARN" = "false" }
      }
      }, {
      # An accidentally reattached account-wide SSM Allow must not turn another
      # runner's still-booting credential into a readable resource.
      Sid      = "DenyOtherBootstrapCredentials"
      Effect   = "Deny"
      Action   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParameterHistory", "ssm:GetParametersByPath", "ssm:DeleteParameter"]
      Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
      Condition = {
        StringNotEqualsIfExists = { "ssm:resourceTag/InstanceArn" = "$${ec2:SourceInstanceARN}" }
      }
      }, {
      Sid      = "ClaimOwnRunnerRegistration"
      Effect   = "Allow"
      Action   = ["dynamodb:GetItem", "dynamodb:PutItem"]
      Resource = aws_dynamodb_table.runner_registration[0].arn
      Condition = {
        "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["$${ec2:SourceInstanceARN}"] }
        Null                        = { "ec2:SourceInstanceARN" = "false" }
      }
    }]
  })
}

# Controller-first additive grant. The old runner PAT/user-data grant and broad
# EC2 launch permissions remain until live acceptance; PassRole is narrowed now.
resource "aws_iam_role_policy" "runner_bootstrap_controller" {
  count = var.enable_github_runner ? 1 : 0
  name  = "broker-instance-bound-bootstrap"
  role  = aws_iam_role.runner_lambda[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CreateInstanceBoundBootstrapCredential"
        Effect   = "Allow"
        Action   = ["ssm:PutParameter", "ssm:AddTagsToResource"]
        Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
        Condition = {
          StringEquals = { "aws:RequestTag/Role" = "github-runner" }
          # One pattern per runner region (a list is OR'd): the controller tags the credential with the ARN of the instance it
          # just launched, which names that instance's region. The SSM parameter itself stays in us-west-1.
          StringLike = { "aws:RequestTag/InstanceArn" = [for r in local.runner_regions : "arn:aws:ec2:${r}:${data.aws_caller_identity.current.account_id}:instance/i-*"] }
        }
      },
      {
        Sid      = "ReadBootstrapProvenanceOnly"
        Effect   = "Allow"
        Action   = "ssm:ListTagsForResource"
        Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
      },
      {
        Sid      = "DeleteControllerBootstrapCredentialOnly"
        Effect   = "Allow"
        Action   = "ssm:DeleteParameter"
        Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
        Condition = {
          StringEquals = { "aws:ResourceTag/Role" = "github-runner" }
        }
      },
      {
        # DescribeParameters cannot be resource-scoped and returns no values.
        Sid      = "ListParameterMetadata"
        Effect   = "Allow"
        Action   = "ssm:DescribeParameters"
        Resource = "*"
        Condition = {
          StringEquals = { "aws:RequestedRegion" = "us-west-1" }
        }
      },
    ]
  })
}

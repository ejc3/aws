# GitHub Actions OIDC for Terraform drift detection

# OIDC Provider for GitHub Actions
resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = ["6938fd4d98bab03faadb97b34396831e3780aea1"]
}

# IAM Role for GitHub Actions
resource "aws_iam_role" "github_actions_terraform" {
  name = "github-actions-terraform"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = {
        Federated = aws_iam_openid_connect_provider.github.arn
      }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        }
        StringLike = {
          "token.actions.githubusercontent.com:sub" = "repo:EJ-Campbell/aws-setup:*"
        }
      }
    }]
  })
}

# Read-only policy for drift detection
resource "aws_iam_role_policy" "github_actions_terraform" {
  name = "terraform-read-only"
  role = aws_iam_role.github_actions_terraform.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:Describe*",
          "iam:Get*",
          "iam:List*",
          "s3:Get*",
          "s3:List*",
          "cloudwatch:Describe*",
          "cloudwatch:Get*",
          "cloudwatch:List*",
          "logs:Describe*",
          "logs:Get*",
          "rds:Describe*",
          "lambda:Get*",
          "lambda:List*",
          "apigateway:GET",
          "budgets:View*",
          "budgets:Describe*",
          "ses:Get*",
          "ses:List*",
          "ssm:GetParameter*",
          "ssm:DescribeParameters",
          "backup:Describe*",
          "backup:Get*",
          "backup:List*",
          "sms-voice:Describe*"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "arn:aws:s3:::aws-infrastructure-*-tf-state/*"
      }
    ]
  })
}

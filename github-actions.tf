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
          # Allow both aws and firepod repos
          "token.actions.githubusercontent.com:sub" = ["repo:ejc3/aws:*", "repo:ejc3/firepod:*"]
        }
      }
    }]
  })
}

# Policy for drift detection and AMI builds
resource "aws_iam_role_policy" "github_actions_terraform" {
  name = "github-actions-policy"
  role = aws_iam_role.github_actions_terraform.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadOnly"
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
        Sid    = "TerraformState"
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:ListBucket"]
        Resource = [
          "arn:aws:s3:::aws-infrastructure-*-tf-state",
          "arn:aws:s3:::aws-infrastructure-*-tf-state/*"
        ]
      },
      {
        Sid    = "TerraformLock"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:DeleteItem"
        ]
        Resource = "arn:aws:dynamodb:us-west-1:928413605543:table/ejc3-terraform-locks"
      },
      {
        Sid    = "AMIBuilder"
        Effect = "Allow"
        Action = [
          "ec2:RunInstances",
          "ec2:StopInstances",
          "ec2:TerminateInstances",
          "ec2:CreateImage",
          "ec2:CreateTags",
          "ec2:RegisterImage",
          "ec2:DeregisterImage"
        ]
        Resource = "*"
      },
      {
        Sid      = "PassRole"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.jumpbox_admin[0].arn
      }
    ]
  })
}

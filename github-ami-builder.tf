# Additive producer stage: deploy these resources and ssm-managed-instance.tf,
# switch fcvm's build consumer, then retire the old shared CI identity. Keeping
# this stage separate permits fresh FULL plans, never a broad -target bypass.
# The builder host only reports its progress and accepts SSM maintenance. It has
# no reusable PAT, Terraform state, Cloudflare credentials, or administrator role.
resource "aws_iam_role" "ami_builder" {
  count = var.enable_github_runner ? 1 : 0
  name  = "ami-builder-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_instance_profile" "ami_builder" {
  count = var.enable_github_runner ? 1 : 0
  name  = "ami-builder-profile"
  role  = aws_iam_role.ami_builder[0].name
}

resource "aws_iam_role_policy_attachment" "ami_builder_ssm" {
  count      = var.enable_github_runner ? 1 : 0
  role       = aws_iam_role.ami_builder[0].name
  policy_arn = aws_iam_policy.ssm_managed_instance.arn
}

resource "aws_iam_role_policy" "ami_builder_status" {
  count = var.enable_github_runner ? 1 : 0
  name  = "report-builder-progress"
  role  = aws_iam_role.ami_builder[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReportBuildStatus"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals                = { "ec2:ResourceTag/Name" = "ami-builder-temp" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["BuildStatus", "KernelVersion"] }
        }
      },
      {
        Sid    = "NeverDelegateOrReadReusableCredentials"
        Effect = "Deny"
        Action = [
          "iam:*", "sts:AssumeRole*", "sts:GetFederationToken", "sts:GetSessionToken",
          "secretsmanager:GetSecretValue", "ssm:GetParameter*", "s3:GetObject*",
        ]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role" "github_actions_ami_builder" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-actions-ami-builder"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        # The GitHub environment permits main only and requires owner approval.
        # Main-ref trust alone is insufficient: publishing an image affects all
        # jobs whose controller selects the newest github-runner AMI.
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:ejc3/fcvm:environment:runner-ami-publish"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy" "github_actions_ami_builder" {
  count = var.enable_github_runner ? 1 : 0
  name  = "build-runner-ami"
  role  = aws_iam_role.github_actions_ami_builder[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadBuildStatus"
        Effect   = "Allow"
        Action   = ["ec2:DescribeImages", "ec2:DescribeInstances", "ec2:DescribeTags", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups"]
        Resource = "*"
      },
      {
        # Canonical's Ubuntu AMIs report the "amazon" owner alias, which
        # ec2:Owner also gives to other verified publishers. Match the source
        # AMI's account instead, as scripts/build-ami.sh does when selecting it.
        Sid       = "LaunchUbuntuImage"
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = "arn:aws:ec2:us-west-1::image/*"
        Condition = { StringEquals = { "aws:ResourceAccount" = "099720109477" } }
      },
      {
        Sid    = "UseIsolatedRunnerNetwork"
        Effect = "Allow"
        Action = "ec2:RunInstances"
        Resource = [
          aws_subnet.runner[0].arn,
          aws_security_group.runner[0].arn,
        ]
      },
      {
        Sid      = "LaunchBuilderInstance"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = {
            "aws:RequestTag/Name"    = "ami-builder-temp"
            "ec2:InstanceType"       = "c7gd.8xlarge"
            "ec2:MetadataHttpTokens" = "required"
          }
          NumericEquals = { "ec2:MetadataHttpPutResponseHopLimit" = "1" }
        }
      },
      {
        Sid      = "LaunchEncryptedBuilderVolume"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:volume/*"
        Condition = {
          StringEquals          = { "aws:RequestTag/Name" = "ami-builder-temp", "ec2:VolumeType" = "gp3" }
          Bool                  = { "ec2:Encrypted" = "true" }
          NumericLessThanEquals = { "ec2:VolumeSize" = "40" }
        }
      },
      {
        Sid       = "LaunchTaggedBuilderInterface"
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:network-interface/*"
        Condition = { StringEquals = { "aws:RequestTag/Name" = "ami-builder-temp" } }
      },
      {
        Sid    = "TagAtLaunch"
        Effect = "Allow"
        Action = "ec2:CreateTags"
        Resource = [
          "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*",
          "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:volume/*",
          "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:network-interface/*",
        ]
        Condition = {
          StringEquals                = { "ec2:CreateAction" = "RunInstances" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Name", "BuildStatus"] }
        }
      },
      {
        Sid       = "ManageBuilderOnly"
        Effect    = "Allow"
        Action    = ["ec2:CreateImage", "ec2:StopInstances", "ec2:TerminateInstances"]
        Resource  = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Name" = "ami-builder-temp" } }
      },
      {
        Sid       = "CreateTaggedRunnerImage"
        Effect    = "Allow"
        Action    = "ec2:CreateImage"
        Resource  = ["arn:aws:ec2:us-west-1::image/*", "arn:aws:ec2:us-west-1::snapshot/*"]
        Condition = { StringEquals = { "aws:RequestTag/Purpose" = "github-runner" } }
      },
      {
        Sid       = "TagCreatedImage"
        Effect    = "Allow"
        Action    = "ec2:CreateTags"
        Resource  = ["arn:aws:ec2:us-west-1::image/*", "arn:aws:ec2:us-west-1::snapshot/*"]
        Condition = { StringEquals = { "ec2:CreateAction" = "CreateImage" } }
      },
      {
        Sid       = "PassOnlyBuilderRole"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.ami_builder[0].arn
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
      {
        Sid         = "NeverPassAnotherRole"
        Effect      = "Deny"
        Action      = "iam:PassRole"
        NotResource = aws_iam_role.ami_builder[0].arn
      },
      {
        Sid    = "NeverChangeIdentityOrReadReusableCredentials"
        Effect = "Deny"
        Action = [
          "iam:Create*", "iam:Update*", "iam:Put*", "iam:Attach*", "iam:Delete*",
          "iam:Detach*", "iam:Set*", "iam:Add*", "iam:Remove*", "iam:Tag*",
          "iam:Untag*", "iam:Upload*", "iam:Enable*", "iam:Disable*", "iam:Change*",
          "sts:AssumeRole*", "sts:GetFederationToken", "sts:GetSessionToken",
          "secretsmanager:GetSecretValue", "ssm:GetParameter*", "ssm:GetCommandInvocation", "s3:GetObject*",
        ]
        Resource = "*"
      },
    ]
  })
}

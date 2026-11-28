# The Family app stack in ejc3/constellation owns ECR and the ECS task/service.
# GitHub can publish images in family/api. The broker, invoked by CI, owns the ECS update permission.
locals {
  family_api_registry   = "${module.family_account.account_id}.dkr.ecr.${var.aws_region}.amazonaws.com"
  family_api_repository = "arn:aws:ecr:${var.aws_region}:${module.family_account.account_id}:repository/family/api"
  family_api_service    = "arn:aws:ecs:${var.aws_region}:${module.family_account.account_id}:service/family/family"
}

resource "aws_iam_openid_connect_provider" "family_github" {
  provider       = aws.family
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "family_stage_api_ci" {
  provider = aws.family
  name     = "family-stage-api-ci"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.family_github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:ejc3@1694850/constellation@1427792887:environment:family-stage"
        "token.actions.githubusercontent.com:ref" = "refs/heads/main"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "family_stage_api_ci" {
  provider = aws.family
  role     = aws_iam_role.family_stage_api_ci.id
  name     = "publish-family-stage-api"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage",
        ]
        Resource = local.family_api_repository
      },
      { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.family_stage_api_release.arn },
    ]
  })
}

resource "aws_dynamodb_table" "family_stage_api_receipts" {
  provider     = aws.family
  name         = "family-stage-api-receipt"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "stage"
  attribute {
    name = "stage"
    type = "S"
  }
}

resource "aws_cloudwatch_log_group" "family_stage_api_release" {
  provider          = aws.family
  name              = "/aws/lambda/family-stage-api-release"
  retention_in_days = 30
}

resource "aws_iam_role" "family_stage_api_release" {
  provider = aws.family
  name     = "family-stage-api-release"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "sts:AssumeRole", Principal = { Service = "lambda.amazonaws.com" } }]
  })
}

resource "aws_iam_role_policy" "family_stage_api_release" {
  provider = aws.family
  name     = "roll-only-family-stage-api"
  role     = aws_iam_role.family_stage_api_release.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.family_stage_api_release.arn}:*" },
      { Effect = "Allow", Action = ["ecr:BatchGetImage", "ecr:PutImage"], Resource = local.family_api_repository },
      { Effect = "Allow", Action = ["ecs:DescribeServices", "ecs:UpdateService"], Resource = local.family_api_service },
      # DescribeTaskDefinition has no resource-level authorization. Only the broker
      # calls it to verify the current service actually uses the staging tag.
      {
        Effect    = "Allow", Action = "ecs:DescribeTaskDefinition", Resource = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Effect    = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem"], Resource = aws_dynamodb_table.family_stage_api_receipts.arn
        Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = ["family"] } }
      },
    ]
  })
}

data "archive_file" "family_stage_api_release" {
  type        = "zip"
  output_path = "${path.module}/.terraform/family-stage-api-release.zip"
  source {
    filename = "release.py"
    content  = file("${path.module}/family-stage-api/release.py")
  }
}

resource "aws_lambda_function" "family_stage_api_release" {
  provider                       = aws.family
  function_name                  = "family-stage-api-release"
  role                           = aws_iam_role.family_stage_api_release.arn
  runtime                        = "python3.13"
  handler                        = "release.handler"
  architectures                  = ["arm64"]
  filename                       = data.archive_file.family_stage_api_release.output_path
  source_code_hash               = data.archive_file.family_stage_api_release.output_base64sha256
  timeout                        = 50
  reserved_concurrent_executions = 1
  environment {
    variables = {
      RECEIPT_TABLE  = aws_dynamodb_table.family_stage_api_receipts.name
      REPOSITORY_URI = "${local.family_api_registry}/family/api"
    }
  }
  depends_on = [aws_iam_role_policy.family_stage_api_release]
}

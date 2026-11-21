# fcvm-fleet-lambda.tf
#
# GPU Lambda for the fleet wall (owner, 2026-11-21: "AWS just offered GPU lambdas. Let's add that into our mix for one of
# the demos"). AWS Lambda runs event functions on GPU instances through Lambda Managed Instances (G4dn, G5, G6, G6e;
# launched 2026-11-18). The candidate demo is a third lane in the wall's race: a tile rendered by a GPU Lambda invocation
# beside the fcvm and EC2 halves (ejc3/fcvm docs/fleet/ARCHITECTURE.md).
#
# This file holds what fcvm-arm needs to build that from the fcvm project Terraform root (fleet/ops/terraform, run by
# scripts/fcvm-fleet-ops) without IAM rights of its own: the image repository, the two roles the functions and capacity
# providers run as, and a grant on fcvm-fleet-terraform scoped to fcvm-lambda-* capacity providers and functions in
# us-east-1. It creates nothing that runs or bills: instances exist only while fcvm-arm's own capacity providers do.

locals {
  fcvm_lambda_region = "us-east-1"
  fcvm_lambda_prefix = "fcvm-lambda"
}

resource "aws_ecr_repository" "fcvm_fleet_lambda_tile" {
  provider             = aws.fcvm_fleet_east1
  name                 = "fcvm-fleet/lambda-tile"
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  tags = { Name = "fcvm-fleet/lambda-tile", Project = "fcvm-fleet" }
}

resource "aws_ecr_lifecycle_policy" "fcvm_fleet_lambda_tile" {
  provider   = aws.fcvm_fleet_east1
  repository = aws_ecr_repository.fcvm_fleet_lambda_tile.name
  policy = jsonencode({ rules = [{
    rulePriority = 1, description = "Keep the newest 20 images"
    selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
    action       = { type = "expire" }
  }] })
}

# The function's execution role: logs only. It reads no secret and no fleet state.
resource "aws_iam_role" "fcvm_fleet_lambda_tile" {
  provider = aws.fcvm_fleet
  name     = "${local.fcvm_lambda_prefix}-tile"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${local.fcvm_lambda_prefix}-tile", Project = "fcvm-fleet" }
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_lambda_tile_logs" {
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_lambda_tile.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# The capacity provider's operator role: Lambda manages the GPU instances with it (AWS's documented managed policy).
resource "aws_iam_role" "fcvm_fleet_lambda_operator" {
  provider = aws.fcvm_fleet
  name     = "${local.fcvm_lambda_prefix}-operator"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "${local.fcvm_lambda_prefix}-operator", Project = "fcvm-fleet" }
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_lambda_operator" {
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_lambda_operator.name
  policy_arn = "arn:aws:iam::aws:policy/AWSLambdaManagedEC2ResourceOperator"
}

resource "aws_iam_role_policy" "fcvm_fleet_terraform_lambda" {
  provider = aws.fcvm_fleet
  name     = "gpu-lambda-functions-and-capacity-providers"
  role     = aws_iam_role.fcvm_fleet_terraform.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Lambda Managed Instances is new and its IAM action list is not yet documented, so every Lambda action is
        # allowed, but only on fcvm-lambda-* functions and capacity providers in us-east-1.
        Sid    = "OnlyFcvmLambdaFunctionsAndCapacityProviders"
        Effect = "Allow"
        Action = "lambda:*"
        Resource = [
          "arn:aws:lambda:${local.fcvm_lambda_region}:${local.fcvm_fleet_account_id}:function:${local.fcvm_lambda_prefix}-*",
          "arn:aws:lambda:${local.fcvm_lambda_region}:${local.fcvm_fleet_account_id}:capacity-provider:${local.fcvm_lambda_prefix}-*",
        ]
      },
      {
        Sid       = "ListLambda"
        Effect    = "Allow"
        Action    = ["lambda:List*", "lambda:GetAccountSettings"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.fcvm_lambda_region } }
      },
      {
        Sid       = "PassOnlyTheTwoLambdaRoles"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = [aws_iam_role.fcvm_fleet_lambda_tile.arn, aws_iam_role.fcvm_fleet_lambda_operator.arn]
        Condition = { StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" } }
      },
      {
        # Lambda creates its service-linked role on the first capacity provider in the account.
        Sid       = "LambdasServiceLinkedRole"
        Effect    = "Allow"
        Action    = "iam:CreateServiceLinkedRole"
        Resource  = "*"
        Condition = { StringEquals = { "iam:AWSServiceName" = "lambda.amazonaws.com" } }
      },
      {
        Sid      = "PushTheLambdaTileImage"
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"]
        Resource = aws_ecr_repository.fcvm_fleet_lambda_tile.arn
      },
      { Sid = "EcrLogin", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Sid      = "TheFunctionsLogs"
        Effect   = "Allow"
        Action   = ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams"]
        Resource = "arn:aws:logs:${local.fcvm_lambda_region}:${local.fcvm_fleet_account_id}:log-group:/aws/lambda/${local.fcvm_lambda_prefix}-*:*"
      },
    ]
  })
}

output "fcvm_fleet_lambda" {
  description = "GPU Lambda inputs for the fcvm project root (no credentials)"
  value = {
    region          = local.fcvm_lambda_region
    repository_url  = aws_ecr_repository.fcvm_fleet_lambda_tile.repository_url
    execution_role  = aws_iam_role.fcvm_fleet_lambda_tile.arn
    operator_role   = aws_iam_role.fcvm_fleet_lambda_operator.arn
    subnet_ids      = module.fcvm_fleet_east1.subnet_ids
    security_groups = [module.fcvm_fleet_east1.security_group_id]
  }
}

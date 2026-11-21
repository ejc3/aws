# fcvm-fleet-lambda.tf
#
# Access for fcvm-arm to build GPU Lambda for the fleet demos itself (owner, 2026-11-21: "small pr to get access then you
# drive the apply"). fcvm-arm creates the ECR repository, roles, capacity providers and functions from the fcvm project
# Terraform root (fleet/ops/terraform, scripts/fcvm-fleet-ops); this file only grants that, scoped by name:
#
# - roles named fcvm-lambda-*, created only with the fcvm-lambda-boundary permissions boundary, which can never be
#   removed or replaced: whatever policy fcvm-arm attaches, such a role cannot read a secret, decrypt, write IAM, assume
#   another role, read SSM parameters or touch S3, so a function cannot reach the fleet's credentials or state;
# - passing those roles to Lambda; Lambda's service-linked role;
# - ECR repositories named fcvm-fleet/lambda-*, every Lambda action on fcvm-lambda-* functions and capacity providers,
#   and their logs, in us-east-1 (the fleet's GPU region).

locals {
  fcvm_lambda_region   = "us-east-1"
  fcvm_lambda_prefix   = "fcvm-lambda"
  fcvm_lambda_roles    = "arn:aws:iam::${local.fcvm_fleet_account_id}:role/${local.fcvm_lambda_prefix}-*"
  fcvm_lambda_boundary = "arn:aws:iam::${local.fcvm_fleet_account_id}:policy/${local.fcvm_lambda_prefix}-boundary"
}

resource "aws_iam_policy" "fcvm_lambda_boundary" {
  provider    = aws.fcvm_fleet
  name        = "${local.fcvm_lambda_prefix}-boundary"
  description = "The ceiling of every fcvm-lambda-* role: no secret, decrypt, IAM write, role assumption, SSM parameter or S3 (fcvm-fleet-lambda.tf)"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "WhatTheRolesPoliciesGrant", Effect = "Allow", Action = "*", Resource = "*" },
      {
        Sid    = "NeverTheFleetsCredentialsOrState"
        Effect = "Deny"
        Action = ["secretsmanager:*", "kms:Decrypt", "sts:AssumeRole*", "ssm:GetParameter*", "s3:*",
        "iam:Create*", "iam:Put*", "iam:Attach*", "iam:Update*", "iam:Delete*", "iam:Detach*", "iam:AddRoleToInstanceProfile"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "fcvm_fleet_terraform_lambda" {
  provider = aws.fcvm_fleet
  name     = "gpu-lambda-functions-and-capacity-providers"
  role     = aws_iam_role.fcvm_fleet_terraform.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "CreateLambdaRolesOnlyWithTheBoundary"
        Effect    = "Allow"
        Action    = ["iam:CreateRole", "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy"]
        Resource  = local.fcvm_lambda_roles
        Condition = { StringEquals = { "iam:PermissionsBoundary" = local.fcvm_lambda_boundary } }
      },
      {
        Sid    = "ManageLambdaRoles"
        Effect = "Allow"
        Action = ["iam:DeleteRole", "iam:GetRole", "iam:GetRolePolicy", "iam:ListRolePolicies", "iam:ListAttachedRolePolicies",
        "iam:ListInstanceProfilesForRole", "iam:TagRole", "iam:UntagRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy"]
        Resource = local.fcvm_lambda_roles
      },
      {
        Sid      = "TheBoundaryStays"
        Effect   = "Deny"
        Action   = ["iam:DeleteRolePermissionsBoundary", "iam:PutRolePermissionsBoundary"]
        Resource = "*"
      },
      {
        Sid       = "PassLambdaRolesToLambdaOnly"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = local.fcvm_lambda_roles
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
      { Sid = "ReadTheBoundaryAndManagedPolicies", Effect = "Allow", Action = ["iam:GetPolicy", "iam:GetPolicyVersion"], Resource = "*" },
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
        Sid      = "LambdaTileRepositories"
        Effect   = "Allow"
        Action   = "ecr:*"
        Resource = "arn:aws:ecr:${local.fcvm_lambda_region}:${local.fcvm_fleet_account_id}:repository/fcvm-fleet/lambda-*"
      },
      { Sid = "EcrLoginAndListing", Effect = "Allow", Action = ["ecr:GetAuthorizationToken", "ecr:DescribeRepositories"], Resource = "*" },
      {
        Sid      = "TheFunctionsLogs"
        Effect   = "Allow"
        Action   = ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams", "logs:DescribeLogGroups"]
        Resource = "arn:aws:logs:${local.fcvm_lambda_region}:${local.fcvm_fleet_account_id}:log-group:/aws/lambda/${local.fcvm_lambda_prefix}-*:*"
      },
    ]
  })
}

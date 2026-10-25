# CI publishes code and invokes one image-only staging release function. It cannot
# read Terraform state, read fleet credentials, pass a role, or call ECS directly.
# The function takes Terraform's existing backend lock before changing the release.
locals {
  fcvm_stage_environment = "fleet-stage-deploy"
  fcvm_stage_lock_id     = "ejc3-terraform-state/aws-infrastructure/terraform.tfstate"
  fcvm_stage_family_arn  = "${aws_ecs_task_definition.fcvm_fleet_coordinator.arn_without_revision}:*"
}

data "aws_ecr_image" "fcvm_coordinator_bootstrap" {
  provider        = aws.fcvm_fleet
  repository_name = aws_ecr_repository.fcvm_fleet_coordinator.name
  image_tag       = local.fcvm_fleet_coordinator_tag
}

# A software release pointer, not Terraform state or a credential. The release
# function owns its value; Terraform still checks every other task/service field.
resource "aws_ssm_parameter" "fcvm_coordinator_release" {
  provider = aws.fcvm_fleet
  name     = "/fcvm-fleet/stage/coordinator-image"
  type     = "String"
  value    = "${aws_ecr_repository.fcvm_fleet_coordinator.repository_url}@${data.aws_ecr_image.fcvm_coordinator_bootstrap.image_digest}"
  lifecycle {
    ignore_changes = [value]
  }
}

data "aws_ssm_parameter" "fcvm_coordinator_release" {
  provider        = aws.fcvm_fleet
  name            = aws_ssm_parameter.fcvm_coordinator_release.name
  with_decryption = false
}

# Saved plans freeze data-source reads. Re-open this parameter at apply time,
# under the backend lock, so a CI release between plan and apply invalidates it.
ephemeral "aws_ssm_parameter" "fcvm_coordinator_release_guard" {
  provider        = aws.fcvm_fleet
  arn             = aws_ssm_parameter.fcvm_coordinator_release.arn
  with_decryption = false
  lifecycle {
    postcondition {
      condition     = self.version == data.aws_ssm_parameter.fcvm_coordinator_release.version
      error_message = "The staging coordinator release changed after this plan; create a fresh plan before applying."
    }
  }
}

resource "github_repository_environment" "fcvm_stage" {
  provider          = github.agents_md
  repository        = "fcvm"
  environment       = local.fcvm_stage_environment
  can_admins_bypass = false
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "fcvm_stage_main" {
  provider       = github.agents_md
  repository     = "fcvm"
  environment    = github_repository_environment.fcvm_stage.environment
  branch_pattern = "main"
}

resource "aws_iam_role" "fcvm_stage_ci" {
  name = "fcvm-stage-ci"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:ejc3/fcvm:environment:${local.fcvm_stage_environment}"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "fcvm_stage_ci" {
  name = "publish-and-release-staging-coordinator"
  role = aws_iam_role.fcvm_stage_ci.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Effect = "Allow"
        Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
        "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:DescribeImages"]
        Resource = aws_ecr_repository.fcvm_fleet_coordinator.arn
      },
      { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.fcvm_coordinator_release.arn },
      {
        Sid    = "NeverAdministerOrReadCredentials"
        Effect = "Deny"
        Action = ["iam:*", "sts:AssumeRole*", "s3:GetObject*", "secretsmanager:GetSecretValue",
        "secretsmanager:BatchGetSecretValue", "ssm:GetParameter*", "ssm:SendCommand", "ecs:*", "ec2:*"]
        Resource = "*"
      },
    ]
  })
}

resource "aws_ecr_repository_policy" "fcvm_coordinator_ci" {
  provider   = aws.fcvm_fleet
  repository = aws_ecr_repository.fcvm_fleet_coordinator.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.fcvm_stage_ci.arn }
      Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:DescribeImages"]
    }]
  })
}

resource "aws_iam_role" "fcvm_coordinator_release" {
  name = "fcvm-coordinator-release"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_cloudwatch_log_group" "fcvm_coordinator_release" {
  name              = "/aws/lambda/fcvm-coordinator-release"
  retention_in_days = 30
}

resource "aws_iam_role_policy" "fcvm_coordinator_release" {
  name = "serialize-and-release-one-coordinator"
  role = aws_iam_role.fcvm_coordinator_release.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.fcvm_coordinator_release.arn}:*"
      },
      { Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.fcvm_coordinator_release_fleet.arn },
      {
        Effect    = "Allow"
        Action    = ["dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource  = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/ejc3-terraform-locks"
        Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [local.fcvm_stage_lock_id] } }
      },
    ]
  })
}

resource "aws_iam_role" "fcvm_coordinator_release_fleet" {
  provider = aws.fcvm_fleet
  name     = "fcvm-coordinator-release"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole"
      Principal = { AWS = aws_iam_role.fcvm_coordinator_release.arn }
    }]
  })
}

resource "aws_iam_role_policy" "fcvm_coordinator_release_fleet" {
  provider = aws.fcvm_fleet
  name     = "release-one-coordinator-image"
  role     = aws_iam_role.fcvm_coordinator_release_fleet.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecs:DescribeServices", Resource = aws_ecs_service.fcvm_fleet_coordinator.id },
      {
        Effect = "Allow", Action = "ecs:UpdateService", Resource = aws_ecs_service.fcvm_fleet_coordinator.id
        Condition = {
          ArnLike              = { "ecs:task-definition" = local.fcvm_stage_family_arn }
          StringEqualsIfExists = { "ecs:enable-execute-command" = "false" }
        }
      },
      # These two APIs have no resource-level authorization. Only the release
      # function can assume this role; its code checks the service's exact family.
      {
        Effect   = "Allow"
        Action   = ["ecs:DescribeTaskDefinition", "ecs:DeregisterTaskDefinition"]
        Resource = "*"
      },
      { Effect = "Allow", Action = "ecs:ListTagsForResource", Resource = local.fcvm_stage_family_arn },
      {
        Effect = "Allow", Action = "ecs:RegisterTaskDefinition", Resource = local.fcvm_stage_family_arn
        Condition = {
          StringEquals  = { "aws:RequestTag/Project" = "fcvm-fleet" }
          NumericEquals = { "ecs:task-cpu" = 512, "ecs:task-memory" = 1024 }
        }
      },
      {
        Effect    = "Allow", Action = "ecs:TagResource", Resource = local.fcvm_stage_family_arn
        Condition = { StringEquals = { "ecs:CreateAction" = "RegisterTaskDefinition" } }
      },
      { Effect = "Allow", Action = "ecr:DescribeImages", Resource = aws_ecr_repository.fcvm_fleet_coordinator.arn },
      {
        Effect   = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = aws_ssm_parameter.fcvm_coordinator_release.arn
      },
      {
        Effect    = "Allow", Action = "iam:PassRole"
        Resource  = [aws_iam_role.fcvm_fleet_coordinator_execution.arn, aws_iam_role.fcvm_fleet_coordinator_task.arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
    ]
  })
}

data "archive_file" "fcvm_coordinator_release" {
  type        = "zip"
  source_dir  = "${path.module}/fcvm-coordinator-release"
  output_path = "${path.module}/.terraform/fcvm-coordinator-release.zip"
}

resource "aws_lambda_function" "fcvm_coordinator_release" {
  function_name                  = "fcvm-coordinator-release"
  role                           = aws_iam_role.fcvm_coordinator_release.arn
  runtime                        = "python3.12"
  handler                        = "release.lambda_handler"
  timeout                        = 900
  memory_size                    = 256
  reserved_concurrent_executions = 1
  filename                       = data.archive_file.fcvm_coordinator_release.output_path
  source_code_hash               = data.archive_file.fcvm_coordinator_release.output_base64sha256
  environment {
    variables = {
      RELEASE_ROLE    = aws_iam_role.fcvm_coordinator_release_fleet.arn
      REPOSITORY_URL  = aws_ecr_repository.fcvm_fleet_coordinator.repository_url
      IMAGE_PARAMETER = aws_ssm_parameter.fcvm_coordinator_release.name
      LOCK_TABLE      = "ejc3-terraform-locks"
      LOCK_ID         = local.fcvm_stage_lock_id
    }
  }
  depends_on = [aws_iam_role_policy.fcvm_coordinator_release, aws_iam_role_policy.fcvm_coordinator_release_fleet]
}

resource "cloudflare_zero_trust_access_service_token" "fcvm_stage_ci" {
  account_id = var.cloudflare_account_id
  name       = "fcvm-stage-ci"
  duration   = "8760h"
  lifecycle { create_before_destroy = true }
}

resource "cloudflare_zero_trust_access_policy" "fcvm_stage_ci" {
  account_id = var.cloudflare_account_id
  name       = "fcvm staging: main-only CI"
  decision   = "non_identity"
  include    = [{ service_token = { token_id = cloudflare_zero_trust_access_service_token.fcvm_stage_ci.id } }]
}

resource "github_actions_environment_variable" "fcvm_stage" {
  provider = github.agents_md
  for_each = {
    AWS_ROLE_ARN        = aws_iam_role.fcvm_stage_ci.arn
    COORDINATOR_REPO    = aws_ecr_repository.fcvm_fleet_coordinator.repository_url
    COORDINATOR_RELEASE = aws_lambda_function.fcvm_coordinator_release.function_name
    STAGING_API_URL     = "https://${local.fcvm_dev_api_stage_hostname}"
    STAGING_ORIGIN      = "https://${local.fcvm_dev_site_stage_host}"
  }
  repository    = "fcvm"
  environment   = github_repository_environment.fcvm_stage.environment
  variable_name = each.key
  value         = each.value
}

resource "github_actions_environment_secret" "fcvm_stage" {
  provider    = github.agents_md
  for_each    = toset(["CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET"])
  repository  = "fcvm"
  environment = github_repository_environment.fcvm_stage.environment
  secret_name = each.key
  plaintext_value = {
    CF_ACCESS_CLIENT_ID     = cloudflare_zero_trust_access_service_token.fcvm_stage_ci.client_id
    CF_ACCESS_CLIENT_SECRET = cloudflare_zero_trust_access_service_token.fcvm_stage_ci.client_secret
  }[each.key]
}

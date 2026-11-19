# fcvm-fleet-prod-ci.tf
#
# The production fleet's release machinery (ejc3/fcvm fleet production plan, A4), behind
# var.fcvm_fleet_prod_enabled like the stack it releases (fcvm-fleet-prod.tf). Owner, 2026-11-19: promotion is automatic
# after a green staging smoke; the environment has no approval gate. ejc3/fcvm's workflows (fleet-prod-promote.yml,
# fleet-prod-rollback.yml, fleet-prod-smoke.yml) promote the set staging smoked together: the host and tile images,
# the coordinator image and the Worker.
#
# - GitHub environment fleet-prod-deploy on ejc3/fcvm, main only. The repository variable FLEET_PROD_ENABLED tells
#   the workflows the stack exists; without it they skip with a message.
# - fcvm-prod-ci: the OIDC role of that environment only. It may invoke the two prod release functions below and
#   nothing else: no ECR push (prod releases images staging already published), no staging function, no state.
# - fcvm-coordinator-release-prod: release.py with STACK=prod. It changes only the prod service's image, takes
#   Terraform's backend lock as staging's does, and goes ahead only while ActiveViewers{Fleet=prod} (namespace
#   FCVM/Fleet, ejc3/fcvm src/fleet/coord/metrics.rs) has been 0 for 5 minutes; otherwise it returns `held`. `force`
#   skips that, only for an image the prod pointer held before (a rollback). It also writes /fcvm-fleet/prod/release,
#   the record of what prod runs ({coordinator, worker_version, generation, build_id, smoke_run, previous}).
# - fcvm-fleet-ami-prod-release: publish.py's ProdPublisher. promote-prod moves prod's four launch templates and
#   /fcvm-fleet/prod/ami-release to the receipt the image release gate verified, only while staging runs it and
#   only while Power{Fleet=prod} is 0 (asleep) with no prod instance running; rollback-prod returns to the pointer's
#   previous release under the same conditions. Its fleet role can change prod's templates only. Staging's publisher
#   reads the prod pointer so its retention never removes an image prod launches from (fcvm-fleet-ami-pipeline.tf).
# - A Workers deploy token of prod's own (Workers Scripts Write and Account Settings Read, as workers-deploy.tf's).
#   Cloudflare scopes Workers Scripts Write to the account, not to one script, so it could deploy staging's Worker
#   too; it lives only in fleet-prod-deploy, never in a repository secret, and the staging token lives only in
#   cloudflare-deploy.

locals {
  fcvm_prod_environment           = "fleet-prod-deploy"
  fcvm_prod_ami_release_parameter = "/fcvm-fleet/prod/ami-release" # aws_ssm_parameter.fcvm_fleet_prod_ami_release
  fcvm_prod_release_record        = "/fcvm-fleet/prod/release"
  fcvm_prod_ami_state_prefix      = "/fcvm-fleet/prod/ami-pipeline"
  fcvm_prod_family_arn            = [for t in aws_ecs_task_definition.fcvm_fleet_prod_coordinator : "${t.arn_without_revision}:*"]
  fcvm_prod_release_functions = concat(
    [for f in aws_lambda_function.fcvm_coordinator_release_prod : f.arn],
    [for f in aws_lambda_function.fcvm_ami_prod_release : f.arn],
  )
}

# ------------------------------------------------------------------------------------------------ GitHub
resource "github_repository_environment" "fcvm_prod" {
  count             = local.fcvm_fleet_prod
  provider          = github.agents_md
  repository        = "fcvm"
  environment       = local.fcvm_prod_environment
  can_admins_bypass = false
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "fcvm_prod_main" {
  count          = local.fcvm_fleet_prod
  provider       = github.agents_md
  repository     = "fcvm"
  environment    = github_repository_environment.fcvm_prod[0].environment
  branch_pattern = "main"
}

# The workflows run nothing in fleet-prod-deploy until this exists (and the role secret below is set).
resource "github_actions_variable" "fcvm_prod_enabled" {
  count         = local.fcvm_fleet_prod
  provider      = github.agents_md
  repository    = "fcvm"
  variable_name = "FLEET_PROD_ENABLED"
  value         = "true"
}

resource "github_actions_environment_variable" "fcvm_prod" {
  provider = github.agents_md
  for_each = var.fcvm_fleet_prod_enabled ? {
    COORDINATOR_RELEASE = aws_lambda_function.fcvm_coordinator_release_prod[0].function_name
    IMAGE_RELEASE       = aws_lambda_function.fcvm_ami_prod_release[0].function_name
    # The smoke's coordinator, through Access (owner and prod CI only; fcvm-dev-prod.tf).
    PROD_API_URL = "https://${local.fcvm_dev_ops_prod_hostname}"
    # The public /live a visitor's page opens: the unverified-visitor probe.
    PROD_PUBLIC_URL = "https://${local.fcvm_dev_api_prod_hostname}"
    # The one browser origin the prod coordinator accepts (fcvm-fleet-prod.tf).
    PROD_ORIGIN = local.fcvm_fleet_prod_site_origin
    # Whether fcvm.dev serves the Worker yet: the go-live PR (plan A6) sets "true", and the smoke then requires the page.
    PROD_SITE_LIVE = "false"
  } : {}
  repository    = "fcvm"
  environment   = github_repository_environment.fcvm_prod[0].environment
  variable_name = each.key
  value         = each.value
}

# The role ARN carries the account id: a secret, as staging's (fcvm-fleet-ci.tf).
resource "github_actions_environment_secret" "fcvm_prod" {
  provider    = github.agents_md
  for_each    = var.fcvm_fleet_prod_enabled ? toset(["AWS_ROLE_ARN", "CF_ACCESS_CLIENT_ID", "CF_ACCESS_CLIENT_SECRET", "CLOUDFLARE_API_TOKEN"]) : toset([])
  repository  = "fcvm"
  environment = github_repository_environment.fcvm_prod[0].environment
  secret_name = each.key
  plaintext_value = {
    AWS_ROLE_ARN            = aws_iam_role.fcvm_prod_ci[0].arn
    CF_ACCESS_CLIENT_ID     = cloudflare_zero_trust_access_service_token.fcvm_prod_ci[0].client_id
    CF_ACCESS_CLIENT_SECRET = cloudflare_zero_trust_access_service_token.fcvm_prod_ci[0].client_secret
    CLOUDFLARE_API_TOKEN    = cloudflare_account_token.fcvm_prod_workers_deploy[0].value
  }[each.key]
}

# Prod's Worker (fcvm-fleet, ejc3/fcvm fleet/web/wrangler.jsonc env.production): its own token, same permissions as
# workers-deploy.tf's. Rotate with `terraform apply -replace='cloudflare_account_token.fcvm_prod_workers_deploy[0]'`.
resource "cloudflare_account_token" "fcvm_prod_workers_deploy" {
  count      = local.fcvm_fleet_prod
  provider   = cloudflare.token_minter
  account_id = var.cloudflare_account_id
  name       = "fcvm-prod-workers-deploy"
  policies   = local.workers_deploy_policies
}

# ------------------------------------------------------------------------------------------------ the CI role
resource "aws_iam_role" "fcvm_prod_ci" {
  count = local.fcvm_fleet_prod
  name  = "fcvm-prod-ci"
  # The promotion's hold and a rollback wait up to 350 minutes for prod to go quiet, on one session.
  max_session_duration = 21600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = { StringEquals = {
        "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
        "token.actions.githubusercontent.com:sub" = "repo:ejc3/fcvm:environment:${local.fcvm_prod_environment}"
      } }
    }]
  })
}

resource "aws_iam_role_policy" "fcvm_prod_ci" {
  count = local.fcvm_fleet_prod
  name  = "invoke-the-prod-release-functions-only"
  role  = aws_iam_role.fcvm_prod_ci[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = local.fcvm_prod_release_functions },
      # Staging's release, publisher and gate functions included.
      { Sid = "NoOtherFunction", Effect = "Deny", Action = "lambda:*", NotResource = local.fcvm_prod_release_functions },
      {
        Sid    = "NeverPublishAdministerOrReadCredentials"
        Effect = "Deny"
        Action = ["ecr:*", "iam:*", "sts:AssumeRole*", "s3:*", "secretsmanager:*", "ssm:*", "ecs:*", "ec2:*",
        "dynamodb:*", "cloudwatch:*", "logs:*"]
        Resource = "*"
      },
    ]
  })
}

# ------------------------------------------------------------------------------------------------ the release record
# What prod runs: {coordinator: {commit, digest}, worker_version, generation, build_id, smoke_run, previous}. The
# promotion writes it through the coordinator release function, which checks the coordinator is the one prod runs and
# that the record it replaces is the one the promotion read. "{}" until the first promotion.
resource "aws_ssm_parameter" "fcvm_prod_release_record" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = local.fcvm_prod_release_record
  type     = "String"
  value    = "{}"
  tags     = local.fcvm_fleet_prod_tags
  lifecycle {
    ignore_changes = [value]
  }
}

# ------------------------------------------------------------------------------------------------ coordinator release
resource "aws_iam_role" "fcvm_coordinator_release_prod" {
  count = local.fcvm_fleet_prod
  name  = "fcvm-coordinator-release-prod"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_cloudwatch_log_group" "fcvm_coordinator_release_prod" {
  count             = local.fcvm_fleet_prod
  name              = "/aws/lambda/fcvm-coordinator-release-prod"
  retention_in_days = 30
}

resource "aws_iam_role_policy" "fcvm_coordinator_release_prod" {
  count = local.fcvm_fleet_prod
  name  = "serialize-and-release-the-prod-coordinator"
  role  = aws_iam_role.fcvm_coordinator_release_prod[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.fcvm_coordinator_release_prod[0].arn}:*"
      },
      { Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.fcvm_coordinator_release_prod_fleet[0].arn },
      # The prod service is in the same Terraform state as staging's: the same backend lock.
      {
        Effect    = "Allow"
        Action    = ["dynamodb:PutItem", "dynamodb:DeleteItem"]
        Resource  = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/ejc3-terraform-locks"
        Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [local.fcvm_stage_lock_id] } }
      },
    ]
  })
}

resource "aws_iam_role" "fcvm_coordinator_release_prod_fleet" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "fcvm-coordinator-release-prod"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Action = "sts:AssumeRole"
      Principal = { AWS = aws_iam_role.fcvm_coordinator_release_prod[0].arn }
    }]
  })
  tags = local.fcvm_fleet_prod_tags
}

resource "aws_iam_role_policy" "fcvm_coordinator_release_prod_fleet" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "release-the-prod-coordinator-image"
  role     = aws_iam_role.fcvm_coordinator_release_prod_fleet[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "ecs:DescribeServices", Resource = aws_ecs_service.fcvm_fleet_prod_coordinator[0].id },
      {
        Effect = "Allow", Action = "ecs:UpdateService", Resource = aws_ecs_service.fcvm_fleet_prod_coordinator[0].id
        Condition = {
          ArnLike              = { "ecs:task-definition" = local.fcvm_prod_family_arn[0] }
          StringEqualsIfExists = { "ecs:enable-execute-command" = "false" }
        }
      },
      # No resource-level authorization for these two; release.py checks the service's exact family.
      { Effect = "Allow", Action = ["ecs:DescribeTaskDefinition", "ecs:DeregisterTaskDefinition"], Resource = "*" },
      { Effect = "Allow", Action = "ecs:ListTagsForResource", Resource = local.fcvm_prod_family_arn[0] },
      {
        Effect = "Allow", Action = "ecs:RegisterTaskDefinition", Resource = local.fcvm_prod_family_arn[0]
        Condition = {
          StringEquals = { "aws:RequestTag/Project" = "fcvm-fleet", "aws:RequestTag/Fleet" = "prod" }
          NumericEquals = {
            "ecs:task-cpu"    = tonumber(aws_ecs_task_definition.fcvm_fleet_prod_coordinator[0].cpu)
            "ecs:task-memory" = tonumber(aws_ecs_task_definition.fcvm_fleet_prod_coordinator[0].memory)
          }
        }
      },
      {
        Effect    = "Allow", Action = "ecs:TagResource", Resource = local.fcvm_prod_family_arn[0]
        Condition = { StringEquals = { "ecs:CreateAction" = "RegisterTaskDefinition" } }
      },
      { Effect = "Allow", Action = "ecr:DescribeImages", Resource = aws_ecr_repository.fcvm_fleet_coordinator.arn },
      {
        Effect   = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter", "ssm:GetParameterHistory"]
        Resource = aws_ssm_parameter.fcvm_fleet_prod_coordinator_release[0].arn
      },
      {
        Effect   = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = aws_ssm_parameter.fcvm_prod_release_record[0].arn
      },
      # Staging's coordinator image, read only: the promotion's candidate.
      { Effect = "Allow", Action = "ssm:GetParameter", Resource = aws_ssm_parameter.fcvm_coordinator_release.arn },
      # ActiveViewers{Fleet=prod}. GetMetricData has no resource-level authorization.
      { Effect = "Allow", Action = "cloudwatch:GetMetricData", Resource = "*" },
      {
        Effect = "Allow", Action = "iam:PassRole"
        Resource = [aws_iam_role.fcvm_fleet_prod_coordinator_execution[0].arn,
        aws_iam_role.fcvm_fleet_prod_coordinator_task[0].arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      },
    ]
  })
}

resource "aws_lambda_function" "fcvm_coordinator_release_prod" {
  count                          = local.fcvm_fleet_prod
  function_name                  = "fcvm-coordinator-release-prod"
  role                           = aws_iam_role.fcvm_coordinator_release_prod[0].arn
  runtime                        = "python3.12"
  handler                        = "release.lambda_handler"
  timeout                        = 900
  memory_size                    = 256
  reserved_concurrent_executions = 1
  filename                       = data.archive_file.fcvm_coordinator_release.output_path
  source_code_hash               = data.archive_file.fcvm_coordinator_release.output_base64sha256
  environment {
    variables = {
      STACK                 = "prod"
      CLUSTER               = aws_ecs_cluster.fcvm_fleet_prod[0].name
      SERVICE               = aws_ecs_service.fcvm_fleet_prod_coordinator[0].name
      FAMILY                = aws_ecs_task_definition.fcvm_fleet_prod_coordinator[0].family
      RELEASE_ROLE          = aws_iam_role.fcvm_coordinator_release_prod_fleet[0].arn
      REPOSITORY_URL        = aws_ecr_repository.fcvm_fleet_coordinator.repository_url
      IMAGE_PARAMETER       = aws_ssm_parameter.fcvm_fleet_prod_coordinator_release[0].name
      STAGE_IMAGE_PARAMETER = aws_ssm_parameter.fcvm_coordinator_release.name
      RELEASE_RECORD        = aws_ssm_parameter.fcvm_prod_release_record[0].name
      METRICS_NAMESPACE     = "FCVM/Fleet"
      METRIC_FLEET          = local.fcvm_fleet_prod_stack.fleet
      LOCK_TABLE            = "ejc3-terraform-locks"
      LOCK_ID               = local.fcvm_stage_lock_id
    }
  }
  depends_on = [aws_iam_role_policy.fcvm_coordinator_release_prod, aws_iam_role_policy.fcvm_coordinator_release_prod_fleet]
}

# ------------------------------------------------------------------------------------------------ image release
# publish.py alone, without staging's bundle (publisher-config.json names staging's templates).
data "archive_file" "fcvm_ami_prod_release" {
  type        = "zip"
  output_path = "${path.module}/.terraform/fcvm-fleet-ami-prod-release.zip"
  source {
    content  = file("${path.module}/fcvm-fleet-ami-pipeline/publish.py")
    filename = "publish.py"
  }
}

resource "aws_iam_role" "fcvm_ami_prod_release" {
  count = local.fcvm_fleet_prod
  name  = "fcvm-fleet-ami-prod-release"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}

resource "aws_iam_role" "fcvm_ami_prod_release_fleet" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet_east1
  name     = "fcvm-fleet-ami-prod-release"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { AWS = aws_iam_role.fcvm_ami_prod_release[0].arn }, Action = "sts:AssumeRole"
  }] })
  tags = local.fcvm_fleet_prod_tags
}

resource "aws_cloudwatch_log_group" "fcvm_ami_prod_release" {
  count             = local.fcvm_fleet_prod
  name              = "/aws/lambda/fcvm-fleet-ami-prod-release"
  retention_in_days = 30
}

resource "aws_iam_role_policy" "fcvm_ami_prod_release" {
  count = local.fcvm_fleet_prod
  name  = "gate-record-prod-journal-and-backend-lock"
  role  = aws_iam_role.fcvm_ami_prod_release[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "ssm:GetParameter", Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.fcvm_ami_state_prefix}/gate" },
    {
      Effect   = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"]
      Resource = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.fcvm_prod_ami_state_prefix}/publications/*"
    },
    { Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.fcvm_ami_prod_release_fleet[0].arn },
    {
      Effect    = "Allow", Action = ["dynamodb:PutItem", "dynamodb:DeleteItem"]
      Resource  = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/ejc3-terraform-locks"
      Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [local.fcvm_stage_lock_id] } }
    },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.fcvm_ami_prod_release[0].arn}:*" },
  ] })
}

resource "aws_iam_role_policy" "fcvm_ami_prod_release_fleet" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet_east1
  name     = "image-only-prod-template-release"
  role     = aws_iam_role.fcvm_ami_prod_release_fleet[0].id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ec2:DescribeImages", "ec2:DescribeInstances", "ec2:DescribeLaunchTemplateVersions", "ec2:DescribeLaunchTemplates"], Resource = "*" },
    {
      Effect   = "Allow", Action = ["ec2:CreateLaunchTemplateVersion", "ec2:ModifyLaunchTemplate", "ec2:DeleteLaunchTemplateVersions"]
      Resource = [for t in module.fcvm_fleet_prod_east1[0].image_templates : "arn:aws:ec2:${t.region}:${data.aws_caller_identity.fcvm_fleet_prod[0].account_id}:launch-template/${t.id}"]
    },
    # Staging's live release, read only; prod's pointer, which only this function writes after Terraform creates it.
    { Effect = "Allow", Action = "ssm:GetParameter", Resource = aws_ssm_parameter.fcvm_ami_release.arn },
    { Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"], Resource = aws_ssm_parameter.fcvm_fleet_prod_ami_release[0].arn },
    # Power{Fleet=prod}. GetMetricData has no resource-level authorization.
    { Effect = "Allow", Action = "cloudwatch:GetMetricData", Resource = "*" },
    {
      Effect = "Deny", Action = ["iam:*", "sts:AssumeRole*", "secretsmanager:*", "ssm:SendCommand", "ec2:RunInstances",
      "ec2:TerminateInstances", "ec2:CreateFleet", "ec2:DeregisterImage", "s3:*"], Resource = "*"
    },
  ] })
}

resource "aws_lambda_function" "fcvm_ami_prod_release" {
  count                          = local.fcvm_fleet_prod
  function_name                  = "fcvm-fleet-ami-prod-release"
  role                           = aws_iam_role.fcvm_ami_prod_release[0].arn
  runtime                        = "python3.12"
  handler                        = "publish.prod_handler"
  timeout                        = 900
  memory_size                    = 256
  reserved_concurrent_executions = 1
  filename                       = data.archive_file.fcvm_ami_prod_release.output_path
  source_code_hash               = data.archive_file.fcvm_ami_prod_release.output_base64sha256
  environment {
    variables = {
      TEMPLATES_JSON          = jsonencode(module.fcvm_fleet_prod_east1[0].image_templates)
      CONTROL_REGION          = var.aws_region
      COMPUTE_REGION          = local.fcvm_ami_region
      CONTROL_ACCOUNT         = data.aws_caller_identity.current.account_id
      FLEET_ACCOUNT           = data.aws_caller_identity.fcvm_fleet_prod[0].account_id
      FLEET_ROLE              = aws_iam_role.fcvm_ami_prod_release_fleet[0].arn
      STATE_PREFIX            = local.fcvm_prod_ami_state_prefix
      RELEASE_PARAMETER       = aws_ssm_parameter.fcvm_fleet_prod_ami_release[0].name
      STAGE_RELEASE_PARAMETER = aws_ssm_parameter.fcvm_ami_release.name
      GATE_PARAMETER          = "${local.fcvm_ami_state_prefix}/gate"
      GUARD_SHA256            = local.fcvm_ami_guard_sha
      LOCK_TABLE              = "ejc3-terraform-locks"
      LOCK_ID                 = local.fcvm_stage_lock_id
      COPY_REGIONS            = ""
      BUILD_REGION            = local.fcvm_ami_build_region
      METRICS_REGION          = local.fcvm_fleet_home_region
      METRIC_FLEET            = local.fcvm_fleet_prod_stack.fleet
    }
  }
  depends_on = [aws_iam_role_policy.fcvm_ami_prod_release, aws_iam_role_policy.fcvm_ami_prod_release_fleet]
  lifecycle {
    precondition {
      condition = (
        length(module.fcvm_fleet_prod_east1[0].image_templates) == 4
        && length([for t in module.fcvm_fleet_prod_east1[0].image_templates : t if t.kind == "host"]) == 1
        && length(setintersection([for t in module.fcvm_fleet_prod_east1[0].image_templates : t.id], [for t in local.fcvm_ami_templates : t.id])) == 0
      )
      error_message = "The prod image release takes prod's four templates (one host) and none of staging's."
    }
  }
}

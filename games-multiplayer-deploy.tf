# games-multiplayer-deploy.tf
#
# Automatic, pull-based deployment of the games repo's multiplayer images. AWS pulls from
# GitHub; GitHub holds no AWS credential and cannot start or change anything here.
#
#   EventBridge Scheduler (1 min) --> games-mp-poller  (Colton's read-only token)
#       reads every branch head of CoderColton/colton-games; for each new commit, uploads its
#       source to S3 and starts
#         main         -> games-mp-images          router + engines -> games/*
#         other branch -> games-mp-images-preview  engines only      -> games-preview/*
#   CodeBuild state change --> EventBridge --> games-mp-release
#       main:    registers games-<game> revisions; runs games-mp-migrate first when the commit's
#                mp schema revision is not the database's; makes the commit production's current
#                release (releases table `current#main`, which games-mp-launch-production reads on
#                every launch); moves games/mp-router:live and rolls the router when the
#                router's files changed
#       preview: registers games-preview-<game> revisions for exactly that commit, which
#                games-mp-launch-preview launches for a lobby built from it
#       failure: recorded; main and migration failures alert cost-alerts
#
# NEVER KICKS A LIVE MATCH. A running engine never changes (a release only picks what the next
# launch runs; no revision is ever deregistered, and production images are never expired).
# A router rollout drains each old task at the ALB for up to an hour and no match lasts
# longer (games-multiplayer.tf, the target group). Migrations must be backward compatible
# with the lobby and engines still running (docs/games-multiplayer.md "Shipping").
#
# TRUST THIS ADDS (docs/games-multiplayer.md): a push to colton-games main deploys production
# engines, the router and mp migrations with no human step; any branch's code runs in preview
# engines (preview families, repositories, launch role and ceiling only).
#
# Terraform owns everything here and in the other two files: roles, network, ceilings, alarms,
# the functions and the router service. It owns no image commit and never fights a release:
# the releases table is written only by the functions, and the router task definition runs the
# `live` tag.

variable "games_mp_autodeploy" {
  description = "Poll colton-games for new commits and deploy them. false stops new builds (production keeps its current release); the release function still handles builds already running."
  type        = bool
  default     = true
}

locals {
  games_mp_poller_name     = "games-mp-poller"
  games_mp_release_name    = "games-mp-release"
  games_mp_preview_project = "games-mp-images-preview"
  games_mp_migrate_project = "games-mp-migrate"
  games_mp_db_url_secret   = "games/mp-db-url"

  # The shape of every engine revision games-mp-release registers (release.py register()).
  games_mp_engine_templates = { for id, cfg in local.mp_games : id => {
    cpu              = cfg.cpu
    memory           = cfg.memory
    executionRoleArn = aws_iam_role.games_engine_execution.arn
    taskRoleArn      = aws_iam_role.games_engine_task.arn
    logGroup         = aws_cloudwatch_log_group.games_engines.name
    region           = var.aws_region
    main = {
      family        = "${local.mp_engine_channels.main.family_prefix}${id}"
      repository    = "${local.mp_engine_channels.main.repository_prefix}${id}-engine"
      repositoryUrl = aws_ecr_repository.games_mp["${local.mp_engine_channels.main.repository_prefix}${id}-engine"].repository_url
    }
    preview = {
      family        = "${local.mp_engine_channels.preview.family_prefix}${id}"
      repository    = "${local.mp_engine_channels.preview.repository_prefix}${id}-engine"
      repositoryUrl = aws_ecr_repository.games_mp["${local.mp_engine_channels.preview.repository_prefix}${id}-engine"].repository_url
    }
  } }

  games_mp_production_repo_arns = [for name, repo in aws_ecr_repository.games_mp : repo.arn if startswith(name, "games/")]
  games_mp_preview_repo_arns    = [for name, repo in aws_ecr_repository.games_mp : repo.arn if startswith(name, "games-preview/")]
}

# -------------------------------------------------------------------------------------
# The releases table
# -------------------------------------------------------------------------------------
#
#   build#<main|preview>#<commit>  the poller's claim, then the build's and release's status
#                                  (starting, building, built, migrating, released, failed,
#                                  superseded) and revisions. Preview items expire (expiresAt).
#   seq#main                       the poller's counter: main releases are ordered by it
#   current#main                   production's current release: commit, seq, per game the
#                                  revision and simVersion (games-mp-launch-production)
#   sim#<game>#<simVersion>        the newest main revision released for that simVersion
#   router#main                    what games/mp-router:live was last rolled to
#   schema#main                    the database's mp schema revision after the last migration
#
# Only the functions write it (the poller: build# and seq# only). Point-in-time recovery keeps
# 35 days of history, so a bad write can be undone.
resource "aws_dynamodb_table" "games_mp_releases" {
  name         = "games-mp-releases"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "id"

  attribute {
    name = "id"
    type = "S"
  }

  ttl {
    attribute_name = "expiresAt"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = { Name = "games-mp-releases", Project = "games-multiplayer" }
}

# Production's current release before the first automatic one: the newest verifying revision
# of each game's production family (what Terraform registered), written once if missing. On a
# platform built from nothing there is none, and production launches wait for the first main
# release (a few minutes after the apply).
resource "terraform_data" "games_mp_current_bootstrap" {
  triggers_replace = {
    table = aws_dynamodb_table.games_mp_releases.name
  }

  provisioner "local-exec" {
    command = "python3 ${local.games_mp_bringup} releases-bootstrap --region ${var.aws_region} --table ${aws_dynamodb_table.games_mp_releases.name} --games \"$GAMES_MP_GAMES\""
    environment = {
      GAMES_MP_GAMES = jsonencode({ for id, t in local.games_mp_engine_templates : id => t.main })
    }
  }
}

# -------------------------------------------------------------------------------------
# Preview image builds: any branch's code, so nothing production can use
# -------------------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "games_mp_codebuild_preview" {
  name              = "/aws/codebuild/${local.games_mp_preview_project}"
  retention_in_days = 14
}

resource "aws_iam_role" "games_mp_codebuild_preview" {
  name = "games-mp-codebuild-preview"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/${local.games_mp_preview_project}" }
      }
    }]
  })
  tags = { Name = "games-mp-codebuild-preview", Project = "games-multiplayer" }
}

# The code this runs is any branch's, so this role is all it gets: its own sources, its own
# logs, and push to games-preview/* only. It cannot push a production or router image, read a
# secret, or touch ECS; whatever it reports is checked by games-mp-release against ECR.
resource "aws_iam_role_policy" "games_mp_codebuild_preview" {
  name = "build-and-push-preview-images"
  role = aws_iam_role.games_mp_codebuild_preview.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadPreviewSources"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${aws_s3_bucket.games_mp_build.arn}/sources/preview/*"
      },
      {
        Sid      = "LocateSourceBucket"
        Effect   = "Allow"
        Action   = ["s3:GetBucketAcl", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.games_mp_build.arn
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.games_mp_codebuild_preview.arn}:*"
      },
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "PushPreviewImages"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
          "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload",
          "ecr:PutImage", "ecr:UploadLayerPart",
        ]
        Resource = local.games_mp_preview_repo_arns
      },
    ]
  })
}

resource "aws_codebuild_project" "games_mp_images_preview" {
  name          = local.games_mp_preview_project
  description   = "Builds and pushes the multiplayer ENGINE images of a colton-games branch into games-preview/* (games-mp-poller starts it)"
  service_role  = aws_iam_role.games_mp_codebuild_preview.arn
  build_timeout = 20

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    type                        = "ARM_CONTAINER"
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/amazonlinux-aarch64-standard:4.0"
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = true

    environment_variable {
      name  = "ACCOUNT_ID"
      value = data.aws_caller_identity.current.account_id
    }

    environment_variable {
      name  = "GAMES_MP_CHANNEL"
      value = "preview"
    }
  }

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.games_mp_build.bucket}/sources/preview/none.zip"
    buildspec = file("${path.module}/games-multiplayer/buildspec.yml")
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.games_mp_codebuild_preview.name
    }
  }

  tags = { Name = local.games_mp_preview_project, Project = "games-multiplayer" }
}

# -------------------------------------------------------------------------------------
# Migrations: a main commit's mp Supabase migrations
# -------------------------------------------------------------------------------------
#
# Its own project and role, because it holds the database URL: it runs only this repo's driver
# and psql with main's .sql files (buildspec-migrate.yml), never the games repo's code. The URL
# is the Supabase integration's POSTGRES_URL_NON_POOLING, copied from Vercel by the apply
# (sync-db-url, below) into games/mp-db-url, which only administration and this role can read.

resource "aws_secretsmanager_secret" "games_mp_db_url" {
  name                    = local.games_mp_db_url_secret
  description             = "Supabase database URL for games-mp-migrate (the integration's POSTGRES_URL_NON_POOLING). Copied from Vercel by the apply; never in state."
  recovery_window_in_days = 7
  tags                    = { Name = local.games_mp_db_url_secret, Managed = "terraform", Project = "games-multiplayer" }
}

resource "aws_secretsmanager_secret_policy" "games_mp_db_url" {
  secret_arn = aws_secretsmanager_secret.games_mp_db_url.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheMigrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.games_mp_db_url.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.games_mp_migrate.arn]) } }
    }]
  })
}

variable "games_mp_db_url_sync" {
  description = "Bump to copy the Supabase database URL from Vercel into games/mp-db-url again (e.g. after the integration rotates its password)."
  type        = string
  default     = "1"
}

resource "terraform_data" "games_mp_db_url" {
  triggers_replace = {
    secret = aws_secretsmanager_secret.games_mp_db_url.arn
    sync   = var.games_mp_db_url_sync
  }

  provisioner "local-exec" {
    command = "python3 ${local.games_mp_bringup} sync-db-url --region ${var.aws_region} --team-id ${var.vercel_team_id} --project-id ${local.colton_games_vercel_project_id} --secret-id ${aws_secretsmanager_secret.games_mp_db_url.name}"
  }

  depends_on = [data.external.games_mp_preflight, aws_secretsmanager_secret_policy.games_mp_db_url]
}

resource "aws_cloudwatch_log_group" "games_mp_migrate" {
  name              = "/aws/codebuild/${local.games_mp_migrate_project}"
  retention_in_days = 14
}

resource "aws_iam_role" "games_mp_migrate" {
  name = "games-mp-migrate"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/${local.games_mp_migrate_project}" }
      }
    }]
  })
  tags = { Name = "games-mp-migrate", Project = "games-multiplayer" }
}

resource "aws_iam_role_policy" "games_mp_migrate" {
  name = "migrate-mp-schema"
  role = aws_iam_role.games_mp_migrate.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadMainSources"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${aws_s3_bucket.games_mp_build.arn}/sources/main/*"
      },
      {
        Sid      = "LocateSourceBucket"
        Effect   = "Allow"
        Action   = ["s3:GetBucketAcl", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.games_mp_build.arn
      },
      {
        Sid      = "ReadTheDatabaseUrl"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.games_mp_db_url.arn
      },
      {
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.games_mp_migrate.arn}:*"
      },
    ]
  })
}

resource "aws_codebuild_project" "games_mp_migrate" {
  name          = local.games_mp_migrate_project
  description   = "Applies a colton-games main commit's mp Supabase migrations (games-mp-release starts it)"
  service_role  = aws_iam_role.games_mp_migrate.arn
  build_timeout = 15

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    type                        = "ARM_CONTAINER"
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/amazonlinux-aarch64-standard:4.0"
    image_pull_credentials_type = "CODEBUILD"
    privileged_mode             = false
  }

  # One migration at a time.
  concurrent_build_limit = 1

  source {
    type      = "S3"
    location  = "${aws_s3_bucket.games_mp_build.bucket}/sources/main/none.zip"
    buildspec = file("${path.module}/games-multiplayer/buildspec-migrate.yml")
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.games_mp_migrate.name
    }
  }

  tags = { Name = local.games_mp_migrate_project, Project = "games-multiplayer" }
}

# -------------------------------------------------------------------------------------
# games-mp-poller
# -------------------------------------------------------------------------------------

data "archive_file" "games_mp_poller" {
  type        = "zip"
  output_path = "${path.module}/.terraform/games-mp-poller.zip"

  source {
    filename = "poller.py"
    content  = file("${path.module}/games-multiplayer/poller.py")
  }

  # The driver CodeBuild runs, and the CA the migration pins: the poller adds both to every
  # source zip under .games-mp/, so what runs in CodeBuild is always this repo's reviewed copy.
  source {
    filename = "bringup.py"
    content  = file("${path.module}/games-multiplayer/bringup.py")
  }

  source {
    filename = "supabase-root-2021-ca.crt"
    content  = file("${path.module}/games-multiplayer/supabase-root-2021-ca.crt")
  }
}

resource "aws_cloudwatch_log_group" "games_mp_poller" {
  name              = "/aws/lambda/${local.games_mp_poller_name}"
  retention_in_days = 14
}

resource "aws_iam_role" "games_mp_poller" {
  name = local.games_mp_poller_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = local.games_mp_poller_name, Project = "games-multiplayer" }
}

resource "aws_iam_role_policy" "games_mp_poller" {
  name = "poll-colton-games-and-start-builds"
  role = aws_iam_role.games_mp_poller.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadTheGitHubReadToken"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.games_mp_github_read.arn
      },
      {
        Sid      = "UploadSources"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:AbortMultipartUpload"]
        Resource = "${aws_s3_bucket.games_mp_build.arn}/sources/*"
      },
      {
        Sid      = "StartImageBuilds"
        Effect   = "Allow"
        Action   = "codebuild:StartBuild"
        Resource = [aws_codebuild_project.games_mp_images.arn, aws_codebuild_project.games_mp_images_preview.arn]
      },
      {
        Sid      = "CountRunningPreviewBuilds"
        Effect   = "Allow"
        Action   = ["codebuild:ListBuildsForProject", "codebuild:BatchGetBuilds"]
        Resource = aws_codebuild_project.games_mp_images_preview.arn
      },
      {
        # Its own items only: build claims and the main sequence. Never a release item.
        Sid      = "ClaimBuilds"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem"]
        Resource = aws_dynamodb_table.games_mp_releases.arn
        Condition = {
          "ForAllValues:StringLike" = { "dynamodb:LeadingKeys" = ["build#*", "seq#main"] }
        }
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.games_mp_poller.arn}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "games_mp_poller" {
  function_name    = local.games_mp_poller_name
  role             = aws_iam_role.games_mp_poller.arn
  handler          = "poller.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 120
  memory_size      = 512
  filename         = data.archive_file.games_mp_poller.output_path
  source_code_hash = data.archive_file.games_mp_poller.output_base64sha256
  # One run at a time, so two never claim and upload the same commit.
  reserved_concurrent_executions = 1

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.games_mp_poller.name
  }

  environment {
    variables = {
      REPO            = local.games_mp_repo
      TOKEN_SECRET    = aws_secretsmanager_secret.games_mp_github_read.name
      RELEASES_TABLE  = aws_dynamodb_table.games_mp_releases.name
      BUCKET          = aws_s3_bucket.games_mp_build.bucket
      MAIN_PROJECT    = aws_codebuild_project.games_mp_images.name
      PREVIEW_PROJECT = aws_codebuild_project.games_mp_images_preview.name
    }
  }

  tags = { Name = local.games_mp_poller_name, Project = "games-multiplayer" }

  # The release function must be listening before the first build ends.
  depends_on = [aws_cloudwatch_event_target.games_mp_release, aws_lambda_permission.games_mp_release]
}

resource "aws_lambda_function_event_invoke_config" "games_mp_poller" {
  function_name                = aws_lambda_function.games_mp_poller.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 60
}

resource "aws_iam_role" "games_mp_poller_scheduler" {
  name = "games-mp-poller-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "games_mp_poller_scheduler" {
  name = "invoke-games-mp-poller"
  role = aws_iam_role.games_mp_poller_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.games_mp_poller.arn
    }]
  })
}

resource "aws_scheduler_schedule" "games_mp_poller" {
  name       = local.games_mp_poller_name
  group_name = "default"
  state      = var.games_mp_autodeploy ? "ENABLED" : "DISABLED"

  flexible_time_window { mode = "OFF" }

  schedule_expression = "rate(1 minute)"

  target {
    arn      = aws_lambda_function.games_mp_poller.arn
    role_arn = aws_iam_role.games_mp_poller_scheduler.arn

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 60
    }
  }
}

# -------------------------------------------------------------------------------------
# games-mp-release
# -------------------------------------------------------------------------------------

data "archive_file" "games_mp_release" {
  type        = "zip"
  source_file = "${path.module}/games-multiplayer/release.py"
  output_path = "${path.module}/.terraform/games-mp-release.zip"
}

resource "aws_cloudwatch_log_group" "games_mp_release" {
  name              = "/aws/lambda/${local.games_mp_release_name}"
  retention_in_days = 90
}

resource "aws_iam_role" "games_mp_release" {
  name = local.games_mp_release_name
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = local.games_mp_release_name, Project = "games-multiplayer" }
}

resource "aws_iam_role_policy" "games_mp_release" {
  name = "release-games-mp-builds"
  role = aws_iam_role.games_mp_release.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadBuilds"
        Effect   = "Allow"
        Action   = "codebuild:BatchGetBuilds"
        Resource = [aws_codebuild_project.games_mp_images.arn, aws_codebuild_project.games_mp_images_preview.arn, aws_codebuild_project.games_mp_migrate.arn]
      },
      {
        Sid      = "StartMigrations"
        Effect   = "Allow"
        Action   = "codebuild:StartBuild"
        Resource = aws_codebuild_project.games_mp_migrate.arn
      },
      {
        Sid      = "Releases"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.games_mp_releases.arn
      },
      {
        Sid      = "CheckImages"
        Effect   = "Allow"
        Action   = "ecr:DescribeImages"
        Resource = [for repo in aws_ecr_repository.games_mp : repo.arn]
      },
      {
        # The router's `live` tag, and nothing else in ECR: it reads a router image's manifest
        # and puts it under `live` (the only mutable tag, local.mp_router_live_tag).
        Sid      = "MoveTheRouterLiveTag"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:PutImage"]
        Resource = aws_ecr_repository.games_mp["games/mp-router"].arn
      },
      {
        # RegisterTaskDefinition supports no resource scope; the revisions it registers can
        # name only the two engine roles (PassRole below), and they must carry the tag.
        Sid       = "RegisterEngineRevisions"
        Effect    = "Allow"
        Action    = "ecs:RegisterTaskDefinition"
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestTag/Project" = "games-multiplayer" } }
      },
      {
        Sid       = "TagEngineRevisionsAtRegistration"
        Effect    = "Allow"
        Action    = "ecs:TagResource"
        Resource  = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/*"
        Condition = { StringEquals = { "ecs:CreateAction" = "RegisterTaskDefinition" } }
      },
      {
        Sid      = "PassOnlyEngineRoles"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [aws_iam_role.games_engine_task.arn, aws_iam_role.games_engine_execution.arn]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
      {
        # Roll the router: a new deployment of the same task definition (it resolves `live`).
        Sid      = "RollTheRouter"
        Effect   = "Allow"
        Action   = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${local.mp_cluster_name}/mp-router"
      },
      {
        Sid       = "ListRouterTasks"
        Effect    = "Allow"
        Action    = "ecs:ListTasks"
        Resource  = "*"
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn } }
      },
      {
        Sid       = "DescribeRouterTasks"
        Effect    = "Allow"
        Action    = "ecs:DescribeTasks"
        Resource  = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.mp_cluster_name}/*"
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn } }
      },
      {
        # Read-only, and it supports no resource scope.
        Sid      = "RouterTargetHealth"
        Effect   = "Allow"
        Action   = "elasticloadbalancing:DescribeTargetHealth"
        Resource = "*"
      },
      {
        Sid      = "Alert"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      },
      {
        Sid      = "WriteOwnLogs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.games_mp_release.arn}:*"
      },
    ]
  })
}

resource "aws_lambda_function" "games_mp_release" {
  function_name    = local.games_mp_release_name
  role             = aws_iam_role.games_mp_release.arn
  handler          = "release.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 900
  memory_size      = 256
  filename         = data.archive_file.games_mp_release.output_path
  source_code_hash = data.archive_file.games_mp_release.output_base64sha256
  # One release at a time: promotions and router rollouts never interleave. Events that
  # arrive meanwhile are throttled and retried by Lambda (for up to the event age below).
  reserved_concurrent_executions = 1

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.games_mp_release.name
  }

  environment {
    variables = {
      RELEASES_TABLE    = aws_dynamodb_table.games_mp_releases.name
      MAIN_PROJECT      = aws_codebuild_project.games_mp_images.name
      PREVIEW_PROJECT   = aws_codebuild_project.games_mp_images_preview.name
      MIGRATE_PROJECT   = aws_codebuild_project.games_mp_migrate.name
      BUCKET            = aws_s3_bucket.games_mp_build.bucket
      ENGINE_TEMPLATES  = jsonencode(local.games_mp_engine_templates)
      ROUTER_REPOSITORY = aws_ecr_repository.games_mp["games/mp-router"].name
      ROUTER_LIVE_TAG   = local.mp_router_live_tag
      CLUSTER           = aws_ecs_cluster.games.name
      ROUTER_SERVICE    = "mp-router"
      TARGET_GROUP_ARN  = aws_lb_target_group.games_mp_router.arn
      SNS_TOPIC_ARN     = aws_sns_topic.cost_alerts.arn
      ROLL_TIMEOUT_SEC  = "600"
    }
  }

  tags = { Name = local.games_mp_release_name, Project = "games-multiplayer" }
}

# A release that failed is alerted and not retried blindly (it may have rolled half-way); a
# throttled event (another release running) waits up to six hours.
resource "aws_lambda_function_event_invoke_config" "games_mp_release" {
  function_name                = aws_lambda_function.games_mp_release.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 21600
}

resource "aws_cloudwatch_event_rule" "games_mp_builds" {
  name        = "games-mp-build-state"
  description = "Finished games multiplayer CodeBuild runs -> games-mp-release"
  event_pattern = jsonencode({
    source        = ["aws.codebuild"]
    "detail-type" = ["CodeBuild Build State Change"]
    detail = {
      "project-name" = [aws_codebuild_project.games_mp_images.name, aws_codebuild_project.games_mp_images_preview.name, aws_codebuild_project.games_mp_migrate.name]
      "build-status" = ["SUCCEEDED", "FAILED", "FAULT", "STOPPED", "TIMED_OUT"]
    }
  })
  tags = { Name = "games-mp-build-state", Project = "games-multiplayer" }
}

resource "aws_cloudwatch_event_target" "games_mp_release" {
  rule = aws_cloudwatch_event_rule.games_mp_builds.name
  arn  = aws_lambda_function.games_mp_release.arn
}

resource "aws_lambda_permission" "games_mp_release" {
  statement_id  = "games-mp-build-state"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.games_mp_release.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.games_mp_builds.arn
}

# -------------------------------------------------------------------------------------
# Alarms (the release function also publishes each failure itself)
# -------------------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "games_mp_release_errors" {
  alarm_name          = "games-mp-release-errors"
  alarm_description   = "games-mp-release raised or timed out: a colton-games main commit may not have reached production, or a router rollout failed. See /aws/lambda/games-mp-release."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.games_mp_release.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# A GitHub hiccup is one failed minute; 15 in a row is a broken token (expired: Colton
# regenerates it, docs/games-multiplayer.md) or a broken poller.
resource "aws_cloudwatch_metric_alarm" "games_mp_poller_errors" {
  alarm_name          = "games-mp-poller-errors"
  alarm_description   = "games-mp-poller failed for 15 minutes: new colton-games commits are not being built (expired GitHub token games/colton-games-read?). See /aws/lambda/games-mp-poller."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.games_mp_poller.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# -------------------------------------------------------------------------------------
# The router's `live` tag, once
# -------------------------------------------------------------------------------------

resource "terraform_data" "games_mp_router_live" {
  triggers_replace = {
    repository = aws_ecr_repository.games_mp["games/mp-router"].arn
    tag        = local.mp_router_live_tag
  }

  provisioner "local-exec" {
    command = "python3 ${local.games_mp_bringup} router-live --region ${var.aws_region} --cluster ${local.mp_cluster_name} --service mp-router --repository ${aws_ecr_repository.games_mp["games/mp-router"].name} --tag ${local.mp_router_live_tag}"
  }

  # On a platform built from nothing it waits for the first main release to create the tag.
  depends_on = [aws_scheduler_schedule.games_mp_poller, aws_lambda_function.games_mp_release, aws_cloudwatch_event_target.games_mp_release]
}

output "games_mp_releases_table" {
  description = "The releases table: current#main is production's current engine release (aws dynamodb get-item --table-name games-mp-releases --key '{\"id\":{\"S\":\"current#main\"}}')"
  value       = aws_dynamodb_table.games_mp_releases.name
}

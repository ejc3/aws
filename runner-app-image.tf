# runner-app-image.tf
#
# Prebuilt runner images for Pattern C (runner-app.tf). A repo that sets `image` in runner_app_repos boots the newest
# build of that image instead of Canonical's stock Ubuntu 24.04, so its jobs stop installing the same toolchains on
# every boot. Every other repo stays on Canonical's image, unchanged.
#
#   EventBridge, weekly     --> github-app-runner-image {"build": true}   launch one builder per image
#   EventBridge, every 5 min --> github-app-runner-image {}               image a finished builder, prune
#   on demand: aws lambda invoke --function-name github-app-runner-image \
#                --cli-binary-format raw-in-base64-out --payload '{"build": ["imagine-runner"]}' /dev/stdout
#   (any image's name; several at once, or `true` for all of them)
#
# The same shape as fcvm's image pipeline (github-ami-builder.tf): a named builder host provisions itself from user
# data, can do nothing but report one progress tag, and is imaged and terminated by whoever launched it. Here that is a
# Lambda rather than a GitHub workflow, because ejc3/aws's workflows carry no AWS identity (github-actions.tf). The
# policy lives in runner-app/image_builder.py; the builder's user data is runner-app/image-build.sh, which runs
# runner-app/bootstrap.sh's own install steps (so the GitHub runner is bootstrap.sh's pin, refreshed at every build)
# and then runner-app/images/<script>.
#
# An image only saves time; it never decides what a job gets. The image's tools are the job's own setup steps' first
# choice, and those steps still install whatever is missing or of another version, so an image that falls behind a
# version change is only slower. If no image exists the controller launches Canonical's image and logs that it did.
#
# Images are tagged Purpose=github-app-runner-image, Image=<name>, Repo=<repo>, never fcvm's Purpose=github-runner
# (fcvm's launcher and the Ohio replicator pick images by that tag). The newest three of each name are kept; older ones
# are deregistered and their snapshots deleted.
#
# Cost: about 15 GB of snapshot per image, three kept (about $2.25 a month at $0.05/GB-month), and a weekly build of
# about 20-30 minutes on one on-demand c7a.2xlarge (under $1 a month): about $2-3 a month. Fast Snapshot Restore is not
# enabled (about $540 a month per AZ), so a host's first read of each block comes from S3: see GITHUB-RUNNERS.md,
# "Prebuilt images", for the trial that measures what that costs.
locals {
  runner_app_image_purpose           = "github-app-runner-image"
  runner_app_image_builder           = "github-app-runner-image-builder"
  runner_app_image_builder_types     = ["c7a.2xlarge", "c7i.2xlarge", "c6a.2xlarge"]
  runner_app_image_builder_volume_gb = 30

  # One entry per image. `script` is under runner-app/images/; `versions` are exported to it as environment variables.
  runner_app_images = {
    "imagine-runner" = {
      repo   = "ejc3/imagine"
      script = "imagine-runner.sh"
      # These must match ejc3/imagine: RUST_* its rust-toolchain.toml (channel, components, targets); OTP_VERSION,
      # ELIXIR_VERSION and the libSQL server image its .github/workflows/ci.yml; PNPM_VERSION and PLAYWRIGHT_VERSION its
      # web/package.json (packageManager, @playwright/test); NODE_VERSION the Node its setup action pins
      # (.github/actions/setup/action.yml, node-version), which a job uses when it finds that version. They are not
      # read from that repository, which is private. A pin that falls behind costs time, never correctness: the job's
      # own setup installs the version it asks for.
      versions = {
        RUST_TOOLCHAIN     = "1.93.0"
        RUST_COMPONENTS    = "rustfmt,clippy"
        RUST_TARGETS       = "wasm32-unknown-unknown"
        NODE_VERSION       = "22.23.3"
        PNPM_VERSION       = "10.29.1"
        OTP_VERSION        = "29.1.1"
        ELIXIR_VERSION     = "1.20.4"
        PLAYWRIGHT_VERSION = "1.63.0"
        LIBSQL_IMAGE       = "ghcr.io/tursodatabase/libsql-server:v0.24.32"
      }
    }

    # The Node web sites, one image per repo (an image carries its repo's tag; the controller boots it for that repo
    # alone). One script, runner-app/images/node-web-runner.sh; it reads what each pin means. The pins must match each
    # repository: NODE_MAJOR the node-version (or .nvmrc) of its actions/setup-node steps, PYTHON_VERSIONS the
    # python-version of its actions/setup-python steps, PLAYWRIGHT_VERSION the @playwright/test version in the
    # package-lock.json its e2e jobs install from and PLAYWRIGHT_BROWSERS the browsers they install, APT_PACKAGES what
    # its jobs apt-get install. A pin that falls behind costs time, never correctness.
    "dolphin-labs-runner" = {
      repo   = "dolphin-labs-hq/dolphin-labs"
      script = "node-web-runner.sh"
      # .github/workflows/ci.yml, cloudflare-stage.yml, refresh-publish.yml; web/package-lock.json.
      versions = {
        NODE_MAJOR          = "22"
        PYTHON_VERSIONS     = "3.11"
        PLAYWRIGHT_VERSION  = "1.62.1"
        PLAYWRIGHT_BROWSERS = "chromium,webkit"
      }
    }
    "dolphin-films-runner" = {
      repo   = "dolphin-labs-hq/dolphin-films"
      script = "node-web-runner.sh"
      # .github/workflows/ci.yml, films-ci.yml; web/package-lock.json.
      versions = {
        NODE_MAJOR          = "22"
        PYTHON_VERSIONS     = "3.11"
        PLAYWRIGHT_VERSION  = "1.62.1"
        PLAYWRIGHT_BROWSERS = "chromium"
      }
    }
    "dolphin-maps-runner" = {
      repo   = "dolphin-labs-hq/dolphin-maps"
      script = "node-web-runner.sh"
      # .github/workflows/ci.yml, scrape-live.yml (Python's Playwright drives a remote browser: nothing to install).
      versions = {
        NODE_MAJOR      = "22"
        PYTHON_VERSIONS = "3.12"
      }
    }
    "colton-games-runner" = {
      repo   = "CoderColton/colton-games"
      script = "node-web-runner.sh"
      # .nvmrc; .github/workflows/e2e.yml (PostgreSQL for the isolated storage tests); package-lock.json.
      versions = {
        NODE_MAJOR          = "24"
        PLAYWRIGHT_VERSION  = "1.61.1"
        PLAYWRIGHT_BROWSERS = "chromium,webkit"
        APT_PACKAGES        = "postgresql-16,postgresql-client-16"
      }
    }
  }
}

# ============================================ builder host
# Runs only this repository's scripts (no job code), and can do one thing: tag a builder BuildStatus=complete. No SSM
# agent, no reads of any credential, no other EC2 action.
resource "aws_iam_role" "runner_app_image_builder" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-app-runner-image-builder-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = "github-app-runner-image-builder-role" }
}

resource "aws_iam_instance_profile" "runner_app_image_builder" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-app-runner-image-builder-profile"
  role  = aws_iam_role.runner_app_image_builder[0].name
}

resource "aws_iam_role_policy" "runner_app_image_builder" {
  count = var.enable_github_runner ? 1 : 0
  name  = "report-build-complete-only"
  role  = aws_iam_role.runner_app_image_builder[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReportBuildComplete"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = {
            "ec2:ResourceTag/Name"       = local.runner_app_image_builder
            "aws:RequestTag/BuildStatus" = "complete"
          }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["BuildStatus"] }
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

# ============================================ the pipeline's function
data "archive_file" "runner_app_image" {
  type        = "zip"
  output_path = "${path.module}/.terraform/github-app-runner-image.zip"

  source {
    content  = file("${path.module}/runner-app/image_builder.py")
    filename = "image_builder.py"
  }
  source {
    content  = file("${path.module}/runner-app/image-build.sh")
    filename = "image-build.sh"
  }
  source {
    content  = file("${path.module}/runner-app/bootstrap.sh")
    filename = "bootstrap.sh"
  }
  dynamic "source" {
    # Each script once: several images share one.
    for_each = toset([for image in local.runner_app_images : image.script])
    content {
      content  = file("${path.module}/runner-app/images/${source.value}")
      filename = "images/${source.value}"
    }
  }
}

resource "aws_iam_role" "runner_app_image_lambda" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-app-runner-image-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

# Launch only named builders from Canonical's image, on demand, into the runner subnets with the app runners' no-inbound
# group and the builder's profile; image only those builders, and only as tagged images; terminate only builders;
# deregister and delete only this pipeline's images and snapshots.
resource "aws_iam_role_policy" "runner_app_image_lambda" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-app-runner-image"
  role  = aws_iam_role.runner_app_image_lambda[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OwnLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [
          "arn:aws:logs:us-west-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/github-app-runner-image",
          "arn:aws:logs:us-west-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/github-app-runner-image:*",
        ]
      },
      {
        Sid      = "ResolveUbuntuImage"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:us-west-1::parameter/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
      },
      {
        # Describe calls take no resource ARN, so the region is the bound: the function's clients are pinned to us-west-1.
        Sid       = "Describe"
        Effect    = "Allow"
        Action    = ["ec2:DescribeInstances", "ec2:DescribeImages", "ec2:DescribeSnapshots"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = "us-west-1" } }
      },
      {
        # As runner-app.tf's LaunchCanonicalImagesOnly: Canonical's images report ImageOwnerAlias "amazon".
        Sid       = "BuildFromCanonicalImagesOnly"
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = "arn:aws:ec2:us-west-1::image/*"
        Condition = { StringEquals = { "ec2:Owner" = [local.runner_app_ami_owner, "amazon"] } }
      },
      {
        Sid      = "LaunchIntoRunnerSubnetsWithAppGroup"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = concat(local.runner_launch_subnet_arns, [aws_security_group.runner_app[0].arn])
      },
      {
        Sid      = "LaunchNamedBuilder"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = {
            "aws:RequestTag/Name"    = local.runner_app_image_builder
            "ec2:InstanceType"       = local.runner_app_image_builder_types
            "ec2:MetadataHttpTokens" = "required"
          }
          ArnEquals = { "ec2:InstanceProfile" = aws_iam_instance_profile.runner_app_image_builder[0].arn }
        }
      },
      {
        Sid      = "LaunchEncryptedBuilderVolume"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:volume/*"
        Condition = {
          StringEquals          = { "aws:RequestTag/Name" = local.runner_app_image_builder, "ec2:VolumeType" = "gp3" }
          Bool                  = { "ec2:Encrypted" = "true" }
          NumericLessThanEquals = { "ec2:VolumeSize" = tostring(local.runner_app_image_builder_volume_gb) }
        }
      },
      {
        Sid      = "LaunchNamedBuilderENI"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:network-interface/*"
        Condition = {
          StringEquals = { "aws:RequestTag/Name" = local.runner_app_image_builder }
          ArnEquals    = { "ec2:Subnet" = local.runner_launch_subnet_arns }
        }
      },
      {
        Sid      = "TagBuilderAtLaunch"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = [for kind in ["instance", "volume", "network-interface"] : "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:${kind}/*"]
        Condition = {
          StringEquals                = { "ec2:CreateAction" = "RunInstances", "aws:RequestTag/Name" = local.runner_app_image_builder }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Name", "Image", "Repo", "BuildStatus"] }
        }
      },
      {
        Sid       = "ImageTerminateAndReadBuildersOnly"
        Effect    = "Allow"
        Action    = ["ec2:CreateImage", "ec2:TerminateInstances", "ec2:GetConsoleOutput"]
        Resource  = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = { StringEquals = { "aws:ResourceTag/Name" = local.runner_app_image_builder } }
      },
      {
        Sid       = "CreateTaggedImage"
        Effect    = "Allow"
        Action    = "ec2:CreateImage"
        Resource  = ["arn:aws:ec2:us-west-1::image/*", "arn:aws:ec2:us-west-1::snapshot/*"]
        Condition = { StringEquals = { "aws:RequestTag/Purpose" = local.runner_app_image_purpose } }
      },
      {
        Sid       = "TagCreatedImage"
        Effect    = "Allow"
        Action    = "ec2:CreateTags"
        Resource  = ["arn:aws:ec2:us-west-1::image/*", "arn:aws:ec2:us-west-1::snapshot/*"]
        Condition = { StringEquals = { "ec2:CreateAction" = "CreateImage", "aws:RequestTag/Purpose" = local.runner_app_image_purpose } }
      },
      {
        Sid       = "RetireOwnImagesOnly"
        Effect    = "Allow"
        Action    = ["ec2:DeregisterImage", "ec2:DeleteSnapshot"]
        Resource  = ["arn:aws:ec2:us-west-1::image/*", "arn:aws:ec2:us-west-1::snapshot/*"]
        Condition = { StringEquals = { "aws:ResourceTag/Purpose" = local.runner_app_image_purpose } }
      },
      {
        Sid      = "DenyTagChangesOutsideCreation"
        Effect   = "Deny"
        Action   = ["ec2:CreateTags", "ec2:DeleteTags"]
        Resource = "*"
        Condition = {
          StringNotEqualsIfExists = { "ec2:CreateAction" = ["RunInstances", "CreateImage"] }
        }
      },
      {
        Sid       = "PassBuilderRoleToEC2Only"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.runner_app_image_builder[0].arn
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
      {
        Sid         = "DenyPassingOtherRoles"
        Effect      = "Deny"
        Action      = "iam:PassRole"
        NotResource = aws_iam_role.runner_app_image_builder[0].arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "runner_app_image" {
  count             = var.enable_github_runner ? 1 : 0
  name              = "/aws/lambda/github-app-runner-image"
  retention_in_days = 90
}

resource "aws_lambda_function" "runner_app_image" {
  count            = var.enable_github_runner ? 1 : 0
  filename         = data.archive_file.runner_app_image.output_path
  source_code_hash = data.archive_file.runner_app_image.output_base64sha256
  function_name    = "github-app-runner-image"
  role             = aws_iam_role.runner_app_image_lambda[0].arn
  handler          = "image_builder.handler"
  runtime          = "python3.12"
  timeout          = 120
  memory_size      = 256

  # One step at a time, so a weekly build and an on-demand one cannot both find no builder and launch two.
  reserved_concurrent_executions = 1

  environment {
    variables = {
      IMAGES = jsonencode({
        for name, image in local.runner_app_images : name => merge(image, { builder_types = local.runner_app_image_builder_types })
      })
      LAUNCH_SUBNETS    = jsonencode([for subnet in local.runner_launch_subnets : { subnet_id = subnet.id, availability_zone = subnet.availability_zone }])
      SECURITY_GROUP_ID = aws_security_group.runner_app[0].id
      INSTANCE_PROFILE  = aws_iam_instance_profile.runner_app_image_builder[0].name
      ACCOUNT_ID        = data.aws_caller_identity.current.account_id
      KEEP_IMAGES       = "3"
      BUILDER_VOLUME_GB = tostring(local.runner_app_image_builder_volume_gb)
    }
  }

  tags = { Name = "github-app-runner-image" }

  depends_on = [aws_iam_role_policy.runner_app_image_lambda, aws_cloudwatch_log_group.runner_app_image]
}

resource "aws_lambda_function_event_invoke_config" "runner_app_image" {
  count                        = var.enable_github_runner ? 1 : 0
  function_name                = aws_lambda_function.runner_app_image[0].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

resource "aws_cloudwatch_event_rule" "runner_app_image_weekly" {
  count               = var.enable_github_runner ? 1 : 0
  name                = "github-app-runner-image-weekly"
  description         = "Build every prebuilt app runner image afresh"
  schedule_expression = "cron(0 9 ? * MON *)"
}

resource "aws_cloudwatch_event_target" "runner_app_image_weekly" {
  count = var.enable_github_runner ? 1 : 0
  rule  = aws_cloudwatch_event_rule.runner_app_image_weekly[0].name
  arn   = aws_lambda_function.runner_app_image[0].arn
  input = jsonencode({ build = true })
}

resource "aws_lambda_permission" "runner_app_image_weekly" {
  count         = var.enable_github_runner ? 1 : 0
  statement_id  = "AllowEventBridgeWeeklyBuild"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.runner_app_image[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.runner_app_image_weekly[0].arn
}

resource "aws_cloudwatch_event_rule" "runner_app_image_advance" {
  count               = var.enable_github_runner ? 1 : 0
  name                = "github-app-runner-image-advance"
  description         = "Image finished app runner image builders and prune old images"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "runner_app_image_advance" {
  count = var.enable_github_runner ? 1 : 0
  rule  = aws_cloudwatch_event_rule.runner_app_image_advance[0].name
  arn   = aws_lambda_function.runner_app_image[0].arn
  input = jsonencode({})
}

resource "aws_lambda_permission" "runner_app_image_advance" {
  count         = var.enable_github_runner ? 1 : 0
  statement_id  = "AllowEventBridgeAdvance"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.runner_app_image[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.runner_app_image_advance[0].arn
}

# A failed build (a builder that stopped without completing, timed out, or whose image failed) prints the builder's
# console to the function's log and fails the invocation. Repos keep launching the newest image that did build.
resource "aws_cloudwatch_metric_alarm" "runner_app_image_errors" {
  count               = var.enable_github_runner ? 1 : 0
  alarm_name          = "github-app-runner-image-errors"
  alarm_description   = "A prebuilt app runner image failed to build (its console is in /aws/lambda/github-app-runner-image); repos keep the newest image that did build"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.runner_app_image[0].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

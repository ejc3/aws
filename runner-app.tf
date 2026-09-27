# runner-app.tf
#
# Self-hosted runners for repos other than ejc3/fcvm (step 2; step 1 is runner-repos.tf, the
# per-repo controller tokens). ejc3/fcvm keeps its own metal controller in runner-autoscale.tf,
# untouched. These repos get ephemeral x86 SPOT VMs from a separate, much smaller controller:
#
#   GitHub workflow_job --> front (runner-webhook-front.tf, routes by repository.full_name)
#                              |-- ejc3/fcvm --> github-runner-webhook (metal, unchanged)
#                              '-- served repo --> github-app-runner (this file)
#   EventBridge, every 2 min --> github-app-runner reconcile
#
# The policy lives in runner-app/app_runner.py: one runner per queued job, no reuse, reap what
# never registered, sat idle or outlived any job. A job opts in with
#
#   runs-on: [self-hosted, <repo label>, <size>]      # e.g. [self-hosted, cc-games, xl]
#
# and nothing else; a job asking for any other label is never served here.
#
# SIZES. Diversified x86 compute pools, tried in order, each subnet in turn. x86 because every
# one of these pools scored 9/10 for spot placement in us-west-1 against 3/10 for arm64
# (2026-09-27), and the repos' toolchains (Playwright webkit, semgrep, SwiftShader) are known
# good there. c7a first: its vCPUs are whole cores, and this work is CPU-bound.
locals {
  runner_app_sizes = {
    xl = ["c7a.16xlarge", "c7i.16xlarge", "c8i.16xlarge", "c6a.16xlarge", "c6i.16xlarge"]
    l  = ["c7a.8xlarge", "c7i.8xlarge", "c8i.8xlarge", "c6a.8xlarge", "c6i.8xlarge"]
    s  = ["c7a.2xlarge", "c7i.2xlarge", "c8i.2xlarge", "c6a.2xlarge", "c6i.2xlarge"]
  }

  # One entry per served repo. `max` bounds that repo's concurrent hosts; it is separate from
  # fcvm's four per architecture, so these repos can never take fcvm's metal.
  runner_app_repos = {
    "CoderColton/colton-games"     = { label = "cc-games", max = 8 }
    "dolphin-labs-hq/dolphin-labs" = { label = "dolphin", max = 8 }
  }

  runner_app_config = {
    for repo, c in local.runner_app_repos : repo => {
      label      = c.label
      max        = c.max
      pat_secret = "github-runner/repo-pat/${repo}"
      sizes      = local.runner_app_sizes
    }
  }
  runner_app_max_total = sum([for c in values(local.runner_app_repos) : c.max])

  # Canonical's own account. Only its images may be launched by this controller.
  runner_app_ami_owner = "099720109477"
}

# ============================================ network
# No inbound at all: a runner only dials out (GitHub, package mirrors). Nothing may SSH in,
# not even another runner; operators use SSM Session Manager.
resource "aws_security_group" "runner_app" {
  count       = var.enable_github_runner ? 1 : 0
  name        = "github-app-runner-sg"
  description = "Ephemeral app-repo runners: no inbound, outbound internet"
  vpc_id      = aws_vpc.runner[0].id

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
    description      = "Internet access"
  }

  tags = { Name = "github-app-runner-sg" }
}

# ============================================ controller
data "archive_file" "runner_app" {
  type        = "zip"
  output_path = "${path.module}/.terraform/github-app-runner.zip"

  source {
    content  = file("${path.module}/runner-app/app_runner.py")
    filename = "app_runner.py"
  }
  source {
    content  = file("${path.module}/runner-app/bootstrap.sh")
    filename = "bootstrap.sh"
  }
}

resource "aws_iam_role" "runner_app_lambda" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-app-runner-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })
}

# Launch, tag and terminate only github-app-runner instances, only from Canonical's images,
# only into the runner subnets with github-app-runner-sg and github-runner-profile (whose role
# cannot read any token). Broker a host's own bootstrap credential exactly as fcvm's controller
# does. Read only the served repos' controller tokens.
resource "aws_iam_role_policy" "runner_app_lambda" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-app-runner"
  role  = aws_iam_role.runner_app_lambda[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "OwnLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = [
          "arn:aws:logs:us-west-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/github-app-runner",
          "arn:aws:logs:us-west-1:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/github-app-runner:*",
        ]
      },
      {
        Sid      = "ReadServedRepoTokens"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = [for repo in local.runner_extra_repos : aws_secretsmanager_secret.github_runner_repo_pat[repo].arn]
      },
      {
        Sid      = "ResolveUbuntuImage"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = "arn:aws:ssm:us-west-1::parameter/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
      },
      {
        Sid      = "DescribeInstances"
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances", "ec2:DescribeImages"]
        Resource = "*"
      },
      {
        Sid      = "LaunchCanonicalImagesOnly"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1::image/*"
        Condition = {
          StringEquals = { "ec2:Owner" = local.runner_app_ami_owner }
        }
      },
      {
        Sid      = "LaunchIntoRunnerSubnetsWithAppGroup"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = concat(local.runner_launch_subnet_arns, [aws_security_group.runner_app[0].arn])
      },
      {
        Sid      = "LaunchTaggedAppInstance"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = { "aws:RequestTag/Role" = "github-app-runner", "ec2:MetadataHttpTokens" = "required" }
          ArnEquals    = { "ec2:InstanceProfile" = aws_iam_instance_profile.runner[0].arn }
        }
      },
      {
        Sid      = "LaunchTaggedAppENI"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:network-interface/*"
        Condition = {
          StringEquals = { "aws:RequestTag/Role" = "github-app-runner" }
          ArnEquals    = { "ec2:Subnet" = local.runner_launch_subnet_arns }
        }
      },
      {
        Sid      = "LaunchEncryptedAppVolume"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:volume/*"
        Condition = {
          StringEquals = { "aws:RequestTag/Role" = "github-app-runner" }
          Bool         = { "ec2:Encrypted" = "true" }
        }
      },
      {
        Sid      = "TagOnlyDuringAppLaunch"
        Effect   = "Allow"
        Action   = "ec2:CreateTags"
        Resource = [for kind in ["instance", "volume", "network-interface"] : "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:${kind}/*"]
        Condition = {
          StringEquals                = { "ec2:CreateAction" = "RunInstances", "aws:RequestTag/Role" = "github-app-runner" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Name", "Role", "Repo", "Size", "JobId", "InspectorEc2Exclusion"] }
        }
      },
      {
        Sid      = "DenyTagChangesOutsideLaunch"
        Effect   = "Deny"
        Action   = ["ec2:CreateTags", "ec2:DeleteTags"]
        Resource = "*"
        Condition = {
          StringNotEqualsIfExists = { "ec2:CreateAction" = "RunInstances" }
        }
      },
      {
        Sid      = "TerminateAppRunnersOnly"
        Effect   = "Allow"
        Action   = "ec2:TerminateInstances"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*"
        Condition = {
          StringEquals = { "aws:ResourceTag/Role" = "github-app-runner" }
        }
      },
      {
        Sid      = "PassRunnerRoleToEC2Only"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.runner[0].arn
        Condition = {
          StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" }
        }
      },
      {
        Sid         = "DenyPassingOtherRoles"
        Effect      = "Deny"
        Action      = "iam:PassRole"
        NotResource = aws_iam_role.runner[0].arn
      },
      {
        # The same instance-bound handoff fcvm uses (runner-bootstrap.tf): created only after
        # launch, tagged with that instance's ARN, which is the only one its role may read.
        Sid      = "BrokerInstanceBoundBootstrapCredential"
        Effect   = "Allow"
        Action   = ["ssm:PutParameter", "ssm:AddTagsToResource"]
        Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
        Condition = {
          StringEquals = { "aws:RequestTag/Role" = "github-runner" }
          StringLike   = { "aws:RequestTag/InstanceArn" = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/i-*" }
        }
      },
      {
        # One launch claim per job (app_runner.py claim()): conditional put, release on failure.
        Sid      = "LaunchClaims"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:Query"]
        Resource = aws_dynamodb_table.runner_app_claims[0].arn
      },
      {
        # Its own runner-count metric, which the alarms below read.
        Sid       = "PublishRunnerCounts"
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "GitHubAppRunner" } }
      },
      {
        Sid      = "DeleteBrokeredCredentialOnly"
        Effect   = "Allow"
        Action   = "ssm:DeleteParameter"
        Resource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
        Condition = {
          StringEquals = { "aws:ResourceTag/Role" = "github-runner" }
        }
      },
    ]
  })
}

resource "aws_lambda_function" "runner_app" {
  count            = var.enable_github_runner ? 1 : 0
  filename         = data.archive_file.runner_app.output_path
  source_code_hash = data.archive_file.runner_app.output_base64sha256
  function_name    = "github-app-runner"
  role             = aws_iam_role.runner_app_lambda[0].arn
  handler          = "app_runner.handler"
  runtime          = "python3.12"
  timeout          = 120

  # One decision at a time, so two deliveries for the same job cannot both launch.
  reserved_concurrent_executions = 1

  environment {
    variables = {
      REPOS             = jsonencode(local.runner_app_config)
      CLAIMS_TABLE      = aws_dynamodb_table.runner_app_claims[0].name
      LAUNCH_SUBNETS    = jsonencode([for subnet in local.runner_launch_subnets : { subnet_id = subnet.id, availability_zone = subnet.availability_zone }])
      SECURITY_GROUP_ID = aws_security_group.runner_app[0].id
      INSTANCE_PROFILE  = aws_iam_instance_profile.runner[0].name
      RUNNER_ACCOUNT_ID = data.aws_caller_identity.current.account_id
      # A VM is registered in about two minutes; metal needed fifteen.
      BOOT_GRACE_MINUTES   = "10"
      IDLE_MINUTES         = "10"
      MAX_LIFETIME_MINUTES = "180"
      VOLUME_GB            = "80"
    }
  }

  tags = { Name = "github-app-runner" }

  depends_on = [aws_iam_role_policy.runner_app_lambda]
}

# Deliveries arrive asynchronously from the front. No retry after an error: a retry after a
# launch could double it, and the two-minute reconcile covers anything dropped.
resource "aws_lambda_function_event_invoke_config" "runner_app" {
  count                        = var.enable_github_runner ? 1 : 0
  function_name                = aws_lambda_function.runner_app[0].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 300
}

resource "aws_cloudwatch_event_rule" "runner_app_reconcile" {
  count               = var.enable_github_runner ? 1 : 0
  name                = "github-app-runner-reconcile"
  description         = "Launch for uncovered queued jobs and reap idle or stuck app runners"
  schedule_expression = "rate(2 minutes)"
}

resource "aws_cloudwatch_event_target" "runner_app_reconcile" {
  count = var.enable_github_runner ? 1 : 0
  rule  = aws_cloudwatch_event_rule.runner_app_reconcile[0].name
  arn   = aws_lambda_function.runner_app[0].arn
  input = jsonencode({ reconcile = true })
}

resource "aws_lambda_permission" "runner_app_reconcile" {
  count         = var.enable_github_runner ? 1 : 0
  statement_id  = "AllowEventBridgeReconcile"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.runner_app[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.runner_app_reconcile[0].arn
}

# ============================================ alarms
# Separate from too-many-runners (which counts Role=github-runner, fcvm's metal only).
# One claim per job (app_runner.py claim()). Items expire by their own `expires_at`; TTL only
# garbage-collects them a day later.
resource "aws_dynamodb_table" "runner_app_claims" {
  count        = var.enable_github_runner ? 1 : 0
  name         = "github-app-runner-claims"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "repo"
  range_key    = "job"

  attribute {
    name = "repo"
    type = "S"
  }

  attribute {
    name = "job"
    type = "S"
  }

  ttl {
    attribute_name = "ttl"
    enabled        = true
  }

  point_in_time_recovery {
    enabled = false
  }

  tags = { Name = "github-app-runner-claims" }
}

# The controller publishes GitHubAppRunner/LiveRunners (Repo=ALL and per repo) on every
# 2-minute reconcile. AWS/EC2 has no per-tag instance count, so this is the only reliable one.
resource "aws_cloudwatch_metric_alarm" "too_many_app_runners" {
  count               = var.enable_github_runner ? 1 : 0
  alarm_name          = "too-many-app-runners"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3 # 15 minutes
  threshold           = local.runner_app_max_total
  alarm_description   = "More app-repo runners than every repo's cap allows, for 15+ minutes - the controller is over-launching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  namespace           = "GitHubAppRunner"
  metric_name         = "LiveRunners"
  dimensions          = { Repo = "ALL" }
  statistic           = "Maximum"
  period              = 300
  treat_missing_data  = "notBreaching"
}

# No count at all for 15 minutes means the reconcile is not running, so nothing reaps hosts
# or enforces lifetimes. Missing data is the signal here.
resource "aws_cloudwatch_metric_alarm" "app_runner_reconcile_silent" {
  count               = var.enable_github_runner ? 1 : 0
  alarm_name          = "github-app-runner-reconcile-silent"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  threshold           = 0
  alarm_description   = "github-app-runner published no runner count for 15 minutes: its reconcile (reaping, lifetimes) is not running"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
  namespace           = "GitHubAppRunner"
  metric_name         = "LiveRunners"
  dimensions          = { Repo = "ALL" }
  statistic           = "SampleCount"
  period              = 300
  treat_missing_data  = "breaching"
}

resource "aws_cloudwatch_metric_alarm" "runner_app_errors" {
  count               = var.enable_github_runner ? 1 : 0
  alarm_name          = "github-app-runner-errors"
  alarm_description   = "github-app-runner raised or timed out; app-repo jobs may be waiting for runners"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.runner_app[0].function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# ============================================ GitHub side
# One workflow_job hook per repo, through the same API Gateway URL and with the same HMAC secret
# as fcvm's hook; the front tells them apart by repository.full_name. Each hook is created with
# that repo's own controller token (Webhooks RW, runner-repos.tf), read ephemerally so it never
# enters state. A provider cannot for_each, hence one alias per owner.
#
# Gated separately because the tokens are put into their secrets by hand after runner-repos.tf
# applies: until then there is no secret version to read.
variable "enable_runner_app_webhooks" {
  description = "Create the workflow_job webhooks on the served repos (needs their controller tokens in Secrets Manager)"
  type        = bool
  default     = true
}

locals {
  runner_app_webhooks = var.enable_github_runner && var.enable_runner_app_webhooks
}

ephemeral "aws_secretsmanager_secret_version" "runner_repo_pat" {
  for_each  = local.runner_app_webhooks ? toset(local.runner_extra_repos) : toset([])
  secret_id = aws_secretsmanager_secret.github_runner_repo_pat[each.value].id
}

provider "github" {
  alias = "colton_games"
  owner = "CoderColton"
  token = local.runner_app_webhooks ? ephemeral.aws_secretsmanager_secret_version.runner_repo_pat["CoderColton/colton-games"].secret_string : null
}

provider "github" {
  alias = "dolphin_labs"
  owner = "dolphin-labs-hq"
  token = local.runner_app_webhooks ? ephemeral.aws_secretsmanager_secret_version.runner_repo_pat["dolphin-labs-hq/dolphin-labs"].secret_string : null
}

resource "github_repository_webhook" "runner_app_colton_games" {
  count      = local.runner_app_webhooks ? 1 : 0
  provider   = github.colton_games
  repository = "colton-games"
  events     = ["workflow_job"]
  active     = true

  configuration {
    url          = "${aws_apigatewayv2_api.runner_webhook[0].api_endpoint}/webhook"
    content_type = "json"
    insecure_ssl = false
    secret       = random_password.github_webhook[0].result
  }

  depends_on = [aws_lambda_function.runner_app, aws_lambda_function.runner_webhook_front]
}

resource "github_repository_webhook" "runner_app_dolphin_labs" {
  count      = local.runner_app_webhooks ? 1 : 0
  provider   = github.dolphin_labs
  repository = "dolphin-labs"
  events     = ["workflow_job"]
  active     = true

  configuration {
    url          = "${aws_apigatewayv2_api.runner_webhook[0].api_endpoint}/webhook"
    content_type = "json"
    insecure_ssl = false
    secret       = random_password.github_webhook[0].result
  }

  depends_on = [aws_lambda_function.runner_app, aws_lambda_function.runner_webhook_front]
}

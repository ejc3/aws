# games-multiplayer-observe.tf
#
# Read-only view of the games deploy pipeline for the dev boxes, so whoever pushes to
# colton-games can check what AWS did with it (ejc3/aws#194) without an administrator:
# which builds ran and how they ended, their logs and the functions' logs, the engine
# images in ECR, the task definitions the release registered, the release table
# (current#, sim#, build#, router#, schema# -- the schema revision is in schema#main), the
# running tasks, and the games alarms. Plus one secret, the mp test key, so the dev box can run
# the live smoke (scripts/mp-e2e.mjs --remote) itself: it only opens the hidden games in
# production, and the owner chose to let everyone on these boxes use it.
#
# Deliberately NOT here, because each can carry a credential or change something:
#   - any other secret or ssm parameter (the DB URL, the cron secret, Vercel and GitHub tokens);
#   - lambda:GetFunction* (returns the functions' environment);
#   - codebuild:BatchGetProjects (returns the projects' environment);
#   - ecr image pulls, and anything that starts, retries, stops, invokes or writes;
#   - Terraform state and its lock table (dev boxes never read state: it holds credentials).
# BatchGetBuilds returns a build's environment too: every value there is a plain setting
# (channel, repository, sizes) or the commit, and secrets reach builds only as
# SECRETS_MANAGER/PARAMETER_STORE references, which it returns as names, not values.
# scripts/test-games-mp-observe.py pins all of this.
#
# Both dev roles get it: the metal boxes (dev-server-role) and nextjs-dev, where every
# account has sudo, so this is a grant to every person on that box. Nothing in it is more
# than the pipeline's own status and logs.
data "aws_iam_policy_document" "games_mp_observe" {
  statement {
    sid     = "ReadGamesBuilds"
    actions = ["codebuild:BatchGetBuilds", "codebuild:ListBuildsForProject"]
    # ListBuildsForProject is authorized on the project, BatchGetBuilds on its builds
    # (build/<project>:<id>).
    resources = flatten([for p in [
      aws_codebuild_project.games_mp_images,
      aws_codebuild_project.games_mp_images_preview,
      aws_codebuild_project.games_mp_migrate,
    ] : [p.arn, "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:build/${p.name}:*"]])
  }

  statement {
    sid     = "ReadGamesLogs"
    actions = ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams"]
    resources = flatten([for arn in concat(
      [
        aws_cloudwatch_log_group.games_mp_codebuild.arn,
        aws_cloudwatch_log_group.games_mp_codebuild_preview.arn,
        aws_cloudwatch_log_group.games_mp_migrate.arn,
        aws_cloudwatch_log_group.games_mp_poller.arn,
        aws_cloudwatch_log_group.games_mp_release.arn,
        aws_cloudwatch_log_group.games_mp_sweeper.arn,
        aws_cloudwatch_log_group.games_mp_router.arn,
        aws_cloudwatch_log_group.games_engines.arn,
      ],
      [for g in aws_cloudwatch_log_group.games_mp_launch : g.arn],
    ) : [arn, "${arn}:*"]])
  }

  statement {
    sid       = "ReadGamesImageTags"
    actions   = ["ecr:DescribeImages", "ecr:ListImages", "ecr:DescribeRepositories"]
    resources = [for r in aws_ecr_repository.games_mp : r.arn]
  }

  statement {
    # ECS has no resource-level permissions for these. Every family in this account is a
    # games one, and none carries a secret value: secrets are `secrets` references.
    sid       = "ReadTaskDefinitions"
    actions   = ["ecs:DescribeTaskDefinition", "ecs:ListTaskDefinitions", "ecs:ListTaskDefinitionFamilies"]
    resources = ["*"]
  }

  statement {
    sid       = "ListGamesTasks"
    actions   = ["ecs:ListTasks", "ecs:ListServices"]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.games.arn]
    }
  }

  statement {
    sid     = "DescribeGamesTasks"
    actions = ["ecs:DescribeTasks", "ecs:DescribeServices"]
    resources = [
      "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.mp_cluster_name}/*",
      "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${local.mp_cluster_name}/*",
    ]
  }

  statement {
    sid       = "ReadReleaseTable"
    actions   = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:DescribeTable"]
    resources = [aws_dynamodb_table.games_mp_releases.arn]
  }

  statement {
    sid       = "ReadGamesAlarms"
    actions   = ["cloudwatch:DescribeAlarms"]
    resources = ["arn:aws:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:games-*"]
  }

  statement {
    # The one secret: MP_TEST_KEY, for the live smoke. Its exact ARN, nothing wider. Its
    # resource policy (games_mp_admin_only in games-multiplayer-bringup.tf) names these two
    # roles too; without that its Deny would override this.
    sid       = "ReadMpTestKey"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.games_mp_test_key.arn]
  }

  statement {
    # Metric reads have no resource-level permissions; they are numbers, never secrets.
    sid       = "ReadMetrics"
    actions   = ["cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "dev_server_games_mp_observe" {
  name   = "games-mp-observe"
  role   = aws_iam_role.dev_server.id
  policy = data.aws_iam_policy_document.games_mp_observe.json
}

resource "aws_iam_role_policy" "nextjs_dev_games_mp_observe" {
  name   = "games-mp-observe"
  role   = aws_iam_role.nextjs_dev.id
  policy = data.aws_iam_policy_document.games_mp_observe.json
}

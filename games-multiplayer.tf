# games-multiplayer.tf
#
# AWS half of the Colton Games multiplayer platform: one Fargate task per match, reached
# through a stateless token router behind an ALB at play.cc-games.app.
#
# The design and the binding names live in the games repo (CoderColton/colton-games):
#   docs/MULTIPLAYER.md           why it is shaped like this
#   docs/MULTIPLAYER-CONTRACT.md  "Fixed names": cluster `games`, task definitions
#                                 `games-<game>`, ECR `games/<game>-engine` and
#                                 `games/mp-router`, service `mp-router`, port 8080,
#                                 https://play.cc-games.app (+ *.play.cc-games.app)
# Changing a name here without changing that file breaks the lobby, the router or the
# image build script. Apply order and costs: docs/games-multiplayer.md.
#
#   browser --wss://play.cc-games.app/m/<id>?t=<token>--> ALB games-play (443, ACM)
#       --> mp-router service (games-router SG; verifies the HMAC token, proxies)
#           --> the match's engine task, private IP from the token (games-engine SG)
#   Vercel lobby --OIDC--> role games-mp-launcher --ecs:RunTask--> engine task
#   EventBridge Scheduler (5 min) --> games-mp-sweeper Lambda --ecs:StopTask--> over-age engines
#
# Everything is in us-west-1, in the existing `main` VPC's public subnets a/b. Tasks get a
# public IPv4 for OUTBOUND only (ECR pulls, callbacks to Vercel): that is ~$3.65/month per
# always-on task against ~$33/month plus data for a NAT gateway. Nothing can connect IN to
# a task except through the security-group chain below.
#
# FIRST APPLY IS SAFE BEFORE ANY IMAGE EXISTS. The router's task definition and service,
# and each engine's task definition, are created only once their image tag variable is
# set. Everything they depend on (ECR, cluster, IAM, ALB, certificate, DNS, secret,
# sweeper) is created unconditionally, so images can be pushed after the first apply.

variable "mp_router_image_tag" {
  description = "Tag of games/mp-router to run (e.g. a git sha). Empty = no router service yet; set it after the first image is pushed."
  type        = string
  default     = ""
}

variable "mp_engine_image_tags" {
  description = "Engine image tag (the game's simVersion) per game id in local.mp_games. A game with no entry has no task definition yet."
  type        = map(string)
  default     = {}
}

variable "mp_router_desired_count" {
  description = "mp-router tasks. One is plenty for a family; the router is stateless, so raising this is the whole scaling story."
  type        = number
  default     = 1
}

variable "mp_env" {
  description = "Default MP_ENV baked into the engine task definitions (the launcher overrides it per match) and the router's primary env. The router accepts the whole of mp_router_envs."
  type        = string
  default     = "production"
}

# ONE router serves every lobby environment. A join token carries `n` (the minting lobby's
# env); the router accepts a token whose n is any value in MP_ENVS. This is a correctness
# guard (a token is only honoured by the deployment family that minted it), NOT a security
# boundary: every lobby that holds MP_TOKEN_KEYS can mint a token with any n. The boundary
# is which Vercel environments get MP_TOKEN_KEYS at all -- Production and Preview only.
#
# development is left out on purpose. A development lobby runs on a laptop from
# `vercel env pull`; if it could mint router-accepted tokens, the signing key would have
# to sit in a .env.local file on that laptop. Local development uses MP_LAUNCHER=local
# and its own throwaway key instead, so it never needs this router. The launcher role
# (games-mp-launcher) trusts the same two environments, so the envs that can start an
# engine and the envs whose players can reach it are one list; keep them in step.
variable "mp_router_envs" {
  description = "Token `n` values the router accepts (MP_ENVS, comma-joined). Must include var.mp_env."
  type        = list(string)
  default     = ["production", "preview"]

  validation {
    condition     = length(var.mp_router_envs) > 0 && alltrue([for e in var.mp_router_envs : can(regex("^[a-z0-9-]+$", e))])
    error_message = "mp_router_envs must be a non-empty list of lowercase env names (no commas)."
  }

  validation {
    condition     = contains(var.mp_router_envs, var.mp_env)
    error_message = "mp_router_envs must include var.mp_env."
  }
}

locals {
  # ADDING A GAME IS ONE ENTRY HERE (plus its image tag in var.mp_engine_image_tags once
  # the image is pushed). The key is the contract's game id: it names the ECR repository
  # `games/<id>-engine`, the task definition `games-<id>`, and the log stream prefix.
  # Sizes are the contract's 2 vCPU / 4 GB; a game may override them if it needs to.
  mp_games = {
    mptest = { cpu = 2048, memory = 4096 }
  }

  mp_cluster_name = "games"
  # The router's task definition family. The sweeper exempts exactly this family and the
  # launcher is denied RunTask on it; one local so the two can never disagree.
  mp_router_family = "games-mp-router"
  mp_port          = 8080

  # Engines launch in exactly these subnets. MP_SUBNETS (the launcher) and MP_TARGET_CIDRS
  # (the router's "a token may only point here" check) both derive from this one list so
  # they cannot drift apart. Narrower than the whole VPC on purpose: the VPC also holds the
  # dev boxes and jumpboxes, and a token (or a stolen token key) must never be able to aim
  # the router at them.
  mp_subnets = [aws_subnet.subnet_a, aws_subnet.subnet_b]

  mp_play_domain = "play.cc-games.app"

  # Browser origins the router accepts (MP_ALLOWED_ORIGINS, comma-separated). The three
  # production spellings, then Vercel preview deployments of the colton-games project,
  # whose URLs look like https://colton-games-<hash>-coltons-projects-7f9a4e8b.vercel.app.
  # COORDINATION WITH server/mp-router: the router must treat `*` in an entry as matching
  # exactly one run of [a-z0-9-] (never a `.`), anchored at both ends. A router that only
  # does exact matching simply never matches the preview entry, which fails closed
  # (preview pages get 403 origin) rather than open.
  mp_allowed_origins = [
    "https://cc-games.app",
    "https://ccgames.app",
    "https://colton-games.vercel.app",
    "https://colton-games-*-coltons-projects-7f9a4e8b.vercel.app",
  ]

  mp_engine_task_defs = {
    for id, cfg in local.mp_games : id => cfg
    if lookup(var.mp_engine_image_tags, id, "") != ""
  }

  mp_router_enabled = var.mp_router_image_tag != ""

  # Vercel team slug and project. The OIDC issuer, audience and subject all embed these
  # strings; renaming the team or project in Vercel changes the token claims and locks the
  # lobby out until this trust policy is updated (see the Vercel OIDC reference below).
  vercel_team_slug    = "coltons-projects-7f9a4e8b"
  vercel_project_name = "colton-games"
  vercel_oidc_host    = "oidc.vercel.com/${local.vercel_team_slug}"
}

# -------------------------------------------------------------------------------------
# ECR
# -------------------------------------------------------------------------------------
#
# IMMUTABLE tags on both repositories. An engine tag IS the game's simVersion: clients
# match only against equal simVersions and a match launches the image with that tag, so a
# tag that could be re-pushed would let two matches of the "same" version run different
# code, and a running match's image could change under a reconnect. For the router, the
# tag is what var.mp_router_image_tag pins, and a rollback to a previous tag must mean the
# previous bytes. The cost: an engine fix that does not change simulation behaviour still
# needs a new tag (bump simVersion, or the build script uses `<simVersion>-<n>`), and the
# build script must treat "tag already exists" as "already pushed", not an error.
#
# Scan on push is the free basic scan. Keeping the last 20 images bounds storage (a few
# cents) while leaving far more history than any rollback or simVersion overlap needs.

resource "aws_ecr_repository" "games_mp" {
  for_each = toset(concat([for id in keys(local.mp_games) : "games/${id}-engine"], ["games/mp-router"]))

  name                 = each.key
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = each.key, Project = "games-multiplayer" }
}

resource "aws_ecr_lifecycle_policy" "games_mp" {
  for_each   = aws_ecr_repository.games_mp
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 20 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 20
      }
      action = { type = "expire" }
    }]
  })
}

# -------------------------------------------------------------------------------------
# Cluster and logs
# -------------------------------------------------------------------------------------

resource "aws_ecs_cluster" "games" {
  name = local.mp_cluster_name

  # Container Insights bills per task-metric; with a task per match it would cost more
  # than the router. Task logs below plus the sweeper's one-line-per-run summary are
  # enough to see what ran.
  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = { Name = local.mp_cluster_name, Project = "games-multiplayer" }
}

# FARGATE is the default. FARGATE_SPOT is registered so the launcher CAN choose it
# (capacityProviderStrategy on RunTask) for bot-only or test matches; it is not the
# default because a Spot reclaim gives two minutes' notice and ends a live match.
resource "aws_ecs_cluster_capacity_providers" "games" {
  cluster_name       = aws_ecs_cluster.games.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_cloudwatch_log_group" "games_engines" {
  name              = "/games/engines"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "games_mp_router" {
  name              = "/games/mp-router"
  retention_in_days = 14
}

# -------------------------------------------------------------------------------------
# Token key secret
# -------------------------------------------------------------------------------------
#
# Container only; EJ sets the value outside Terraform so it never enters state (the same
# pattern as vercel_api_token in vercel.tf). The value is the contract's MP_TOKEN_KEYS:
#
#   kid1:<base64 of 32+ random bytes>[,kid2:<...>]
#
# The first key signs, every listed key verifies. Rotate by prepending a new key, waiting
# for both the lobby and the router to pick it up (tokens live 120 s), then dropping the
# old one. The SAME string goes into the Vercel project's MP_TOKEN_KEYS env var: the lobby
# signs with it, the router verifies with it.
#
# Set it from the jumpbox (the key goes through stdin, never argv, which every local user
# can read in /proc/<pid>/cmdline):
#
#   printf 'kid1:%s' "$(openssl rand -base64 32)" | aws secretsmanager put-secret-value \
#     --region us-west-1 --secret-id games/mp-token-keys --secret-string file:///dev/stdin
#
# The router reads it only at task start (ECS injects it), so after changing it run
# `aws ecs update-service --cluster games --service mp-router --force-new-deployment`
# (a documented operational action, like a reboot; it changes no managed configuration).
#
# No prevent_destroy: unlike the one-time Cloudflare tokens, this key is minted locally
# and every token it signs expires in two minutes, so losing it costs one re-mint.

resource "aws_secretsmanager_secret" "games_mp_token_keys" {
  name                    = "games/mp-token-keys"
  description             = "MP_TOKEN_KEYS for the games mp-router: kid1:<base64 32+ bytes>[,kid2:...]. Value set outside Terraform."
  recovery_window_in_days = 7
  tags                    = { Name = "games/mp-token-keys", Managed = "terraform", Project = "games-multiplayer" }
}

# Only administration and the router's execution role may read it. Anyone holding this
# key can mint join tokens for any match, so a dev box must not be able to read it.
resource "aws_secretsmanager_secret_policy" "games_mp_token_keys" {
  secret_arn = aws_secretsmanager_secret.games_mp_token_keys.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheRouterCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.games_mp_token_keys.arn
      Condition = {
        ArnNotLike = {
          "aws:PrincipalArn" = [
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
            aws_iam_role.jumpbox_admin[0].arn,
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*",
            "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AdministratorAccess_*",
            aws_iam_role.games_mp_router_execution.arn,
          ]
        }
      }
    }]
  })
}

# -------------------------------------------------------------------------------------
# Task IAM
# -------------------------------------------------------------------------------------
#
# TWO execution roles, not one. The launcher (Vercel) must be able to pass the engine's
# execution role, and RunTask lets the caller override executionRoleArn. If engines and
# the router shared one execution role, the role Vercel can pass would also be the role
# that can read the token key. Split, the role Vercel can pass can pull images and write
# engine logs and nothing else.

locals {
  ecs_tasks_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      # Confused-deputy guard: only ECS acting for THIS account may assume these roles.
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*" }
      }
    }]
  })
}

resource "aws_iam_role" "games_engine_execution" {
  name               = "games-engine-execution"
  description        = "ECS execution role for match engines: pull engine images, write /games/engines. No secrets."
  assume_role_policy = local.ecs_tasks_trust
}

resource "aws_iam_role_policy" "games_engine_execution" {
  name = "pull-and-log"
  role = aws_iam_role.games_engine_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # GetAuthorizationToken has no resource-level scope.
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = [for name, repo in aws_ecr_repository.games_mp : repo.arn if name != "games/mp-router"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.games_engines.arn}:*"
      },
    ]
  })
}

resource "aws_iam_role" "games_mp_router_execution" {
  name               = "games-mp-router-execution"
  description        = "ECS execution role for mp-router: pull its image, write /games/mp-router, inject MP_TOKEN_KEYS."
  assume_role_policy = local.ecs_tasks_trust
}

resource "aws_iam_role_policy" "games_mp_router_execution" {
  name = "pull-log-and-token-key"
  role = aws_iam_role.games_mp_router_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = aws_ecr_repository.games_mp["games/mp-router"].arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.games_mp_router.arn}:*"
      },
      {
        # The secret uses the AWS-managed aws/secretsmanager key, whose key policy already
        # lets account principals decrypt through Secrets Manager: no kms:Decrypt needed.
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.games_mp_token_keys.arn
      },
    ]
  })
}

# Engines get NO AWS permissions. They talk only to Vercel (HTTPS, per-match secret) and
# to players via the router. The role exists so the task definition names a role the
# launcher is allowed to pass, rather than letting a RunTask override pick one.
resource "aws_iam_role" "games_engine_task" {
  name               = "games-engine-task"
  description        = "Match engine task role. Deliberately has no policies."
  assume_role_policy = local.ecs_tasks_trust
}

# The router needs no AWS API either: its key arrives as an env var from the execution
# role. Kept as its own empty role so a future need (e.g. reading a second secret at
# runtime) is added here without touching what engines can do.
resource "aws_iam_role" "games_mp_router_task" {
  name               = "games-mp-router-task"
  description        = "mp-router task role. Deliberately has no policies."
  assume_role_policy = local.ecs_tasks_trust
}

# -------------------------------------------------------------------------------------
# Vercel OIDC and the launcher role
# -------------------------------------------------------------------------------------
#
# The lobby (Vercel functions) gets AWS credentials by exchanging its Vercel OIDC token
# with sts:AssumeRoleWithWebIdentity: no AWS keys are stored in Vercel.
#
# Docs: https://vercel.com/docs/oidc/aws and https://vercel.com/docs/oidc/reference
# (checked 2026-09-26). In the project's default TEAM issuer mode:
#   iss = https://oidc.vercel.com/<team slug>
#   aud = https://vercel.com/<team slug>          (the default audience)
#   sub = owner:<team slug>:project:<project name>:environment:<environment>
# where <environment> is development, preview, production or a Custom Environment slug.
#
# THE LAUNCHER MUST USE THE DEFAULT AUDIENCE. Vercel's own examples pass
# `audience: 'sts.amazonaws.com'` to awsCredentialsProvider; that exchanges the token for
# one with a different aud, which this provider does not list and STS will reject. Call
# awsCredentialsProvider({ roleArn }) with no audience. The project must also have "Secure
# backend access with OIDC federation" enabled in Team issuer mode (Project Settings ->
# Security); in Global mode the issuer is plain https://oidc.vercel.com and this provider
# would not match.
#
# thumbprint_list is omitted: IAM now validates OIDC providers against its own trusted CA
# store and fills the thumbprint in itself (the argument is optional in provider v6).

resource "aws_iam_openid_connect_provider" "vercel" {
  url            = "https://${local.vercel_oidc_host}"
  client_id_list = ["https://vercel.com/${local.vercel_team_slug}"]
  tags           = { Name = "vercel-${local.vercel_team_slug}", Project = "games-multiplayer" }
}

# TRUST: exactly the colton-games project in this team, for exactly two environments.
# Both conditions are StringEquals against full values, not StringLike, so no other project
# in the team, no Custom Environment, and no other Vercel team can assume the role.
#   production  - cc-games.app
#   preview     - PR and branch deployments (they launch real tasks; the sweeper and the
#                 per-task hard cap bound what a bad preview can cost)
# development is deliberately NOT trusted. Its tokens are what `vercel env pull` hands
# any team member (valid 12 h), so trusting it would let a laptop start real Fargate
# tasks. Local development uses MP_LAUNCHER=local instead, and the router does not accept
# development tokens either (var.mp_router_envs), so the two lists match.
resource "aws_iam_role" "games_mp_launcher" {
  name        = "games-mp-launcher"
  description = "Assumed by the colton-games Vercel project via OIDC to start and stop match engines"
  # A lobby request is short; one hour is the minimum and plenty.
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.vercel.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.vercel_oidc_host}:aud" = "https://vercel.com/${local.vercel_team_slug}"
          "${local.vercel_oidc_host}:sub" = [
            for env in ["production", "preview"] :
            "owner:${local.vercel_team_slug}:project:${local.vercel_project_name}:environment:${env}"
          ]
        }
      }
    }]
  })
  tags = { Name = "games-mp-launcher", Project = "games-multiplayer" }
}

resource "aws_iam_role_policy" "games_mp_launcher" {
  name = "run-and-stop-match-engines"
  role = aws_iam_role.games_mp_launcher.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Any revision of any games-<game> engine task definition, only on this cluster.
        Sid      = "RunEngineTasks"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/games-*:*"
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn }
        }
      },
      {
        # games-* also matches the router's task definition, whose execution role injects
        # the token key. RunTask on it already fails because the launcher cannot pass the
        # router's roles, but that is an indirect guarantee; this makes it explicit.
        Sid      = "NeverRunTheRouter"
        Effect   = "Deny"
        Action   = "ecs:RunTask"
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${local.mp_router_family}:*"
      },
      {
        # RunTask requires PassRole for the task definition's roles and for any override.
        # Only the two engine roles, and only to ECS tasks.
        Sid      = "PassOnlyEngineRoles"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [aws_iam_role.games_engine_task.arn, aws_iam_role.games_engine_execution.arn]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
      {
        # RunTask with `tags` also authorises ecs:TagResource on the new task. Allowed only
        # as part of RunTask, so the launcher cannot retag existing tasks (e.g. raise a
        # running task's `hardcap` to dodge the sweeper).
        Sid      = "TagTasksAtLaunch"
        Effect   = "Allow"
        Action   = "ecs:TagResource"
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.mp_cluster_name}/*"
        Condition = {
          StringEquals = { "ecs:CreateAction" = "RunTask" }
        }
      },
      {
        # Read-only, so cluster-wide: the lobby may look at any task here, router included.
        Sid       = "DescribeTasksInThisCluster"
        Effect    = "Allow"
        Action    = "ecs:DescribeTasks"
        Resource  = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.mp_cluster_name}/*"
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn } }
      },
      {
        # StopTask reaches ENGINE tasks only: ones in this cluster that carry the `match`
        # tag, which the lobby sets on every RunTask, so its own cleanup of its own tasks
        # works. Router tasks are started by the ECS service, never carry `match`, and so
        # are outside this Allow. ecs:StopTask supports exactly two condition keys,
        # aws:ResourceTag/${TagKey} and ecs:cluster, on the `task` resource
        # (https://docs.aws.amazon.com/service-authorization/latest/reference/list_amazonelasticcontainerservice.html,
        # machine-readable: https://servicereference.us-east-1.amazonaws.com/v1/ecs/ecs.json,
        # checked 2026-09-27). The launcher cannot add `match` to an existing task: its
        # only TagResource grant is at RunTask creation (TagTasksAtLaunch), and router tasks
        # are never created by its RunTask.
        Sid      = "StopOnlyMatchEngines"
        Effect   = "Allow"
        Action   = "ecs:StopTask"
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.mp_cluster_name}/*"
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn }
          Null      = { "aws:ResourceTag/match" = "false" }
        }
      },
      {
        # Belt and braces for the router: its service propagates the service tag
        # games-role=router onto every task it starts (aws_ecs_service.games_mp_router), and
        # this Deny wins over any Allow. It keys on our own tag rather than the ECS-managed
        # aws:ecs:serviceName tag because AWS does not document that aws:-prefixed managed
        # tags are evaluated as aws:ResourceTag conditions; a condition on a key IAM never
        # sees would silently match nothing. The launcher could stamp games-role on a task
        # it launches itself, but that only stops IT from stopping that task; the sweeper
        # has no such Deny and still reaps it.
        Sid      = "NeverStopTheRouter"
        Effect   = "Deny"
        Action   = ["ecs:StopTask", "ecs:TagResource", "ecs:UntagResource"]
        Resource = "*"
        Condition = {
          StringEquals = { "aws:ResourceTag/games-role" = "router" }
        }
      },
      {
        # ListTasks is authorised against the cluster via the ecs:cluster condition.
        Sid      = "ListTasksInThisCluster"
        Effect   = "Allow"
        Action   = "ecs:ListTasks"
        Resource = "*"
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn }
        }
      },
      {
        # To read a task's private IP from its ENI when the engine does not report it.
        # EC2 Describe* has no resource-level scope; the region is the only narrowing.
        Sid       = "ReadTaskNetworkInterfaces"
        Effect    = "Allow"
        Action    = "ec2:DescribeNetworkInterfaces"
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
    ]
  })
}

# -------------------------------------------------------------------------------------
# Security groups: internet -> ALB -> router -> engine
# -------------------------------------------------------------------------------------
#
# The chain is the engines' whole access control. An engine trusts the X-MP-Player and
# X-MP-Seat headers without checking a signature, which is safe ONLY because nothing but
# the router can open a connection to it. Never add another ingress rule to games-engine.
#
# Rules are separate aws_vpc_security_group_*_rule resources (not inline blocks) because
# the router's egress references the engine group while the engine's ingress references
# the router group; inline blocks would make that a dependency cycle.

resource "aws_security_group" "games_alb" {
  name        = "games-alb"
  description = "games-play ALB: HTTP/HTTPS from anywhere"
  vpc_id      = local.vpc_id
  tags        = { Name = "games-alb", Project = "games-multiplayer" }
}

resource "aws_vpc_security_group_ingress_rule" "games_alb" {
  for_each = {
    http4  = { port = 80, cidr4 = "0.0.0.0/0", cidr6 = null }
    https4 = { port = 443, cidr4 = "0.0.0.0/0", cidr6 = null }
    http6  = { port = 80, cidr4 = null, cidr6 = "::/0" }
    https6 = { port = 443, cidr4 = null, cidr6 = "::/0" }
  }
  security_group_id = aws_security_group.games_alb.id
  ip_protocol       = "tcp"
  from_port         = each.value.port
  to_port           = each.value.port
  cidr_ipv4         = each.value.cidr4
  cidr_ipv6         = each.value.cidr6
  description       = "players (${each.key}); port 80 only redirects to 443"
}

resource "aws_vpc_security_group_egress_rule" "games_alb" {
  for_each          = { v4 = { cidr4 = "0.0.0.0/0", cidr6 = null }, v6 = { cidr4 = null, cidr6 = "::/0" } }
  security_group_id = aws_security_group.games_alb.id
  ip_protocol       = "-1"
  cidr_ipv4         = each.value.cidr4
  cidr_ipv6         = each.value.cidr6
  description       = "all outbound (${each.key}); in practice only to router targets"
}

resource "aws_security_group" "games_router" {
  name        = "games-router"
  description = "mp-router tasks: 8080 from the games-play ALB only"
  vpc_id      = local.vpc_id
  tags        = { Name = "games-router", Project = "games-multiplayer" }
}

resource "aws_vpc_security_group_ingress_rule" "games_router_from_alb" {
  security_group_id            = aws_security_group.games_router.id
  referenced_security_group_id = aws_security_group.games_alb.id
  ip_protocol                  = "tcp"
  from_port                    = local.mp_port
  to_port                      = local.mp_port
  description                  = "ALB listener traffic and /healthz checks"
}

# The router's egress is deliberately NOT "anywhere". It proxies to an address taken from
# the token, so its egress is the second fence (after MP_TARGET_CIDRS) around where a
# forged or stolen-key token could send it: engines on 8080, and HTTPS out for its own
# image pull, logs and secret injection. It cannot reach a dev box's SSH or anything else
# in the VPC. (DNS to the VPC resolver is not subject to security groups.)
resource "aws_vpc_security_group_egress_rule" "games_router_to_engines" {
  security_group_id            = aws_security_group.games_router.id
  referenced_security_group_id = aws_security_group.games_engine.id
  ip_protocol                  = "tcp"
  from_port                    = local.mp_port
  to_port                      = local.mp_port
  description                  = "proxy to match engines"
}

resource "aws_vpc_security_group_egress_rule" "games_router_https" {
  for_each          = { v4 = { cidr4 = "0.0.0.0/0", cidr6 = null }, v6 = { cidr4 = null, cidr6 = "::/0" } }
  security_group_id = aws_security_group.games_router.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = each.value.cidr4
  cidr_ipv6         = each.value.cidr6
  description       = "HTTPS out (${each.key}): ECR, CloudWatch Logs, Secrets Manager"
}

resource "aws_security_group" "games_engine" {
  name        = "games-engine"
  description = "Match engine tasks: 8080 from mp-router only"
  vpc_id      = local.vpc_id
  tags        = { Name = "games-engine", Project = "games-multiplayer" }
}

resource "aws_vpc_security_group_ingress_rule" "games_engine_from_router" {
  security_group_id            = aws_security_group.games_engine.id
  referenced_security_group_id = aws_security_group.games_router.id
  ip_protocol                  = "tcp"
  from_port                    = local.mp_port
  to_port                      = local.mp_port
  description                  = "player traffic via mp-router, the ONLY way in"
}

# Engines need the internet: ECR pulls and HTTPS callbacks to the lobby on Vercel, whose
# addresses are not fixed. The public IP is outbound-only because ingress is router-only.
resource "aws_vpc_security_group_egress_rule" "games_engine" {
  for_each          = { v4 = { cidr4 = "0.0.0.0/0", cidr6 = null }, v6 = { cidr4 = null, cidr6 = "::/0" } }
  security_group_id = aws_security_group.games_engine.id
  ip_protocol       = "-1"
  cidr_ipv4         = each.value.cidr4
  cidr_ipv6         = each.value.cidr6
  description       = "all outbound (${each.key}): ECR, Vercel callbacks"
}

# -------------------------------------------------------------------------------------
# ALB, certificate and DNS: play.cc-games.app
# -------------------------------------------------------------------------------------

resource "aws_acm_certificate" "games_play" {
  domain_name               = local.mp_play_domain
  subject_alternative_names = ["*.${local.mp_play_domain}"]
  validation_method         = "DNS"

  tags = { Name = local.mp_play_domain, Project = "games-multiplayer" }

  lifecycle {
    create_before_destroy = true
  }
}

# ACM asks a wildcard and its base name to prove control with the SAME CNAME
# (_<hash>.play.cc-games.app), so both entries of domain_validation_options describe one
# record. Creating it once from the base name's entry avoids a duplicate-record error in
# Cloudflare, and keys only on a name known at plan time. DNS-only: Cloudflare must hand
# ACM the literal CNAME, not proxy it.
locals {
  games_play_validation = one([
    for dvo in aws_acm_certificate.games_play.domain_validation_options : dvo
    if dvo.domain_name == local.mp_play_domain
  ])
}

resource "cloudflare_dns_record" "games_play_acm_validation" {
  zone_id = var.cc_games_app_zone_id
  name    = trimsuffix(local.games_play_validation.resource_record_name, ".")
  type    = local.games_play_validation.resource_record_type
  content = trimsuffix(local.games_play_validation.resource_record_value, ".")
  proxied = false
  ttl     = 300
  comment = "ACM DNS validation for play.cc-games.app and *.play.cc-games.app (games-multiplayer.tf)"
}

resource "aws_acm_certificate_validation" "games_play" {
  certificate_arn         = aws_acm_certificate.games_play.arn
  validation_record_fqdns = [trimsuffix(local.games_play_validation.resource_record_name, ".")]
  depends_on              = [cloudflare_dns_record.games_play_acm_validation]
}

# Dualstack: both subnets carry an IPv6 /64 and the public route table has ::/0 to the
# IGW (main.tf), which is what an internet-facing dualstack ALB requires. The ALB's DNS
# name then answers A and AAAA, so the CNAMEs below give players both.
#
# idle_timeout 3600: the ALB closes a connection that carries no data for this long. A
# live match streams snapshots constantly, but a WebSocket sitting in a results screen or
# a paused custom lobby should not be cut at the 60 s default. 3600 is the ALB maximum
# the design wants; engines end matches well before that anyway.
#
# The router sees the player's address as the LAST X-Forwarded-For entry (the ALB
# appends it); per-IP limits in server/mp-router must use that one, not the first, which
# the client controls.
resource "aws_lb" "games_play" {
  name                       = "games-play"
  internal                   = false
  load_balancer_type         = "application"
  ip_address_type            = "dualstack"
  security_groups            = [aws_security_group.games_alb.id]
  subnets                    = [for s in local.mp_subnets : s.id]
  idle_timeout               = 3600
  drop_invalid_header_fields = true

  tags = { Name = "games-play", Project = "games-multiplayer" }
}

# Router targets. deregistration_delay 30: when a router task is replaced, the ALB stops
# sending it new connections at once and cuts its remaining ones after 30 s. A cut player
# reconnects with a fresh join token (the lobby mints one per request), so a long drain
# would only slow rollouts down.
resource "aws_lb_target_group" "games_mp_router" {
  name                 = "games-mp-router"
  port                 = local.mp_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = local.vpc_id
  deregistration_delay = 30

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "games-mp-router", Project = "games-multiplayer" }
}

resource "aws_lb_listener" "games_play_http" {
  load_balancer_arn = aws_lb.games_play.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      protocol    = "HTTPS"
      port        = "443"
      status_code = "HTTP_301"
    }
  }
}

# TLS 1.2 minimum, TLS 1.3 preferred: AWS's recommended policy for new listeners.
resource "aws_lb_listener" "games_play_https" {
  load_balancer_arn = aws_lb.games_play.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.games_play.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.games_mp_router.arn
  }
}

# DNS-only (grey cloud), like the apex records in vercel-cc-games.tf. Proxying through
# Cloudflare would end TLS at Cloudflare (the ACM certificate would never be seen), add an
# edge hop to every game packet, and subject WebSockets to Cloudflare's own idle and
# connection limits. Players connect straight to the ALB.
resource "cloudflare_dns_record" "games_play" {
  for_each = toset(["play", "*.play"])

  zone_id = var.cc_games_app_zone_id
  name    = each.key
  type    = "CNAME"
  content = aws_lb.games_play.dns_name
  proxied = false
  ttl     = 300
  comment = "games multiplayer entry -> ALB games-play (us-west-1); DNS-only for direct WebSockets"
}

# -------------------------------------------------------------------------------------
# Router service
# -------------------------------------------------------------------------------------
#
# Absent until var.mp_router_image_tag is set. An ECS service pointed at an image that
# does not exist yet would sit in a pull-fail loop (and a task definition with an empty
# tag is not a valid image reference), so the first apply creates everything around the
# router and the second, after the image is pushed, creates the router itself. Until then
# the ALB answers 503 (no healthy targets), which is the honest answer.

resource "aws_ecs_task_definition" "games_mp_router" {
  count = local.mp_router_enabled ? 1 : 0

  family                   = local.mp_router_family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.games_mp_router_execution.arn
  task_role_arn            = aws_iam_role.games_mp_router_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name         = "mp-router"
    image        = "${aws_ecr_repository.games_mp["games/mp-router"].repository_url}:${var.mp_router_image_tag}"
    essential    = true
    portMappings = [{ containerPort = local.mp_port, protocol = "tcp" }]
    environment = [
      { name = "PORT", value = tostring(local.mp_port) },
      # MP_ENVS is what the router checks a token's `n` against (any listed value);
      # MP_ENV stays for logs and for code that wants the primary env.
      { name = "MP_ENV", value = var.mp_env },
      { name = "MP_ENVS", value = join(",", var.mp_router_envs) },
      { name = "MP_ALLOWED_ORIGINS", value = join(",", local.mp_allowed_origins) },
      { name = "MP_TARGET_CIDRS", value = join(",", [for s in local.mp_subnets : s.cidr_block]) },
    ]
    secrets = [
      { name = "MP_TOKEN_KEYS", valueFrom = aws_secretsmanager_secret.games_mp_token_keys.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.games_mp_router.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "router"
      }
    }
    # Time for the router to stop accepting and let in-flight HTTP requests finish.
    stopTimeout = 30
  }])

  tags = { Name = "games-mp-router", Project = "games-multiplayer" }
}

# Zero-downtime rollout: minimum 100% / maximum 200% means ECS starts the new task, waits
# for it to pass the ALB health check, and only then drains the old one. With one task
# there is briefly two. The circuit breaker rolls back to the previous task definition if
# the new one never becomes healthy (a bad image tag, a missing secret value).
resource "aws_ecs_service" "games_mp_router" {
  count = local.mp_router_enabled ? 1 : 0

  name            = "mp-router"
  cluster         = aws_ecs_cluster.games.id
  task_definition = aws_ecs_task_definition.games_mp_router[0].arn
  desired_count   = var.mp_router_desired_count
  # Plain FARGATE, not Spot: a reclaim would drop every live WebSocket at once.
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 30
  enable_ecs_managed_tags            = true
  # Copies the service tags below onto every router task. games-role=router is what the
  # launcher's NeverStopTheRouter Deny keys on; do not drop it or the propagation.
  propagate_tags = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets         = [for s in local.mp_subnets : s.id]
    security_groups = [aws_security_group.games_router.id]
    # Outbound only (ECR, logs, secret): the router SG admits nothing but the ALB.
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.games_mp_router.arn
    container_name   = "mp-router"
    container_port   = local.mp_port
  }

  # ECS refuses to attach a target group that no load balancer uses yet.
  depends_on = [aws_lb_listener.games_play_https]

  tags = { Name = "mp-router", Project = "games-multiplayer", "games-role" = "router" }
}

# -------------------------------------------------------------------------------------
# Engine task definitions: games-<game>
# -------------------------------------------------------------------------------------
#
# One per entry in local.mp_games that has an image tag. The lobby launches these with
# RunTask, adding MATCH_ID, MATCH_SECRET and MP_API as container overrides and tagging the
# task (see the sweeper below).
#
# skip_destroy = true: moving a game to a new simVersion registers a new revision, and the
# old revision stays ACTIVE instead of being deregistered. Clients still on the old
# simVersion can then still be matched onto `games-<game>:<old revision>` while the new
# site rolls out; the lobby adapter maps simVersion -> revision (output
# games_mp_engine_task_definitions). Old revisions cost nothing.

resource "aws_ecs_task_definition" "games_engine" {
  for_each = local.mp_engine_task_defs

  family                   = "games-${each.key}"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = each.value.cpu
  memory                   = each.value.memory
  execution_role_arn       = aws_iam_role.games_engine_execution.arn
  task_role_arn            = aws_iam_role.games_engine_task.arn
  skip_destroy             = true

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name         = "engine"
    image        = "${aws_ecr_repository.games_mp["games/${each.key}-engine"].repository_url}:${var.mp_engine_image_tags[each.key]}"
    essential    = true
    portMappings = [{ containerPort = local.mp_port, protocol = "tcp" }]
    environment = [
      { name = "GAME_ID", value = each.key },
      { name = "PORT", value = tostring(local.mp_port) },
      { name = "MP_ENV", value = var.mp_env },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.games_engines.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = each.key
      }
    }
    # Room to post a result on SIGTERM (StopTask, the sweeper) before SIGKILL.
    stopTimeout = 30
  }])

  tags = { Name = "games-${each.key}", Project = "games-multiplayer", SimVersion = var.mp_engine_image_tags[each.key] }
}

# -------------------------------------------------------------------------------------
# Sweeper: the backstop for engines that do not exit
# -------------------------------------------------------------------------------------
#
# THE RULE THE LOBBY MUST FOLLOW: every RunTask sets tags
#   game    = the game id                    (e.g. mptest)
#   match   = the match id
#   env     = MP_ENV of the launching lobby  (production / preview)
#   hardcap = the match's hard cap in SECONDS (spec limits.hardCapSec)
# The sweeper stops any task in the cluster older than hardcap + 10 min, or
# older than 2 h when hardcap is missing or not a positive integer; hardcap is clamped to
# 4 h. Every task counts except the router's task definition family, games-mp-router,
# which the launcher cannot RunTask; group, startedBy and tags are caller-set on RunTask
# and never exempt anything. A launcher bug that forgets the tags still gets 2 h.
# The engine's own timers should always win; the sweeper firing means an engine hung.
#
# Source: games-multiplayer/sweeper.py (offline test: scripts/test-games-mp-sweeper.py).

data "archive_file" "games_mp_sweeper" {
  type        = "zip"
  source_file = "${path.module}/games-multiplayer/sweeper.py"
  output_path = "${path.module}/.terraform/games-mp-sweeper.zip"
}

resource "aws_cloudwatch_log_group" "games_mp_sweeper" {
  name              = "/aws/lambda/games-mp-sweeper"
  retention_in_days = 14
}

resource "aws_iam_role" "games_mp_sweeper" {
  name = "games-mp-sweeper"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "games_mp_sweeper" {
  name = "sweep-games-cluster"
  role = aws_iam_role.games_mp_sweeper.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "ecs:ListTasks"
        Resource  = "*"
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn } }
      },
      {
        # ListTagsForResource: DescribeTasks include=TAGS reads tags; granting it here keeps a
        # missing tag permission from silently turning every hardcap into "missing".
        Effect   = "Allow"
        Action   = ["ecs:DescribeTasks", "ecs:StopTask", "ecs:ListTagsForResource"]
        Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.mp_cluster_name}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.games_mp_sweeper.arn}:*"
      },
      {
        # Only to report a StopTask that FAILED; routine stops are just logged.
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      },
    ]
  })
}

resource "aws_lambda_function" "games_mp_sweeper" {
  function_name    = "games-mp-sweeper"
  role             = aws_iam_role.games_mp_sweeper.arn
  handler          = "sweeper.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 60
  memory_size      = 128
  filename         = data.archive_file.games_mp_sweeper.output_path
  source_code_hash = data.archive_file.games_mp_sweeper.output_base64sha256

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.games_mp_sweeper.name
  }

  environment {
    variables = {
      CLUSTER           = aws_ecs_cluster.games.name
      GRACE_SEC         = "600"
      DEFAULT_LIMIT_SEC = "7200"
      MAX_HARDCAP_SEC   = "14400"
      # The ONLY exemption: the router's family, which the launcher is denied RunTask on
      # (NeverRunTheRouter).
      ROUTER_FAMILY = local.mp_router_family
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
    }
  }

  tags = { Name = "games-mp-sweeper", Project = "games-multiplayer" }
}

resource "aws_iam_role" "games_mp_sweeper_scheduler" {
  name = "games-mp-sweeper-scheduler"
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

resource "aws_iam_role_policy" "games_mp_sweeper_scheduler" {
  name = "invoke-games-mp-sweeper"
  role = aws_iam_role.games_mp_sweeper_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.games_mp_sweeper.arn
    }]
  })
}

# Every 5 minutes, so a hung engine overstays by at most cap + 10 min + 5 min.
resource "aws_scheduler_schedule" "games_mp_sweeper" {
  name       = "games-mp-sweeper"
  group_name = "default"

  flexible_time_window { mode = "OFF" }

  schedule_expression = "rate(5 minutes)"

  target {
    arn      = aws_lambda_function.games_mp_sweeper.arn
    role_arn = aws_iam_role.games_mp_sweeper_scheduler.arn

    # A missed sweep is replaced by the next one five minutes later; retrying stale ones
    # would only pile invocations up.
    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 300
    }
  }
}

# -------------------------------------------------------------------------------------
# Outputs
# -------------------------------------------------------------------------------------

# Copy these into the colton-games Vercel project's env (all three environments):
#   terraform output -json games_mp_vercel_env
# Also set AWS_REGION=us-west-1 there: Vercel otherwise sets AWS_REGION to the function's
# own region, which can move (https://vercel.com/docs/oidc/aws).
output "games_mp_vercel_env" {
  description = "Vercel env vars for the multiplayer lobby's ECS launcher"
  value = {
    MP_ROLE_ARN     = aws_iam_role.games_mp_launcher.arn
    MP_CLUSTER      = aws_ecs_cluster.games.name
    MP_SUBNETS      = join(",", [for s in local.mp_subnets : s.id])
    MP_ENGINE_SG    = aws_security_group.games_engine.id
    MP_REGION       = var.aws_region
    MP_LAUNCHER     = "ecs"
    MP_PUBLIC_ENTRY = "wss://${local.mp_play_domain}"
  }
}

output "games_mp_alb_dns_name" {
  description = "games-play ALB; play.cc-games.app and *.play.cc-games.app CNAME to it"
  value       = aws_lb.games_play.dns_name
}

output "games_mp_ecr_repositories" {
  description = "ECR repository URLs the games repo's image build script pushes to"
  value       = { for name, repo in aws_ecr_repository.games_mp : name => repo.repository_url }
}

output "games_mp_engine_task_definitions" {
  description = "Current task definition (family:revision) per game; older revisions stay ACTIVE"
  value       = { for id, td in aws_ecs_task_definition.games_engine : id => "${td.family}:${td.revision}" }
}

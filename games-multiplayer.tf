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
# image build script. Bring-up, shipping and costs: docs/games-multiplayer.md.
#
# games-multiplayer-bringup.tf is the other half: it builds the images (CodeBuild),
# generates the secrets, writes the Vercel env, applies the Supabase migration and waits
# for a healthy router, so ONE `terraform apply` goes from nothing to a working platform.
#
#   browser --wss://play.cc-games.app/m/<id>?t=<token>--> ALB games-play (443, ACM)
#       --> mp-router service (games-router SG; verifies the HMAC token, proxies)
#           --> the match's engine task, private IP from the token (games-engine SG)
#   Vercel lobby --OIDC--> role games-mp-launcher[-preview] --lambda:InvokeFunction-->
#       games-mp-launch-<production|preview> (admission) --ecs:RunTask--> engine task
#   EventBridge Scheduler (1 min) --> games-mp-sweeper Lambda --ecs:StopTask--> over-age engines
#
# Everything is in us-west-1, in the existing `main` VPC. The ALB sits in the public subnets
# a/b; the router and the engines each have their own subnets (10.0.66-67 and 10.0.64-65, see
# "Network" below) with a route table that has NO route to the I/O box's VPC peer, and the
# engine subnets carry a network ACL that refuses the rest of the VPC. Tasks get a public
# IPv4 for OUTBOUND only (ECR pulls, callbacks to Vercel): that is ~$3.65/month per always-on
# task against ~$33/month plus data for a NAT gateway. Nothing can connect IN to a task
# except through the security-group chain below.
#
# IMAGES COME FROM ONE PINNED COMMIT. var.games_mp_source_ref names a commit of
# CoderColton/colton-games; its image tags are derived from it (router `<sha12>`, engine
# `<simVersion>-<sha12>`, the same scheme as the repo's scripts/mp-images.mjs), CodeBuild
# builds whatever ECR lacks, and the task definitions are created only after that build
# succeeded. Shipping a new version = changing that one variable and applying.

# The pin must include engine-side preview support: engines launched by a preview lobby
# call back its protected *.vercel.app URL with x-vercel-protection-bypass (MP_API_BYPASS,
# passed by the lobby). 38bcb780 (games repo PR #62, mp/preview) has it.
# It must also have the Ed25519 join-token router and engine kit: the task definition gives
# the router MP_TOKEN_PUBLIC_KEYS and no MP_TOKEN_KEYS (an older router refuses to boot
# without the latter), and each games-mp-launch-<environment> gives engines MP_TOKEN_PUBLIC_KEYS and expects them
# to verify the X-MP-Token the router forwards. 1eb83917 (games repo branch
# mp-asymmetric-tokens, PR #65, on top of PR #64) has both. Re-pin to the merged commit when
# that branch merges.
variable "games_mp_source_ref" {
  description = "Full 40-hex commit of CoderColton/colton-games whose multiplayer images (and mp migration) this stack runs."
  type        = string
  default     = "1eb83917d994f362fe1e0efe0e06cd022137781b"

  validation {
    condition     = can(regex("^[0-9a-f]{40}$", var.games_mp_source_ref))
    error_message = "games_mp_source_ref must be a full 40-character lowercase commit sha, not a branch or a short sha."
  }
}

variable "games_mp_sim_versions" {
  description = "Each game's SIM_VERSION at games_mp_source_ref (server/<game>/version.mjs). The build fails if the repo disagrees."
  type        = map(string)
  default     = { mptest = "mptest-1" }

  validation {
    # The lobby's simVersion shape (colton-games lib/multiplayer/games.ts SIM_VERSION, and the
    # SQL), narrowed to a Docker-valid first character: the engine image tag starts with it,
    # and a tag cannot begin with "." or "-".
    condition     = alltrue([for v in values(var.games_mp_sim_versions) : can(regex("^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}$", v))])
    error_message = "Each games_mp_sim_versions value must be 1-64 of A-Z a-z 0-9 . _ -, starting with a letter, digit or _ (it begins the image tag)."
  }
}

# EMERGENCY SWITCH. false = no CodeBuild run during apply; the task definitions then use the
# explicit tags below (which must already be in ECR), exactly like the pre-automation stack.
# Use it when CodeBuild or GitHub is down and a known-good image must be pinned.
variable "games_mp_build" {
  description = "Build images with CodeBuild during apply. false = use mp_router_image_tag / mp_engine_image_tags as given."
  type        = bool
  default     = true
}

variable "mp_router_image_tag" {
  description = "Only when games_mp_build = false: games/mp-router tag to run. Empty = no router service."
  type        = string
  default     = ""
}

variable "mp_engine_image_tags" {
  description = "Only when games_mp_build = false: engine tag per game id, <simVersion>-<sha12>. A game with no entry has no task definition."
  type        = map(string)
  default     = {}

  validation {
    # games-mp-launch reads the simVersion out of the tag (local.mp_engine_sim_versions).
    condition     = alltrue([for t in values(var.mp_engine_image_tags) : t == "" || can(regex("^[A-Za-z0-9_][A-Za-z0-9._-]{0,63}-[0-9a-f]{12}$", t))])
    error_message = "Each mp_engine_image_tags value must be <simVersion>-<sha12> (or empty), as the build tags them."
  }
}

# Launch limits, in one place: the lobby enforces the per-environment caps and per-IP rates
# in SQL at the launch claim (colton-games lib/multiplayer/config.ts), and Terraform writes
# them to its Vercel env (games-multiplayer-bringup.tf) so they are not code defaults. The
# AWS side does not trust the lobby: each environment's games-mp-launch function refuses a
# launch at its share of var.games_mp_engine_ceiling (local.games_mp_launch_environments), the
# sweeper stops the newest engines above the whole ceiling, and an alarm fires when more run
# than these caps allow.
locals {
  games_mp_lobby_max_active = { production = 20, preview = 5 }
  games_mp_ip_max_active    = 3
  games_mp_ip_max_per_hour  = 10
  # Most engines a correctly behaving lobby can have running at once.
  games_mp_lobby_engines_max = sum(values(local.games_mp_lobby_max_active))
}

variable "games_mp_engine_ceiling" {
  description = "Most match engines running at once: split between the production and preview games-mp-launch functions (preview min(8, this), production the rest), which refuse to launch at their share, and the sweeper stops the newest above it. A little above the lobby's caps (they sum to 25), for engines still exiting after their match."
  type        = number
  default     = 30
  validation {
    # 0 is the committed kill switch: both launch functions' shares are 0, so they refuse every
    # launch, and the sweeper stops every engine (never the router) within a minute.
    # Whole numbers only: the Lambdas int() it (or their share) at start-up, so "10.5" would
    # break every call.
    condition     = floor(var.games_mp_engine_ceiling) == var.games_mp_engine_ceiling && var.games_mp_engine_ceiling >= 0 && var.games_mp_engine_ceiling <= 200
    error_message = "games_mp_engine_ceiling must be a whole number 0..200 (0 stops every match)."
  }
}

variable "mp_router_desired_count" {
  description = "mp-router tasks at creation. After that, autoscaling owns the count between mp_router_min_count and mp_router_max_count; this is not reapplied."
  type        = number
  default     = 2
}

variable "mp_router_min_count" {
  description = "Fewest mp-router tasks autoscaling keeps. 2 so one task is never a single point of failure; 0 (with mp_router_max_count = 0) takes play.cc-games.app off the internet."
  type        = number
  default     = 2
}

variable "mp_router_max_count" {
  description = "Most mp-router tasks autoscaling may run."
  type        = number
  default     = 6
}

variable "mp_env" {
  description = "Default MP_ENV baked into the engine task definitions (games-mp-launch overrides it per match) and the router's primary env. The router accepts the whole of mp_router_envs."
  type        = string
  default     = "production"
}

# ONE router serves every lobby environment. A join token carries `n` (the minting lobby's
# env); the router accepts a token whose n is any value in MP_ENVS AND is the environment of
# the Ed25519 key that signed it. Each environment signs with its own private key (Vercel
# MP_TOKEN_SIGNING_KEYS, per target), and the router holds only public keys, each bound to
# its env (MP_TOKEN_PUBLIC_KEYS). So this IS a boundary: a Preview build holds only
# Preview's private key and cannot mint a token the router honours as production.
#
# development is left out on purpose. A development lobby runs on a laptop from
# `vercel env pull`; if it could mint router-accepted tokens, a signing key would have
# to sit in a .env.local file on that laptop. Local development uses MP_LAUNCHER=local
# and its own throwaway key instead, so it never needs this router. The launcher roles
# (games-mp-launcher, games-mp-launcher-preview) trust the same two environments, so the envs
# that can start an engine and the envs whose players can reach it are one list; keep them
# in step.
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
  # ADDING A GAME IS ONE ENTRY HERE plus its SIM_VERSION in var.games_mp_sim_versions (and
  # an image in the games repo's scripts/mp-images.mjs). The key is the contract's game
  # id: it names the ECR repository `games/<id>-engine`, the task definition `games-<id>`,
  # and the log stream prefix.
  # Sizes are the contract's 2 vCPU / 4 GB; a game may override them if it needs to.
  mp_games = {
    mptest = { cpu = 2048, memory = 4096 }
  }

  mp_cluster_name = "games"
  # The router's task definition family. The sweeper and games-mp-launch exempt exactly this
  # family from the engine count, and the launch function is denied RunTask on it; one local
  # so they can never disagree.
  mp_router_family = "games-mp-router"
  mp_port          = 8080

  # Engines launch in exactly these subnets. SUBNETS (games-mp-launch) and MP_TARGET_CIDRS
  # (the router's "a token may only point here" check) both derive from this one list so
  # they cannot drift apart. Narrower than the whole VPC on purpose: the VPC also holds the
  # dev boxes and jumpboxes, and a token (or a stolen signing key) must never be able to aim
  # the router at them. The subnets themselves are in "Network" below.
  mp_engine_subnets = values(aws_subnet.games_engine)
  mp_router_subnets = values(aws_subnet.games_router)

  # The router accepts exactly the engine subnets. The move out of the dev fleet's subnets is
  # applied when no engine runs (docs/games-multiplayer.md), so no old-subnet engine is left
  # for a router to reach.
  mp_router_target_cidrs = [for s in local.mp_engine_subnets : s.cidr_block]
  # The ALB stays in the dev fleet's public subnets: it is AWS-managed, runs none of our
  # code, and moving it would re-address a live load balancer for no isolation gain.
  mp_alb_subnets = local.dev_fleet_subnets

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

  games_mp_sha12 = substr(var.games_mp_source_ref, 0, 12)

  # The tags the task definitions run. With the build on they are derived from the pinned
  # commit; CodeBuild is told to produce exactly these and fails if the repo disagrees.
  mp_router_tag = var.games_mp_build ? local.games_mp_sha12 : var.mp_router_image_tag
  mp_engine_tags = var.games_mp_build ? {
    for id in keys(local.mp_games) : id => "${var.games_mp_sim_versions[id]}-${local.games_mp_sha12}"
    if contains(keys(var.games_mp_sim_versions), id)
  } : var.mp_engine_image_tags

  mp_engine_task_defs = {
    for id, cfg in local.mp_games : id => cfg
    if lookup(local.mp_engine_tags, id, "") != ""
  }

  # The simVersion each current engine revision runs: its tag without the trailing -<sha12>
  # (both tag variables are validated to that shape). games-mp-launch launches the pinned
  # revision for exactly this version and looks older ones up by tag.
  mp_engine_sim_versions = {
    for id in keys(local.mp_engine_task_defs) : id => regex("^(.+)-[0-9a-f]{12}$", local.mp_engine_tags[id])[0]
  }

  mp_router_enabled = local.mp_router_tag != ""

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
# tag is the commit it was built from, and a rollback to a previous commit must mean the
# previous bytes. Every tag carries the commit (`<sha12>`, `<simVersion>-<sha12>`), so a fix
# is always a new tag, and the build treats "tag already exists" as "already pushed".
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

# FARGATE is the default. FARGATE_SPOT is registered so a launch CAN use it
# (capacityProviderStrategy on RunTask) for bot-only or test matches; games-mp-launch does
# not today, because a Spot reclaim gives two minutes' notice and ends a live match.
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
# Join-token keys
# -------------------------------------------------------------------------------------
#
# Ed25519 key pairs generated by Terraform (games-multiplayer-bringup.tf,
# tls_private_key.games_mp_token): one per lobby environment and key generation. The
# private halves go only to the Vercel lobby (MP_TOKEN_SIGNING_KEYS, sensitive, each
# environment its own). The router gets the public halves as a plain environment variable
# (MP_TOKEN_PUBLIC_KEYS, below), so the one component that parses internet input can check a
# token but never mint one. There is no Secrets Manager copy: nothing on AWS needs a private
# key. The private keys are in Terraform state, which lives in the encrypted, versioned S3
# backend that only administration can read (the same place the old shared HMAC key was).
#
# The shared HMAC secret games/mp-token-keys this replaced is deleted by the apply that
# lands this change (7-day recovery window, readable only by administration meanwhile).

locals {
  # Who may read the games secrets besides a secret's own consumer: the same administration
  # set as vercel_api_token (vercel.tf).
  games_mp_admin_principals = [
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root",
    aws_iam_role.jumpbox_admin[0].arn,
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/*/AWSReservedSSO_AdministratorAccess_*",
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/aws-reserved/sso.amazonaws.com/AWSReservedSSO_AdministratorAccess_*",
  ]
}

# -------------------------------------------------------------------------------------
# Task IAM
# -------------------------------------------------------------------------------------
#
# TWO execution roles, not one. The launch function must be able to pass the engine's
# execution role, and RunTask lets the caller override executionRoleArn. If engines and
# the router shared one execution role, the role it can pass would also be the router's.
# Split, the role it can pass can pull images and write engine logs and nothing else. (The
# router's execution role reads no secret either since join tokens moved to Ed25519: the
# router's public keys are a plain environment variable.)

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
  description        = "ECS execution role for mp-router: pull its image, write /games/mp-router. No secrets."
  assume_role_policy = local.ecs_tasks_trust
}

# Pull and log only: since join tokens moved to Ed25519 it reads no secret. The name is kept
# from when it also read the token key, on purpose: renaming an inline policy replaces it,
# and both ways of replacing it are worse than a stale name (destroy-then-create leaves a
# window with no pull grant while the router rolls; create_before_destroy forms a cycle with
# the destroyed secret). Same name = one in-place PutRolePolicy that drops the statement.
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
    ]
  })
}

# Engines get NO AWS permissions. They talk only to Vercel (HTTPS, per-match secret) and
# to players via the router. The role exists so the task definition names a role the
# launch function is allowed to pass, rather than letting a RunTask override pick one.
resource "aws_iam_role" "games_engine_task" {
  name               = "games-engine-task"
  description        = "Match engine task role. Deliberately has no policies."
  assume_role_policy = local.ecs_tasks_trust
}

# The router needs no AWS API either: its public keys are a plain env var. Kept as its own
# empty role so a future need (e.g. reading a second secret at
# runtime) is added here without touching what engines can do.
resource "aws_iam_role" "games_mp_router_task" {
  name               = "games-mp-router-task"
  description        = "mp-router task role. Deliberately has no policies."
  assume_role_policy = local.ecs_tasks_trust
}

# -------------------------------------------------------------------------------------
# Vercel OIDC and the launcher roles
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

# TRUST: exactly the colton-games project in this team, one role per environment.
# Both conditions are StringEquals against full values, not StringLike, so no other project
# in the team, no Custom Environment, and no other Vercel team can assume either role.
#   production  - cc-games.app                       -> games-mp-launcher
#   preview     - PR and branch deployments          -> games-mp-launcher-preview
# Two roles, not one, because each may invoke only its own environment's launch function
# (games-mp-launch-<environment>), and the function is what fixes the engine's environment
# and ceiling. With one shared role, a Preview build could invoke the production function.
# development is deliberately NOT trusted. Its tokens are what `vercel env pull` hands
# any team member (valid 12 h), so trusting it would let a laptop start real Fargate
# tasks. Local development uses MP_LAUNCHER=local instead, and the router does not accept
# development tokens either (var.mp_router_envs), so the lists match.
locals {
  games_mp_launcher_role_names = {
    production = "games-mp-launcher"
    preview    = "games-mp-launcher-preview"
  }
}

resource "aws_iam_role" "games_mp_launcher" {
  for_each = local.games_mp_launcher_role_names

  name        = each.value
  description = "Assumed by colton-games ${each.key} deployments via Vercel OIDC to invoke games-mp-launch-${each.key}"
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
          "${local.vercel_oidc_host}:sub" = "owner:${local.vercel_team_slug}:project:${local.vercel_project_name}:environment:${each.key}"
        }
      }
    }]
  })
  tags = { Name = each.value, Project = "games-multiplayer" }
}

# The production role kept its name and address history; only the preview role is new.
moved {
  from = aws_iam_role.games_mp_launcher
  to   = aws_iam_role.games_mp_launcher["production"]
}

# The launcher's ONLY permission: invoke its own environment's launch function. No ECS, no
# PassRole, no EC2: every RunTask and StopTask is built by games-mp-launch-<environment>
# (below), which is where admission control lives. The Resource is that one function's
# unqualified ARN, which is what the lobby invokes; the other environment's function is a
# different ARN. (The functions publish no versions or aliases, so no qualifier could reach
# other code or settings anyway.)
resource "aws_iam_role_policy" "games_mp_launcher" {
  for_each = local.games_mp_launcher_role_names

  name = "invoke-games-mp-launch"
  role = aws_iam_role.games_mp_launcher[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "InvokeOwnLaunchFunction"
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.games_mp_launch[each.key].arn
    }]
  })
}

moved {
  from = aws_iam_role_policy.games_mp_launcher
  to   = aws_iam_role_policy.games_mp_launcher["production"]
}

# -------------------------------------------------------------------------------------
# Launch functions: the only RunTask of an engine, and admission control
# -------------------------------------------------------------------------------------
#
# One function per Vercel environment, games-mp-launch-production and games-mp-launch-preview,
# from the same source. The lobby invokes its own with
#   {"action":"start","matchId","game","simVersion","secret","hardCapSec","apiBase"[,"apiBypass"]}
#   {"action":"stop","matchId"}
# and the function builds the RunTask itself: the game's revision for that simVersion (the
# pinned current one, or an older ACTIVE one of the same family and ECR repository), the
# engine subnets and security group, the engine environment from validated fields, and the
# tags. Before launching, it counts its environment's running engines (every task that is not
# the router's family whose env tag is its environment or missing) and refuses at its own
# ceiling. Reserved concurrency 1 per function serializes each environment's admission; the
# source explains why that is enough and what it cannot cover (games-multiplayer/launch.py).
# The sweeper stays as the backstop for anything launched around them (an administrator) and
# for engines that never exit.
#
# WHY TWO FUNCTIONS. With one function and one concurrency slot, a compromised Preview build
# could keep that slot busy (synchronous calls, or queued asynchronous ones) and throttle
# every production launch. Separate functions have separate slots and separate async queues,
# so Preview can only slow Preview.
#
# THE CEILING IS SPLIT, NOT SHARED. Each function enforces only its own environment's share,
# and the shares sum to var.games_mp_engine_ceiling, so the total is bounded by construction
# with no lock between the functions: 30 -> production 22 + preview 8; 0 -> 0 + 0.

locals {
  # A Preview build is any writer's code, so it gets a small share: the lobby's preview cap (5)
  # plus room for engines still exiting after their match.
  games_mp_preview_ceiling = min(8, var.games_mp_engine_ceiling)
  games_mp_launch_environments = {
    production = {
      ceiling  = var.games_mp_engine_ceiling - local.games_mp_preview_ceiling
      api_base = "^https://cc-games\\.app$"
      bypass   = false
      # Engines verify join tokens themselves: this environment's PUBLIC keys only.
      token_public_keys = local.games_mp_token_public_keys_by_env["production"]
    }
    preview = {
      ceiling = local.games_mp_preview_ceiling
      # Its own deployment URL (https://$VERCEL_URL), the pattern the router's origin
      # allowlist uses for previews, so engines call back only this project's previews.
      api_base = "^https://${local.vercel_project_name}-[a-z0-9-]+-${local.vercel_team_slug}\\.vercel\\.app$"
      bypass   = true
      # Preview's own public keys: a preview engine never accepts a production token.
      token_public_keys = local.games_mp_token_public_keys_by_env["preview"]
    }
  }
}

# The lobby's own caps must fit inside each environment's share, or a correctly behaving
# lobby gets `capacity` refusals below its cap. A warning, not a validation: lowering the
# ceiling below them (0 included) is a deliberate emergency brake.
check "games_mp_lobby_caps_fit_the_launch_ceilings" {
  assert {
    condition = var.games_mp_engine_ceiling == 0 || alltrue([
      for env, cap in local.games_mp_lobby_max_active : cap <= local.games_mp_launch_environments[env].ceiling
    ])
    error_message = "A lobby cap (games_mp_lobby_max_active) exceeds its environment's share of games_mp_engine_ceiling; that environment's lobby will be refused below its own cap."
  }
}

data "archive_file" "games_mp_launch" {
  type        = "zip"
  source_file = "${path.module}/games-multiplayer/launch.py"
  output_path = "${path.module}/.terraform/games-mp-launch.zip"
}

resource "aws_cloudwatch_log_group" "games_mp_launch" {
  for_each = local.games_mp_launch_environments

  name              = "/aws/lambda/games-mp-launch-${each.key}"
  retention_in_days = 14
}

resource "aws_iam_role" "games_mp_launch" {
  name = "games-mp-launch"
  # Shared by both environments' functions: they run the same code with the same ECS rights;
  # what differs (environment, ceiling, callback URL) is each function's own configuration.
  description = "games-mp-launch-<environment> Lambdas: RunTask engine task definition revisions, StopTask engines"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      # The standard execution-role trust, as every Lambda role here: Lambda does not supply
      # aws:SourceAccount when it assumes an execution role, so a condition on it would make
      # the function unrunnable.
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = "games-mp-launch", Project = "games-multiplayer" }
}

resource "aws_iam_role_policy" "games_mp_launch" {
  name = "launch-and-stop-match-engines"
  role = aws_iam_role.games_mp_launch.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      # Any revision of exactly the engine families (games-<game> for each game), only on this
      # cluster: the current revision, and the older ones skip_destroy keeps ACTIVE for clients
      # still on an older simVersion. The function picks the revision (launch.py SIM VERSIONS);
      # `:*` after a family matches only its revisions, as a family name has no colon. A
      # revision naming any role but the two engine roles fails at PassRole below. (No
      # statement without a game: IAM rejects an empty Resource list.)
      length(local.mp_games) == 0 ? [] : [{
        Sid       = "RunEngineFamilyRevisions"
        Effect    = "Allow"
        Action    = "ecs:RunTask"
        Resource  = [for id in keys(local.mp_games) : "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/games-${id}:*"]
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn } }
      }],
      [
        {
          # The Allow above never names the router; this keeps it so even if a game id ever
          # collided with its family name.
          Sid      = "NeverRunTheRouter"
          Effect   = "Deny"
          Action   = "ecs:RunTask"
          Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task-definition/${local.mp_router_family}:*"
        },
        {
          # Finding the revision for an older simVersion: list a family's ACTIVE revisions and
          # read one's engine image. Read-only, and neither action supports a resource or
          # condition key (Service Authorization Reference), so "*" is the only Resource.
          Sid      = "ReadEngineTaskDefinitions"
          Effect   = "Allow"
          Action   = ["ecs:ListTaskDefinitions", "ecs:DescribeTaskDefinition"]
          Resource = "*"
        },
        {
          # RunTask needs PassRole for the task definition's two roles. Only those, only to ECS.
          Sid      = "PassOnlyEngineRoles"
          Effect   = "Allow"
          Action   = "iam:PassRole"
          Resource = [aws_iam_role.games_engine_task.arn, aws_iam_role.games_engine_execution.arn]
          Condition = {
            StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
          }
        },
        {
          # RunTask with `tags` also authorises ecs:TagResource on the new task; only then.
          Sid      = "TagTasksAtLaunch"
          Effect   = "Allow"
          Action   = "ecs:TagResource"
          Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.mp_cluster_name}/*"
          Condition = {
            StringEquals = { "ecs:CreateAction" = "RunTask" }
          }
        },
        {
          # Counting engines and finding a match's task.
          Sid       = "ListTasksInThisCluster"
          Effect    = "Allow"
          Action    = "ecs:ListTasks"
          Resource  = "*"
          Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn } }
        },
        {
          Sid       = "DescribeTasksInThisCluster"
          Effect    = "Allow"
          Action    = "ecs:DescribeTasks"
          Resource  = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.mp_cluster_name}/*"
          Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn } }
        },
        {
          # DescribeTasks include=TAGS returns tags only with this, and the per-environment
          # ceiling and stop() read the env and match tags (the sweeper has it too). Its own
          # statement: ecs:ListTagsForResource does not support the ecs:cluster condition key, so
          # under it the grant would never match. The task ARN already names the cluster.
          Sid      = "ReadTaskTagsInThisCluster"
          Effect   = "Allow"
          Action   = "ecs:ListTagsForResource"
          Resource = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:task/${local.mp_cluster_name}/*"
        },
        {
          # The code stops only tasks of an engine family whose env and match tags are the
          # caller's; IAM adds that the task must carry `match` and be in this cluster.
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
          # Belt and braces for the router: its service propagates games-role=router onto
          # every task it starts (aws_ecs_service.games_mp_router), and this Deny wins.
          Sid      = "NeverStopTheRouter"
          Effect   = "Deny"
          Action   = ["ecs:StopTask", "ecs:TagResource", "ecs:UntagResource"]
          Resource = "*"
          Condition = {
            StringEquals = { "aws:ResourceTag/games-role" = "router" }
          }
        },
        {
          Sid      = "WriteOwnLogs"
          Effect   = "Allow"
          Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
          Resource = [for lg in aws_cloudwatch_log_group.games_mp_launch : "${lg.arn}:*"]
        },
      ],
    )
  })
}

resource "aws_lambda_function" "games_mp_launch" {
  for_each = local.games_mp_launch_environments

  function_name    = "games-mp-launch-${each.key}"
  role             = aws_iam_role.games_mp_launch.arn
  handler          = "launch.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 30
  memory_size      = 128
  filename         = data.archive_file.games_mp_launch.output_path
  source_code_hash = data.archive_file.games_mp_launch.output_base64sha256
  # ONE invocation at a time per environment: its admission (count, then RunTask) can never
  # interleave. It is also the environment's launch rate limit. Never raise it without
  # replacing the count with an atomic one.
  reserved_concurrent_executions = 1

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.games_mp_launch[each.key].name
  }

  environment {
    variables = {
      # This function's environment: the engine's MP_ENV and env tag, and whose engines it
      # counts and stops. Set here, never by the caller.
      LAUNCH_ENV   = each.key
      ENV_CEILING  = tostring(each.value.ceiling)
      API_BASE     = each.value.api_base
      ALLOW_BYPASS = tostring(each.value.bypass)
      # This environment's join-token PUBLIC keys, for its engines to verify tokens with.
      TOKEN_PUBLIC_KEYS = each.value.token_public_keys
      # The rest is the same for both environments.
      CLUSTER          = aws_ecs_cluster.games.name
      SUBNETS          = join(",", [for s in local.mp_engine_subnets : s.id])
      SECURITY_GROUP   = aws_security_group.games_engine.id
      TASK_DEFINITIONS = jsonencode({ for id, td in aws_ecs_task_definition.games_engine : id => td.arn })
      ROUTER_FAMILY    = local.mp_router_family
      MIN_HARDCAP_SEC  = "60"
      # The sweeper's MAX_HARDCAP_SEC: a hardcap it would clamp is refused here instead.
      MAX_HARDCAP_SEC = "14400"
      SETTLE_SEC      = "120"
      LOOKUP_TTL_SEC  = "60"
      # Per game: the current revision's simVersion (launched from TASK_DEFINITIONS with no ECS
      # read) and the ECR repository an older revision's image must be in to be launched.
      ENGINE_IMAGES = jsonencode({ for id, td in aws_ecs_task_definition.games_engine : id => {
        simVersion = local.mp_engine_sim_versions[id]
        repository = aws_ecr_repository.games_mp["games/${id}-engine"].repository_url
      } })
    }
  }

  tags = { Name = "games-mp-launch-${each.key}", Project = "games-multiplayer" }

  # An engine subnet without its route table would fall back to the VPC's main table (no
  # internet: every launch fails its image pull), and without its ACL it would reach the
  # whole VPC. The functions learn the subnets only once both are in place, and only after
  # every router task runs the definition that accepts them (games_mp_healthy), so no engine
  # launches into subnets a router still rejects. The same wait covers join tokens: engines
  # accept only the X-MP-Token a current router forwards, and get their environment's public
  # keys from this function, so new engines start only once every router task forwards it.
  depends_on = [aws_route_table_association.games_engine, aws_network_acl.games_engine, terraform_data.games_mp_healthy]
}

# The lobby invokes synchronously. An asynchronous (InvocationType=Event) call cannot be
# refused by IAM, so make it worthless: no retries, and an event not run within a minute is
# dropped, not left queued to occupy the function's one slot later. Each function has its
# own queue, so this is about Preview's backlog slowing Preview's launches, never production's.
resource "aws_lambda_function_event_invoke_config" "games_mp_launch" {
  for_each = local.games_mp_launch_environments

  function_name                = aws_lambda_function.games_mp_launch[each.key].function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 60
}

# -------------------------------------------------------------------------------------
# Network: the router's and the engines' own subnets
# -------------------------------------------------------------------------------------
#
# Engines run code we build but treat as hostile once a match is live, and the router is the
# internet-facing hop. Neither may share subnets with the dev fleet, for two reasons that a
# security group alone cannot fix:
#   - The I/O box (io-box.tf, us-west-2) exports read-write NFS over an INTER-REGION peer,
#     where security groups cannot be referenced, so its only filter is by source CIDR. The
#     games tasks must therefore live in CIDRs that are not the NFS clients' CIDRs.
#   - The dev subnets' route table carries the peer route. These subnets get their own table
#     with no peer route, so a games task cannot even route a packet toward the I/O box.
#
#   10.0.64.0/24, 10.0.65.0/24  engines (AZs of subnet_a, subnet_b)  games-rt + games-engine ACL
#   10.0.66.0/24, 10.0.67.0/24  router  (AZs of subnet_a, subnet_b)  games-rt, default ACL
#
# The router gets its own subnets, rather than staying beside the dev boxes, so that (1) the
# engine ACL can admit exactly the router's CIDRs instead of the dev fleet's, and (2) the
# router, which parses untrusted input from the internet, is outside the NFS client CIDRs
# too. The ALB stays in subnet_a/subnet_b (local.mp_alb_subnets). IPv6 matches the dev
# subnets (a /64 each, assigned on creation, ::/0 to the IGW): ECS dualStackIPv6 is on for the
# account, so tasks keep getting an IPv6 address, and engine egress stays 443 on v4 and v6.
# Public IPv4 on each task (assignPublicIp) instead of a NAT gateway, as before.
#
# The /24 index is also the IPv6 /64 index within the VPC's /56 (subnet_a is 1, subnet_b 2),
# so 10.0.64.0/24 pairs with /64 number 64 (2600:1f1c:494:240::/64 today).
locals {
  games_subnet_layout = {
    a = { az = aws_subnet.subnet_a.availability_zone, engine = 64, router = 66 }
    b = { az = aws_subnet.subnet_b.availability_zone, engine = 65, router = 67 }
  }
}

resource "aws_subnet" "games_engine" {
  for_each = local.games_subnet_layout

  vpc_id                          = local.vpc_id
  cidr_block                      = cidrsubnet(data.aws_vpc.selected.cidr_block, 8, each.value.engine)
  availability_zone               = each.value.az
  ipv6_cidr_block                 = cidrsubnet(aws_vpc_ipv6_cidr_block_association.main.ipv6_cidr_block, 8, each.value.engine)
  assign_ipv6_address_on_creation = true
  # games-mp-launch asks for a public IPv4 per task (assignPublicIp=ENABLED); nothing else
  # launches here, so the subnet default stays off.
  map_public_ip_on_launch = false

  tags = { Name = "games-engine-${each.key}", Project = "games-multiplayer" }
}

resource "aws_subnet" "games_router" {
  for_each = local.games_subnet_layout

  vpc_id                          = local.vpc_id
  cidr_block                      = cidrsubnet(data.aws_vpc.selected.cidr_block, 8, each.value.router)
  availability_zone               = each.value.az
  ipv6_cidr_block                 = cidrsubnet(aws_vpc_ipv6_cidr_block_association.main.ipv6_cidr_block, 8, each.value.router)
  assign_ipv6_address_on_creation = true
  map_public_ip_on_launch         = false

  tags = { Name = "games-router-${each.key}", Project = "games-multiplayer" }
}

# Internet both ways, the VPC's implicit local route, and NOTHING else. Never add the I/O
# box peer route (or any peer, VPN or transit route) here: that is the whole point of these
# subnets. Inline routes, like main.tf's public table, so the provider owns the full set.
resource "aws_route_table" "games" {
  vpc_id = local.vpc_id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  route {
    ipv6_cidr_block = "::/0"
    gateway_id      = aws_internet_gateway.main.id
  }

  tags = { Name = "games-rt", Project = "games-multiplayer" }
}

resource "aws_route_table_association" "games_engine" {
  for_each       = aws_subnet.games_engine
  subnet_id      = each.value.id
  route_table_id = aws_route_table.games.id
}

resource "aws_route_table_association" "games_router" {
  for_each       = aws_subnet.games_router
  subnet_id      = each.value.id
  route_table_id = aws_route_table.games.id
}

# The engine subnets' ACL: a second, stateless fence under the games-engine security group
# (which already admits only the router and sends only 443). The security group is enough
# while it is right; this makes "an engine cannot talk to the rest of the VPC" true even if a
# rule is ever loosened there, because an ACL is attached to the subnet, not to the task.
#
#   in   8080 from the router subnets          player traffic, the ONLY in-VPC flow; 8080 from
#                                              anywhere else is denied, v4 and v6
#   in   1024-65535 tcp from the internet      replies to the engine's own 443 connections
#   in   ICMP "fragmentation needed" (v4) / "packet too big" (v6), so path-MTU discovery works
#   out  1024-65535 tcp to the router subnets  replies to the router
#   out  443 tcp to the internet               Vercel callbacks, ECR, CloudWatch Logs
#   everything else to or from 10.0.0.0/16 or the VPC's IPv6 /56 is DENIED before the
#   internet allows, so "the internet" never includes a dev box's address, v4 or v6.
#
# Not filtered by ACLs at all (AWS documents these): the VPC DNS resolver, the ECS task
# metadata endpoint (169.254.170.2) and Amazon Time Sync, which is why they need no rule.
# Traffic between two engines in the SAME subnet never crosses an ACL; the security group
# (router-only ingress) is what stops it.
resource "aws_network_acl" "games_engine" {
  vpc_id     = local.vpc_id
  subnet_ids = [for s in local.mp_engine_subnets : s.id]

  dynamic "ingress" {
    for_each = { for i, s in local.mp_router_subnets : i => s.cidr_block }
    content {
      rule_no    = 100 + ingress.key
      action     = "allow"
      protocol   = "tcp"
      cidr_block = ingress.value
      from_port  = local.mp_port
      to_port    = local.mp_port
    }
  }

  ingress {
    rule_no    = 110
    action     = "deny"
    protocol   = "-1"
    cidr_block = data.aws_vpc.selected.cidr_block
    from_port  = 0
    to_port    = 0
  }

  ingress {
    rule_no         = 120
    action          = "deny"
    protocol        = "-1"
    ipv6_cidr_block = aws_vpc_ipv6_cidr_block_association.main.ipv6_cidr_block
    from_port       = 0
    to_port         = 0
  }

  # 8080 lies inside the reply range below; without these, anyone on the internet could reach
  # an engine's public IP on it whenever the security group allowed. Only the router may.
  ingress {
    rule_no    = 130
    action     = "deny"
    protocol   = "tcp"
    cidr_block = "0.0.0.0/0"
    from_port  = local.mp_port
    to_port    = local.mp_port
  }

  ingress {
    rule_no         = 131
    action          = "deny"
    protocol        = "tcp"
    ipv6_cidr_block = "::/0"
    from_port       = local.mp_port
    to_port         = local.mp_port
  }

  ingress {
    rule_no    = 200
    action     = "allow"
    protocol   = "tcp"
    cidr_block = "0.0.0.0/0"
    from_port  = 1024
    to_port    = 65535
  }

  ingress {
    rule_no         = 201
    action          = "allow"
    protocol        = "tcp"
    ipv6_cidr_block = "::/0"
    from_port       = 1024
    to_port         = 65535
  }

  ingress {
    rule_no    = 210
    action     = "allow"
    protocol   = "icmp"
    cidr_block = "0.0.0.0/0"
    icmp_type  = 3
    icmp_code  = 4
    from_port  = 0
    to_port    = 0
  }

  ingress {
    rule_no         = 211
    action          = "allow"
    protocol        = "58"
    ipv6_cidr_block = "::/0"
    icmp_type       = 2
    icmp_code       = 0
    from_port       = 0
    to_port         = 0
  }

  dynamic "egress" {
    for_each = { for i, s in local.mp_router_subnets : i => s.cidr_block }
    content {
      rule_no    = 100 + egress.key
      action     = "allow"
      protocol   = "tcp"
      cidr_block = egress.value
      from_port  = 1024
      to_port    = 65535
    }
  }

  egress {
    rule_no    = 110
    action     = "deny"
    protocol   = "-1"
    cidr_block = data.aws_vpc.selected.cidr_block
    from_port  = 0
    to_port    = 0
  }

  egress {
    rule_no         = 120
    action          = "deny"
    protocol        = "-1"
    ipv6_cidr_block = aws_vpc_ipv6_cidr_block_association.main.ipv6_cidr_block
    from_port       = 0
    to_port         = 0
  }

  egress {
    rule_no    = 200
    action     = "allow"
    protocol   = "tcp"
    cidr_block = "0.0.0.0/0"
    from_port  = 443
    to_port    = 443
  }

  egress {
    rule_no         = 201
    action          = "allow"
    protocol        = "tcp"
    ipv6_cidr_block = "::/0"
    from_port       = 443
    to_port         = 443
  }

  tags = { Name = "games-engine", Project = "games-multiplayer" }
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

# Engines need HTTPS and nothing else: the lobby callbacks on Vercel (spec, ready, heartbeat,
# result: colton-games server/mp-kit/api.mjs), whose addresses are not fixed, plus the image
# pull and log delivery Fargate makes over the task's ENI. So 443 only, v4 and v6; an engine
# running hostile code cannot reach SSH, databases or anything else on the internet or in
# this VPC. DNS to the VPC resolver is not filtered by security groups.
# create_before_destroy: the new 443 rules exist before the old allow-all ones go, so a live
# engine never loses its callbacks mid-match.
resource "aws_vpc_security_group_egress_rule" "games_engine" {
  for_each          = { v4 = { cidr4 = "0.0.0.0/0", cidr6 = null }, v6 = { cidr4 = null, cidr6 = "::/0" } }
  security_group_id = aws_security_group.games_engine.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = each.value.cidr4
  cidr_ipv6         = each.value.cidr6
  description       = "HTTPS only (${each.key}): Vercel callbacks, ECR, logs"

  lifecycle {
    create_before_destroy = true
  }
}

# The ECS task metadata endpoint, where an engine reads its own private IP
# (resolveEngineIp in mp-kit/api.mjs). Link-local traffic is not filtered by security groups;
# this rule states the dependency so tightening egress can never silently break it.
resource "aws_vpc_security_group_egress_rule" "games_engine_task_metadata" {
  security_group_id = aws_security_group.games_engine.id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "169.254.170.2/32"
  description       = "ECS task metadata endpoint (the engine learns its own IP here)"
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
  subnets                    = [for s in local.mp_alb_subnets : s.id]
  idle_timeout               = 3600
  drop_invalid_header_fields = true

  # games-multiplayer-edge.tf: the bucket, its delivery policy and 30-day expiry.
  access_logs {
    bucket  = aws_s3_bucket.games_play_alb_logs.id
    prefix  = "games-play"
    enabled = true
  }

  tags = { Name = "games-play", Project = "games-multiplayer" }

  depends_on = [aws_s3_bucket_policy.games_play_alb_logs]
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
# Created only after its image exists: the task definition depends on the CodeBuild step
# (terraform_data.games_mp_build), so a new tag is never registered before it is pushed,
# and an ECS service never sits in a pull-fail loop. With games_mp_build = false the
# router exists once mp_router_image_tag is set (that tag must already be in ECR).

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
    image        = "${aws_ecr_repository.games_mp["games/mp-router"].repository_url}:${local.mp_router_tag}"
    essential    = true
    portMappings = [{ containerPort = local.mp_port, protocol = "tcp" }]
    environment = [
      { name = "PORT", value = tostring(local.mp_port) },
      # MP_ENVS is what the router checks a token's `n` against (any listed value);
      # MP_ENV stays for logs and for code that wants the primary env.
      { name = "MP_ENV", value = var.mp_env },
      { name = "MP_ENVS", value = join(",", var.mp_router_envs) },
      { name = "MP_ALLOWED_ORIGINS", value = join(",", local.mp_allowed_origins) },
      { name = "MP_TARGET_CIDRS", value = join(",", local.mp_router_target_cidrs) },
      # Ed25519 PUBLIC keys only ("<env>:<kid>:<base64 SPKI>"), each bound to its env: not a
      # secret, so no `secrets` entry and no Secrets Manager read. A key rotation changes this
      # value, so ECS rolls the router onto a new task definition, and the health step tracks
      # that ARN: a rollback can never pass for the rotated deployment. The router never gets
      # a private key (and refuses to boot if MP_TOKEN_SIGNING_KEYS or MP_TOKEN_KEYS is set).
      { name = "MP_TOKEN_PUBLIC_KEYS", value = local.games_mp_token_public_keys },
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

  tags = { Name = "games-mp-router", Project = "games-multiplayer", ImageTag = local.mp_router_tag }

  # The image must be in ECR before a task definition names it.
  depends_on = [terraform_data.games_mp_build]
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
  # launch function's NeverStopTheRouter Deny keys on; do not drop it or the propagation.
  propagate_tags = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets         = [for s in local.mp_router_subnets : s.id]
    security_groups = [aws_security_group.games_router.id]
    # Outbound only (ECR, logs, secret): the router SG admits nothing but the ALB.
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.games_mp_router.arn
    container_name   = "mp-router"
    container_port   = local.mp_port
  }

  # ECS refuses to attach a target group that no load balancer uses yet. The router subnets
  # must have their route table before a task starts there (else: no image pull).
  depends_on = [aws_lb_listener.games_play_https, aws_route_table_association.games_router]

  tags = { Name = "mp-router", Project = "games-multiplayer", "games-role" = "router" }

  # Application Auto Scaling owns the running count (games-multiplayer-edge.tf).
  lifecycle {
    ignore_changes = [desired_count]
  }
}

# -------------------------------------------------------------------------------------
# Engine task definitions: games-<game>
# -------------------------------------------------------------------------------------
#
# One per entry in local.mp_games that has an image tag, registered only after the build
# pushed that tag. Only games-mp-launch runs these, adding MATCH_ID, MATCH_SECRET and MP_API
# as container overrides and tagging the task (see the sweeper below).
#
# skip_destroy = true: moving a game to a new simVersion registers a new revision, and the
# old revision stays ACTIVE instead of being deregistered. Clients still on the old
# simVersion can then still be matched onto `games-<game>:<old revision>` while the new
# site rolls out. The lobby sends each launch the match's simVersion; games-mp-launch runs
# the revision below for the current one, and for an older one the newest ACTIVE revision of
# the family whose engine image is `<this game's repository>:<simVersion>-<sha12>`, refusing
# (`unknown-sim-version`) when there is none. So never deregister a revision while clients may
# still send its simVersion, and never register a revision in a games-<game> family by hand:
# the function would run it for its tag. Old revisions cost nothing; their images are kept
# only while ECR's last-20 lifecycle rule keeps them.

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
    image        = "${aws_ecr_repository.games_mp["games/${each.key}-engine"].repository_url}:${local.mp_engine_tags[each.key]}"
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

  tags = { Name = "games-${each.key}", Project = "games-multiplayer", ImageTag = local.mp_engine_tags[each.key] }

  depends_on = [terraform_data.games_mp_build]
}

# -------------------------------------------------------------------------------------
# Sweeper: the backstop for engines that do not exit
# -------------------------------------------------------------------------------------
#
# games-mp-launch sets these tags on every RunTask:
#   game    = the game id                    (e.g. mptest)
#   match   = the match id
#   env     = the function's own environment (production / preview)
#   hardcap = the match's hard cap in SECONDS (spec limits.hardCapSec, 60..14400)
# The sweeper stops any task in the cluster older than hardcap + 10 min, or
# older than 2 h when hardcap is missing or not a positive integer; hardcap is clamped to
# 4 h. Every task counts except the router's task definition family, games-mp-router,
# which nothing but the router service runs; group, startedBy and tags are caller-set on
# RunTask and never exempt anything, so a task an administrator starts without tags still
# gets 2 h.
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
        # To report a StopTask that FAILED and a ceiling stop; routine age stops are just logged.
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      },
      {
        # RunningEngines / EnginesStopped every run (games-mp-engines-over-lobby-caps reads it).
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "GamesMultiplayer" } }
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
      # The ONLY exemption: the router's family, which games-mp-launch is denied RunTask on
      # (NeverRunTheRouter).
      ROUTER_FAMILY = local.mp_router_family
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
      # AWS-side ceiling on concurrent engines, independent of the lobby.
      ENGINE_CEILING   = tostring(var.games_mp_engine_ceiling)
      METRIC_NAMESPACE = "GamesMultiplayer"
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

# Every minute: a hung engine overstays by at most cap + 10 min + 1 min, and a launch burst
# above the engine ceiling is cut back within a minute.
resource "aws_scheduler_schedule" "games_mp_sweeper" {
  name       = "games-mp-sweeper"
  group_name = "default"

  flexible_time_window { mode = "OFF" }

  schedule_expression = "rate(1 minute)"

  target {
    arn      = aws_lambda_function.games_mp_sweeper.arn
    role_arn = aws_iam_role.games_mp_sweeper_scheduler.arn

    # A missed sweep is replaced by the next one a minute later; retrying stale ones would
    # only pile invocations up.
    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 60
    }
  }
}

# The sweeper is the only cost backstop for a hung engine, so a broken sweeper must not be
# silent. Two different failures, two alarms, both to the same topic as the other cost alerts:
#   errors  - the function ran and raised (an IAM regression on ListTasks/DescribeTasks, an ECS
#             API error, or the 60 s timeout). Lambda counts all of these in AWS/Lambda Errors.
#   silent  - the function did NOT run at all (the Scheduler role lost lambda:InvokeFunction,
#             the schedule was disabled or deleted). That produces no Errors datapoint, so
#             absence of Invocations is the signal; missing data counts as breaching.
# The schedule itself keeps retries off: the next sweep is five minutes away, and the alarms
# are what make a persistent failure visible.
resource "aws_cloudwatch_metric_alarm" "games_mp_sweeper_errors" {
  alarm_name          = "games-mp-sweeper-errors"
  alarm_description   = "games-mp-sweeper raised or timed out; hung match engines may be running uncapped"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.games_mp_sweeper.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "games_mp_sweeper_silent" {
  alarm_name          = "games-mp-sweeper-not-running"
  alarm_description   = "games-mp-sweeper has not been invoked for 15 minutes (schedule or its role broken); hung match engines may be running uncapped"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.games_mp_sweeper.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 3
  datapoints_to_alarm = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# More engines running than the lobby's own caps allow, for 5 minutes: the lobby's admission is
# broken, a compromised deployment is launching through its games-mp-launch function up to
# its ceiling, or an administrator launched around them. The ceiling still caps the damage;
# this says why.
resource "aws_cloudwatch_metric_alarm" "games_mp_engines_over_lobby_caps" {
  alarm_name          = "games-mp-engines-over-lobby-caps"
  alarm_description   = "More match engines are running than the colton-games lobby's caps allow (${local.games_mp_lobby_engines_max}) for 5 minutes; games-mp-launch-production refuses at ${local.games_mp_launch_environments.production.ceiling} and games-mp-launch-preview at ${local.games_mp_launch_environments.preview.ceiling}, and the sweeper stops the newest above ${var.games_mp_engine_ceiling}. Check the lobby's admission and the launch functions' logs."
  namespace           = "GamesMultiplayer"
  metric_name         = "RunningEngines"
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 5
  datapoints_to_alarm = 5
  comparison_operator = "GreaterThanThreshold"
  threshold           = local.games_mp_lobby_engines_max
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# The count above is published by the sweeper every minute, even when it is 0. The sweeper
# swallows a failed PutMetricData so the sweep itself still succeeds, which the silence alarm
# (invocations) would not notice: alarm on the metric itself going missing.
resource "aws_cloudwatch_metric_alarm" "games_mp_engine_count_missing" {
  alarm_name          = "games-mp-engine-count-missing"
  alarm_description   = "GamesMultiplayer/RunningEngines has not been published for 10 minutes: the engine-count alarm is blind. Check games-mp-sweeper's log for put_metric_data failures."
  namespace           = "GamesMultiplayer"
  metric_name         = "RunningEngines"
  statistic           = "SampleCount"
  period              = 300
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# ECS/Fargate spend. AWS/Billing EstimatedCharges does not exist in this account (billing
# alerts are not enabled; `aws cloudwatch list-metrics --namespace AWS/Billing` is empty in
# every region), so a Budgets budget filtered to ECS is the alarm here. It lags by hours:
# the engine ceiling is the real-time control, this is the backstop that sees the bill.
resource "aws_budgets_budget" "games_ecs_daily" {
  name         = "games-ecs-daily"
  budget_type  = "COST"
  limit_amount = "15"
  limit_unit   = "USD"
  time_unit    = "DAILY"

  cost_filter {
    name   = "Service"
    values = ["Amazon Elastic Container Service"]
  }

  notification {
    comparison_operator       = "GREATER_THAN"
    threshold                 = 100
    threshold_type            = "PERCENTAGE"
    notification_type         = "ACTUAL"
    subscriber_sns_topic_arns = [aws_sns_topic.cost_alerts.arn]
  }
}

# -------------------------------------------------------------------------------------
# Outputs
# -------------------------------------------------------------------------------------

# The non-secret Vercel settings, for reference. Terraform itself writes them (and the
# secrets) to the colton-games project: games-multiplayer-bringup.tf.
output "games_mp_vercel_env" {
  description = "Non-secret multiplayer settings Terraform writes to the colton-games Vercel project"
  value       = local.games_mp_vercel_shared_config
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

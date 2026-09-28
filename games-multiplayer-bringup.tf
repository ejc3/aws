# games-multiplayer-bringup.tf
#
# Everything the multiplayer platform needs besides the AWS resources in
# games-multiplayer.tf, done by ONE `terraform apply` on the jumpbox:
#
#   secrets      generated here (random and tls providers) and written to Secrets Manager and
#                Vercel; the join-token signing keys go to Vercel only
#   router       the one-time `live` tag the router task definition runs (the images, engine
#                revisions, router rollouts and migrations are games-multiplayer-deploy.tf's)
#   Vercel env   the MP_* settings and secrets on the colton-games project (Production and
#                Preview), the automation bypass, OIDC in Team issuer mode, and Preview's
#                Supabase connection
#   Supabase     the database URL games-mp-migrate uses, copied into its secret
#   verified     the apply waits for the new router deployment, a healthy target and a
#                200 `ok` from https://play.cc-games.app/healthz, and fails otherwise
#
# PLAN-TIME PREFLIGHT. Before anything changes, `terraform plan` runs a read-only check
# (data "external" games_mp_preflight, bringup.py preflight) that proves every later step
# can finish: the Vercel token reads the project, its env and settings, decrypts what the
# Preview copy and the database-URL sync need, and is live and scoped to the team; the GitHub
# read token games-mp-poller uses can read main. Any gap fails the PLAN with the exact fix, so
# an apply either does everything or does not start.
#
# The steps that are not plain resources are terraform_data local-execs running
# games-multiplayer/bringup.py on the jumpbox with its administrator role. Each one checks
# live state first, changes only what is missing, verifies, and exits non-zero on failure.
# They re-run only when their inputs change, so a second apply with nothing changed is an
# empty plan.
#
# SECRETS ARE IN TERRAFORM STATE. The join-token signing keys (tls provider), MP_TEST_KEY,
# CRON_SECRET, the cookie secrets, Preview's Skyhook secret and the automation bypass are
# generated here, so their values are stored in state: the S3 backend (ejc3-terraform-state) is
# encrypted, versioned, and readable only by administration, which is the same boundary as
# the Secrets Manager copies. What is NOT in state: the Vercel API token and GitHub PAT
# (read by bringup.py at apply time), the Supabase database URL, and the Supabase values
# copied to Preview (both read from Vercel's API into memory and never written to disk).
#
# Terraform is pinned to 1.10.3, so write-only arguments (value_wo, 1.11+) are not an
# option for keeping these out of state.

variable "games_mp_token_kids" {
  description = "Join-token key generations, newest first. Each is one Ed25519 key pair per lobby environment, key id <env>-<kid>. The first signs, all verify. Rotate (docs/games-multiplayer.md): append the new id and apply (the router learns it); move it first and apply, then redeploy Production and every Preview still in use; once none built earlier serves, wait 2 min, drop the old id and apply."
  type        = list(string)
  default     = ["kid1"]

  validation {
    # 20, not 32: the key id on the wire is "<env>-<kid>" ("production-" is 11 characters),
    # and the token module caps key ids at 32.
    condition = (
      length(var.games_mp_token_kids) > 0 &&
      length(distinct(var.games_mp_token_kids)) == length(var.games_mp_token_kids) &&
      alltrue([for k in var.games_mp_token_kids : can(regex("^[A-Za-z0-9_-]{1,20}$", k))])
    )
    error_message = "games_mp_token_kids must be distinct ids of 1-20 characters from [A-Za-z0-9_-]."
  }
}

variable "games_mp_preview_supabase_sync" {
  description = "Bump to re-copy the Supabase URL and server key from Production to Preview (e.g. after the integration rotates its keys)."
  type        = string
  default     = "1"
}

locals {
  # Copied Production -> Preview: the URL and server key the site reads first
  # (lib/skyhook/supabase-config.ts: SUPABASE_URL || NEXT_PUBLIC_SUPABASE_URL,
  # SUPABASE_SECRET_KEY || SUPABASE_SERVICE_ROLE_KEY), plus the public URL for the browser.
  games_mp_preview_copy = "SUPABASE_URL:encrypted,NEXT_PUBLIC_SUPABASE_URL:encrypted,SUPABASE_SECRET_KEY:sensitive"
  games_mp_repo         = "CoderColton/colton-games"
  games_mp_bringup      = "${path.module}/games-multiplayer/bringup.py"

  # JOIN-TOKEN KEYS: Ed25519, one key pair per lobby environment and key generation, so
  # minting and verifying need different keys and each environment mints with its own.
  #   lobby   MP_TOKEN_SIGNING_KEYS  "<env>-<kid>:<base64 PKCS#8 DER>[,...]", first signs.
  #           Vercel only, sensitive, and each environment gets ONLY its own private keys:
  #           a Preview build cannot mint a Production token.
  #   router  MP_TOKEN_PUBLIC_KEYS   "<env>:<env>-<kid>:<base64 SPKI DER>[,...]". Public keys,
  #           a plain task-definition environment variable. The router binds each key to its
  #           env and refuses a token whose `n` is not that env; it holds no private key and
  #           cannot mint (and refuses to boot if MP_TOKEN_SIGNING_KEYS is ever set).
  #   engine  MP_TOKEN_PUBLIC_KEYS   its own environment's public keys only, set by
  #           games-mp-launch at RunTask. Engines verify every token again (the router forwards
  #           it) and take the seat from it alone, so a compromised router cannot claim a seat.
  # The private keys are in no Secrets Manager secret: nothing on AWS needs them.
  games_mp_token_envs = ["production", "preview"]
  games_mp_token_pairs = {
    for pair in setproduct(local.games_mp_token_envs, var.games_mp_token_kids) :
    "${pair[0]}-${pair[1]}" => { env = pair[0], kid = pair[1] }
  }
  # The base64 DER inside a PEM: its body lines, joined (tls_private_key's ED25519 PEMs are
  # PKCS#8 "PRIVATE KEY" and SPKI "PUBLIC KEY", the formats token.mjs parses). Two maps, so
  # the public one never inherits the private one's sensitivity and the task definition's
  # plan diff stays readable.
  games_mp_token_private_der = {
    for id, _ in local.games_mp_token_pairs :
    id => join("", [for l in split("\n", trimspace(tls_private_key.games_mp_token[id].private_key_pem)) : l if !startswith(l, "-----")])
  }
  games_mp_token_public_der = {
    for id, _ in local.games_mp_token_pairs :
    id => join("", [for l in split("\n", trimspace(tls_private_key.games_mp_token[id].public_key_pem)) : l if !startswith(l, "-----")])
  }
  games_mp_token_signing_keys = {
    for env in local.games_mp_token_envs : env => join(",", [
      for kid in var.games_mp_token_kids : "${env}-${kid}:${local.games_mp_token_private_der["${env}-${kid}"]}"
    ])
  }
  # One environment's public keys: what games-mp-launch gives that environment's engines
  # (games_mp_launch_environments), each engine verifying tokens with its own env's keys only.
  games_mp_token_public_keys_by_env = {
    for env in local.games_mp_token_envs : env => join(",", [
      for kid in var.games_mp_token_kids : "${env}:${env}-${kid}:${local.games_mp_token_public_der["${env}-${kid}"]}"
    ])
  }
  # The router's: every environment it serves (var.mp_router_envs), newest generation first.
  games_mp_token_public_keys = join(",", [
    for env in local.games_mp_token_envs : local.games_mp_token_public_keys_by_env[env] if contains(var.mp_router_envs, env)
  ])
}

# -------------------------------------------------------------------------------------
# Generated secrets
# -------------------------------------------------------------------------------------

# One Ed25519 key pair per lobby environment and key generation (local.games_mp_token_pairs).
# Replacing one is a rotation: see "Rotating secrets" in docs/games-multiplayer.md.
resource "tls_private_key" "games_mp_token" {
  for_each  = local.games_mp_token_pairs
  algorithm = "ED25519"
}

# MP_TEST_KEY opens the hidden `mptest` game in production (x-mp-test-key); the remote e2e
# driver needs it: `terraform output -raw games_mp_test_key`.
resource "random_password" "games_mp_test_key" {
  length  = 40
  special = false
}

# CRON_SECRET guards GET /api/mp/sweep (Vercel cron sends it as a bearer token).
resource "random_password" "games_mp_cron_secret" {
  length  = 48
  special = false
}

# MP_COOKIE_SECRET signs the guest id cookie. One per environment. Production's own copy is
# needed because the lobby's fallback, SKYHOOK_LEADERBOARD_SECRET, exists only in
# Development today. Replacing it resets every guest identity, so it has no keepers.
resource "random_password" "games_mp_cookie_secret" {
  for_each = toset(["production", "preview"])
  length   = 64
  special  = false
}

# Preview's own Skyhook signing key: previews share the database but never production's key.
resource "random_password" "games_mp_skyhook_preview_secret" {
  length  = 64
  special = false
}

# Vercel requires exactly 32 alphanumerics for an automation bypass secret.
resource "random_password" "games_mp_bypass" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "games_mp_test_key" {
  name                    = "games/mp-test-key"
  description             = "MP_TEST_KEY: opens hidden multiplayer games in production. Generated by Terraform."
  recovery_window_in_days = 7
  tags                    = { Name = "games/mp-test-key", Managed = "terraform", Project = "games-multiplayer" }
}

resource "aws_secretsmanager_secret_version" "games_mp_test_key" {
  secret_id     = aws_secretsmanager_secret.games_mp_test_key.id
  secret_string = random_password.games_mp_test_key.result
}

resource "aws_secretsmanager_secret" "games_mp_cron_secret" {
  name                    = "games/mp-cron-secret"
  description             = "CRON_SECRET for the lobby's /api/mp/sweep. Generated by Terraform."
  recovery_window_in_days = 7
  tags                    = { Name = "games/mp-cron-secret", Managed = "terraform", Project = "games-multiplayer" }
}

resource "aws_secretsmanager_secret_version" "games_mp_cron_secret" {
  secret_id     = aws_secretsmanager_secret.games_mp_cron_secret.id
  secret_string = random_password.games_mp_cron_secret.result
}

resource "aws_secretsmanager_secret_policy" "games_mp_admin_only" {
  for_each = {
    test_key    = aws_secretsmanager_secret.games_mp_test_key.arn
    cron_secret = aws_secretsmanager_secret.games_mp_cron_secret.arn
  }
  secret_arn = each.value
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = each.value
      Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }
    }]
  })
}

# -------------------------------------------------------------------------------------
# Plan-time preflight (read-only)
# -------------------------------------------------------------------------------------
#
# Every input is a literal or a variable, never a managed resource's attribute, so
# Terraform reads it during the plan instead of deferring it to the apply. It returns only
# non-secret facts: the database host the migration will reach, and how the token's scope
# was proven. On 2026-09-27 every fact it checks was confirmed by hand with a team
# member's login (OIDC in Team mode, no bypass yet, the integration's variables type
# `encrypted` and decryptable on Production), so it is a formality that catches a problem
# with the Terraform-held token specifically.
#
# What it CANNOT prove read-only: that Vercel will accept the env and bypass writes. Vercel
# tokens have a user or team scope and an expiry but no per-endpoint permissions, so a
# live, unrevoked token scoped to this team (or a user token) is as far as a read can go.

data "external" "games_mp_preflight" {
  program = ["python3", local.games_mp_bringup, "preflight"]

  query = {
    region              = var.aws_region
    team_id             = var.vercel_team_id
    project_id          = local.colton_games_vercel_project_id
    vercel_token_secret = "vercel-api-token"
    copy_keys           = join(",", [for kv in split(",", local.games_mp_preview_copy) : split(":", kv)[0]])
    url_key             = "POSTGRES_URL_NON_POOLING"
    repo                = local.games_mp_repo
    github_pat_secret   = local.games_mp_github_read_secret
  }
}

# -------------------------------------------------------------------------------------
# Image builds: CodeBuild from an S3 copy of one commit
# -------------------------------------------------------------------------------------
#
# SOURCE. CoderColton/colton-games is private. No build gets a GitHub credential: games-mp-poller
# (games-multiplayer-deploy.tf) downloads each new commit with the read token in secret
# `games/colton-games-read`, repacks it, and uploads it to this bucket; CodeBuild reads only that
# object. And GitHub gets no AWS credential: AWS pulls. Rejected alternatives:
#   - `github-pat-ejc3`, the dev boxes' clone credential: it is a fine-grained token owned
#     by ejc3, and a fine-grained token can only reach repos owned by its creator or an
#     org they belong to, never another user's personal repo, collaborator or not. It is
#     also readable by every dev box (dev-instance-common.tf), so widening it would hand
#     Colton's repo to agents running there. Its 404 on the first jumpbox plan is how this
#     was found (2026-09-27).
#   - a classic ejc3 PAT with `repo` scope: it would reach colton-games, but read AND write
#     on every repo ejc3 can touch, parked on the jumpbox for a build step.
#   - CodeBuild's own GitHub source and webhook: a webhook needs admin on the repo (ejc3 has
#     write), a source credential is account-wide for every CodeBuild project, and the build
#     runs the games repo's code, which could then reach that credential through the build.
#   - a CodeConnections GitHub connection: it starts PENDING until someone completes a
#     browser handshake and installs the AWS Connector app on CoderColton, which ejc3 (a
#     collaborator, not the owner) cannot approve. Two human steps, forever in the loop.
#   - the repo's `github` provider token: it is the webhook-admin PAT for ejc3's repos and
#     cannot read CoderColton's.

# Colton's fine-grained token: resource owner CoderColton, only colton-games, Contents:
# Read-only (enough for games-mp-poller: branch heads and zipballs). Created by Colton at https://github.com/settings/personal-access-tokens/new
# and written straight into this secret, never through Terraform, so the value is not in
# state. Only administration and games-mp-poller can read it; no CodeBuild role has Secrets
# Manager access to it, and no dev box role is granted it.
#
# Bootstrap: the preflight reads this secret, so it must exist (with a value) before a full
# plan can pass. Create the container alone first:
#   terraform apply -target=aws_secretsmanager_secret.games_mp_github_read \
#                   -target=aws_secretsmanager_secret_policy.games_mp_github_read
# then put the token in, then plan normally.
locals {
  games_mp_github_read_secret = "games/colton-games-read"
}

resource "aws_secretsmanager_secret" "games_mp_github_read" {
  name                    = local.games_mp_github_read_secret
  description             = "Fine-grained GitHub token owned by CoderColton: colton-games, Contents read-only. Set by hand."
  recovery_window_in_days = 7
  tags                    = { Name = local.games_mp_github_read_secret, Managed = "terraform", Project = "games-multiplayer" }
}

resource "aws_secretsmanager_secret_policy" "games_mp_github_read" {
  secret_arn = aws_secretsmanager_secret.games_mp_github_read.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndThePollerCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.games_mp_github_read.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.games_mp_poller.arn]) } }
    }]
  })
}

resource "aws_s3_bucket" "games_mp_build" {
  bucket = "games-mp-build-${data.aws_caller_identity.current.account_id}"
  tags   = { Name = "games-mp-build", Project = "games-multiplayer" }
}

resource "aws_s3_bucket_public_access_block" "games_mp_build" {
  bucket                  = aws_s3_bucket.games_mp_build.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "games_mp_build" {
  bucket = aws_s3_bucket.games_mp_build.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Sources are only needed for the builds that use them (images, then the migration).
resource "aws_s3_bucket_lifecycle_configuration" "games_mp_build" {
  bucket = aws_s3_bucket.games_mp_build.id
  rule {
    id     = "expire-sources"
    status = "Enabled"
    filter { prefix = "sources/" }
    expiration { days = 30 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

resource "aws_s3_bucket_policy" "games_mp_build" {
  bucket = aws_s3_bucket.games_mp_build.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.games_mp_build.arn, "${aws_s3_bucket.games_mp_build.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.games_mp_build]
}

resource "aws_cloudwatch_log_group" "games_mp_codebuild" {
  name              = "/aws/codebuild/games-mp-images"
  retention_in_days = 14
}

resource "aws_iam_role" "games_mp_codebuild" {
  name = "games-mp-codebuild"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "codebuild.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/games-mp-images" }
      }
    }]
  })
  tags = { Name = "games-mp-codebuild", Project = "games-multiplayer" }
}

# Least privilege: read main's uploaded sources, write its own logs, push to the PRODUCTION
# games repositories (games/*, the router included). No Secrets Manager, no GitHub credential,
# no ECS, no IAM. It runs main's code, which is production's code. Preview builds have their
# own role (games-multiplayer-deploy.tf) that cannot push here.
resource "aws_iam_role_policy" "games_mp_codebuild" {
  name = "build-and-push-games-images"
  role = aws_iam_role.games_mp_codebuild.id
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
        Sid      = "Logs"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.games_mp_codebuild.arn}:*"
      },
      {
        Sid      = "EcrLogin"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "PushGamesImages"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
          "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload",
          "ecr:PutImage", "ecr:UploadLayerPart",
        ]
        Resource = local.games_mp_production_repo_arns
      },
    ]
  })
}

# Smallest ARM compute (2 vCPU / 4 GB), native arm64 builds for Fargate ARM64. Privileged
# because the build runs docker. Node 24 comes from the image's runtime-versions.
resource "aws_codebuild_project" "games_mp_images" {
  name          = "games-mp-images"
  description   = "Builds and pushes the games multiplayer images of a colton-games main commit (games-mp-poller starts it)"
  service_role  = aws_iam_role.games_mp_codebuild.arn
  build_timeout = 30

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

    # What this project builds and where it pushes (bringup.py codebuild-images); the poller
    # sets only GAMES_MP_COMMIT.
    environment_variable {
      name  = "GAMES_MP_CHANNEL"
      value = "main"
    }

    environment_variable {
      name  = "GAMES_MP_ENGINE_REPOSITORY"
      value = local.mp_engine_channels.main.repository
    }

    environment_variable {
      name  = "GAMES_MP_MAX_CPU"
      value = tostring(local.mp_engine_max_cpu)
    }

    environment_variable {
      name  = "GAMES_MP_MAX_MEMORY"
      value = tostring(local.mp_engine_max_memory)
    }
  }

  # One build at a time: games-mp-release orders main releases anyway, and the poller starts
  # the next head when this one is done.
  concurrent_build_limit = 1

  # Each build overrides the location with sources/main/<commit>.zip.
  source {
    type      = "S3"
    location  = "${aws_s3_bucket.games_mp_build.bucket}/sources/main/none.zip"
    buildspec = file("${path.module}/games-multiplayer/buildspec.yml")
  }

  logs_config {
    cloudwatch_logs {
      group_name = aws_cloudwatch_log_group.games_mp_codebuild.name
    }
  }

  tags = { Name = "games-mp-images", Project = "games-multiplayer" }
}

# -------------------------------------------------------------------------------------
# Vercel: env, automation bypass, OIDC issuer mode, Preview's Supabase
# -------------------------------------------------------------------------------------
#
# Only the multiplayer set below is managed here; every other env var of the project stays
# managed in Vercel (vercel-cc-games.tf). Env changes reach a deployment only when it is
# (re)built, so they take effect on the next production deploy and on new previews.
#
# Production and Preview get the same settings, except:
#   MP_ENV   production / preview (the lobby requires MP_ENV == VERCEL_ENV)
#   MP_API   production only: https://cc-games.app. Preview derives https://$VERCEL_URL per
#            deployment, so engines call back the preview that launched them.
#   MP_LAUNCH_ROLE_ARN, MP_LAUNCH_FUNCTION  each environment's own launcher role and its own
#            launch function, games-mp-launch-<environment> (games-multiplayer.tf): the
#            function fixes the engines' environment and ceiling, so Preview must never hold
#            production's.
#   Preview only: SKYHOOK_LEADERBOARD_ENVIRONMENT=preview and its own Skyhook key, because
#            Preview now shares the Supabase database (rows are scoped by environment).
# Development gets nothing: local development uses MP_LAUNCHER=local, and the launcher
# role and the router trust only production and preview.

locals {
  games_mp_vercel_shared_config = {
    # The launch function's region. The cluster, subnets and security group are
    # games-mp-launch's own settings now; the lobby never names them.
    MP_REGION = var.aws_region
    # Pins the AWS SDK's region; Vercel otherwise sets AWS_REGION to the function's own
    # region, which can move (https://vercel.com/docs/oidc/aws).
    AWS_REGION      = var.aws_region
    MP_LAUNCHER     = "ecs"
    MP_PUBLIC_ENTRY = "wss://${local.mp_play_domain}"
  }

  # "<KEY>" targets both environments; "<KEY>/<env>" targets one. Sensitive values stay out
  # of this map (for_each keys and metadata must not be sensitive); they are looked up from
  # games_mp_vercel_secret_values by the same name.
  games_mp_vercel_env = merge(
    { for k, v in local.games_mp_vercel_shared_config : k => { targets = ["production", "preview"], sensitive = false, value = v } },
    {
      "MP_ENV/production" = { targets = ["production"], sensitive = false, value = "production" }
      "MP_ENV/preview"    = { targets = ["preview"], sensitive = false, value = "preview" }
      "MP_API/production" = { targets = ["production"], sensitive = false, value = "https://cc-games.app" }
      # New key names rather than per-environment copies of MP_ROLE_ARN: Terraform may create
      # the per-environment variables before it deletes the shared one, and Vercel refuses two
      # variables with one key on the same target.
      "MP_LAUNCH_ROLE_ARN/production" = { targets = ["production"], sensitive = false, value = aws_iam_role.games_mp_launcher["production"].arn }
      "MP_LAUNCH_ROLE_ARN/preview"    = { targets = ["preview"], sensitive = false, value = aws_iam_role.games_mp_launcher["preview"].arn }
      "MP_LAUNCH_FUNCTION/production" = { targets = ["production"], sensitive = false, value = aws_lambda_function.games_mp_launch["production"].arn }
      "MP_LAUNCH_FUNCTION/preview"    = { targets = ["preview"], sensitive = false, value = aws_lambda_function.games_mp_launch["preview"].arn }
      # Launch admission (games-multiplayer.tf locals): pinned here, not left to code defaults.
      "MP_MAX_ACTIVE_MATCHES/production"        = { targets = ["production"], sensitive = false, value = tostring(local.games_mp_lobby_max_active.production) }
      "MP_MAX_ACTIVE_MATCHES/preview"           = { targets = ["preview"], sensitive = false, value = tostring(local.games_mp_lobby_max_active.preview) }
      "MP_IP_MAX_ACTIVE"                        = { targets = ["production", "preview"], sensitive = false, value = tostring(local.games_mp_ip_max_active) }
      "MP_IP_MAX_PER_HOUR"                      = { targets = ["production", "preview"], sensitive = false, value = tostring(local.games_mp_ip_max_per_hour) }
      "SKYHOOK_LEADERBOARD_ENVIRONMENT/preview" = { targets = ["preview"], sensitive = false, value = "preview" }
      # Each environment's own private keys: never one variable spanning both targets.
      "MP_TOKEN_SIGNING_KEYS/production"   = { targets = ["production"], sensitive = true, value = null }
      "MP_TOKEN_SIGNING_KEYS/preview"      = { targets = ["preview"], sensitive = true, value = null }
      "MP_TEST_KEY"                        = { targets = ["production", "preview"], sensitive = true, value = null }
      "CRON_SECRET"                        = { targets = ["production", "preview"], sensitive = true, value = null }
      "MP_COOKIE_SECRET/production"        = { targets = ["production"], sensitive = true, value = null }
      "MP_COOKIE_SECRET/preview"           = { targets = ["preview"], sensitive = true, value = null }
      "SKYHOOK_LEADERBOARD_SECRET/preview" = { targets = ["preview"], sensitive = true, value = null }
    },
  )

  games_mp_vercel_secret_values = {
    "MP_TOKEN_SIGNING_KEYS/production"   = local.games_mp_token_signing_keys["production"]
    "MP_TOKEN_SIGNING_KEYS/preview"      = local.games_mp_token_signing_keys["preview"]
    "MP_TEST_KEY"                        = random_password.games_mp_test_key.result
    "CRON_SECRET"                        = random_password.games_mp_cron_secret.result
    "MP_COOKIE_SECRET/production"        = random_password.games_mp_cookie_secret["production"].result
    "MP_COOKIE_SECRET/preview"           = random_password.games_mp_cookie_secret["preview"].result
    "SKYHOOK_LEADERBOARD_SECRET/preview" = random_password.games_mp_skyhook_preview_secret.result
  }
}

resource "vercel_project_environment_variable" "games_mp" {
  for_each   = local.games_mp_vercel_env
  depends_on = [data.external.games_mp_preflight]

  team_id    = var.vercel_team_id
  project_id = local.colton_games_vercel_project_id
  key        = split("/", each.key)[0]
  value      = each.value.sensitive ? local.games_mp_vercel_secret_values[each.key] : each.value.value
  target     = each.value.targets
  sensitive  = each.value.sensitive
  comment    = "games multiplayer; managed by ejc3/aws games-multiplayer-bringup.tf"
}

# Deployment Protection covers every preview. Engines launched by a preview lobby call back
# its protected URL, and the remote e2e driver drives previews, so both present this
# secret as x-vercel-protection-bypass. is_env_var exposes it to deployments as
# VERCEL_AUTOMATION_BYPASS_SECRET; the lobby hands it to engines as MP_API_BYPASS.
resource "vercel_project_protection_bypass" "games_mp" {
  team_id    = var.vercel_team_id
  project_id = local.colton_games_vercel_project_id
  secret     = random_password.games_mp_bypass.result
  is_env_var = true
  note       = "games multiplayer: engine callbacks and remote e2e (ejc3/aws)"
  depends_on = [data.external.games_mp_preflight]
}

# OIDC Team issuer mode. The provider can set oidc_token_config only on a whole
# vercel_project resource, which would mean importing the project and letting Terraform own
# every one of its settings. This step instead GETs the project, PATCHes only
# oidcTokenConfig if it is not already {enabled, team}, and verifies with a second GET.
# (It was already on in Team mode on 2026-09-27; the step keeps it that way.)
resource "terraform_data" "games_mp_vercel_oidc" {
  triggers_replace = {
    project = local.colton_games_vercel_project_id
    mode    = "team"
    # Live oidcTokenConfig, read fresh by every plan (data.external.games_mp_preflight is
    # not deferred to apply). If Vercel drifts out of Team issuer mode after this first
    # converges, that shows up here as a changed trigger, so terraform_data is replaced and
    # its creation provisioner (cmd_vercel_oidc) runs again and puts it back. Without this,
    # the two constants above never change and the provisioner would never run a second time
    # at all, however far OIDC drifted.
    live = data.external.games_mp_preflight.result.oidc_state
  }

  provisioner "local-exec" {
    command = "python3 ${local.games_mp_bringup} vercel-oidc --region ${var.aws_region} --team-id ${var.vercel_team_id} --project-id ${local.colton_games_vercel_project_id}"
  }

  depends_on = [data.external.games_mp_preflight]
}

# Preview's Supabase connection. The integration (store_XlZp0ZAQE6nghCEU) is connected to
# Production and Development only. Vercel's API has no documented call to add an
# environment to an EXISTING integration connection (only "connect resource to project",
# whose effect on the live Production connection is undocumented), so this step copies the
# values the lobby and Skyhook read (games_mp_preview_copy) from
# Production into Preview-only variables. It never writes a variable that reaches
# Production or Development, and refuses if one with the same key spans Preview and
# another environment. The values pass through memory only: not in config, not in state.
# If the integration rotates its keys, bump var.games_mp_preview_supabase_sync.
resource "terraform_data" "games_mp_preview_supabase" {
  triggers_replace = {
    project = local.colton_games_vercel_project_id
    keys    = local.games_mp_preview_copy
    sync    = var.games_mp_preview_supabase_sync
  }

  provisioner "local-exec" {
    command = "python3 ${local.games_mp_bringup} preview-supabase --region ${var.aws_region} --team-id ${var.vercel_team_id} --project-id ${local.colton_games_vercel_project_id} --keys ${local.games_mp_preview_copy}"
  }

  depends_on = [data.external.games_mp_preflight]
}

# -------------------------------------------------------------------------------------
# Verification
# -------------------------------------------------------------------------------------
#
# After every router change: waits until the service's PRIMARY deployment runs this task
# definition with its desired count (a circuit-breaker rollback FAILS the apply rather than
# passing on the old version), every task of that deployment is a healthy target, and GET
# /healthz answers 200 `ok` over TLS with the certificate verified for play.cc-games.app. It
# does not wait for ECS's COMPLETED: the old tasks drain for up to an hour first (live matches
# keep their connections). Bounded: 15 minutes, then it fails loudly.
resource "terraform_data" "games_mp_healthy" {
  count = local.mp_router_enabled ? 1 : 0

  # The task definition ARN changes with the image AND with every token-key change (the
  # public keys are in its environment), so this covers rotations too.
  triggers_replace = {
    task_definition = aws_ecs_task_definition.games_mp_router[0].arn
  }

  provisioner "local-exec" {
    command = "python3 ${local.games_mp_bringup} wait-healthy --region ${var.aws_region} --cluster ${aws_ecs_cluster.games.name} --service ${aws_ecs_service.games_mp_router[0].name} --task-definition-arn ${aws_ecs_task_definition.games_mp_router[0].arn} --target-group-arn ${aws_lb_target_group.games_mp_router.arn} --alb-dns ${aws_lb.games_play.dns_name} --host ${local.mp_play_domain}"
  }

  depends_on = [
    aws_ecs_service.games_mp_router,
    aws_lb_listener.games_play_https,
    cloudflare_dns_record.games_play,
  ]
}

# -------------------------------------------------------------------------------------
# Outputs for the remote e2e driver (sensitive: shown only with -raw / -json)
# -------------------------------------------------------------------------------------

output "games_mp_test_key" {
  description = "MP_TEST_KEY for the remote e2e run against production: terraform output -raw games_mp_test_key"
  value       = random_password.games_mp_test_key.result
  sensitive   = true
}

output "games_mp_cron_secret" {
  description = "CRON_SECRET, for an authorised /api/mp/sweep in the remote e2e run"
  value       = random_password.games_mp_cron_secret.result
  sensitive   = true
}

output "games_mp_automation_bypass_secret" {
  description = "x-vercel-protection-bypass value for driving preview deployments"
  value       = random_password.games_mp_bypass.result
  sensitive   = true
}

# imagine.tf
#
# AWS half of Imagine (ejc3/imagine), a collaborative canvas editor: an Elixir cluster with a
# Rust document engine, on Fargate, that runs ZERO tasks while nobody has a document open.
# The web app is a Next.js project on Vercel (team ejc3-7031s-projects, project imagine).
# The design is in that repo's docs/ARCHITECTURE.md.
#
#   browser --https--> Vercel (Google sign-in)
#       POST /api/session --OIDC--> role imagine-waker --lambda:InvokeFunction-->
#           imagine-scale {"action":"wake"}  --ecs:UpdateService--> service imagine, 0 -> 2
#       <-- a join token (Ed25519, signed by the web app) and the socket URL
#   browser --wss://imagine.play.cc-games.app/socket--> ALB games-play (host rule, below)
#       --> imagine tasks (imagine-server SG), which cluster through Cloud Map DNS and keep
#           each open document on one node; snapshots and ownership leases in S3
#   EventBridge Scheduler (1 min) --> imagine-scale {"action":"sweep"}: no socket for 5 min
#       --> desired count 0
#   GitHub Actions on ejc3/imagine main --OIDC--> role imagine-deploy --> ECR push (commit
#       tag + `live`) --> imagine-scale {"action":"deploy"}
#
# WHAT IT SHARES WITH THE GAMES PLATFORM, and nothing else: the games-play load balancer
# (one host rule, its wildcard certificate and wildcard DNS record, its WAF and access
# logs) and the games route table (internet both ways, no route to the I/O box peer). It
# has its own ECS cluster on purpose: games-mp-sweeper stops every task in the `games`
# cluster that outlives a match, and the launch functions count them.
#
# WHAT IT COSTS AT REST: no compute. The standing charges are the Cloud Map private zone
# ($0.50/month), one Secrets Manager secret ($0.40/month), and cents of ECR, S3 and logs.
# Awake it is two 0.25 vCPU arm64 Fargate tasks (about $0.02/hour together) plus a public
# IPv4 each. games-ecs-daily (games-multiplayer.tf) already alarms on ECS spend as a whole.
#
# BRING-UP, once, in this order (README of ejc3/imagine, "Deployment"):
#   1. Apply this. The service is created at 0 tasks, so no image need exist yet.
#   2. In ejc3/imagine set the Actions variables from the imagine_deploy output and run the
#      deploy workflow: it pushes the first `live` image.
#   3. In the Vercel project set the environment from the imagine_vercel_env output, plus
#      the secrets Terraform does not hold (the token signing key, AUTH_SECRET, the Google
#      OAuth client).
# After that a deploy is a merge to main there, and nothing here names an image commit.

locals {
  imagine_port = 8080
  # Under the games wildcard: *.play.cc-games.app already has a certificate on the ALB and
  # a DNS record pointing at it, so this name needs neither.
  imagine_host = "imagine.${local.mp_play_domain}"

  # The Vercel team and project. The OIDC issuer, audience and subject embed these strings;
  # renaming either in Vercel locks the web app out until this file changes with it.
  imagine_vercel_team    = "ejc3-7031s-projects"
  imagine_vercel_project = "imagine"
  imagine_vercel_oidc    = "oidc.vercel.com/${local.imagine_vercel_team}"

  # The mutable tag the task definition runs; every other tag is a commit and never moves.
  imagine_live_tag = "live"

  # How many tasks "awake" is. Two, so that production always runs as a cluster.
  imagine_awake_count = 2
}

# imagine-pink.vercel.app is the project's production domain (Vercel assigned it); the
# other is the alias Vercel gives every production deployment of a team project.
variable "imagine_allowed_origins" {
  description = "Browser origins the imagine backend accepts sockets from: the Vercel project's production domains."
  type        = list(string)
  default     = ["https://imagine-pink.vercel.app", "https://imagine-ejc3-7031s-projects.vercel.app"]
}

variable "imagine_token_public_keys" {
  description = "Join-token PUBLIC keys the backend accepts, <kid>:<base64 raw Ed25519 public key>, comma-separated. The private halves exist only in the Vercel project's environment (ejc3/imagine: scripts/dev-keys.mjs --print)."
  type        = string
  default     = "prod1:gJ8nzWb4XImbFYO0il1ROEUCsqP4gBUPvdZdfjwiq5E="
}

# -------------------------------------------------------------------------------------
# Image repository
# -------------------------------------------------------------------------------------
#
# Commit tags are immutable, so a rollback to a commit means that commit's bytes. `live`
# is the one tag that moves (the deploy workflow moves it).

resource "aws_ecr_repository" "imagine" {
  name                 = "imagine/server"
  image_tag_mutability = "IMMUTABLE_WITH_EXCLUSION"

  image_tag_mutability_exclusion_filter {
    filter      = local.imagine_live_tag
    filter_type = "WILDCARD"
  }

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "imagine/server", Project = "imagine" }
}

# Rule 1 matches the live image first and never expires it (an image matched by a rule is
# not considered by later ones), so rule 2 can trim old commits without stranding the
# service on a deleted image.
resource "aws_ecr_lifecycle_policy" "imagine" {
  repository = aws_ecr_repository.imagine.name
  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Never the live image"
        selection = {
          tagStatus      = "tagged"
          tagPatternList = [local.imagine_live_tag]
          countType      = "imageCountMoreThan"
          countNumber    = 1000
        }
        action = { type = "expire" }
      },
      {
        rulePriority = 2
        description  = "Keep the 20 newest images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 20
        }
        action = { type = "expire" }
      },
    ]
  })
}

# -------------------------------------------------------------------------------------
# Documents
# -------------------------------------------------------------------------------------
#
# Two small objects per document under docs/: <id>.json (the snapshot) and <id>.lease (who
# owns it). Every write is conditional (If-Match / If-None-Match), which S3 supports on
# general purpose buckets. Not versioned: an open document rewrites its lease every ten
# seconds, and each rewrite would be kept.

resource "aws_s3_bucket" "imagine_docs" {
  bucket = "imagine-docs-${data.aws_caller_identity.current.account_id}"
  tags   = { Name = "imagine-docs", Project = "imagine" }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_public_access_block" "imagine_docs" {
  bucket                  = aws_s3_bucket.imagine_docs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "imagine_docs" {
  bucket = aws_s3_bucket.imagine_docs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "imagine_docs" {
  bucket = aws_s3_bucket.imagine_docs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# -------------------------------------------------------------------------------------
# Cluster, logs, and the cluster cookie
# -------------------------------------------------------------------------------------

resource "aws_ecs_cluster" "imagine" {
  name = "imagine"

  setting {
    name  = "containerInsights"
    value = "disabled"
  }

  tags = { Name = "imagine", Project = "imagine" }
}

resource "aws_ecs_cluster_capacity_providers" "imagine" {
  cluster_name       = aws_ecs_cluster.imagine.name
  capacity_providers = ["FARGATE"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 1
  }
}

resource "aws_cloudwatch_log_group" "imagine_server" {
  name              = "/imagine/server"
  retention_in_days = 14
}

# The Erlang distribution cookie: what one node must present to another to join the
# cluster. The security group already admits distribution only from the service's own
# tasks; this is the second factor. Replacing it needs every task replaced at once (old
# and new cookies cannot cluster), which a wake from zero does anyway.
resource "random_password" "imagine_cookie" {
  length  = 48
  special = false
}

resource "aws_secretsmanager_secret" "imagine_cookie" {
  name                    = "imagine/release-cookie"
  description             = "Erlang distribution cookie of the imagine backend. Generated by Terraform."
  recovery_window_in_days = 7
  tags                    = { Name = "imagine/release-cookie", Managed = "terraform", Project = "imagine" }
}

resource "aws_secretsmanager_secret_version" "imagine_cookie" {
  secret_id     = aws_secretsmanager_secret.imagine_cookie.id
  secret_string = random_password.imagine_cookie.result
}

# Administration, and the execution role that injects it into the task.
resource "aws_secretsmanager_secret_policy" "imagine_cookie" {
  secret_arn = aws_secretsmanager_secret.imagine_cookie.arn
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "OnlyAdministrationAndTheTaskCanRead"
      Effect    = "Deny"
      Principal = "*"
      Action    = "secretsmanager:GetSecretValue"
      Resource  = aws_secretsmanager_secret.imagine_cookie.arn
      Condition = { ArnNotLike = { "aws:PrincipalArn" = concat(local.games_mp_admin_principals, [aws_iam_role.imagine_execution.arn]) } }
    }]
  })
}

# -------------------------------------------------------------------------------------
# Task IAM
# -------------------------------------------------------------------------------------

resource "aws_iam_role" "imagine_execution" {
  name               = "imagine-execution"
  description        = "ECS execution role for the imagine backend: pull its image, write /imagine/server, read the cluster cookie."
  assume_role_policy = local.ecs_tasks_trust
}

resource "aws_iam_role_policy" "imagine_execution" {
  name = "pull-log-and-cookie"
  role = aws_iam_role.imagine_execution.id
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
        Resource = aws_ecr_repository.imagine.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.imagine_server.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.imagine_cookie.arn
      },
    ]
  })
}

# What the running backend may do in AWS: read and write its documents. Nothing else, and
# in particular nothing about ECS: a task cannot change how many tasks there are.
resource "aws_iam_role" "imagine_task" {
  name               = "imagine-task"
  description        = "imagine backend task role: its documents in S3."
  assume_role_policy = local.ecs_tasks_trust
}

resource "aws_iam_role_policy" "imagine_task" {
  name = "documents"
  role = aws_iam_role.imagine_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.imagine_docs.arn}/docs/*"
      },
      {
        # Without ListBucket, S3 answers 403 instead of 404 for a document that does not
        # exist yet, and the backend (rightly) refuses to treat an error as "new document".
        # No s3:prefix condition: a GetObject carries no prefix, so the condition would
        # never hold for exactly the request this is here for.
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.imagine_docs.arn
      },
    ]
  })
}

# -------------------------------------------------------------------------------------
# Network
# -------------------------------------------------------------------------------------
#
# Its own subnets (10.0.70.0/24, 10.0.71.0/24) on the games route table: internet both
# ways, and no route to the I/O box's VPC peer, for the reason given at "Network" in
# games-multiplayer.tf. Tasks get a public IPv4 for outbound only (ECR, S3, logs).
#
# The security group is the whole inbound story:
#   8080        from the games-play ALB           sockets and health checks
#   4369, 9100  from this group itself            Erlang distribution between the tasks
#                                                 (epmd, and the one port the release pins)
# and outbound: 443 anywhere (ECR, S3, logs, Secrets Manager), distribution to itself, and
# the ECS endpoint where a task reads its credentials and its own address.

resource "aws_subnet" "imagine" {
  for_each = {
    a = { az = aws_subnet.subnet_a.availability_zone, index = 70 }
    b = { az = aws_subnet.subnet_b.availability_zone, index = 71 }
  }

  vpc_id                          = local.vpc_id
  cidr_block                      = cidrsubnet(data.aws_vpc.selected.cidr_block, 8, each.value.index)
  availability_zone               = each.value.az
  ipv6_cidr_block                 = cidrsubnet(aws_vpc_ipv6_cidr_block_association.main.ipv6_cidr_block, 8, each.value.index)
  assign_ipv6_address_on_creation = true
  map_public_ip_on_launch         = false

  tags = { Name = "imagine-${each.key}", Project = "imagine" }
}

resource "aws_route_table_association" "imagine" {
  for_each       = aws_subnet.imagine
  subnet_id      = each.value.id
  route_table_id = aws_route_table.games.id
}

resource "aws_security_group" "imagine_server" {
  name        = "imagine-server"
  description = "imagine backend tasks: 8080 from the games-play ALB, distribution among themselves"
  vpc_id      = local.vpc_id
  tags        = { Name = "imagine-server", Project = "imagine" }
}

resource "aws_vpc_security_group_ingress_rule" "imagine_from_alb" {
  security_group_id            = aws_security_group.imagine_server.id
  referenced_security_group_id = aws_security_group.games_alb.id
  ip_protocol                  = "tcp"
  from_port                    = local.imagine_port
  to_port                      = local.imagine_port
  description                  = "ALB listener traffic and /healthz checks"
}

resource "aws_vpc_security_group_ingress_rule" "imagine_distribution" {
  for_each = { epmd = 4369, dist = 9100 }

  security_group_id            = aws_security_group.imagine_server.id
  referenced_security_group_id = aws_security_group.imagine_server.id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  description                  = "Erlang distribution (${each.key}) from the other imagine tasks"
}

resource "aws_vpc_security_group_egress_rule" "imagine_distribution" {
  for_each = { epmd = 4369, dist = 9100 }

  security_group_id            = aws_security_group.imagine_server.id
  referenced_security_group_id = aws_security_group.imagine_server.id
  ip_protocol                  = "tcp"
  from_port                    = each.value
  to_port                      = each.value
  description                  = "Erlang distribution (${each.key}) to the other imagine tasks"
}

resource "aws_vpc_security_group_egress_rule" "imagine_https" {
  for_each          = { v4 = { cidr4 = "0.0.0.0/0", cidr6 = null }, v6 = { cidr4 = null, cidr6 = "::/0" } }
  security_group_id = aws_security_group.imagine_server.id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = each.value.cidr4
  cidr_ipv6         = each.value.cidr6
  description       = "HTTPS out (${each.key}): ECR, S3, CloudWatch Logs, Secrets Manager"
}

# Link-local traffic is not filtered by security groups; this rule states the dependency
# so tightening egress can never silently break it.
resource "aws_vpc_security_group_egress_rule" "imagine_task_endpoint" {
  security_group_id = aws_security_group.imagine_server.id
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
  cidr_ipv4         = "169.254.170.2/32"
  description       = "ECS task endpoint (task role credentials and the task address)"
}

# -------------------------------------------------------------------------------------
# How the tasks find each other
# -------------------------------------------------------------------------------------
#
# ECS registers every task of the service under one name; each task resolves it every few
# seconds and connects to the addresses it does not know (DNS_CLUSTER_QUERY). A task that
# has not found the others yet is still correct: document ownership is a lease in S3, so
# it refuses a document another task holds rather than opening a second copy.

resource "aws_service_discovery_private_dns_namespace" "imagine" {
  name        = "imagine.internal"
  description = "imagine backend tasks, for clustering"
  vpc         = local.vpc_id
  tags        = { Name = "imagine.internal", Project = "imagine" }
}

resource "aws_service_discovery_service" "imagine_nodes" {
  name = "nodes"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.imagine.id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "A"
      ttl  = 5
    }
  }

  tags = { Name = "nodes.imagine.internal", Project = "imagine" }
}

# -------------------------------------------------------------------------------------
# The entry: one host rule on the games-play load balancer
# -------------------------------------------------------------------------------------
#
# Health checks every 5 s so a woken task takes traffic about 10 s after it answers; the
# whole cold start is then mostly Fargate's own (image pull, boot). Deregistration waits
# 30 s, not the hour the router gets: a client that loses its socket reconnects and rejoins
# from the stored document, so nothing is held open for it.

resource "aws_lb_target_group" "imagine" {
  name                 = "imagine-server"
  port                 = local.imagine_port
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = local.vpc_id
  deregistration_delay = 30

  health_check {
    path                = "/healthz"
    matcher             = "200"
    interval            = 5
    timeout             = 3
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }

  tags = { Name = "imagine-server", Project = "imagine" }
}

# Everything else on the listener still goes to mp-router (its default action). With no
# healthy target, which is the resting state, the ALB answers this host with 503 and the
# web app's client keeps retrying while the service wakes.
resource "aws_lb_listener_rule" "imagine" {
  listener_arn = aws_lb_listener.games_play_https.arn
  priority     = 10

  condition {
    host_header {
      values = [local.imagine_host]
    }
  }

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.imagine.arn
  }

  tags = { Name = "imagine", Project = "imagine" }
}

# -------------------------------------------------------------------------------------
# Task definition and service
# -------------------------------------------------------------------------------------

resource "aws_ecs_task_definition" "imagine" {
  family                   = "imagine"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.imagine_execution.arn
  task_role_arn            = aws_iam_role.imagine_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([{
    name         = "server"
    image        = "${aws_ecr_repository.imagine.repository_url}:${local.imagine_live_tag}"
    essential    = true
    portMappings = [{ containerPort = local.imagine_port, protocol = "tcp" }]
    environment = [
      { name = "PORT", value = tostring(local.imagine_port) },
      # The token audience: the web app signs tokens for this value and no other.
      { name = "IMAGINE_ENV", value = "production" },
      # PUBLIC keys only; the backend can check a join token but never mint one.
      { name = "IMAGINE_TOKEN_PUBLIC_KEYS", value = var.imagine_token_public_keys },
      { name = "IMAGINE_ALLOWED_ORIGINS", value = join(",", var.imagine_allowed_origins) },
      { name = "IMAGINE_STORE", value = "s3:${aws_s3_bucket.imagine_docs.bucket}/docs/" },
      { name = "AWS_REGION", value = var.aws_region },
      { name = "DNS_CLUSTER_QUERY", value = "${aws_service_discovery_service.imagine_nodes.name}.${aws_service_discovery_private_dns_namespace.imagine.name}" },
    ]
    secrets = [
      { name = "RELEASE_COOKIE", valueFrom = aws_secretsmanager_secret.imagine_cookie.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.imagine_server.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "server"
      }
    }
    # On SIGTERM every open document saves and releases its lease before the node exits.
    stopTimeout = 30
  }])

  tags = { Name = "imagine", Project = "imagine" }

  depends_on = [aws_secretsmanager_secret_version.imagine_cookie]
}

# Created at ZERO tasks, and Terraform never sets the count again: imagine-scale owns it
# (lifecycle below). Zero tasks also means this needs no image to exist when it is applied.
resource "aws_ecs_service" "imagine" {
  name            = "imagine"
  cluster         = aws_ecs_cluster.imagine.id
  task_definition = aws_ecs_task_definition.imagine.arn
  desired_count   = 0
  # Plain FARGATE, not Spot: a reclaim would drop every open socket at once.
  launch_type                        = "FARGATE"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  health_check_grace_period_seconds  = 60
  enable_ecs_managed_tags            = true
  propagate_tags                     = "SERVICE"

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets         = [for s in aws_subnet.imagine : s.id]
    security_groups = [aws_security_group.imagine_server.id]
    # Outbound only: the security group admits nothing but the ALB and its own tasks.
    assign_public_ip = true
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.imagine.arn
    container_name   = "server"
    container_port   = local.imagine_port
  }

  service_registries {
    registry_arn = aws_service_discovery_service.imagine_nodes.arn
  }

  # ECS refuses a target group no listener uses yet, and a task needs its subnet's route
  # table before it can pull an image.
  depends_on = [aws_lb_listener_rule.imagine, aws_route_table_association.imagine]

  tags = { Name = "imagine", Project = "imagine" }

  # desired_count: imagine-scale's. tags: it keeps its state in two of them
  # (imagine:wanted-at, imagine:unreachable-since), which Terraform must not remove.
  lifecycle {
    ignore_changes = [desired_count, tags, tags_all]
  }
}

# -------------------------------------------------------------------------------------
# imagine-scale: the only thing that changes the task count
# -------------------------------------------------------------------------------------
#
# Source: imagine/scale.py (offline test: scripts/test-imagine-scale.py). Three callers,
# each of which can do nothing in AWS but invoke it:
#   the web app      {"action":"wake"}     role imagine-waker (Vercel OIDC, production only)
#   the schedule     {"action":"sweep"}    every minute
#   the image build  {"action":"deploy"}   role imagine-deploy (GitHub OIDC, main only)
# It sets the count to 0 or local.imagine_awake_count and to nothing else, so none of them
# can run more than that. Reserved concurrency 1 runs the actions one at a time.

data "archive_file" "imagine_scale" {
  type        = "zip"
  source_file = "${path.module}/imagine/scale.py"
  output_path = "${path.module}/.terraform/imagine-scale.zip"
}

resource "aws_cloudwatch_log_group" "imagine_scale" {
  name              = "/aws/lambda/imagine-scale"
  retention_in_days = 14
}

resource "aws_iam_role" "imagine_scale" {
  name        = "imagine-scale"
  description = "imagine-scale Lambda: set the imagine service's desired count, roll it, tag it"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = { Name = "imagine-scale", Project = "imagine" }
}

resource "aws_iam_role_policy" "imagine_scale" {
  name = "scale-the-imagine-service"
  role = aws_iam_role.imagine_scale.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # This one service: its count, a new deployment, and its two state tags.
        Effect   = "Allow"
        Action   = ["ecs:DescribeServices", "ecs:UpdateService", "ecs:TagResource", "ecs:ListTagsForResource"]
        Resource = aws_ecs_service.imagine.id
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.imagine_scale.arn}:*"
      },
      {
        # To report stopping a backend that would not answer.
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      },
    ]
  })
}

resource "aws_lambda_function" "imagine_scale" {
  function_name    = "imagine-scale"
  role             = aws_iam_role.imagine_scale.arn
  handler          = "scale.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 30
  memory_size      = 128
  filename         = data.archive_file.imagine_scale.output_path
  source_code_hash = data.archive_file.imagine_scale.output_base64sha256

  reserved_concurrent_executions = 1

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.imagine_scale.name
  }

  environment {
    variables = {
      CLUSTER     = aws_ecs_cluster.imagine.name
      SERVICE     = aws_ecs_service.imagine.name
      AWAKE_COUNT = tostring(local.imagine_awake_count)
      IDLE_SEC    = "300"
      GRACE_SEC   = "600"
      TOUCH_SEC   = "60"
      # Through the front door, as a visitor reaches it: if this answers, sockets can too.
      STATUS_URL    = "https://${local.imagine_host}/api/status"
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
    }
  }

  tags = { Name = "imagine-scale", Project = "imagine" }
}

# The schedule invokes asynchronously; a failed sweep is replaced by the next one.
resource "aws_lambda_function_event_invoke_config" "imagine_scale" {
  function_name                = aws_lambda_function.imagine_scale.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 60
}

resource "aws_iam_role" "imagine_scale_scheduler" {
  name = "imagine-scale-scheduler"
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

resource "aws_iam_role_policy" "imagine_scale_scheduler" {
  name = "invoke-imagine-scale"
  role = aws_iam_role.imagine_scale_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.imagine_scale.arn
    }]
  })
}

# Every minute: an idle backend is stopped at most a minute after its five idle minutes.
resource "aws_scheduler_schedule" "imagine_sweep" {
  name       = "imagine-sweep"
  group_name = "default"

  flexible_time_window { mode = "OFF" }

  schedule_expression = "rate(1 minute)"

  target {
    arn      = aws_lambda_function.imagine_scale.arn
    role_arn = aws_iam_role.imagine_scale_scheduler.arn
    input    = jsonencode({ action = "sweep" })

    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 60
    }
  }
}

# The sweep is what stops an idle backend, so a broken sweep must not be silent: the same
# two alarms as games-mp-sweeper, for the same two failures (it raised; it did not run).
resource "aws_cloudwatch_metric_alarm" "imagine_scale_errors" {
  alarm_name          = "imagine-scale-errors"
  alarm_description   = "imagine-scale raised or timed out; the imagine backend may be running with nobody using it, or failing to wake"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.imagine_scale.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "imagine_scale_silent" {
  alarm_name          = "imagine-scale-not-running"
  alarm_description   = "imagine-scale has not been invoked for 15 minutes (schedule or its role broken); an idle imagine backend would not be stopped"
  namespace           = "AWS/Lambda"
  metric_name         = "Invocations"
  dimensions          = { FunctionName = aws_lambda_function.imagine_scale.function_name }
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

# -------------------------------------------------------------------------------------
# Who may invoke it: the web app, and the image build
# -------------------------------------------------------------------------------------
#
# Vercel OIDC, as for the games lobby (games-multiplayer.tf, "Vercel OIDC and the launcher
# roles"), but another team, so another provider: the issuer embeds the team. Production
# only: preview and development deployments cannot start tasks. The web app must call
# awsCredentialsProvider({ roleArn }) with no audience, for the reason given there.

resource "aws_iam_openid_connect_provider" "vercel_imagine" {
  url            = "https://${local.imagine_vercel_oidc}"
  client_id_list = ["https://vercel.com/${local.imagine_vercel_team}"]
  tags           = { Name = "vercel-${local.imagine_vercel_team}", Project = "imagine" }
}

resource "aws_iam_role" "imagine_waker" {
  name                 = "imagine-waker"
  description          = "Assumed by the imagine web app's production deployments via Vercel OIDC to invoke imagine-scale"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.vercel_imagine.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.imagine_vercel_oidc}:aud" = "https://vercel.com/${local.imagine_vercel_team}"
          "${local.imagine_vercel_oidc}:sub" = "owner:${local.imagine_vercel_team}:project:${local.imagine_vercel_project}:environment:production"
        }
      }
    }]
  })
  tags = { Name = "imagine-waker", Project = "imagine" }
}

resource "aws_iam_role_policy" "imagine_waker" {
  name = "invoke-imagine-scale"
  role = aws_iam_role.imagine_waker.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.imagine_scale.arn
    }]
  })
}

# The image build: GitHub Actions on ejc3/imagine, main branch only. It can push to the one
# repository and invoke the one function. It cannot change a task definition, a role or
# the task count, so the most a bad build can do is ship a bad image, which the service's
# circuit breaker rolls back.
resource "aws_iam_role" "imagine_deploy" {
  name                 = "imagine-deploy"
  description          = "Assumed by ejc3/imagine's deploy workflow (main) via GitHub OIDC: push the backend image, invoke imagine-scale"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:ejc3/imagine:ref:refs/heads/main"
        }
      }
    }]
  })
  tags = { Name = "imagine-deploy", Project = "imagine" }
}

resource "aws_iam_role_policy" "imagine_deploy" {
  name = "push-image-and-roll"
  role = aws_iam_role.imagine_deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
          "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage",
        ]
        Resource = aws_ecr_repository.imagine.arn
      },
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.imagine_scale.arn
      },
    ]
  })
}

# -------------------------------------------------------------------------------------
# Outputs
# -------------------------------------------------------------------------------------

# The non-secret half of the Vercel project's environment. The rest is set there by hand
# and never passes through Terraform: IMAGINE_TOKEN_SIGNING_KEY, AUTH_SECRET,
# AUTH_GOOGLE_ID, AUTH_GOOGLE_SECRET.
output "imagine_vercel_env" {
  description = "Non-secret environment for the imagine Vercel project (Production)"
  value = {
    IMAGINE_ENV            = "production"
    IMAGINE_WS_URL         = "wss://${local.imagine_host}/socket"
    IMAGINE_SCALE_FUNCTION = aws_lambda_function.imagine_scale.function_name
    IMAGINE_SCALE_ROLE_ARN = aws_iam_role.imagine_waker.arn
    IMAGINE_AWS_REGION     = var.aws_region
  }
}

output "imagine_deploy" {
  description = "Actions variables for ejc3/imagine's deploy workflow"
  value = {
    AWS_REGION          = var.aws_region
    AWS_DEPLOY_ROLE_ARN = aws_iam_role.imagine_deploy.arn
    ECR_REPOSITORY      = aws_ecr_repository.imagine.repository_url
  }
}

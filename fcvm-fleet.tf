# fcvm-fleet.tf
#
# The infrastructure of fcvm's GPU VM wall (ejc3/aws#606; design ejc3/fcvm#1136, docs/fleet/ARCHITECTURE.md), all inside the
# fcvm-fleet member account (fcvm-fleet-account.tf) through its three provider aliases, applied from a jumpbox.
#
# - modules/fcvm-fleet-region, once per region (us-east-2, us-west-2, us-east-1): the network (TCP 443 in, nothing else), the
#   spot GPU host group and the EC2 tiles' launch templates.
# - The coordinator, in us-east-2: a dedicated ECS cluster and one Fargate service (the coordinator and a cloudflared
#   container), its S3 state object, and the task role that may launch and terminate EC2 tiles from the tile templates only.
# - Instance roles for the hosts and the tiles, the asset bucket for fcvm release artifacts, the flow-log bucket, and the
#   wildcard TLS certificate's secret container. The Cloudflare-fed secrets (the DNS token, the coordinator's tunnel token,
#   the agents' Access service token) are fcvm-dev.tf's, written there with their values; here they are named by ARN.
#
# TWO GATES, both off: nothing that bills by the hour runs until the owner turns it on.
# - local.fcvm_fleet_hosts_enabled: one spot GPU metal host per region (about $2-4 an hour each). Also needs the fleet AMI
#   in var.fcvm_fleet_host_amis; a region and architecture without one is left out of the group.
# - local.fcvm_fleet_coordinator_enabled: the coordinator service (one small Fargate task). Also needs
#   var.fcvm_fleet_coordinator_image.
# The AMIs come in through the variable maps; an image build (EC2 Image Builder or a Packer job from fcvm-arm) is a follow-up.

variable "fcvm_fleet_host_amis" {
  description = "fcvm fleet host AMI per region and architecture, e.g. { \"us-east-2\" = { x86_64 = \"ami-...\", arm64 = \"\" } }"
  type        = map(map(string))
  default     = {}
}

variable "fcvm_fleet_tile_amis" {
  description = "EC2 tile AMI per region and architecture, same shape as fcvm_fleet_host_amis"
  type        = map(map(string))
  default     = {}
}

variable "fcvm_fleet_coordinator_image" {
  description = "The coordinator's container image (an ECR or public registry reference); empty keeps the service at 0"
  type        = string
  default     = ""
}

locals {
  # The two switches (see the header). Both false until the owner turns the fleet on.
  fcvm_fleet_hosts_enabled       = false
  fcvm_fleet_coordinator_enabled = false

  fcvm_fleet_cidrs = { "us-east-2" = "10.20.0.0/16", "us-west-2" = "10.21.0.0/16", "us-east-1" = "10.22.0.0/16" }

  # Secrets of the fleet, all in us-east-2: tls-wildcard is created here, the Cloudflare-fed three by fcvm-dev.tf.
  fcvm_fleet_secret_arn = {
    "tls-wildcard"             = aws_secretsmanager_secret.fcvm_fleet_tls.arn
    "cloudflare-dns-token"     = aws_secretsmanager_secret.fcvm_fleet_dns_token.arn
    "agent-access-token"       = aws_secretsmanager_secret.fcvm_fleet_agent_access.arn
    "coordinator-tunnel-token" = aws_secretsmanager_secret.fcvm_fleet_coordinator_tunnel.arn
  }
  # ECS takes a secret's full ARN with a JSON key: arn:key:version-stage:version-id.
  fcvm_fleet_tunnel_token_ref = "${aws_secretsmanager_secret.fcvm_fleet_coordinator_tunnel.arn}:TUNNEL_TOKEN::"

  fcvm_fleet_coordinator_running = local.fcvm_fleet_coordinator_enabled && var.fcvm_fleet_coordinator_image != ""
}

# ------------------------------------------------------------------------------------------------ buckets
resource "aws_s3_bucket" "fcvm_fleet_flow_logs" {
  provider = aws.fcvm_fleet
  bucket   = "fcvm-fleet-flow-logs-${local.fcvm_fleet_account_id}"
  tags     = { Name = "fcvm-fleet-flow-logs", Managed = "terraform" }
}

resource "aws_s3_bucket_public_access_block" "fcvm_fleet_flow_logs" {
  provider                = aws.fcvm_fleet
  bucket                  = aws_s3_bucket.fcvm_fleet_flow_logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "fcvm_fleet_flow_logs" {
  provider = aws.fcvm_fleet
  bucket   = aws_s3_bucket.fcvm_fleet_flow_logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "fcvm_fleet_flow_logs" {
  provider = aws.fcvm_fleet
  bucket   = aws_s3_bucket.fcvm_fleet_flow_logs.id
  rule {
    id     = "expire-flow-logs"
    status = "Enabled"
    filter {}
    expiration {
      days = 90
    }
  }
}

# Every region's VPC delivers here (flow logs to S3 cross region, as the pbox fallback sites do to the audit bucket).
resource "aws_s3_bucket_policy" "fcvm_fleet_flow_logs" {
  provider = aws.fcvm_fleet
  bucket   = aws_s3_bucket.fcvm_fleet_flow_logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "VPCFlowBucketCheck"
        Effect    = "Allow"
        Principal = { Service = "delivery.logs.amazonaws.com" }
        Action    = "s3:GetBucketAcl"
        Resource  = aws_s3_bucket.fcvm_fleet_flow_logs.arn
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.fcvm_fleet_account_id }
          ArnLike      = { "aws:SourceArn" = "arn:aws:logs:*:${local.fcvm_fleet_account_id}:*" }
        }
      },
      {
        Sid       = "VPCFlowDelivery"
        Effect    = "Allow"
        Principal = { Service = "delivery.logs.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.fcvm_fleet_flow_logs.arn}/vpc-flow/AWSLogs/${local.fcvm_fleet_account_id}/*"
        Condition = {
          StringEquals = { "aws:SourceAccount" = local.fcvm_fleet_account_id, "s3:x-amz-acl" = "bucket-owner-full-control" }
          ArnLike      = { "aws:SourceArn" = "arn:aws:logs:*:${local.fcvm_fleet_account_id}:*" }
        }
      },
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.fcvm_fleet_flow_logs.arn, "${aws_s3_bucket.fcvm_fleet_flow_logs.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
    ]
  })
  depends_on = [aws_s3_bucket_public_access_block.fcvm_fleet_flow_logs]
}

# fcvm release artifacts per architecture (fcvm, the gpu kernel, the Firecracker fork, the NVIDIA driver image), read by
# the hosts and by the image build.
resource "aws_s3_bucket" "fcvm_fleet_assets" {
  provider = aws.fcvm_fleet
  bucket   = "fcvm-fleet-assets-${local.fcvm_fleet_account_id}"
  tags     = { Name = "fcvm-fleet-assets", Managed = "terraform" }
}

# The coordinator's state: one JSON object, rewritten whole on every change and read once at start.
resource "aws_s3_bucket" "fcvm_fleet_coordinator_state" {
  provider = aws.fcvm_fleet
  bucket   = "fcvm-fleet-coordinator-state-${local.fcvm_fleet_account_id}"
  tags     = { Name = "fcvm-fleet-coordinator-state", Managed = "terraform" }
}

resource "aws_s3_bucket_versioning" "fcvm_fleet_private" {
  for_each = { assets = aws_s3_bucket.fcvm_fleet_assets.id, state = aws_s3_bucket.fcvm_fleet_coordinator_state.id }
  provider = aws.fcvm_fleet
  bucket   = each.value
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "fcvm_fleet_private" {
  for_each                = { assets = aws_s3_bucket.fcvm_fleet_assets.id, state = aws_s3_bucket.fcvm_fleet_coordinator_state.id }
  provider                = aws.fcvm_fleet
  bucket                  = each.value
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "fcvm_fleet_private" {
  for_each = { assets = aws_s3_bucket.fcvm_fleet_assets.id, state = aws_s3_bucket.fcvm_fleet_coordinator_state.id }
  provider = aws.fcvm_fleet
  bucket   = each.value
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_ownership_controls" "fcvm_fleet_private" {
  for_each = { assets = aws_s3_bucket.fcvm_fleet_assets.id, state = aws_s3_bucket.fcvm_fleet_coordinator_state.id }
  provider = aws.fcvm_fleet
  bucket   = each.value
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# ------------------------------------------------------------------------------------------------ the TLS certificate
# The wildcard certificate for fcvm.dev (JSON: cert, key), set out of band. Hosts and tiles read it at boot.
resource "aws_secretsmanager_secret" "fcvm_fleet_tls" {
  provider                = aws.fcvm_fleet
  name                    = "fcvm-fleet/tls-wildcard"
  description             = "Wildcard TLS certificate for the fleet zone (JSON: cert, key). Value set out of band; see fcvm-fleet.tf."
  recovery_window_in_days = 7
  tags                    = { Name = "fcvm-fleet/tls-wildcard", Project = "fcvm-fleet" }
}

# ------------------------------------------------------------------------------------------------ instance roles
resource "aws_iam_role" "fcvm_fleet_host" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-host"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "fcvm-fleet-host" }
}

# The certificate, the agents' Access service token and the release artifacts; nothing else (no EC2, no other S3).
resource "aws_iam_role_policy" "fcvm_fleet_host" {
  provider = aws.fcvm_fleet
  name     = "fleet-host"
  role     = aws_iam_role.fcvm_fleet_host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadTheCertificateAndTheAgentToken"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = [local.fcvm_fleet_secret_arn["tls-wildcard"], local.fcvm_fleet_secret_arn["agent-access-token"]]
      },
      {
        Sid      = "ReadReleaseArtifacts"
        Effect   = "Allow"
        Action   = "s3:GetObject"
        Resource = "${aws_s3_bucket.fcvm_fleet_assets.arn}/*"
      },
      {
        Sid      = "ListReleaseArtifacts"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.fcvm_fleet_assets.arn
      },
    ]
  })
}

resource "aws_iam_role" "fcvm_fleet_tile" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-tile"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "fcvm-fleet-tile" }
}

resource "aws_iam_role_policy" "fcvm_fleet_tile" {
  provider = aws.fcvm_fleet
  name     = "fleet-tile"
  role     = aws_iam_role.fcvm_fleet_tile.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "ReadTheCertificate"
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = local.fcvm_fleet_secret_arn["tls-wildcard"]
    }]
  })
}

# Session Manager is the only way onto a host or a tile (no SSH anywhere in the fleet).
resource "aws_iam_role_policy_attachment" "fcvm_fleet_ssm" {
  for_each   = { host = aws_iam_role.fcvm_fleet_host.name, tile = aws_iam_role.fcvm_fleet_tile.name }
  provider   = aws.fcvm_fleet
  role       = each.value
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "fcvm_fleet_host" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-host"
  role     = aws_iam_role.fcvm_fleet_host.name
}

resource "aws_iam_instance_profile" "fcvm_fleet_tile" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-tile"
  role     = aws_iam_role.fcvm_fleet_tile.name
}

# ------------------------------------------------------------------------------------------------ the three regions
module "fcvm_fleet_east2" {
  source                    = "./modules/fcvm-fleet-region"
  providers                 = { aws = aws.fcvm_fleet }
  cidr_block                = local.fcvm_fleet_cidrs["us-east-2"]
  gpu_amis                  = lookup(var.fcvm_fleet_host_amis, "us-east-2", {})
  tile_amis                 = lookup(var.fcvm_fleet_tile_amis, "us-east-2", {})
  hosts_enabled             = local.fcvm_fleet_hosts_enabled
  host_profile_name         = aws_iam_instance_profile.fcvm_fleet_host.name
  tile_profile_name         = aws_iam_instance_profile.fcvm_fleet_tile.name
  flow_log_destination      = "${aws_s3_bucket.fcvm_fleet_flow_logs.arn}/vpc-flow"
  flow_log_bucket_policy_id = aws_s3_bucket_policy.fcvm_fleet_flow_logs.id
}

module "fcvm_fleet_west2" {
  source                    = "./modules/fcvm-fleet-region"
  providers                 = { aws = aws.fcvm_fleet_west2 }
  cidr_block                = local.fcvm_fleet_cidrs["us-west-2"]
  gpu_amis                  = lookup(var.fcvm_fleet_host_amis, "us-west-2", {})
  tile_amis                 = lookup(var.fcvm_fleet_tile_amis, "us-west-2", {})
  hosts_enabled             = local.fcvm_fleet_hosts_enabled
  host_profile_name         = aws_iam_instance_profile.fcvm_fleet_host.name
  tile_profile_name         = aws_iam_instance_profile.fcvm_fleet_tile.name
  flow_log_destination      = "${aws_s3_bucket.fcvm_fleet_flow_logs.arn}/vpc-flow"
  flow_log_bucket_policy_id = aws_s3_bucket_policy.fcvm_fleet_flow_logs.id
}

module "fcvm_fleet_east1" {
  source                    = "./modules/fcvm-fleet-region"
  providers                 = { aws = aws.fcvm_fleet_east1 }
  cidr_block                = local.fcvm_fleet_cidrs["us-east-1"]
  gpu_amis                  = lookup(var.fcvm_fleet_host_amis, "us-east-1", {})
  tile_amis                 = lookup(var.fcvm_fleet_tile_amis, "us-east-1", {})
  hosts_enabled             = local.fcvm_fleet_hosts_enabled
  host_profile_name         = aws_iam_instance_profile.fcvm_fleet_host.name
  tile_profile_name         = aws_iam_instance_profile.fcvm_fleet_tile.name
  flow_log_destination      = "${aws_s3_bucket.fcvm_fleet_flow_logs.arn}/vpc-flow"
  flow_log_bucket_policy_id = aws_s3_bucket_policy.fcvm_fleet_flow_logs.id
}

locals {
  fcvm_fleet_tile_template_arns = concat(module.fcvm_fleet_east2.tile_template_arns, module.fcvm_fleet_west2.tile_template_arns, module.fcvm_fleet_east1.tile_template_arns)
}

# ------------------------------------------------------------------------------------------------ the coordinator (us-east-2)
resource "aws_ecs_cluster" "fcvm_fleet" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet"
  tags     = { Name = "fcvm-fleet" }
}

resource "aws_cloudwatch_log_group" "fcvm_fleet_coordinator" {
  provider          = aws.fcvm_fleet
  name              = "/ecs/fcvm-fleet-coordinator"
  retention_in_days = 30
}

resource "aws_iam_role" "fcvm_fleet_coordinator_execution" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-coordinator-execution"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_coordinator_execution" {
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_coordinator_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# The execution role injects cloudflared's tunnel token from its secret; nothing else.
resource "aws_iam_role_policy" "fcvm_fleet_coordinator_execution_secret" {
  provider = aws.fcvm_fleet
  name     = "tunnel-token"
  role     = aws_iam_role.fcvm_fleet_coordinator_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "secretsmanager:GetSecretValue"
      Resource = local.fcvm_fleet_secret_arn["coordinator-tunnel-token"]
    }]
  })
}

resource "aws_iam_role" "fcvm_fleet_coordinator_task" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-coordinator"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

# Its state object; the fleet secrets; EC2 tiles from the tile templates only, tagged at launch, and terminated only when
# tagged so; and passing exactly the tile role. In the fleet regions only (the account's SCP denies the others anyway).
resource "aws_iam_role_policy" "fcvm_fleet_coordinator_task" {
  provider = aws.fcvm_fleet
  name     = "coordinator"
  role     = aws_iam_role.fcvm_fleet_coordinator_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "StateObject"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/*"
      },
      {
        Sid      = "FleetSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = values(local.fcvm_fleet_secret_arn)
      },
      {
        Sid       = "LaunchTilesFromTheirTemplates"
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = "*"
        Condition = { ArnLike = { "ec2:LaunchTemplate" = length(local.fcvm_fleet_tile_template_arns) > 0 ? local.fcvm_fleet_tile_template_arns : ["arn:aws:ec2:*:*:launch-template/none"] }, StringEquals = { "aws:RequestedRegion" = local.fcvm_fleet_regions } }
      },
      {
        # A launch from a template can still override the instance type: only the two tile types, whatever the request says.
        Sid       = "TileTypesOnly"
        Effect    = "Deny"
        Action    = "ec2:RunInstances"
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringNotEquals = { "ec2:InstanceType" = ["g4dn.xlarge", "g5g.xlarge"] } }
      },
      {
        # The instance a launch creates must carry Purpose=fcvm-fleet-tile, which is what termination is limited to.
        Sid       = "TileInstancesAreTagged"
        Effect    = "Deny"
        Action    = "ec2:RunInstances"
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringNotEquals = { "aws:RequestTag/Purpose" = "fcvm-fleet-tile" } }
      },
      {
        Sid       = "TagAtLaunchOnly"
        Effect    = "Allow"
        Action    = "ec2:CreateTags"
        Resource  = "*"
        Condition = { StringEquals = { "ec2:CreateAction" = "RunInstances" } }
      },
      {
        Sid       = "TerminateTilesOnly"
        Effect    = "Allow"
        Action    = "ec2:TerminateInstances"
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = "fcvm-fleet-tile", "aws:RequestedRegion" = local.fcvm_fleet_regions } }
      },
      {
        Sid      = "DescribeEc2"
        Effect   = "Allow"
        Action   = ["ec2:Describe*"]
        Resource = "*"
      },
      {
        Sid       = "PassTheTileRoleOnly"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.fcvm_fleet_tile.arn
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
    ]
  })
}

resource "aws_security_group" "fcvm_fleet_coordinator" {
  provider    = aws.fcvm_fleet
  name        = "fcvm-fleet-coordinator"
  description = "fcvm fleet coordinator task: nothing in (browsers and agents arrive through its Cloudflare tunnel), all out"
  vpc_id      = module.fcvm_fleet_east2.vpc_id

  egress {
    description      = "All outbound"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = { Name = "fcvm-fleet-coordinator" }
}

resource "aws_ecs_task_definition" "fcvm_fleet_coordinator" {
  provider                 = aws.fcvm_fleet
  family                   = "fcvm-fleet-coordinator"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.fcvm_fleet_coordinator_execution.arn
  task_role_arn            = aws_iam_role.fcvm_fleet_coordinator_task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  container_definitions = jsonencode([
    {
      name      = "coordinator"
      image     = var.fcvm_fleet_coordinator_image != "" ? var.fcvm_fleet_coordinator_image : "public.ecr.aws/docker/library/busybox:stable"
      essential = true
      # Its state is an S3 object, not a file (ECS.5: no writable root; a task volume where something must write).
      readonlyRootFilesystem = true
      environment = [
        { name = "FLEET_STATE_BUCKET", value = aws_s3_bucket.fcvm_fleet_coordinator_state.bucket },
        { name = "FLEET_STATE_KEY", value = "state.json" },
        { name = "FLEET_REGIONS", value = join(",", local.fcvm_fleet_regions) },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options   = { awslogs-group = aws_cloudwatch_log_group.fcvm_fleet_coordinator.name, awslogs-region = local.fcvm_fleet_home_region, awslogs-stream-prefix = "coordinator" }
      }
    },
    {
      name                   = "cloudflared"
      image                  = "cloudflare/cloudflared:${local.cloudflared_version}" # the pin claw-master-tunnel.tf keeps
      essential              = true
      command                = ["tunnel", "--no-autoupdate", "run"]
      readonlyRootFilesystem = true
      secrets                = [{ name = "TUNNEL_TOKEN", valueFrom = local.fcvm_fleet_tunnel_token_ref }]
      logConfiguration = {
        logDriver = "awslogs"
        options   = { awslogs-group = aws_cloudwatch_log_group.fcvm_fleet_coordinator.name, awslogs-region = local.fcvm_fleet_home_region, awslogs-stream-prefix = "cloudflared" }
      }
    },
  ])
}

resource "aws_ecs_service" "fcvm_fleet_coordinator" {
  provider        = aws.fcvm_fleet
  name            = "fcvm-fleet-coordinator"
  cluster         = aws_ecs_cluster.fcvm_fleet.id
  task_definition = aws_ecs_task_definition.fcvm_fleet_coordinator.arn
  launch_type     = "FARGATE"
  # One task: the coordinator is the single authority for fleet state (fcvm#1136, "Coordinator").
  desired_count                      = local.fcvm_fleet_coordinator_running ? 1 : 0
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = module.fcvm_fleet_east2.subnet_ids
    security_groups  = [aws_security_group.fcvm_fleet_coordinator.id]
    assign_public_ip = true
  }
}

output "fcvm_fleet_infra" {
  description = "The fcvm fleet's buckets, cluster and whether hosts and the coordinator are on (fcvm-fleet.tf)"
  value = {
    asset_bucket       = aws_s3_bucket.fcvm_fleet_assets.bucket
    state_bucket       = aws_s3_bucket.fcvm_fleet_coordinator_state.bucket
    cluster            = aws_ecs_cluster.fcvm_fleet.name
    hosts_desired      = { "us-east-2" = module.fcvm_fleet_east2.host_group_desired, "us-west-2" = module.fcvm_fleet_west2.host_group_desired, "us-east-1" = module.fcvm_fleet_east1.host_group_desired }
    coordinator_on     = local.fcvm_fleet_coordinator_running
    tile_template_arns = local.fcvm_fleet_tile_template_arns
  }
}

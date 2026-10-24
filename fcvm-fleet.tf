# fcvm-fleet.tf
#
# The infrastructure of fcvm's GPU VM wall (ejc3/aws#606; design ejc3/fcvm#1136, docs/fleet/ARCHITECTURE.md), all inside the
# fcvm-fleet member account (fcvm-fleet-account.tf) through its three provider aliases, applied from a jumpbox.
#
# - modules/fcvm-fleet-region, once per region (us-east-2, us-west-2, us-east-1): the network (TCP 443 in, and the GPU
#   hosts' slot ports over IPv4), the spot GPU host group and the EC2 tiles' launch templates.
# - The coordinator, in us-east-2: a dedicated ECS cluster and one Fargate service (the coordinator and a cloudflared
#   container), its S3 state object, and the task role that may launch and terminate EC2 tiles from the tile templates only.
# - Instance roles for the hosts and the tiles, the asset bucket for fcvm release artifacts, the flow-log bucket, and the
#   wildcard TLS certificate's secret container. The Cloudflare-fed secrets (the DNS token, the coordinator's tunnel token,
#   the agents' Access service token) are fcvm-dev.tf's, written there with their values; here they are named by ARN.
#
# TWO GATES, both ON since ejc3/fcvm's fleet AMIs exist (built by fcvm's fleet/ami/build-ami.sh from fcvm-arm):
# - local.fcvm_fleet_hosts_enabled: one spot GPU metal host per region that has a fleet AMI in var.fcvm_fleet_host_amis. Only
#   us-east-1 has one (and the only G spot quota), so one g4dn.metal: $4.76-5.44 an hour spot there on 2026-10-22, about
#   $3,600 a month always on. A region and architecture without an AMI is left out of the group.
#   ON-DEMAND FALLBACK: each region with an AMI also has fcvm-fleet-hosts-on-demand, 0 unless a wake finds no spot capacity
#   (modules/fcvm-fleet-region, "On-demand fallback"), capped at local.fcvm_fleet_on_demand_max = 8 hosts (owner,
#   2026-10-24: "Up to 8 on-demand max"). A g4dn.metal on demand is $7.824 an hour in us-east-1, so 8 is $62.59 an hour.
#   The account's on-demand G and VT quota there (L-DB2E81BA) is 384 vCPU, 4 hosts at 96 each, less any on-demand EC2
#   tiles (4 vCPU each); the group's cap is 8, and launches beyond the quota fail. The coordinator only falls back with
#   local.fcvm_fleet_coordinator_falls_back, which needs a coordinator image that has --on-demand-group.
# - local.fcvm_fleet_coordinator_enabled: the coordinator service (one Fargate task, 0.5 vCPU and 1 GB ARM64, about $15 a
#   month), running fcvm-fleet/coordinator:<local.fcvm_fleet_coordinator_tag> unless var.fcvm_fleet_coordinator_image names
#   another image.

variable "fcvm_fleet_host_amis" {
  description = "fcvm fleet host AMI per region and architecture, e.g. { \"us-east-2\" = { x86_64 = \"ami-...\", arm64 = \"\" } }"
  type        = map(map(string))
  # Ubuntu 24.04, IOMMU on, every NVIDIA GPU on vfio-pci at boot, fcvm with the gpu profile set up and the renderer warmed;
  # the store is restored onto the instance store at boot (fcvm fleet/ami/host). Built in us-east-1 only, from ejc3/fcvm main
  # bdaa0003 with main's recipe (fleet/ami, ejc3/fcvm#1217): a GPU guest's NIC stays eth0 so its routed address is on it
  # (#1197), the agent unit runs as root (routed VMs) and the host forwards IPv6; the agent dials the coordinator and takes
  # --public-ipv4-from-imds (ejc3/fcvm#1229).
  default = { "us-east-1" = { x86_64 = "ami-0913aacc80394e1bd" } }
}

variable "fcvm_fleet_tile_amis" {
  description = "EC2 tile AMI per region and architecture, same shape as fcvm_fleet_host_amis"
  type        = map(map(string))
  # Ubuntu 24.04, the NVIDIA driver and container toolkit, the renderer image loaded, TLS on 443 from fcvm-fleet/tls-wildcard
  # (fcvm fleet/ami/tile). Its renderer is the host AMI's: the same image, from ejc3/fcvm main c93a01d9.
  default = { "us-east-1" = { x86_64 = "ami-08f3cc41395353a2f" } }
}

variable "fcvm_fleet_coordinator_image" {
  description = "Another coordinator image than fcvm-fleet/coordinator:<local.fcvm_fleet_coordinator_tag> (an ECR or public registry reference)"
  type        = string
  default     = ""
}

locals {
  # The two switches (see the header).
  fcvm_fleet_hosts_enabled       = true
  fcvm_fleet_coordinator_enabled = true
  # The ejc3/fcvm commit the coordinator image in fcvm-fleet/coordinator is tagged with.
  fcvm_fleet_coordinator_tag = "bdaa0003"
  # Most on-demand GPU hosts per region (the fallback group's max_size; owner, 2026-10-24: "Up to 8 on-demand max").
  fcvm_fleet_on_demand_max = 8
  # Whether the coordinator gives a short spot group's capacity to the region's on-demand group (--on-demand-group,
  # --spot-wait, ejc3/fcvm#1215). On with the image at c93a01d9 (ejc3/fcvm main, which has those flags); the image at
  # 8c96e28e refuses an unknown argument and would not start, so the two change together.
  fcvm_fleet_coordinator_falls_back = true

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

  # The host agent's arguments (fcvm fleet/PROTOCOL.md, "Agent", "Running it"): it dials the coordinator through Access with
  # the agents' service token, names the host by its instance id, runs each slot's renderer VM routed at an address of the
  # host's delegated /80, reports it as slot-<n>.fcvm.dev, and hands the renderers the wildcard certificate. The secrets
  # are in the home region, not the host's.
  # The IPv4 path to a GPU host's slots: its one public IPv4 address, slot i's renderer TLS forwarded on port base + i.
  # The agent's --tls-publish-base and the fleet security group's port range both come from these two numbers.
  fcvm_fleet_slot_tls_port_base = 20200
  fcvm_fleet_host_gpu_slots     = 8 # a g4dn.metal's T4s
  fcvm_fleet_slot_tls_ports = {
    from = local.fcvm_fleet_slot_tls_port_base
    to   = local.fcvm_fleet_slot_tls_port_base + local.fcvm_fleet_host_gpu_slots - 1
  }
  # The zone's authoritative nameservers: the coordinator publishes a launch's names only once both answer with them.
  fcvm_dev_nameservers = ["cullen.ns.cloudflare.com", "lilyana.ns.cloudflare.com"]

  fcvm_fleet_host_agent_args = join(" ", [
    "--dial wss://${local.fcvm_dev_agent_path}",
    "--access-secret ${aws_secretsmanager_secret.fcvm_fleet_agent_access.name}",
    "--tls-secret ${aws_secretsmanager_secret.fcvm_fleet_tls.name}",
    "--secret-region ${local.fcvm_fleet_home_region}",
    "--host-from-imds",
    "--image localhost/fcvm-renderer:fleet",
    "--base-port 20100",
    "--publish-ip 0.0.0.0",
    "--ipv6-prefix-from-imds",
    "--frame-zone fcvm.dev",
    "--tls-publish-base ${local.fcvm_fleet_slot_tls_port_base}",
    "--public-ipv4-from-imds",
    "--spot-notice-url http://169.254.169.254",
    "--store-ready-file /var/lib/fcvm-fleet/store-ready",
  ])

  # The coordinator's arguments (fcvm fleet/PROTOCOL.md, "Coordinator", "Running it"); its state bucket and key come from
  # the task's environment. It owns the host groups' desired capacity: 0 after 10 minutes with no viewer, 1 for the next
  # one, moved to the on-demand group when the spot group has no instance 120 s into a wake. The EC2 side runs its tiles in us-east-1, the one region with a tile AMI, and only while the fleet is awake.
  fcvm_fleet_coordinator_command = concat(
    ["--listen", "0.0.0.0:${local.fcvm_fleet_coordinator_port}", "--trust-cf-connecting-ip", "--allowed-origin", "https://${local.fcvm_dev_site_stage_host}"],
    flatten([for r in local.fcvm_fleet_regions : ["--scale-group", "${r}:fcvm-fleet-hosts"] if length(lookup(var.fcvm_fleet_host_amis, r, {})) > 0]),
    local.fcvm_fleet_coordinator_falls_back ? concat(
      flatten([for r in local.fcvm_fleet_regions : ["--on-demand-group", "${r}:fcvm-fleet-hosts-on-demand"] if length(lookup(var.fcvm_fleet_host_amis, r, {})) > 0]),
      ["--spot-wait", "120"],
    ) : [],
    ["--ec2", "aws", "--ec2-tiles", "8", "--ec2-regions", "us-east-1", "--ec2-template", "fcvm-fleet-tile-x86_64", "--ec2-on-demand-template", "fcvm-fleet-tile-on-demand-x86_64"],
    ["--dns-zone", "fcvm.dev", "--dns-token-secret", aws_secretsmanager_secret.fcvm_fleet_dns_token.name, "--dns-zone-id", var.fcvm_dev_zone_id, "--tls-secret", aws_secretsmanager_secret.fcvm_fleet_tls.name, "--secrets-region", local.fcvm_fleet_home_region, "--dns-nameservers", join(",", local.fcvm_dev_nameservers)],
  )

  fcvm_fleet_coordinator_image   = var.fcvm_fleet_coordinator_image != "" ? var.fcvm_fleet_coordinator_image : "${aws_ecr_repository.fcvm_fleet_coordinator.repository_url}:${local.fcvm_fleet_coordinator_tag}"
  fcvm_fleet_coordinator_running = local.fcvm_fleet_coordinator_enabled && local.fcvm_fleet_coordinator_tag != ""
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
  host_agent_args           = local.fcvm_fleet_host_agent_args
  slot_tls_ports            = local.fcvm_fleet_slot_tls_ports
  host_on_demand_max        = local.fcvm_fleet_on_demand_max
  alert_email               = local.browser_manager_owner
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
  host_agent_args           = local.fcvm_fleet_host_agent_args
  slot_tls_ports            = local.fcvm_fleet_slot_tls_ports
  host_on_demand_max        = local.fcvm_fleet_on_demand_max
  alert_email               = local.browser_manager_owner
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
  host_agent_args           = local.fcvm_fleet_host_agent_args
  slot_tls_ports            = local.fcvm_fleet_slot_tls_ports
  host_on_demand_max        = local.fcvm_fleet_on_demand_max
  alert_email               = local.browser_manager_owner
  host_profile_name         = aws_iam_instance_profile.fcvm_fleet_host.name
  tile_profile_name         = aws_iam_instance_profile.fcvm_fleet_tile.name
  flow_log_destination      = "${aws_s3_bucket.fcvm_fleet_flow_logs.arn}/vpc-flow"
  flow_log_bucket_policy_id = aws_s3_bucket_policy.fcvm_fleet_flow_logs.id
}

locals {
  fcvm_fleet_tile_template_arns = concat(module.fcvm_fleet_east2.tile_template_arns, module.fcvm_fleet_west2.tile_template_arns, module.fcvm_fleet_east1.tile_template_arns)

  # What a tile launch may reference, by exact ARN (the split of parallel-box-launch.tf): the tile AMIs, the fleet's
  # subnets and security group in each region, and the tile templates. A launch that names any other image, subnet or
  # group is refused even through a tile template. A placeholder keeps the list non-empty before any tile AMI exists.
  fcvm_fleet_regional = {
    "us-east-2" = module.fcvm_fleet_east2
    "us-west-2" = module.fcvm_fleet_west2
    "us-east-1" = module.fcvm_fleet_east1
  }
  fcvm_fleet_tile_referenced_arns = concat(
    flatten([for r, amis in var.fcvm_fleet_tile_amis : [for ami in values(amis) : "arn:aws:ec2:${r}::image/${ami}"]]),
    flatten([for r, m in local.fcvm_fleet_regional : [for s in m.subnet_ids : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:subnet/${s}"]]),
    [for r, m in local.fcvm_fleet_regional : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:security-group/${m.security_group_id}"],
    local.fcvm_fleet_tile_template_arns,
    ["arn:aws:ec2:us-east-1::image/ami-none"],
  )
  # What a tile launch creates: the instance, its volume, its network interface and its one-time spot request.
  fcvm_fleet_tile_created_arns = flatten([for r in local.fcvm_fleet_regions : [
    for kind in ["instance", "volume", "network-interface", "spot-instances-request"] :
    "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:${kind}/*"
  ]])
  fcvm_fleet_tile_template_condition = length(local.fcvm_fleet_tile_template_arns) > 0 ? local.fcvm_fleet_tile_template_arns : ["arn:aws:ec2:*:*:launch-template/none"]
}

# Spot launches (the tile templates are one-time spot) need EC2's spot service-linked role, which a fresh account lacks
# and the coordinator may not create.
resource "aws_iam_service_linked_role" "fcvm_fleet_spot" {
  provider         = aws.fcvm_fleet
  aws_service_name = "spot.amazonaws.com"
}

# ------------------------------------------------------------------------------------------------ the coordinator's image
# The coordinator's container image (fcvm's fleet/coord, ARM64), pushed from fcvm-arm through fcvm-fleet-terraform; the task's
# execution role pulls it (AmazonECSTaskExecutionRolePolicy). Tags are fcvm commits and immutable, so a running task's image
# cannot change under it; the 20 newest are kept, which outlasts any one rollout.
resource "aws_ecr_repository" "fcvm_fleet_coordinator" {
  provider             = aws.fcvm_fleet
  name                 = "fcvm-fleet/coordinator"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "fcvm-fleet/coordinator", Project = "fcvm-fleet" }
}

resource "aws_ecr_lifecycle_policy" "fcvm_fleet_coordinator" {
  provider   = aws.fcvm_fleet
  repository = aws_ecr_repository.fcvm_fleet_coordinator.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 20 newest images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 20 }
      action       = { type = "expire" }
    }]
  })
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
        Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/state.json" # FLEET_STATE_KEY
      },
      {
        # Without ListBucket, S3 answers a read of a missing object with 403, which the coordinator must treat as an error;
        # with it, a fresh fleet's first read is a 404 and it starts empty. The bucket holds only the state object.
        Sid      = "StateObjectMissingReadsAs404"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.fcvm_fleet_coordinator_state.arn
      },
      {
        # Scale to zero: with no viewer the coordinator sets the host groups' desired capacity to 0 and wakes them for the
        # next one (fcvm fleet coord --scale-group <region>:fcvm-fleet-hosts), and falls back to the on-demand group when
        # spot has no capacity (--on-demand-group <region>:fcvm-fleet-hosts-on-demand). Only those groups; their max_size
        # stays the gate (0 while local.fcvm_fleet_hosts_enabled is false, at most 8 on demand), so the coordinator cannot
        # start a host the gate holds off or more on-demand hosts than the cap.
        Sid    = "ScaleTheHostGroups"
        Effect = "Allow"
        Action = "autoscaling:SetDesiredCapacity"
        Resource = flatten([for r in local.fcvm_fleet_regions : [for g in ["fcvm-fleet-hosts", "fcvm-fleet-hosts-on-demand"] :
        "arn:aws:autoscaling:${r}:${local.fcvm_fleet_account_id}:autoScalingGroup:*:autoScalingGroupName/${g}"]])
      },
      {
        # Reading the groups' instances while waking. DescribeAutoScalingGroups takes no resource-level permission.
        Sid      = "DescribeTheHostGroups"
        Effect   = "Allow"
        Action   = "autoscaling:DescribeAutoScalingGroups"
        Resource = "*"
      },
      {
        # The DNS token (slot records and the ACME challenge) and the wildcard certificate it issues and renews. Not the
        # tunnel token (cloudflared gets it through the execution role) nor the agents' Access token.
        Sid      = "FleetSecrets"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = [local.fcvm_fleet_secret_arn["cloudflare-dns-token"], local.fcvm_fleet_secret_arn["tls-wildcard"]]
      },
      {
        Sid      = "StoreTheIssuedCertificate"
        Effect   = "Allow"
        Action   = "secretsmanager:PutSecretValue"
        Resource = local.fcvm_fleet_secret_arn["tls-wildcard"]
      },
      {
        # Created resources: only through a tile template, in the fleet regions.
        Sid       = "LaunchTilesFromTheirTemplates"
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = local.fcvm_fleet_tile_created_arns
        Condition = { ArnLike = { "ec2:LaunchTemplate" = local.fcvm_fleet_tile_template_condition } }
      },
      {
        # Referenced resources: exactly the tile AMIs, the fleet's subnets and group, and the tile templates.
        Sid       = "LaunchTilesWithTheirOwnResources"
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = local.fcvm_fleet_tile_referenced_arns
        Condition = { ArnLike = { "ec2:LaunchTemplate" = local.fcvm_fleet_tile_template_condition } }
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
      image     = local.fcvm_fleet_coordinator_running ? local.fcvm_fleet_coordinator_image : "public.ecr.aws/docker/library/busybox:stable"
      essential = true
      command   = local.fcvm_fleet_coordinator_running ? local.fcvm_fleet_coordinator_command : null
      # Its state is an S3 object, not a file (ECS.5: no writable root; a task volume where something must write).
      readonlyRootFilesystem = true
      environment = [
        { name = "FLEET_STATE_BUCKET", value = aws_s3_bucket.fcvm_fleet_coordinator_state.bucket },
        { name = "FLEET_STATE_KEY", value = "state.json" },
        { name = "FLEET_REGIONS", value = join(",", local.fcvm_fleet_regions) },
        # The coordinator logs its launches, scaling and agent connections at info; without this only warnings reach the log.
        { name = "RUST_LOG", value = "info" },
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
    coordinator_repo   = aws_ecr_repository.fcvm_fleet_coordinator.repository_url
    hosts_desired      = { "us-east-2" = module.fcvm_fleet_east2.host_group_desired, "us-west-2" = module.fcvm_fleet_west2.host_group_desired, "us-east-1" = module.fcvm_fleet_east1.host_group_desired }
    on_demand_max      = { "us-east-2" = module.fcvm_fleet_east2.host_on_demand_max, "us-west-2" = module.fcvm_fleet_west2.host_on_demand_max, "us-east-1" = module.fcvm_fleet_east1.host_on_demand_max }
    coordinator_on     = local.fcvm_fleet_coordinator_running
    tile_template_arns = local.fcvm_fleet_tile_template_arns
  }
}

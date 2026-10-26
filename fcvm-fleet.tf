# fcvm-fleet.tf
#
# The infrastructure of fcvm's GPU VM wall (ejc3/aws#606; design ejc3/fcvm#1136, docs/fleet/ARCHITECTURE.md), all inside the
# fcvm-fleet member account (fcvm-fleet-account.tf) through its three provider aliases, applied from a jumpbox.
#
# - modules/fcvm-fleet-region, once per region (us-east-2, us-west-2, us-east-1): the network (TCP 443 in, and the GPU
#   hosts' slot ports over IPv4), the GPU host and EC2 tile launch templates.
# - The coordinator, in us-east-2: a dedicated ECS cluster and one Fargate service (the coordinator and a cloudflared
#   container), its S3 state object, and the task role that may launch and terminate GPU hosts and EC2 tiles from their
#   templates only.
# - Instance roles for the hosts and the tiles, the asset bucket for fcvm release artifacts, the flow-log bucket, and the
#   wildcard TLS certificate's secret container. The Cloudflare-fed secrets (the DNS token, the coordinator's tunnel token,
#   the agents' Access service token) are fcvm-dev.tf's, written there with their values; here they are named by ARN.
#
# TWO GATES, both ON since ejc3/fcvm's fleet AMIs exist (built by fcvm's fleet/ami/build-ami.sh from fcvm-arm):
# - local.fcvm_fleet_hosts_enabled: one spot GPU metal host per region that has a fleet AMI in var.fcvm_fleet_host_amis, which
#   the coordinator launches while the fleet is awake and terminates while it sleeps. Only us-east-1 has one (and the only G
#   spot quota), so one g4dn.metal: $4.76-5.44 an hour spot there on 2026-10-22. A region and architecture without an AMI
#   gets no template.
#   ON-DEMAND FALLBACK: when spot is refused for capacity the coordinator launches the host on demand in the same pass
#   (modules/fcvm-fleet-region, "GPU hosts"), at most local.fcvm_fleet_on_demand_max = 8 on-demand hosts across the fleet
#   (owner, 2026-10-24: "Up to 8 on-demand max"; FLEET_HOST_ON_DEMAND_MAX). A g4dn.metal on demand is $7.824 an hour in
#   us-east-1, so 8 is $62.59 an hour. The account's on-demand G and VT quota there (L-DB2E81BA) is 384 vCPU, 4 hosts at 96
#   each, less any on-demand EC2 tiles (4 vCPU each); launches beyond the quota fail.
# - local.fcvm_fleet_coordinator_enabled: the coordinator service (one Fargate task, 0.5 vCPU and 1 GB ARM64, about $15 a
#   month). Its initial image is fcvm-fleet/coordinator:<local.fcvm_fleet_coordinator_tag>;
#   main-only CI releases immutable digests through fcvm-fleet-ci.tf.

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

locals {
  # The two switches (see the header).
  fcvm_fleet_hosts_enabled       = true
  fcvm_fleet_coordinator_enabled = true
  # The bootstrap image, before main-only CI's first release. Active releases are
  # digest-pinned in /fcvm-fleet/stage/coordinator-image (fcvm-fleet-ci.tf).
  fcvm_fleet_coordinator_tag = "bdaa0003"
  # Most on-demand GPU hosts across the fleet (the coordinator's cap; owner, 2026-10-24: "Up to 8 on-demand max").
  fcvm_fleet_on_demand_max       = 8
  fcvm_fleet_on_demand_alarm_arn = "arn:aws:cloudwatch:${local.fcvm_fleet_home_region}:${local.fcvm_fleet_account_id}:alarm:fcvm-fleet-on-demand-host-launches"
  fcvm_fleet_alert_topic_arns    = concat(module.fcvm_fleet_east2.alert_topic_arns, module.fcvm_fleet_west2.alert_topic_arns, module.fcvm_fleet_east1.alert_topic_arns)
  # The coordinator's host launches (fcvm fleet coord, FLEET_HOST_TEMPLATES and FLEET_HOST_SUBNETS, ejc3/fcvm#1257): every
  # region's host templates while the gate is on, and the subnets of the AZs that offer a host type. Environment, not
  # arguments: an image from before ejc3/fcvm#1257 ignores them, where it would refuse an unknown argument and not start.
  fcvm_fleet_host_templates = flatten([for r, m in local.fcvm_fleet_regional : [for t in m.host_template_names : "${r}:${t}"]])
  fcvm_fleet_host_subnets = flatten([for r, m in local.fcvm_fleet_regional : [for s in m.host_subnet_ids : "${r}:${s}"]
  if length(m.host_template_names) > 0])
  # Whether the coordinator keeps its 8 EC2 tiles stopped between wakes and starts them at the next one (--ec2-pool,
  # ejc3/fcvm#1273) instead of launching them: a tile then reaches its first frame in about 16 s instead of 81 to 84 s.
  # Needs a coordinator image with --ec2-pool (an unknown flag stops the task). Standing cost: 8 stopped 32 GB gp3 root
  # volumes, $20.48 a month.
  fcvm_fleet_tile_pool = true

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

  # Aggregated streaming (ejc3/fcvm#1232): each GPU host's aggregator, and the EC2 half's beside the coordinator, serve
  # browsers with TLS under per-launch names (agg-<host>-<token>, agg-ec2-<token>). Off: the page runs direct (one stream
  # per tile). On needs a host AMI and a coordinator image with --aggregate-tls-port and --ec2-aggregate-tls (fcvm #1232's
  # stack); an image without them refuses the argument and does not start, so the switch moves with the images.
  fcvm_fleet_aggregated = false
  # A host aggregator's IPv4 path: the port after the slots', on the host's public IPv4 address (one address per host, a
  # port per stream). Over IPv6 it has its own address, <host prefix>::1, on 443. Its private listener (layout, stats) is
  # on fcvm_fleet_aggregate_port, which the security group does not admit.
  fcvm_fleet_aggregate_tls_port = local.fcvm_fleet_slot_tls_port_base + local.fcvm_fleet_host_gpu_slots
  fcvm_fleet_aggregate_port     = 20300
  fcvm_fleet_slot_tls_ports = {
    from = local.fcvm_fleet_slot_tls_port_base
    to   = local.fcvm_fleet_aggregated ? local.fcvm_fleet_aggregate_tls_port : local.fcvm_fleet_slot_tls_port_base + local.fcvm_fleet_host_gpu_slots - 1
  }
  fcvm_fleet_host_agent_args = join(" ", concat([
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
    ], local.fcvm_fleet_aggregated ? [
    "--aggregate-port ${local.fcvm_fleet_aggregate_port}",
    "--aggregate-tls-port ${local.fcvm_fleet_aggregate_tls_port}",
  ] : []))

  # The coordinator's arguments (fcvm fleet/PROTOCOL.md, "Coordinator", "Running it"); its state bucket and key and its
  # host templates come from the task's environment. The fleet sleeps after 10 minutes with no viewer (every GPU host
  # terminated) and wakes for the next one (one host per region with a template, spot first, on demand at once when spot
  # is refused). The EC2 side runs its tiles in us-east-1, the one region with a tile AMI, and only while the fleet is awake.
  fcvm_fleet_coordinator_command = concat(
    ["--listen", "0.0.0.0:${local.fcvm_fleet_coordinator_port}", "--trust-cf-connecting-ip", "--allowed-origin", "https://${local.fcvm_dev_site_stage_host}"],
    # Sleeping is implied by the host templates; the flag keeps an image from before ejc3/fcvm#1257 sleeping too.
    ["--sleep-when-idle"],
    ["--ec2", "aws", "--ec2-tiles", "8", "--ec2-regions", "us-east-1", "--ec2-template", "fcvm-fleet-tile-x86_64", "--ec2-on-demand-template", "fcvm-fleet-tile-on-demand-x86_64"],
    local.fcvm_fleet_tile_pool ? ["--ec2-pool", "8", "--ec2-pool-template", "fcvm-fleet-tile-pool-x86_64"] : [],
    ["--dns-zone", "fcvm.dev", "--dns-token-secret", aws_secretsmanager_secret.fcvm_fleet_dns_token.name, "--dns-zone-id", var.fcvm_dev_zone_id, "--tls-secret", aws_secretsmanager_secret.fcvm_fleet_tls.name, "--secrets-region", local.fcvm_fleet_home_region],
    # Launch names are served by the coordinator itself, from the subzone fcvm-fleet-dns.tf delegates to it.
    ["--dns-subzone", local.fcvm_fleet_names_zone, "--dns-subzone-ns", join(",", local.fcvm_fleet_names_ns), "--dns-listen", "0.0.0.0:${local.fcvm_fleet_dns_port}"],
    # The EC2 half's aggregator beside the coordinator: its layout on loopback, browsers on 443 of the task's own addresses.
    local.fcvm_fleet_aggregated ? ["--ec2-aggregate-listen", "127.0.0.1:${local.fcvm_fleet_aggregate_port}", "--ec2-aggregate-tls", "--ec2-aggregate-tls-dir", "/run/fcvm-fleet/tls"] : [],
  )

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
  alert_email               = local.browser_manager_owner
  host_agent_args           = local.fcvm_fleet_host_agent_args
  slot_tls_ports            = local.fcvm_fleet_slot_tls_ports
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
  alert_email               = local.browser_manager_owner
  host_agent_args           = local.fcvm_fleet_host_agent_args
  slot_tls_ports            = local.fcvm_fleet_slot_tls_ports
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
  alert_email               = local.browser_manager_owner
  host_agent_args           = local.fcvm_fleet_host_agent_args
  slot_tls_ports            = local.fcvm_fleet_slot_tls_ports
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
  # Created resources with launch-template context. A tagged spot request has no ec2:LaunchTemplate context and gets
  # its own region/Purpose-scoped authorization below; putting that condition on its ARN rejects persistent tile pools.
  fcvm_fleet_tile_created_arns = flatten([for r in local.fcvm_fleet_regions : [
    for kind in ["instance", "volume", "network-interface"] :
    "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:${kind}/*"
  ]])
  fcvm_fleet_tile_spot_request_arns  = [for r in local.fcvm_fleet_regions : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:spot-instances-request/*"]
  fcvm_fleet_tile_template_condition = length(local.fcvm_fleet_tile_template_arns) > 0 ? local.fcvm_fleet_tile_template_arns : ["arn:aws:ec2:*:*:launch-template/none"]
  fcvm_fleet_tile_types              = ["g4dn.xlarge", "g5g.xlarge"]

  # The same for GPU hosts: their AMIs, host-eligible fleet subnets and groups, and enabled host templates.
  fcvm_fleet_host_template_arns = concat(module.fcvm_fleet_east2.host_template_arns, module.fcvm_fleet_west2.host_template_arns, module.fcvm_fleet_east1.host_template_arns)
  fcvm_fleet_host_referenced_arns = concat(
    flatten([for r, amis in var.fcvm_fleet_host_amis : [for ami in values(amis) : "arn:aws:ec2:${r}::image/${ami}" if ami != ""]]),
    flatten([for r, m in local.fcvm_fleet_regional : [for s in m.host_subnet_ids : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:subnet/${s}"] if length(m.host_template_names) > 0]),
    [for r, m in local.fcvm_fleet_regional : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:security-group/${m.security_group_id}" if length(m.host_template_names) > 0],
    local.fcvm_fleet_host_template_arns,
    ["arn:aws:ec2:us-east-1::image/ami-none"],
  )
  fcvm_fleet_host_template_condition = length(local.fcvm_fleet_host_template_arns) > 0 ? local.fcvm_fleet_host_template_arns : ["arn:aws:ec2:*:*:launch-template/none"]
  fcvm_fleet_host_types              = ["g4dn.metal", "g5g.metal"]
  # CreateFleet authorizes the fleet and its newly created instances/volumes separately from the resources in the
  # launch template (those still need RunInstances). Keep instance-only conditions off fleet and volume resources.
  fcvm_fleet_host_fleet_arns    = [for r in local.fcvm_fleet_regions : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:fleet/*"]
  fcvm_fleet_host_instance_arns = [for r in local.fcvm_fleet_regions : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:instance/*"]
  fcvm_fleet_host_volume_arns   = [for r in local.fcvm_fleet_regions : "arn:aws:ec2:${r}:${local.fcvm_fleet_account_id}:volume/*"]
}

# Spot launches (the tile templates are one-time spot, the hosts spot first) need EC2's spot service-linked role, which a
# fresh account lacks and the coordinator may not create.
resource "aws_iam_service_linked_role" "fcvm_fleet_spot" {
  provider         = aws.fcvm_fleet
  aws_service_name = "spot.amazonaws.com"
}

# Instant EC2 Fleets use the caller's permissions and need no EC2 Fleet service-linked role. Do not create the role
# used by persistent request/maintain fleets: the coordinator owns the lifetime of each launched host itself.

# ------------------------------------------------------------------------------------------------ the coordinator's image
# The coordinator's container image (fcvm's fleet/coord, ARM64), pushed from fcvm-arm through fcvm-fleet-terraform; the task's
# execution role pulls it (AmazonECSTaskExecutionRolePolicy). Tags are fcvm commits and immutable, so a running task's image
# cannot change under it; the bootstrap image and the 20 newest are kept.
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
      # A higher-priority tag match excludes the immutable bootstrap image from
      # the rolling rule, even after many releases. Exactly one image has this tag.
      rulePriority = 1
      description  = "Retain the bootstrap image used by infrastructure initialization"
      selection = {
        tagStatus      = "tagged"
        tagPatternList = [local.fcvm_fleet_coordinator_tag]
        countType      = "imageCountMoreThan"
        countNumber    = 1
      }
      action = { type = "expire" }
      }, {
      rulePriority = 2
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

# Count only accepted on-demand launches, including an accepted response recovered with its idempotency token
# (ejc3/fcvm#1282, src/fleet/coord/hosts.rs). The earlier "launching on demand now" line is only an attempt.
# This is an event notification, not a count of running hosts; a quiet minute does not mean an expensive host stopped.
resource "aws_cloudwatch_log_metric_filter" "fcvm_fleet_on_demand_launches" {
  provider       = aws.fcvm_fleet
  name           = "fcvm-fleet-on-demand-host-launches"
  log_group_name = aws_cloudwatch_log_group.fcvm_fleet_coordinator.name
  pattern        = "\"wake: host launch accepted\" \"on-demand\""

  metric_transformation {
    name          = "OnDemandHostLaunches"
    namespace     = "FCVM/Fleet"
    value         = "1"
    default_value = 0
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_on_demand_launches" {
  provider            = aws.fcvm_fleet
  alarm_name          = "fcvm-fleet-on-demand-host-launches"
  alarm_description   = "The coordinator accepted an on-demand GPU host launch. A g4dn.metal costs $7.824/hour in us-east-1 and runs until the fleet sleeps; inspect the coordinator hosts and logs. This counts launch events, not running hosts."
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  namespace           = aws_cloudwatch_log_metric_filter.fcvm_fleet_on_demand_launches.metric_transformation[0].namespace
  metric_name         = aws_cloudwatch_log_metric_filter.fcvm_fleet_on_demand_launches.metric_transformation[0].name
  statistic           = "Sum"
  period              = 60
  threshold           = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]
  depends_on          = [aws_lambda_permission.fcvm_fleet_on_demand_notify]
}

# Alarm actions stay in the coordinator's region. This small forwarder publishes through each retained topic's SNS
# regional endpoint, preserving the existing confirmed subscriber without relying on cross-region alarm actions.
data "archive_file" "fcvm_fleet_on_demand_notify" {
  type        = "zip"
  output_path = "${path.module}/.terraform/fcvm-fleet-on-demand-notify.zip"
  source {
    content  = file("${path.module}/scripts/fcvm-fleet-on-demand-notify.py")
    filename = "notify.py"
  }
}

resource "aws_cloudwatch_log_group" "fcvm_fleet_on_demand_notify" {
  provider          = aws.fcvm_fleet
  name              = "/aws/lambda/fcvm-fleet-on-demand-notify"
  retention_in_days = 30
}

resource "aws_iam_role" "fcvm_fleet_on_demand_notify" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-on-demand-notify"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "fcvm_fleet_on_demand_notify" {
  provider = aws.fcvm_fleet
  name     = "notify-existing-fleet-subscribers"
  role     = aws_iam_role.fcvm_fleet_on_demand_notify.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.fcvm_fleet_on_demand_notify.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = local.fcvm_fleet_alert_topic_arns
      },
    ]
  })
}

resource "aws_lambda_function" "fcvm_fleet_on_demand_notify" {
  provider         = aws.fcvm_fleet
  function_name    = "fcvm-fleet-on-demand-notify"
  filename         = data.archive_file.fcvm_fleet_on_demand_notify.output_path
  source_code_hash = data.archive_file.fcvm_fleet_on_demand_notify.output_base64sha256
  role             = aws_iam_role.fcvm_fleet_on_demand_notify.arn
  runtime          = "python3.12"
  handler          = "notify.handler"
  timeout          = 30
  memory_size      = 128
  environment {
    variables = {
      ALARM_ARN  = local.fcvm_fleet_on_demand_alarm_arn
      TOPIC_ARNS = jsonencode(local.fcvm_fleet_alert_topic_arns)
    }
  }
  depends_on = [aws_iam_role_policy.fcvm_fleet_on_demand_notify, aws_cloudwatch_log_group.fcvm_fleet_on_demand_notify]
}

resource "aws_lambda_permission" "fcvm_fleet_on_demand_notify" {
  provider       = aws.fcvm_fleet
  statement_id   = "ThisOnDemandLaunchAlarmOnly"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.fcvm_fleet_on_demand_notify.function_name
  principal      = "lambda.alarms.cloudwatch.amazonaws.com"
  source_account = local.fcvm_fleet_account_id
  source_arn     = local.fcvm_fleet_on_demand_alarm_arn
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

# Its state object; the fleet secrets; GPU hosts and EC2 tiles from their templates only, tagged at launch, and terminated
# only when tagged so; and passing exactly the host and tile roles. In the fleet regions only (the account's SCP denies the others anyway).
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
        # The DNS token (the ACME challenge, and one sweep of launch names written to fcvm.dev before the subzone) and the
        # wildcard certificate it issues and renews. Not the
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
        # RunInstances checks a tagged persistent spot request separately and that resource has no LaunchTemplate key.
        # The instance/AMI/subnet/interface still need their template-scoped grants; this cannot authorize a launch alone.
        Sid      = "LaunchTaggedTileSpotRequestsOnly"
        Effect   = "Allow"
        Action   = "ec2:RunInstances"
        Resource = local.fcvm_fleet_tile_spot_request_arns
        Condition = { StringEquals = {
          "aws:RequestedRegion"    = local.fcvm_fleet_regions
          "aws:RequestTag/Purpose" = "fcvm-fleet-tile"
        } }
      },
      {
        # CreateFleet has its own resource authorization, in addition to RunInstances for the resources in a template.
        # The fleet itself has no instance-type or instance-tag context; do not require those on its ARN.
        Sid       = "LaunchHostsAsInstantFleets"
        Effect    = "Allow"
        Action    = "ec2:CreateFleet"
        Resource  = concat(local.fcvm_fleet_host_fleet_arns, local.fcvm_fleet_host_volume_arns)
        Condition = { StringEquals = { "aws:RequestedRegion" = local.fcvm_fleet_regions } }
      },
      {
        Sid      = "LaunchFleetInstancesAsTaggedHostsOnly"
        Effect   = "Allow"
        Action   = "ec2:CreateFleet"
        Resource = local.fcvm_fleet_host_instance_arns
        Condition = { StringEquals = {
          "aws:RequestedRegion"    = local.fcvm_fleet_regions
          "ec2:InstanceType"       = local.fcvm_fleet_host_types
          "aws:RequestTag/Purpose" = "fcvm-fleet-host"
        } }
      },
      {
        Sid       = "LaunchHostsFromTheirTemplates"
        Effect    = "Allow"
        Action    = "ec2:RunInstances"
        Resource  = local.fcvm_fleet_tile_created_arns
        Condition = { ArnLike = { "ec2:LaunchTemplate" = local.fcvm_fleet_host_template_condition } }
      },
      {
        # Exactly the host AMIs, host-eligible subnets, group and templates for both authorization actions. RunInstances
        # instance creation above still requires a host template. IfExists preserves the template association when
        # RunInstances supplies that key, while CreateFleet authorizes the exact template/resource ARNs without the key.
        Sid      = "LaunchHostsWithTheirOwnResources"
        Effect   = "Allow"
        Action   = ["ec2:RunInstances", "ec2:CreateFleet"]
        Resource = local.fcvm_fleet_host_referenced_arns
        Condition = {
          StringEquals    = { "aws:RequestedRegion" = local.fcvm_fleet_regions }
          ArnLikeIfExists = { "ec2:LaunchTemplate" = local.fcvm_fleet_host_template_condition }
        }
      },
      {
        # A launch from a template can still override the instance type: only the fleet's four types, whatever the
        # request says, and each template only its own kind (below).
        Sid       = "FleetTypesOnly"
        Effect    = "Deny"
        Action    = ["ec2:RunInstances", "ec2:CreateFleet"]
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringNotEquals = { "ec2:InstanceType" = concat(local.fcvm_fleet_tile_types, local.fcvm_fleet_host_types) } }
      },
      {
        Sid      = "TileTemplatesLaunchTileTypesOnly"
        Effect   = "Deny"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:*:*:instance/*"
        Condition = {
          ArnLike         = { "ec2:LaunchTemplate" = local.fcvm_fleet_tile_template_condition }
          StringNotEquals = { "ec2:InstanceType" = local.fcvm_fleet_tile_types }
        }
      },
      {
        Sid      = "HostTemplatesLaunchHostTypesOnly"
        Effect   = "Deny"
        Action   = "ec2:RunInstances"
        Resource = "arn:aws:ec2:*:*:instance/*"
        Condition = {
          ArnLike         = { "ec2:LaunchTemplate" = local.fcvm_fleet_host_template_condition }
          StringNotEquals = { "ec2:InstanceType" = local.fcvm_fleet_host_types }
        }
      },
      {
        # The instance a launch creates must carry Purpose=fcvm-fleet-tile or fcvm-fleet-host, which is what termination
        # is limited to.
        Sid       = "FleetInstancesAreTagged"
        Effect    = "Deny"
        Action    = ["ec2:RunInstances", "ec2:CreateFleet"]
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringNotEquals = { "aws:RequestTag/Purpose" = ["fcvm-fleet-tile", "fcvm-fleet-host"] } }
      },
      {
        Sid       = "TagAtLaunchOnly"
        Effect    = "Allow"
        Action    = "ec2:CreateTags"
        Resource  = "*"
        Condition = { StringEquals = { "ec2:CreateAction" = ["RunInstances", "CreateFleet"] } }
      },
      {
        Sid       = "TerminateFleetInstancesOnly"
        Effect    = "Allow"
        Action    = "ec2:TerminateInstances"
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = ["fcvm-fleet-tile", "fcvm-fleet-host"], "aws:RequestedRegion" = local.fcvm_fleet_regions } }
      },
      {
        # The tile pool (ejc3/fcvm#1273): stop a tile into the pool when the fleet sleeps and start it at the next wake.
        Sid       = "StartAndStopTilesOnly"
        Effect    = "Allow"
        Action    = ["ec2:StartInstances", "ec2:StopInstances"]
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = "fcvm-fleet-tile", "aws:RequestedRegion" = local.fcvm_fleet_regions } }
      },
      {
        # Moving a tile into and out of the pool retags it (FleetPool, FleetTile) and nothing else.
        Sid      = "RetagTilesForThePool"
        Effect   = "Allow"
        Action   = ["ec2:CreateTags", "ec2:DeleteTags"]
        Resource = "arn:aws:ec2:*:*:instance/*"
        Condition = {
          StringEquals                = { "ec2:ResourceTag/Purpose" = "fcvm-fleet-tile", "aws:RequestedRegion" = local.fcvm_fleet_regions }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["FleetPool", "FleetTile"] }
        }
      },
      {
        # A pool tile's spot request is persistent: it is cancelled before its instance is terminated, or it launches a
        # replacement. The pool template tags its requests Purpose=fcvm-fleet-tile.
        Sid       = "CancelTileSpotRequestsOnly"
        Effect    = "Allow"
        Action    = "ec2:CancelSpotInstanceRequests"
        Resource  = "arn:aws:ec2:*:*:spot-instances-request/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = "fcvm-fleet-tile", "aws:RequestedRegion" = local.fcvm_fleet_regions } }
      },
      {
        # The tile pool (ejc3/fcvm#1273): stop a tile into the pool when the fleet sleeps and start it at the next wake.
        Sid       = "StartAndStopTilesOnly"
        Effect    = "Allow"
        Action    = ["ec2:StartInstances", "ec2:StopInstances"]
        Resource  = "arn:aws:ec2:*:*:instance/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = "fcvm-fleet-tile", "aws:RequestedRegion" = local.fcvm_fleet_regions } }
      },
      {
        # Moving a tile into and out of the pool retags it (FleetPool, FleetTile) and nothing else.
        Sid      = "RetagTilesForThePool"
        Effect   = "Allow"
        Action   = ["ec2:CreateTags", "ec2:DeleteTags"]
        Resource = "arn:aws:ec2:*:*:instance/*"
        Condition = {
          StringEquals                = { "ec2:ResourceTag/Purpose" = "fcvm-fleet-tile", "aws:RequestedRegion" = local.fcvm_fleet_regions }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["FleetPool", "FleetTile"] }
        }
      },
      {
        # A pool tile's spot request is persistent: it is cancelled before its instance is terminated, or it launches a
        # replacement. The pool template tags its requests Purpose=fcvm-fleet-tile.
        Sid       = "CancelTileSpotRequestsOnly"
        Effect    = "Allow"
        Action    = "ec2:CancelSpotInstanceRequests"
        Resource  = "arn:aws:ec2:*:*:spot-instances-request/*"
        Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = "fcvm-fleet-tile", "aws:RequestedRegion" = local.fcvm_fleet_regions } }
      },
      {
        Sid      = "DescribeEc2"
        Effect   = "Allow"
        Action   = ["ec2:Describe*"]
        Resource = "*"
      },
      {
        Sid       = "PassTheHostAndTileRolesOnly"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = [aws_iam_role.fcvm_fleet_host.arn, aws_iam_role.fcvm_fleet_tile.arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } }
      },
    ]
  })
}

resource "aws_security_group" "fcvm_fleet_coordinator" {
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-coordinator"
  # The description is the group's original one, kept on purpose: it is ForceNew, and replacing the group would try to
  # delete it while the running task's interface still uses it (DependencyViolation). The ingress below is current.
  description = "fcvm fleet coordinator task: nothing in (browsers and agents arrive through its Cloudflare tunnel), all out"
  vpc_id      = module.fcvm_fleet_east2.vpc_id

  # The launch names' subzone (fcvm-fleet-dns.tf): UDP arrives with the resolver's own address, and TCP and the health
  # check from the load balancer. Nothing else comes in.
  ingress {
    description = "DNS over UDP"
    from_port   = local.fcvm_fleet_dns_port
    to_port     = local.fcvm_fleet_dns_port
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "DNS over TCP"
    from_port   = local.fcvm_fleet_dns_port
    to_port     = local.fcvm_fleet_dns_port
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # With aggregated streaming, browsers read the EC2 half's aggregator on 443 of the task's own addresses, not through the
  # tunnel (which Access gates and which would put Cloudflare in the media path). The coordinator's own port stays closed.
  dynamic "ingress" {
    for_each = local.fcvm_fleet_aggregated ? [1] : []
    content {
      description      = "EC2 half aggregator, TLS"
      from_port        = 443
      to_port          = 443
      protocol         = "tcp"
      cidr_blocks      = ["0.0.0.0/0"]
      ipv6_cidr_blocks = ["::/0"]
    }
  }

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
  depends_on               = [ephemeral.aws_ssm_parameter.fcvm_coordinator_release_guard]
  provider                 = aws.fcvm_fleet
  family                   = "fcvm-fleet-coordinator"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.fcvm_fleet_coordinator_execution.arn
  task_role_arn            = aws_iam_role.fcvm_fleet_coordinator_task.arn
  # The release function changes only the image and records its digest in the
  # release parameter. Track that revision without ignoring infrastructure drift.
  track_latest = true
  tags         = { Project = "fcvm-fleet" }

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  dynamic "volume" {
    for_each = local.fcvm_fleet_aggregated ? ["fleet-run"] : []
    content {
      name = volume.value
    }
  }

  container_definitions = jsonencode([
    merge({
      name      = "coordinator"
      image     = local.fcvm_fleet_coordinator_running ? nonsensitive(data.aws_ssm_parameter.fcvm_coordinator_release.value) : "public.ecr.aws/docker/library/busybox:stable"
      essential = true
      command   = local.fcvm_fleet_coordinator_running ? local.fcvm_fleet_coordinator_command : null
      # Its state is an S3 object, not a file (ECS.5: no writable root; a task volume where something must write).
      readonlyRootFilesystem = true
      # The subzone's DNS server (fcvm-fleet-dns.tf). One protocol per port mapping; TCP reaches the same port.
      portMappings = [{ containerPort = local.fcvm_fleet_dns_port, protocol = "udp" }]
      environment = [
        { name = "FLEET_STATE_BUCKET", value = aws_s3_bucket.fcvm_fleet_coordinator_state.bucket },
        { name = "FLEET_STATE_KEY", value = "state.json" },
        { name = "FLEET_REGIONS", value = join(",", local.fcvm_fleet_regions) },
        { name = "FLEET_HOST_TEMPLATES", value = join(",", local.fcvm_fleet_host_templates) },
        { name = "FLEET_HOST_SUBNETS", value = join(",", local.fcvm_fleet_host_subnets) },
        { name = "FLEET_HOST_ON_DEMAND_MAX", value = tostring(local.fcvm_fleet_on_demand_max) },
        # Each region's fleet subnets (one per AZ) and security group: a tile launch tries every AZ of a region, in this
        # order, before the next region (ejc3/fcvm#1284), instead of only the template's subnet. An image without it
        # ignores both variables and launches in the template's subnet.
        { name = "FLEET_EC2_SUBNETS", value = join(" ", [for r, m in local.fcvm_fleet_regional : "${r}:${join(",", m.subnet_ids)}"]) },
        { name = "FLEET_EC2_SECURITY_GROUPS", value = join(" ", [for r, m in local.fcvm_fleet_regional : "${r}:${m.security_group_id}"]) },
        # The coordinator logs its launches, scaling and agent connections at info; without this only warnings reach the log.
        { name = "RUST_LOG", value = "info" },
      ]
      logConfiguration = {
        logDriver = "awslogs"
        options   = { awslogs-group = aws_cloudwatch_log_group.fcvm_fleet_coordinator.name, awslogs-region = local.fcvm_fleet_home_region, awslogs-stream-prefix = "coordinator" }
      }
      },
      # The EC2 half's aggregator: the image runs as 65534 and binds 443 (ip_unprivileged_port_start covers IPv6 too),
      # and its certificate file goes on a task volume (the root file system is read-only). Absent when off, so the
      # task definition is the same as before.
      [for _ in(local.fcvm_fleet_aggregated ? [1] : []) : {
        systemControls = [{ namespace = "net.ipv4.ip_unprivileged_port_start", value = "443" }]
        mountPoints    = [{ sourceVolume = "fleet-run", containerPath = "/run/fcvm-fleet", readOnly = false }]
    }]...),
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
  lifecycle {
    precondition {
      condition = can(regex(
        "^${replace(aws_ecr_repository.fcvm_fleet_coordinator.repository_url, ".", "\\.")}@sha256:[0-9a-f]{64}$",
        nonsensitive(data.aws_ssm_parameter.fcvm_coordinator_release.value),
      ))
      error_message = "The coordinator release must be an immutable digest in its own ECR repository."
    }
  }
}

# A Fargate task gets an IPv6 address in a dual-stack subnet only with this account setting; the EC2 half's aggregator is
# named at it (agg-ec2-<token> AAAA). Without it the aggregator has its A record only.
resource "aws_ecs_account_setting_default" "fcvm_fleet_dual_stack" {
  count    = local.fcvm_fleet_aggregated ? 1 : 0
  provider = aws.fcvm_fleet
  name     = "dualStackIPv6"
  value    = "enabled"
}

resource "aws_ecs_service" "fcvm_fleet_coordinator" {
  # The listener too: ECS refuses a target group that no load balancer uses yet, and the listener is what attaches it.
  depends_on      = [ephemeral.aws_ssm_parameter.fcvm_coordinator_release_guard, aws_lb_listener.fcvm_fleet_dns]
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
    # Only the DNS load balancer's two AZs (fcvm-fleet-dns.tf): a target in an AZ the load balancer does not enable is
    # unused even with cross-zone load balancing, which would leave the subzone without its server.
    subnets          = local.fcvm_fleet_dns_subnets
    security_groups  = [aws_security_group.fcvm_fleet_coordinator.id]
    assign_public_ip = true
  }

  # The DNS load balancer (fcvm-fleet-dns.tf) registers the task's address; its TCP health check replaces a task whose
  # server stops answering.
  load_balancer {
    target_group_arn = aws_lb_target_group.fcvm_fleet_dns.arn
    container_name   = "coordinator"
    container_port   = local.fcvm_fleet_dns_port
  }
  health_check_grace_period_seconds = 60
}

output "fcvm_fleet_infra" {
  description = "The fcvm fleet's buckets, cluster and whether hosts and the coordinator are on (fcvm-fleet.tf)"
  value = {
    asset_bucket       = aws_s3_bucket.fcvm_fleet_assets.bucket
    state_bucket       = aws_s3_bucket.fcvm_fleet_coordinator_state.bucket
    cluster            = aws_ecs_cluster.fcvm_fleet.name
    coordinator_repo   = aws_ecr_repository.fcvm_fleet_coordinator.repository_url
    host_templates     = local.fcvm_fleet_host_templates
    on_demand_max      = local.fcvm_fleet_on_demand_max
    coordinator_on     = local.fcvm_fleet_coordinator_running
    tile_template_arns = local.fcvm_fleet_tile_template_arns
  }
}

# ohio-pbox.tf
#
# The parallel boxes (parallel-box.tf, parallel-box2.tf, launched by scripts/parallel-box.sh) moved from us-west-2 to
# us-east-2 on 2026-10-05, and the us-west-2 site was retired afterwards. The 192-core Graviton spot pool is about 30% cheaper there (us-east-2c $1.71/h against us-west-2d $2.49/h
# on 2026-10-04), and the 2c / 2a pools score the same 3/10 for placement as any other region does for that size.
#
# This file is the Ohio half: its own VPC, security group and key pair, copies of the two persistent work volumes (made
# from a fresh snapshot of each, so the data comes with them), the launch templates (parallel-box-launch.tf), an idle
# watchdog, and the private path to the shared NFS scratch on the I/O box, which stays in us-west-2.
#
# WHICH REGION A `pbox` COMMAND USES is local.parallel_box_region, published as /infra/parallel-box for the dev boxes.
# Both sites are fully wired and IAM covers both, so the move is that one value. The volumes are COPIES, though, so only
# the region's own is live (tag Live) and `pbox up` refuses the other: going back means copying the Ohio disks to
# us-west-2 first, never just flipping the value.
#
# EBS is AZ-bound, so the volumes pin the box to local.ohio_pbox_az. 2c is the cheapest pool and scores 3; 2a scores 3
# and costs $2.48; 2b scores 1.

locals {
  ohio_pbox_az = "us-east-2c"

  # Where `pbox` launches. Flip it only with the boxes down and the volume snapshots taken just before (apply this file,
  # then flip, in quick succession): work written to the old region's disks after the snapshot is not in the copy.
  # Moved to us-east-2 on 2026-10-05 from the snapshot copied earlier, not a fresh one: anything written to the us-west-2
  # disks after that snapshot was left behind, which the owner accepted. Those volumes are now Live=false and kept as the
  # only copy of that data until they are deleted.
  parallel_box_region = "us-east-2"

  # The persistent volumes, by box number: size and Name tag (scripts/parallel-box.sh finds a volume by its Name tag).
  # One per slot in local.parallel_boxes (parallel-box-launch.tf); slots 3 and 4 were added on 2026-10-10, 5 to 8 on 2026-10-19.
  ohio_pbox_volumes = {
    "1" = { size = 300, name = "parallel-box-work" }
    "2" = { size = 100, name = "parallel-box-2-work" }
    "3" = { size = 100, name = "parallel-box-3-work" }
    "4" = { size = 100, name = "parallel-box-4-work" }
    "5" = { size = 100, name = "parallel-box-5-work" }
    "6" = { size = 100, name = "parallel-box-6-work" }
    "7" = { size = 100, name = "parallel-box-7-work" }
    "8" = { size = 100, name = "parallel-box-8-work" }
  }

  # The largest flavor (pbox-flavors.tf: the -192 ones, .48xlarge and .metal-48xl). Smaller flavors only leave room.
  parallel_box_max_vcpus = 192
}

# ============================================================ spot vCPU quota
# L-34B43A08 = "All Standard (A, C, D, H, I, M, R, T, Z) Spot Instance Requests" in us-east-2, shared by the owner's
# parallel boxes, the members' pboxes (member-pbox.tf) and the fcvm metal runners (runner-vpc.tf). Ohio's default (640)
# was exactly full with 8 metal runners on 2026-10-04, and the launcher (no fallback to another region) logged 18
# MaxSpotInstanceCountExceeded errors an hour, so it went to 1,536 then. With eight slots, 8 x 192 = 1,536 is that
# whole quota on its own, so it is the 1,536 the runners and members' boxes already had plus every slot at full size
# (3,072 for eight slots).
#
# How the provider handles it: a change is a RequestServiceQuotaIncrease at apply. While AWS (or a support case) has the
# request open, a refresh reports the requested value, so the plan stays empty; once approved it reads the applied value,
# also empty. A denied or partly granted request reads back the real quota and shows a diff again (the next apply asks
# again). A value below the current quota cannot be requested, and destroy only drops it from state.
resource "aws_servicequotas_service_quota" "ohio_spot_standard" {
  provider     = aws.ohio
  service_code = "ec2"
  quota_code   = "L-34B43A08"
  value        = 1536 + length(local.parallel_boxes) * local.parallel_box_max_vcpus
}

variable "parallel_box_ami_ohio" {
  description = "Ubuntu 24.04 arm64 in us-east-2 (the same family as var.parallel_box_ami in us-west-2)"
  type        = string
  default     = "ami-0dcc74e3bca0f40dc" # ubuntu-noble-24.04-arm64-server-20260923
}

# The x86_64 build of the same Ubuntu image, for the one x86 flavor (kvm-gpu-96 = g4dn.metal, pbox-flavors.tf). The launch
# templates boot the arm64 image; `pbox up` passes this one with --image-id for an x86 type, and parallel-box-control allows
# it by exact ARN. Bump it together with var.parallel_box_ami_ohio and pbox-fallback.tf's image names.
variable "parallel_box_ami_ohio_x86" {
  description = "Ubuntu 24.04 amd64 in us-east-2 (the x86_64 build of var.parallel_box_ami_ohio), for x86 pbox flavors"
  type        = string
  default     = "ami-0fa99aa8f97f9e30b" # ubuntu-noble-24.04-amd64-server-20260923
}

# ============================================================ network
# 10.12.0.0/16: not the main VPC (10.0), the runner VPCs (10.1, 10.11) or the us-west-2 default VPC (172.31).
resource "aws_vpc" "ohio_pbox" {
  provider                         = aws.ohio
  cidr_block                       = "10.12.0.0/16"
  enable_dns_hostnames             = true
  enable_dns_support               = true
  assign_generated_ipv6_cidr_block = true

  tags = { Name = "parallel-box-vpc-ohio" }
}

resource "aws_internet_gateway" "ohio_pbox" {
  provider = aws.ohio
  vpc_id   = aws_vpc.ohio_pbox.id

  tags = { Name = "parallel-box-igw-ohio" }
}

resource "aws_route_table" "ohio_pbox" {
  provider = aws.ohio
  vpc_id   = aws_vpc.ohio_pbox.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.ohio_pbox.id
  }

  # IPv6 matters even though nothing here is IPv6-first: DNS returns AAAA first, and without it every outbound v6
  # connection sits in SYN-SENT until TCP gives up (see parallel-box.tf).
  route {
    ipv6_cidr_block = "::/0"
    gateway_id      = aws_internet_gateway.ohio_pbox.id
  }

  tags = { Name = "parallel-box-rt-ohio" }

  # The route to the I/O box is its own resource (aws_route.ohio_pbox_to_io_box). Inline routes and aws_route on one table
  # fight: without this each plan proposed stripping the peering route and putting the two defaults back.
  lifecycle {
    ignore_changes = [route]
  }
}

resource "aws_subnet" "ohio_pbox" {
  provider                        = aws.ohio
  vpc_id                          = aws_vpc.ohio_pbox.id
  cidr_block                      = "10.12.1.0/24"
  availability_zone               = local.ohio_pbox_az
  map_public_ip_on_launch         = true
  ipv6_cidr_block                 = cidrsubnet(aws_vpc.ohio_pbox.ipv6_cidr_block, 8, 1)
  assign_ipv6_address_on_creation = true

  tags = { Name = "parallel-box-subnet-ohio" }
}

resource "aws_route_table_association" "ohio_pbox" {
  provider       = aws.ohio
  subnet_id      = aws_subnet.ohio_pbox.id
  route_table_id = aws_route_table.ohio_pbox.id
}

resource "aws_security_group" "ohio_pbox" {
  provider    = aws.ohio
  name_prefix = "parallel-box-"
  description = "On-demand parallel compute box: SSH only"
  vpc_id      = aws_vpc.ohio_pbox.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = { Name = "parallel-box" }

  lifecycle {
    create_before_destroy = true
  }
}

# The second group a box gets when it is launched with `pbox up N FLAVOR --open` (scripts/parallel-box.sh), next to the
# SSH group above: everything in, from anywhere, over IPv4 and IPv6. An --open launch also asks for a delegated IPv6 /80
# on the box's primary interface, so fcvm VMs on it (`--network routed --ipv6-prefix <the /80>`) each get a public
# address that can be reached on any port. The group applies to the box's own addresses as well, so every port the box
# itself listens on is public too. A plain `pbox up` never attaches it. us-east-2 runs no Security Hub
# (security-hub-automation.tf), so the group raises no EC2.18/EC2.19 finding.
resource "aws_security_group" "ohio_pbox_open" {
  provider    = aws.ohio
  name_prefix = "parallel-box-open-"
  description = "Parallel box launched with pbox up --open: all traffic in from anywhere, IPv4 and IPv6"
  vpc_id      = aws_vpc.ohio_pbox.id

  ingress {
    description      = "Everything, from anywhere (pbox up --open only)"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = { Name = "parallel-box-open" }

  lifecycle {
    create_before_destroy = true
  }
}

# The security design logs every VPC's traffic to the audit bucket (security-monitoring.tf).
resource "aws_flow_log" "security_ohio_pbox" {
  provider                 = aws.ohio
  vpc_id                   = aws_vpc.ohio_pbox.id
  traffic_type             = "ALL"
  log_destination_type     = "s3"
  log_destination          = "${aws_s3_bucket.security_audit.arn}/vpc-flow"
  max_aggregation_interval = 600
  tags                     = { Name = "security-ohio-pbox-flow-log", Managed = "terraform" }
  depends_on               = [aws_s3_bucket_policy.security_audit]
}

# The same public key as every other fcvm-ec2 key pair (a key pair is regional); its own name because the runner's
# fcvm-ec2 in this region (ohio.tf) is count-gated by the runner switch.
resource "aws_key_pair" "ohio_pbox" {
  provider   = aws.ohio
  key_name   = "fcvm-ec2-pbox-ohio"
  public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINwtXjjTCVgT9OR3qrnz3zDkV2GveuCBlWFXSOBG2joe fcvm-ec2"
  tags       = { Name = "fcvm-ec2-pbox-ohio" }
}

# ============================================================ the NFS scratch, over a peer
# The boxes mount /srv/io from the I/O box (io-box.tf), which stays in us-west-2: private, over a peering connection,
# exactly as the dev fleet reaches it. Only the I/O box's own subnet is routed from here (not the whole default VPC), and
# the Ohio subnet is added to local.io_box_nfs_client_cidrs. About 50 ms away, so bulk data and caches only; and the box's
# automount means a stopped I/O box never blocks a boot.
#
# The I/O box ignores user_data after creation, so its /etc/exports.d/io-box.exports does not list this subnet from the box's
# build. aws_ssm_association.io_box_exports (io-box.tf) rewrites that file from local.io_box_nfs_client_cidrs whenever the box is
# up, so the server admits exactly what the security group does and nothing is edited by hand.
resource "aws_vpc_peering_connection" "ohio_pbox_io" {
  provider    = aws.ohio
  vpc_id      = aws_vpc.ohio_pbox.id
  peer_vpc_id = data.aws_vpc.west2_default.id
  peer_region = "us-west-2"
  auto_accept = false

  tags = { Name = "pbox-ohio-to-io-box" }
}

resource "aws_vpc_peering_connection_accepter" "ohio_pbox_io" {
  provider                  = aws.west2
  vpc_peering_connection_id = aws_vpc_peering_connection.ohio_pbox_io.id
  auto_accept               = true

  tags = { Name = "pbox-ohio-to-io-box" }
}

resource "aws_route" "ohio_pbox_to_io_box" {
  provider                  = aws.ohio
  route_table_id            = aws_route_table.ohio_pbox.id
  destination_cidr_block    = data.aws_subnet.io_box.cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.ohio_pbox_io.id

  depends_on = [aws_vpc_peering_connection_accepter.ohio_pbox_io]
}

resource "aws_route" "west2_to_ohio_pbox" {
  provider                  = aws.west2
  route_table_id            = data.aws_vpc.west2_default.main_route_table_id
  destination_cidr_block    = aws_vpc.ohio_pbox.cidr_block
  vpc_peering_connection_id = aws_vpc_peering_connection.ohio_pbox_io.id

  depends_on = [aws_vpc_peering_connection_accepter.ohio_pbox_io]
}

# ============================================================ the work volumes
# They were made from a snapshot of each us-west-2 volume, copied across; both snapshots, the copies and the us-west-2 volumes
# are gone, so snapshot_id is ignored (it is not in the config) and these are the only copy of the data.
resource "aws_ebs_volume" "ohio_parallel_work" {
  provider          = aws.ohio
  for_each          = local.ohio_pbox_volumes
  availability_zone = local.ohio_pbox_az
  size              = each.value.size
  type              = "gp3"
  encrypted         = true

  tags = {
    Name    = each.value.name
    Purpose = "persistent scratch for the on-demand 192-core box"
  }

  # Losing one loses real work: nothing but an edit of this block can delete them. snapshot_id is ignored because the
  # volumes were restored from a snapshot that no longer exists.
  lifecycle {
    prevent_destroy = true
    ignore_changes  = [snapshot_id]
  }
}

# ============================================================ idle watchdog
# The same function as parallel-box-watchdog.tf's, in this region: CloudWatch metrics and TerminateInstances are
# regional. It shares that role (IAM is global; the policy already allows terminating the parallel boxes by tag in any
# region) and watches the parallel boxes and the members' pboxes: the GPU boxes stay in us-west-2.
resource "aws_lambda_function" "parallel_watchdog_ohio" {
  provider         = aws.ohio
  function_name    = "parallel-box-watchdog"
  role             = aws_iam_role.parallel_watchdog.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 60
  filename         = data.archive_file.parallel_watchdog.output_path
  source_code_hash = data.archive_file.parallel_watchdog.output_base64sha256

  environment {
    variables = {
      IDLE_MINUTES = tostring(var.parallel_box_idle_minutes)
      IDLE_CPU_PCT = tostring(var.parallel_box_idle_cpu_pct)
      # The owner's parallel boxes and the members' pboxes (member-pbox.tf). Only the members'
      # have a hard lifetime: a long job is the point of the owner's.
      TAG_NAMES       = join(",", concat(local.parallel_box_names, local.member_pbox_names))
      MAX_AGE_MINUTES = jsonencode({ for n in local.member_pbox_names : n => local.member_box_max_hours * 60 })
      SINGLE_NAMES    = join(",", local.member_pbox_names) # one instance per member pbox
      IO_THRESHOLDS   = local.parallel_watchdog_io_thresholds
      LEASE_NAMES     = join(",", local.parallel_box_names) # `pbox up --lease`: the owner's boxes, never a member's
      SNS_REGION      = var.aws_region
      SNS_TOPIC_ARN   = aws_sns_topic.cost_alerts.arn
    }
  }

  tags = { Name = "parallel-box-watchdog" }
}

resource "aws_cloudwatch_event_rule" "parallel_watchdog_ohio" {
  provider            = aws.ohio
  name                = "parallel-box-watchdog"
  description         = "Terminate the parallel box when idle"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "parallel_watchdog_ohio" {
  provider  = aws.ohio
  rule      = aws_cloudwatch_event_rule.parallel_watchdog_ohio.name
  target_id = "parallel-box-watchdog"
  arn       = aws_lambda_function.parallel_watchdog_ohio.arn
}

resource "aws_lambda_permission" "parallel_watchdog_ohio" {
  provider      = aws.ohio
  statement_id  = "AllowExecutionFromCloudWatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.parallel_watchdog_ohio.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.parallel_watchdog_ohio.arn
}

# ============================================================ which region `pbox` uses
# Read by scripts/parallel-box.sh on every dev box (a plain SSM read, the same way /infra/applied-status is). The
# parameter holds no secret; the policy that lets a dev box read it is in applied-status.tf. "x86_image" is the home site's
# x86_64 image, which `pbox up` passes with --image-id for an x86 flavor (pbox-flavors.tf). "fallback" is where
# `pbox up N FLAVOR --fallback` may launch instead (pbox-fallback.tf): the template name and region => AZ => subnet, and
# each region's x86 image. "open" is the network interface `pbox up N FLAVOR --open` launches with in place of the
# template's: the home subnet and both security groups, the SSH one first.
locals {
  parallel_box_open_pointer = {
    subnet          = local.parallel_box_sites[local.parallel_box_region].subnet_id
    security_groups = [local.parallel_box_sites[local.parallel_box_region].security_group_id, local.parallel_box_sites[local.parallel_box_region].open_security_group_id]
  }
}

resource "aws_ssm_parameter" "parallel_box_region" {
  name        = "/infra/parallel-box"
  description = "Which region the parallel boxes launch in (see ohio-pbox.tf) and their fallback sites (pbox-fallback.tf); read by scripts/parallel-box.sh"
  type        = "String"
  tier        = "Standard"
  value       = jsonencode({ region = local.parallel_box_region, x86_image = local.parallel_box_sites[local.parallel_box_region].ami_x86, open = local.parallel_box_open_pointer, fallback = local.pbox_fallback_pointer })
}

# ohio-pbox.tf
#
# The parallel boxes (parallel-box.tf, parallel-box2.tf, launched by scripts/parallel-box.sh) move from us-west-2 to
# us-east-2. The 192-core Graviton spot pool is about 30% cheaper there (us-east-2c $1.71/h against us-west-2d $2.49/h
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
  parallel_box_region = "us-west-2"

  # The two persistent volumes being copied, by box number: the us-west-2 volume, its size and its Name tag.
  pbox_move_volumes = {
    "1" = { west_volume_id = aws_ebs_volume.parallel_work.id, size = 300, name = "parallel-box-work" }
    "2" = { west_volume_id = aws_ebs_volume.parallel_work_2.id, size = 100, name = "parallel-box-2-work" }
  }
}

variable "parallel_box_ami_ohio" {
  description = "Ubuntu 24.04 arm64 in us-east-2 (the same family as var.parallel_box_ami in us-west-2)"
  type        = string
  default     = "ami-0dcc74e3bca0f40dc" # ubuntu-noble-24.04-arm64-server-20260923
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
# ONE STEP NOT IN TERRAFORM: the I/O box ignores user_data after creation, so its /etc/exports.d/io-box.exports does not
# list this subnet until the box is rebuilt. Until then the mount is refused by the server (the security group admits it).
# To enable it on the running box, add the subnet to that file and `exportfs -ra`:
#   echo '/srv/io 10.12.1.0/24(rw,async,no_subtree_check,root_squash,fsid=0)' | sudo tee -a /etc/exports.d/io-box.exports
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
# A snapshot of each us-west-2 volume, copied here and restored as the Ohio volume, so the data moves with the box. The
# snapshot is taken when this is applied: apply it while the boxes are down (`pbox down`) for a copy that is exactly
# the disk. Once the move is done and checked, the two snapshots (and the old volumes) can be deleted.
# A snapshot of a volume that was itself restored from a snapshot reads its blocks lazily and is slow: the 300 GB one took
# about 2.5 hours, which is why the create timeouts here (and on the copy) are 6h, not the 2h that failed it.
resource "aws_ebs_snapshot" "pbox_move" {
  provider    = aws.west2
  for_each    = local.pbox_move_volumes
  volume_id   = each.value.west_volume_id
  description = "${each.value.name} for the move to us-east-2"

  timeouts {
    create = "6h"
  }

  tags = { Name = "${each.value.name}-move" }
}

resource "aws_ebs_snapshot_copy" "pbox_move" {
  provider           = aws.ohio
  for_each           = local.pbox_move_volumes
  source_snapshot_id = aws_ebs_snapshot.pbox_move[each.key].id
  source_region      = "us-west-2"
  encrypted          = true
  description        = "${each.value.name} for the move to us-east-2"

  timeouts {
    create = "6h"
  }

  tags = { Name = "${each.value.name}-move" }

  # AWS records its default EBS key here after the copy; the config names none, and a null against that key forces replacement
  # (a second 8 minute copy, then a destroyed one the volumes were restored from).
  lifecycle {
    ignore_changes = [kms_key_id]
  }
}

# The same protection as the originals: losing one loses real work. snapshot_id is ignored after creation so deleting the
# move snapshot later never replaces the volume.
resource "aws_ebs_volume" "ohio_parallel_work" {
  provider          = aws.ohio
  for_each          = local.pbox_move_volumes
  availability_zone = local.ohio_pbox_az
  snapshot_id       = aws_ebs_snapshot_copy.pbox_move[each.key].id
  size              = each.value.size
  type              = "gp3"
  encrypted         = true

  tags = {
    Name    = each.value.name
    Purpose = "persistent scratch for the on-demand 192-core box"
    # A COPY until local.parallel_box_region is us-east-2; then this is the live disk and the us-west-2 one is the stale
    # copy. `pbox up` refuses a volume whose Live tag is false (scripts/parallel-box.sh).
    Live = local.parallel_box_region == "us-east-2" ? "true" : "false"
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes  = [snapshot_id]
  }
}

# ============================================================ idle watchdog
# The same function as parallel-box-watchdog.tf's, in this region: CloudWatch metrics and TerminateInstances are
# regional. It shares that role (IAM is global; the policy already allows terminating the parallel boxes by tag in any
# region) and watches only the two parallel boxes: the GPU box stays in us-west-2.
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
      IDLE_MINUTES  = tostring(var.parallel_box_idle_minutes)
      IDLE_CPU_PCT  = tostring(var.parallel_box_idle_cpu_pct)
      TAG_NAMES     = "parallel-box,parallel-box-2"
      SNS_REGION    = var.aws_region
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
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
# parameter holds no secret; the policy that lets a dev box read it is in applied-status.tf.
resource "aws_ssm_parameter" "parallel_box_region" {
  name        = "/infra/parallel-box"
  description = "Which region the parallel boxes launch in (see ohio-pbox.tf); read by scripts/parallel-box.sh"
  type        = "String"
  tier        = "Standard"
  value       = jsonencode({ region = local.parallel_box_region })
}

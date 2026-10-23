# modules/fcvm-fleet-region
#
# One region of fcvm's GPU VM wall (fcvm-fleet.tf; design ejc3/fcvm#1136, docs/fleet/ARCHITECTURE.md), inside the fcvm-fleet
# member account (fcvm-fleet-account.tf): the network, the GPU host group and the EC2 comparison tiles' launch templates.
#
# - Network: a VPC with an Amazon-provided IPv6 /56, one public subnet per AZ (IPv4 /20 and IPv6 /64), an internet gateway,
#   and one security group that admits TCP 443 from anywhere and nothing else. No SSH: hosts are reached with SSM Session
#   Manager.
# - GPU hosts: a launch template per architecture (x86_64 for g4dn.metal, arm64 for g5g.metal) and one spot auto scaling
#   group over both. The primary interface asks for a delegated IPv6 /80 (Ipv6PrefixCount = 1) for the host's VMs and no
#   single address: EC2 creates an interface with one of an address count or a prefix count, never both (checked
#   2026-10-22). IMDSv2 with hop limit 1, so no VM or container on the host reaches the metadata service.
# - GATE: the group's capacity is 0 unless var.hosts_enabled and at least one architecture has an AMI. The fleet AMI does
#   not exist yet, and an always-on metal GPU host is the account's main cost; only types whose architecture has an AMI are
#   in the group's overrides.
# - EC2 tiles: a launch template per architecture (g4dn.xlarge, g5g.xlarge) with one IPv6 address; no group, since the
#   coordinator launches and terminates tiles itself (its task role is limited to these templates).

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "cidr_block" {
  description = "The region's VPC /16; one /20 per AZ is carved from it in AZ order"
  type        = string
}

variable "gpu_amis" {
  description = "The fleet host AMI per architecture in this region (x86_64, arm64); an empty value leaves that type out"
  type        = map(string)
}

variable "tile_amis" {
  description = "The EC2 tile AMI per architecture in this region (x86_64, arm64); an empty value skips that template"
  type        = map(string)
}

variable "hosts_enabled" {
  description = "Run one spot GPU host here (fcvm-fleet.tf's local.fcvm_fleet_hosts_enabled); false keeps the group at 0"
  type        = bool
}

variable "host_profile_name" {
  type = string
}

variable "tile_profile_name" {
  type = string
}

variable "flow_log_destination" {
  description = "S3 ARN (bucket/prefix) the VPC flow log is delivered to"
  type        = string
}

# The bucket policy that lets VPC flow logs write there: the flow log waits for it through this value, not through a
# module-level depends_on, which would defer the module's data sources (the AZ list) to apply time.
variable "flow_log_bucket_policy_id" {
  description = "Id of the flow-log bucket's policy; the flow log is created after it"
  type        = string
}

data "aws_region" "current" {}

data "aws_availability_zones" "available" {
  state = "available"
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

locals {
  region = data.aws_region.current.region
  azs    = sort(data.aws_availability_zones.available.names)
  # Architecture => the metal GPU type and the one-GPU tile type of that architecture.
  host_types = { x86_64 = "g4dn.metal", arm64 = "g5g.metal" }
  tile_types = { x86_64 = "g4dn.xlarge", arm64 = "g5g.xlarge" }
  host_archs = [for a in sort(keys(local.host_types)) : a if try(var.gpu_amis[a], "") != ""]
  tile_archs = [for a in sort(keys(local.tile_types)) : a if try(var.tile_amis[a], "") != ""]
  # One host while enabled and an AMI exists; 0 otherwise. max 2 lets a capacity-rebalance replacement start before the old
  # host is gone.
  host_desired = var.hosts_enabled && length(local.host_archs) > 0 ? 1 : 0
  # The AMI carries the agent as a systemd unit; boot only records that it ran (fcvm#1136, "Hosts and capacity").
  user_data = base64encode(<<-EOT
    #!/bin/bash
    echo "fcvm-fleet host booted $(date -u +%FT%TZ)" > /var/log/fcvm-fleet-boot.log
  EOT
  )
}

# ------------------------------------------------------------------------------------------------ network
resource "aws_vpc" "this" {
  cidr_block                       = var.cidr_block
  assign_generated_ipv6_cidr_block = true
  enable_dns_support               = true
  enable_dns_hostnames             = true
  tags                             = { Name = "fcvm-fleet-${local.region}" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "fcvm-fleet-${local.region}" }
}

resource "aws_subnet" "public" {
  for_each                                       = { for i, az in local.azs : az => i }
  vpc_id                                         = aws_vpc.this.id
  availability_zone                              = each.key
  cidr_block                                     = cidrsubnet(var.cidr_block, 4, each.value)
  ipv6_cidr_block                                = cidrsubnet(aws_vpc.this.ipv6_cidr_block, 8, each.value)
  assign_ipv6_address_on_creation                = true
  map_public_ip_on_launch                        = true
  enable_resource_name_dns_aaaa_record_on_launch = true
  tags                                           = { Name = "fcvm-fleet-${each.key}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.this.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }
  route {
    ipv6_cidr_block = "::/0"
    gateway_id      = aws_internet_gateway.this.id
  }
  tags = { Name = "fcvm-fleet-${local.region}" }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# TCP 443 from anywhere, nothing else in: the VMs' renderers and the tiles serve HTTPS on their own IPv6 addresses.
resource "aws_security_group" "fleet" {
  name        = "fcvm-fleet"
  description = "fcvm fleet hosts and tiles: TCP 443 in from anywhere, nothing else in"
  vpc_id      = aws_vpc.this.id

  ingress {
    description      = "HTTPS from anywhere"
    from_port        = 443
    to_port          = 443
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  egress {
    description      = "All outbound"
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = { Name = "fcvm-fleet" }
}

resource "aws_flow_log" "this" {
  vpc_id                   = aws_vpc.this.id
  traffic_type             = "ALL"
  log_destination_type     = "s3"
  log_destination          = var.flow_log_destination
  max_aggregation_interval = 600
  tags                     = { Name = "fcvm-fleet-${local.region}-flow-log", BucketPolicy = var.flow_log_bucket_policy_id }
}

# ------------------------------------------------------------------------------------------------ GPU hosts
resource "aws_launch_template" "host" {
  for_each      = toset(local.host_archs)
  name          = "fcvm-fleet-host-${each.key}"
  description   = "fcvm fleet GPU host, ${each.key} (${local.host_types[each.key]}): the fleet AMI, a delegated IPv6 /80, IMDSv2 hop 1"
  image_id      = var.gpu_amis[each.key]
  instance_type = local.host_types[each.key]
  user_data     = local.user_data

  iam_instance_profile {
    name = var.host_profile_name
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  network_interfaces {
    device_index                = 0
    security_groups             = [aws_security_group.fleet.id]
    associate_public_ip_address = true
    ipv6_prefix_count           = 1
    delete_on_termination       = true
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "fcvm-fleet-host", Purpose = "fcvm-fleet-host" }
  }

  tags = { Name = "fcvm-fleet-host-${each.key}" }
}

resource "aws_autoscaling_group" "hosts" {
  # Created only once an AMI exists: a mixed instances policy needs at least one launch template.
  count = length(local.host_archs) > 0 ? 1 : 0

  name                      = "fcvm-fleet-hosts"
  vpc_zone_identifier       = [for s in aws_subnet.public : s.id]
  min_size                  = 0
  max_size                  = local.host_desired == 0 ? 0 : 2
  desired_capacity          = local.host_desired
  capacity_rebalance        = true
  health_check_type         = "EC2"
  wait_for_capacity_timeout = "0"

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "price-capacity-optimized"
    }

    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.host[local.host_archs[0]].id
        version            = "$Latest"
      }

      dynamic "override" {
        for_each = local.host_archs
        content {
          instance_type = local.host_types[override.value]
          launch_template_specification {
            launch_template_id = aws_launch_template.host[override.value].id
            version            = "$Latest"
          }
        }
      }
    }
  }

  tag {
    key                 = "Purpose"
    value               = "fcvm-fleet-host"
    propagate_at_launch = true
  }

  # The coordinator owns the desired capacity once the gate is on (0 with no viewer, back up for the next one); an apply
  # must not reset it. max_size above is the gate.
  lifecycle {
    ignore_changes = [desired_capacity]
  }
}

# ------------------------------------------------------------------------------------------------ EC2 tiles
resource "aws_launch_template" "tile" {
  for_each      = toset(local.tile_archs)
  name          = "fcvm-fleet-tile-${each.key}"
  description   = "fcvm fleet EC2 comparison tile, ${each.key} (${local.tile_types[each.key]}): launched by the coordinator"
  image_id      = var.tile_amis[each.key]
  instance_type = local.tile_types[each.key]

  iam_instance_profile {
    name = var.tile_profile_name
  }

  instance_market_options {
    market_type = "spot"
    spot_options {
      spot_instance_type             = "one-time"
      instance_interruption_behavior = "terminate"
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  network_interfaces {
    device_index                = 0
    security_groups             = [aws_security_group.fleet.id]
    associate_public_ip_address = true
    ipv6_address_count          = 1
    delete_on_termination       = true
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "fcvm-fleet-tile", Purpose = "fcvm-fleet-tile" }
  }

  tags = { Name = "fcvm-fleet-tile-${each.key}" }
}

# The same tile on demand: the coordinator's fallback when EC2 has no spot capacity for a tile in any fleet region
# (fcvm fleet coord --ec2-on-demand-template fcvm-fleet-tile-on-demand-<arch>). Everything but the market is the spot
# template's.
resource "aws_launch_template" "tile_on_demand" {
  for_each      = toset(local.tile_archs)
  name          = "fcvm-fleet-tile-on-demand-${each.key}"
  description   = "fcvm fleet EC2 comparison tile on demand, ${each.key} (${local.tile_types[each.key]}): the coordinator's fallback"
  image_id      = var.tile_amis[each.key]
  instance_type = local.tile_types[each.key]

  iam_instance_profile {
    name = var.tile_profile_name
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  network_interfaces {
    device_index                = 0
    security_groups             = [aws_security_group.fleet.id]
    associate_public_ip_address = true
    ipv6_address_count          = 1
    delete_on_termination       = true
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "fcvm-fleet-tile", Purpose = "fcvm-fleet-tile" }
  }

  tags = { Name = "fcvm-fleet-tile-on-demand-${each.key}" }
}

output "vpc_id" {
  value = aws_vpc.this.id
}

output "subnet_ids" {
  value = [for s in aws_subnet.public : s.id]
}

output "security_group_id" {
  value = aws_security_group.fleet.id
}

# Both markets: the coordinator's RunInstances is limited to these templates.
output "tile_template_arns" {
  value = concat([for t in aws_launch_template.tile : t.arn], [for t in aws_launch_template.tile_on_demand : t.arn])
}

output "host_group_desired" {
  value = local.host_desired
}

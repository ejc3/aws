# modules/fcvm-fleet-region
#
# One region of fcvm's GPU VM wall (fcvm-fleet.tf; design ejc3/fcvm#1136, docs/fleet/ARCHITECTURE.md), inside the fcvm-fleet
# member account (fcvm-fleet-account.tf): the network and the GPU host and EC2 comparison tile launch templates.
#
# - Network: a VPC with an Amazon-provided IPv6 /56, one public subnet per AZ (IPv4 /20 and IPv6 /64), an internet gateway,
#   and one security group that admits TCP 443 from anywhere and nothing else. No SSH: hosts are reached with SSM Session
#   Manager.
# - GPU hosts: a launch template per architecture (x86_64 for g4dn.metal, arm64 for g5g.metal). No group: the coordinator
#   launches and terminates the hosts itself from these templates (fcvm fleet coord --host-template, --host-subnet,
#   ejc3/fcvm#1257): spot first with EC2 Fleet (CreateFleet, type instant, over every template and host subnet, price-capacity-
#   optimized), and on demand in the same pass when spot is refused for capacity, at most local.fcvm_fleet_on_demand_max
#   on-demand hosts across the fleet; every host is terminated while the fleet sleeps. This replaced a spot auto scaling
#   group and an on-demand one whose desired capacity the coordinator set: Auto Scaling took 6 to 14 s to start a launch
#   after a raise and about 10 s more to record a refused spot launch. The primary interface asks for a delegated IPv6 /80
#   (Ipv6PrefixCount = 1) for the host's VMs and no single address: EC2 creates an interface with one of an address count
#   or a prefix count, never both (checked 2026-10-22). IMDSv2 with hop limit 1, so no VM or container on the host reaches
#   the metadata service.
# - GATE: the coordinator is given this region's host templates (output host_template_names) only while var.hosts_enabled
#   and at least one architecture has an AMI; only types whose architecture has an AMI get a template.
# - Host AZs: hosts launch only in the subnets of AZs that offer a host type (output host_subnet_ids). us-east-1e has no
#   g4dn.metal, and a spot launch there failed with InvalidFleetConfiguration (2026-10-24).
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

variable "ip_options_bootstrap" {
  description = "Reviewed IPv4 TCP-options guard installer; no credentials"
  type        = string
}

variable "ip_options_guard_sha256" {
  description = "The canonical reviewed guard helper digest required on every admitted fleet AMI"
  type        = string
}

variable "tile_amis" {
  description = "The EC2 tile AMI per architecture in this region (x86_64, arm64); an empty value skips that template"
  type        = map(string)
}

variable "hosts_enabled" {
  description = "Give the coordinator this region's host templates (fcvm-fleet.tf's local.fcvm_fleet_hosts_enabled)"
  type        = bool
}

variable "alert_email" {
  description = "The existing fleet-alerts subscriber; preserve the topic and confirmed subscription when groups go away"
  type        = string
}

variable "host_agent_args" {
  description = "The host agent's arguments (fcvm fleet agent, FLEET_AGENT_ARGS in /etc/fcvm-fleet/agent.env); empty leaves the agent off"
  type        = string
  default     = ""
}

variable "slot_tls_ports" {
  description = "The GPU host ports each slot's renderer TLS port is forwarded on, for IPv4 viewers (fcvm fleet agent --tls-publish-base)"
  type        = object({ from = number, to = number })
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

# A public listener may resume before cloud-init. Admit only images with the
# persistent reviewed guard, including every nonempty caller-supplied override.
data "aws_ami" "guarded" {
  for_each = toset(compact(concat(values(var.gpu_amis), values(var.tile_amis))))
  owners   = ["self"]
  filter {
    name   = "image-id"
    values = [each.value]
  }
  filter {
    name   = "is-public"
    values = ["false"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
  filter {
    name   = "tag:GuardHelperSHA256"
    values = [var.ip_options_guard_sha256]
  }
}

data "aws_availability_zones" "available" {
  state = "available"
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

data "aws_ec2_instance_type_offerings" "host" {
  for_each      = toset(local.host_archs)
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [local.host_types[each.key]]
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
  # The AZs that offer a host type of an architecture with an AMI, and their subnets: hosts launch there only.
  host_azs     = sort(distinct(flatten([for o in data.aws_ec2_instance_type_offerings.host : o.locations])))
  host_subnets = [for az in local.host_azs : aws_subnet.public[az].id if contains(local.azs, az)]
  # Whether the coordinator launches hosts here: enabled and an AMI exists.
  hosts_on = var.hosts_enabled && length(local.host_archs) > 0
  # The subnet a tile launches in when the launch names none. The coordinator names each fleet subnet in turn
  # (FLEET_EC2_SUBNETS, fcvm-fleet.tf); an image older than that launches from the template alone, and an interface
  # without a subnet lands in the default VPC, where the fleet's security group does not exist and the task role may
  # not launch (an EC2 tile's RunInstances was refused on the default subnet, 2026-10-24). The first AZ, in name order.
  tile_az = local.azs[0]
  # The tile's root volume, gp3 at 1000 MiB/s and 4000 IOPS instead of the default 125 MiB/s and 3000. A first boot reads
  # about 500 MiB of it (every block of a volume made from an AMI is fetched from S3 on first read). The size stays the
  # AMI's 32 GiB.
  tile_root_volume = { type = "gp3", throughput = 1000, iops = 4000 }
  # The AMI carries the agent as a systemd unit, installed disabled and conditional on /etc/fcvm-fleet/agent.env (fcvm
  # fleet/ami/host/fcvm-fleet-agent.service; its arguments are fcvm fleet/PROTOCOL.md, "Agent", "Running it"). Boot writes
  # that file and starts the unit without waiting for it: the unit itself waits for the GPUs on vfio-pci and the store.
  user_data = base64encode(<<-EOT
    #!/bin/bash
    set -eu
    ${var.ip_options_bootstrap}
    echo "fcvm-fleet host booted $(date -u +%FT%TZ)" > /var/log/fcvm-fleet-boot.log
    %{if var.host_agent_args != ""~}
    install -d -m 0755 /etc/fcvm-fleet
    printf 'FLEET_AGENT_ARGS=%s\n' '${var.host_agent_args}' > /etc/fcvm-fleet/agent.env
    systemctl enable fcvm-fleet-agent.service
    systemctl start --no-block fcvm-fleet-agent.service
    %{endif~}
  EOT
  )
  tile_user_data = base64encode(<<-EOT
    #!/bin/bash
    set -eu
    ${var.ip_options_bootstrap}
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

# CVE-2026-98432: guarded AMIs must exist before public IPv4 can be restored.
# User data alone is too late for the first listener or a stopped-pool resume.
resource "aws_security_group" "fleet" {
  depends_on  = [data.aws_ami.guarded]
  name        = "fcvm-fleet"
  description = "fcvm fleet hosts and tiles: TCP 443 in from anywhere, nothing else in"
  vpc_id      = aws_vpc.this.id

  ingress {
    description      = "HTTPS"
    from_port        = 443
    to_port          = 443
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  ingress {
    description = "GPU slot renderer TLS over IPv4"
    from_port   = var.slot_tls_ports.from
    to_port     = var.slot_tls_ports.to
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
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

  # The root volume is filled from the AMI's snapshot at 300 MiB/s (provisioned rate for volume
  # initialization, the most EBS offers) instead of block by block on first read. A host's first boot
  # reads its root volume throughout: on g4dn.metal from one AMI (ejc3/fcvm#1257, one boot each) the
  # store unit started 21.8 s after kernel start lazily loaded (7.6 ms per read) and 17.2 s at 300 MiB/s
  # (3.7 ms per read); the same first boot on a volume already read took 11.1 s. Fast Snapshot Restore
  # would reach that, at 0.75 USD per AZ-hour for each AZ a host can launch in. This is billed once per
  # launch per GiB of snapshot data: 0.0036 USD in the 201 to 300 MiB/s tier in us-east-1, about 0.12 USD
  # for the 32 GiB host AMI. The device is the Ubuntu AMI's root device.
  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_initialization_rate = 300
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
    ipv6_prefix_count           = 1
    delete_on_termination       = true
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "fcvm-fleet-host", Purpose = "fcvm-fleet-host" }
  }

  tags = { Name = "fcvm-fleet-host-${each.key}" }
}

# Preserve the existing topic and confirmed email subscription at their original Terraform addresses. The coordinator's
# accepted-launch alarm replaces the obsolete Auto Scaling metric; a narrowly scoped Lambda publishes here through SNS.
resource "aws_sns_topic" "alerts" {
  count = length(local.host_archs) > 0 ? 1 : 0
  name  = "fcvm-fleet-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = length(local.host_archs) > 0 ? 1 : 0
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# ------------------------------------------------------------------------------------------------ EC2 tiles
resource "aws_launch_template" "tile" {
  for_each      = toset(local.tile_archs)
  name          = "fcvm-fleet-tile-${each.key}"
  description   = "fcvm fleet EC2 comparison tile, ${each.key} (${local.tile_types[each.key]}): launched by the coordinator"
  image_id      = var.tile_amis[each.key]
  instance_type = local.tile_types[each.key]
  user_data     = local.tile_user_data

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

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_type = local.tile_root_volume.type
      throughput  = local.tile_root_volume.throughput
      iops        = local.tile_root_volume.iops
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
    subnet_id                   = aws_subnet.public[local.tile_az].id
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
  user_data     = local.tile_user_data

  iam_instance_profile {
    name = var.tile_profile_name
  }

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_type = local.tile_root_volume.type
      throughput  = local.tile_root_volume.throughput
      iops        = local.tile_root_volume.iops
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
    subnet_id                   = aws_subnet.public[local.tile_az].id
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "fcvm-fleet-tile", Purpose = "fcvm-fleet-tile" }
  }

  tags = { Name = "fcvm-fleet-tile-on-demand-${each.key}" }
}

# The tile the coordinator keeps a pool of (fcvm fleet coord --ec2-pool <n> --ec2-pool-template
# fcvm-fleet-tile-pool-<arch>, ejc3/fcvm#1273): persistent spot that stops on interruption, so the coordinator can stop a
# tile when the fleet sleeps and start it at the next wake (15.5 s to a first frame, against 81 to 84 s for a launch,
# g4dn.xlarge, 2026-10-25). The spot request is tagged too, so the coordinator's CancelSpotInstanceRequests (it cancels a
# persistent request before terminating its instance, or the request launches a replacement) is limited to tiles'.
# Everything else is the spot template's.
resource "aws_launch_template" "tile_pool" {
  for_each      = toset(local.tile_archs)
  name          = "fcvm-fleet-tile-pool-${each.key}"
  description   = "fcvm fleet EC2 comparison tile, ${each.key} (${local.tile_types[each.key]}): persistent spot, stopped between wakes"
  image_id      = var.tile_amis[each.key]
  instance_type = local.tile_types[each.key]
  user_data     = local.tile_user_data

  iam_instance_profile {
    name = var.tile_profile_name
  }

  instance_market_options {
    market_type = "spot"
    spot_options {
      spot_instance_type             = "persistent"
      instance_interruption_behavior = "stop"
    }
  }

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_type = local.tile_root_volume.type
      throughput  = local.tile_root_volume.throughput
      iops        = local.tile_root_volume.iops
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
    subnet_id                   = aws_subnet.public[local.tile_az].id
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "fcvm-fleet-tile", Purpose = "fcvm-fleet-tile" }
  }

  tag_specifications {
    resource_type = "spot-instances-request"
    tags          = { Name = "fcvm-fleet-tile", Purpose = "fcvm-fleet-tile" }
  }

  tags = { Name = "fcvm-fleet-tile-pool-${each.key}" }
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

# Both markets and the pool: the coordinator's RunInstances is limited to these templates.
output "tile_template_arns" {
  value = concat([for t in aws_launch_template.tile : t.arn], [for t in aws_launch_template.tile_on_demand : t.arn], [for t in aws_launch_template.tile_pool : t.arn])
}

# The coordinator's host launches (fcvm fleet coord --host-template <region>:<name>, --host-subnet <region>:<subnet>): the
# templates only while the gate is on, the subnets of the AZs that offer a host type.
output "host_template_names" {
  value = local.hosts_on ? [for a in local.host_archs : aws_launch_template.host[a].name] : []
}

output "host_subnet_ids" {
  value = local.host_subnets
}

# The coordinator's RunInstances (through CreateFleet) is limited to these templates and the host AMIs.
output "host_template_arns" {
  value = local.hosts_on ? [for t in aws_launch_template.host : t.arn] : []
}

output "alert_topic_arns" {
  description = "Existing fleet alert destinations, with their confirmed subscriptions retained"
  value       = aws_sns_topic.alerts[*].arn
}

output "host_types" {
  value = [for a in local.host_archs : local.host_types[a]]
}

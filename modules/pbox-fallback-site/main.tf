# modules/pbox-fallback-site
#
# One region's FALLBACK site for the parallel boxes (pbox-fallback.tf): where `pbox up N --fallback` launches when the
# home AZ (ohio-pbox.tf) has no capacity. Everything a launch needs that is regional: a VPC with one public subnet per
# listed AZ, a security group, a key pair, the AMI, one launch template per box, a flow log, and (where no other
# watchdog already runs in this region) the idle watchdog.
#
# A fallback box starts with an EMPTY work volume. Its /mnt/work is a fresh gp3 volume that the launch template creates
# with the instance and deletes with it: the persistent work volumes are AZ-locked in the home AZ, and no copy of them
# is kept here. So nothing on a fallback box survives its termination, a spot reclaim or the idle watchdog.
#
# The one launch template carries no subnet and no Name: the caller names the subnet (and so the AZ) and tags the box
# with its Name at launch. The IAM policy (pbox-fallback.tf) allows only these subnets and only the box names, so a
# typo is a denied launch, never a box in the wrong place or one the watchdog cannot see.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

variable "cidr_block" {
  description = "The site VPC's /16; one /24 per AZ is carved from it in list order"
  type        = string
}

variable "availability_zones" {
  description = "The AZs a fallback box may launch in (the home AZ is left out: it has its own site)"
  type        = list(string)
}

variable "ami_name" {
  description = "Exact name of the Canonical Ubuntu arm64 image, so every region boots the same build"
  type        = string
}

variable "template_name" {
  description = "The site's one launch template; every box launches from it and is named by the caller"
  type        = string
}

variable "work_size" {
  description = "GiB of the fresh work volume"
  type        = number
}

variable "user_data_b64" {
  type = string
}

variable "instance_profile_name" {
  type = string
}

variable "spot" {
  description = "Spot (true) or on-demand, as var.parallel_box_spot"
  type        = bool
}

variable "key_name" {
  type = string
}

variable "public_key" {
  type = string
}

variable "flow_log_destination" {
  description = "The audit bucket path every VPC's flow log goes to (security-monitoring.tf)"
  type        = string
}

variable "watchdog" {
  description = "Run the idle watchdog here. false where another watchdog already watches the parallel boxes in this region."
  type = object({
    enabled     = bool
    role_arn    = string
    zip_path    = string
    zip_hash    = string
    environment = map(string)
  })
}

variable "name" {
  description = "Suffix for this site's resource names, e.g. us-east-1"
  type        = string
}

data "aws_ami" "ubuntu" {
  owners = ["099720109477"] # Canonical
  filter {
    name   = "name"
    values = [var.ami_name]
  }
  filter {
    name   = "architecture"
    values = ["arm64"]
  }
}

# ============================================================ network
resource "aws_vpc" "this" {
  cidr_block           = var.cidr_block
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "parallel-box-fallback-${var.name}" }
}

resource "aws_internet_gateway" "this" {
  vpc_id = aws_vpc.this.id
  tags   = { Name = "parallel-box-fallback-${var.name}" }
}

resource "aws_route_table" "this" {
  vpc_id = aws_vpc.this.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this.id
  }

  tags = { Name = "parallel-box-fallback-${var.name}" }
}

# IPv4 only: an instance with no IPv6 address never tries the AAAA answers DNS returns first, so the SYN-SENT stall
# parallel-box.tf describes for a v6 subnet without a v6 route cannot happen here.
resource "aws_subnet" "this" {
  for_each                = { for i, az in var.availability_zones : az => i }
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.cidr_block, 8, each.value + 1)
  availability_zone       = each.key
  map_public_ip_on_launch = true

  tags = { Name = "parallel-box-fallback-${each.key}" }
}

resource "aws_route_table_association" "this" {
  for_each       = aws_subnet.this
  subnet_id      = each.value.id
  route_table_id = aws_route_table.this.id
}

resource "aws_security_group" "this" {
  name_prefix = "parallel-box-fallback-"
  description = "On-demand parallel compute box (fallback site): SSH only"
  vpc_id      = aws_vpc.this.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "parallel-box-fallback" }

  lifecycle {
    create_before_destroy = true
  }
}

# The security design logs every VPC's traffic to the audit bucket (security-monitoring.tf).
resource "aws_flow_log" "this" {
  vpc_id                   = aws_vpc.this.id
  traffic_type             = "ALL"
  log_destination_type     = "s3"
  log_destination          = var.flow_log_destination
  max_aggregation_interval = 600
  tags                     = { Name = "security-pbox-fallback-${var.name}-flow-log", Managed = "terraform" }
}

resource "aws_key_pair" "this" {
  key_name   = var.key_name
  public_key = var.public_key
  tags       = { Name = var.key_name }
}

# ============================================================ launch template
# The home templates' configuration (parallel-box-launch.tf) with three differences: no network interface block, so the
# caller's --subnet-id picks the AZ; a second EBS volume, the fresh work disk, created and deleted with the box; and no
# Name tag, which the caller adds (--tag-specifications) for the box it launches. One template serves every box,
# which keeps the IAM policy that pins it short.
resource "aws_launch_template" "box" {
  name                   = var.template_name
  image_id               = data.aws_ami.ubuntu.id
  key_name               = aws_key_pair.this.key_name
  vpc_security_group_ids = [aws_security_group.this.id]
  update_default_version = true

  iam_instance_profile {
    name = var.instance_profile_name
  }

  dynamic "instance_market_options" {
    for_each = var.spot ? [1] : []
    content {
      market_type = "spot"
      spot_options {
        spot_instance_type             = "one-time"
        instance_interruption_behavior = "terminate"
      }
    }
  }

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_size           = 30
      volume_type           = "gp3"
      delete_on_termination = true
    }
  }

  # The fresh work volume. It dies with the box: a fallback box keeps nothing.
  block_device_mappings {
    device_name = "/dev/sdf"
    ebs {
      volume_size           = var.work_size
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  # The caller adds Name to all three resource types at launch (the IAM policy requires it on each, as at home, and the
  # watchdog finds its targets by it). No DevEBS tag on the volumes, for the reason parallel-box-launch.tf gives.
  tag_specifications {
    resource_type = "instance"
    tags = {
      Purpose  = "on-demand embarrassingly-parallel compute"
      DevEBS   = "true"
      Fallback = "true"
    }
  }

  tag_specifications {
    resource_type = "volume"
    tags = {
      Role = "fallback"
    }
  }

  tags = { Name = var.template_name }

  user_data = var.user_data_b64
}

# ============================================================ idle watchdog
# The same function as parallel-box-watchdog.tf's, for regions where none runs yet: CloudWatch metrics and
# TerminateInstances are regional. Its role is the shared one (IAM is global; it terminates by tag in any region).
resource "aws_lambda_function" "watchdog" {
  count            = var.watchdog.enabled ? 1 : 0
  function_name    = "parallel-box-watchdog"
  role             = var.watchdog.role_arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 60
  filename         = var.watchdog.zip_path
  source_code_hash = var.watchdog.zip_hash

  environment {
    variables = var.watchdog.environment
  }

  tags = { Name = "parallel-box-watchdog" }
}

resource "aws_cloudwatch_event_rule" "watchdog" {
  count               = var.watchdog.enabled ? 1 : 0
  name                = "parallel-box-watchdog"
  description         = "Terminate the parallel box when idle"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "watchdog" {
  count     = var.watchdog.enabled ? 1 : 0
  rule      = aws_cloudwatch_event_rule.watchdog[0].name
  target_id = "parallel-box-watchdog"
  arn       = aws_lambda_function.watchdog[0].arn
}

resource "aws_lambda_permission" "watchdog" {
  count         = var.watchdog.enabled ? 1 : 0
  statement_id  = "AllowExecutionFromCloudWatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.watchdog[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.watchdog[0].arn
}

# ============================================================ outputs
output "subnets" {
  description = "AZ => subnet id, for the caller's --subnet-id"
  value       = { for az, s in aws_subnet.this : az => s.id }
}

output "vpc_arn" {
  value = aws_vpc.this.arn
}

output "vpc_id" {
  description = "The site's VPC, whose default security group default-security-groups.tf empties"
  value       = aws_vpc.this.id
}

output "ami_id" {
  value = data.aws_ami.ubuntu.id
}

output "security_group_id" {
  value = aws_security_group.this.id
}

output "security_group_arn" {
  description = "The box's security group, named by security-hub-automation.tf's open-SSH rule"
  value       = aws_security_group.this.arn
}

output "key_name" {
  value = aws_key_pair.this.key_name
}

output "template_arn" {
  value = aws_launch_template.box.arn
}

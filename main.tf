terraform {
  required_version = "= 1.10.3"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.64"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
    cloudflare = {
      # Exact fork pin adds Worker-native Access destinations and typed Workers Builds
      # resources. Return to the upstream namespace only after an upstream release
      # carries the same schemas and regression coverage.
      source  = "ejc3/cloudflare"
      version = "= 5.24.0"
    }
    github = {
      source  = "integrations/github"
      version = "~> 6.0"
    }
  }

  backend "s3" {
    bucket         = "ejc3-terraform-state"
    key            = "aws-infrastructure/terraform.tfstate"
    region         = "us-west-1"
    dynamodb_table = "ejc3-terraform-locks"
    encrypt        = true
  }
}

provider "aws" {
  region = var.aws_region
}

# Workers Builds creates a deployment token whose value Cloudflare returns only once.
# Version the backend before that token is created so an accidental state overwrite has
# a recoverable predecessor. This resource safely enables versioning on the existing
# backend bucket; it does not try to create or own the bucket itself.
resource "aws_s3_bucket_versioning" "terraform_state" {
  bucket = "ejc3-terraform-state"

  versioning_configuration {
    status = "Enabled"
  }

  lifecycle {
    prevent_destroy = true
  }
}

# VPC Configuration - Use existing or create new
data "aws_vpcs" "existing" {
  filter {
    name   = "tag:Name"
    values = ["${var.project_name}-vpc"]
  }
}

resource "aws_vpc" "main" {
  count                = length(data.aws_vpcs.existing.ids) > 0 ? 0 : 1
  cidr_block           = "10.0.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${var.project_name}-vpc"
  }
}

data "aws_vpc" "selected" {
  id = length(data.aws_vpcs.existing.ids) > 0 ? tolist(data.aws_vpcs.existing.ids)[0] : aws_vpc.main[0].id
}

locals {
  vpc_id = data.aws_vpc.selected.id
}

# IPv6 CIDR block for VPC (works with existing or new VPC)
resource "aws_vpc_ipv6_cidr_block_association" "main" {
  vpc_id                           = local.vpc_id
  assign_generated_ipv6_cidr_block = true
}

# Internet Gateway
resource "aws_internet_gateway" "main" {
  vpc_id = local.vpc_id

  tags = {
    Name = "${var.project_name}-igw"
  }
}

# Route Table for public subnet
resource "aws_route_table" "public" {
  vpc_id = local.vpc_id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  route {
    ipv6_cidr_block = "::/0"
    gateway_id      = aws_internet_gateway.main.id
  }

  # Private path to the shared us-west-2 I/O box. This route must stay inline:
  # mixing aws_route resources with inline routes on the same table makes the
  # AWS provider fight itself and causes recurring drift.
  route {
    cidr_block                = data.aws_vpc.west2_default.cidr_block
    vpc_peering_connection_id = aws_vpc_peering_connection.io_box.id
  }

  tags = {
    Name = "${var.project_name}-public-rt"
  }
}

# Associate route table with subnet
resource "aws_route_table_association" "subnet_a" {
  subnet_id      = aws_subnet.subnet_a.id
  route_table_id = aws_route_table.public.id
}

# Subnet for instances
resource "aws_subnet" "subnet_a" {
  vpc_id            = local.vpc_id
  cidr_block        = "10.0.1.0/24"
  availability_zone = data.aws_availability_zones.available.names[0]

  # IPv6 support
  ipv6_cidr_block                 = cidrsubnet(aws_vpc_ipv6_cidr_block_association.main.ipv6_cidr_block, 8, 1)
  assign_ipv6_address_on_creation = true

  tags = {
    Name = "${var.project_name}-subnet-a"
  }
}

# us-west-1c. Dropped from terraform in 02268cf but never deleted from AWS, and still associated
# with the public route table, so it is imported rather than recreated. It gives the metal boxes
# somewhere to go when us-west-1a has no spot capacity.
import {
  to = aws_subnet.subnet_b
  id = "subnet-04b574af100ff5387"
}

resource "aws_subnet" "subnet_b" {
  vpc_id            = local.vpc_id
  cidr_block        = "10.0.2.0/24"
  availability_zone = "us-west-1c"

  # IPv6 support; fcvm-metal-arm needs it for its routed /64 prefix
  ipv6_cidr_block                 = cidrsubnet(aws_vpc_ipv6_cidr_block_association.main.ipv6_cidr_block, 8, 2)
  assign_ipv6_address_on_creation = true

  tags = {
    Name = "${var.project_name}-subnet-b"
  }
}

import {
  to = aws_route_table_association.subnet_b
  id = "subnet-04b574af100ff5387/rtb-003c0a0a5c556fa1e"
}

resource "aws_route_table_association" "subnet_b" {
  subnet_id      = aws_subnet.subnet_b.id
  route_table_id = aws_route_table.public.id
}

# One subnet per AZ, so placing a box in an AZ is a single variable
# (e.g. firecracker_availability_zone).
locals {
  subnet_ids_by_az = {
    (aws_subnet.subnet_a.availability_zone) = aws_subnet.subnet_a.id
    (aws_subnet.subnet_b.availability_zone) = aws_subnet.subnet_b.id
  }
}

# Data source for availability zones
data "aws_availability_zones" "available" {
  state = "available"
}

# Data source for current AWS account
data "aws_caller_identity" "current" {}

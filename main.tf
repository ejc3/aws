terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
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

# Data source for availability zones
data "aws_availability_zones" "available" {
  state = "available"
}

# Data source for current AWS account
data "aws_caller_identity" "current" {}

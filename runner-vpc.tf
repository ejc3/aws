# Isolated VPC for GitHub Runners
# No connectivity to main VPC - internet only

resource "aws_vpc" "runner" {
  count                            = var.enable_github_runner ? 1 : 0
  cidr_block                       = "10.1.0.0/16"
  enable_dns_hostnames             = true
  enable_dns_support               = true
  assign_generated_ipv6_cidr_block = true

  tags = {
    Name = "github-runner-vpc"
  }
}

resource "aws_internet_gateway" "runner" {
  count  = var.enable_github_runner ? 1 : 0
  vpc_id = aws_vpc.runner[0].id

  tags = {
    Name = "github-runner-igw"
  }
}

resource "aws_subnet" "runner" {
  count                   = var.enable_github_runner ? 1 : 0
  vpc_id                  = aws_vpc.runner[0].id
  cidr_block              = "10.1.1.0/24"
  availability_zone       = "us-west-1a"
  map_public_ip_on_launch = true

  # IPv6 support
  ipv6_cidr_block                 = cidrsubnet(aws_vpc.runner[0].ipv6_cidr_block, 8, 1)
  assign_ipv6_address_on_creation = true

  tags = {
    Name = "github-runner-subnet"
  }
}

resource "aws_route_table" "runner" {
  count  = var.enable_github_runner ? 1 : 0
  vpc_id = aws_vpc.runner[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.runner[0].id
  }

  route {
    ipv6_cidr_block = "::/0"
    gateway_id      = aws_internet_gateway.runner[0].id
  }

  tags = {
    Name = "github-runner-rt"
  }
}

resource "aws_route_table_association" "runner" {
  count          = var.enable_github_runner ? 1 : 0
  subnet_id      = aws_subnet.runner[0].id
  route_table_id = aws_route_table.runner[0].id
}

# Security group - SSH for debugging, outbound for internet
resource "aws_security_group" "runner" {
  count       = var.enable_github_runner ? 1 : 0
  name        = "github-runner-sg"
  description = "GitHub runner - SSH + outbound internet"
  vpc_id      = aws_vpc.runner[0].id

  ingress {
    from_port        = 22
    to_port          = 22
    protocol         = "tcp"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
    description      = "SSH access for debugging"
  }

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
    description      = "Internet access"
  }

  tags = {
    Name = "github-runner-sg"
  }
}

# IAM role for runners - SSM Session Manager + PAT access
resource "aws_iam_role" "runner" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-runner-instance-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "runner_ssm" {
  count      = var.enable_github_runner ? 1 : 0
  role       = aws_iam_role.runner[0].name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "runner" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-runner-policy"
  role  = aws_iam_role.runner[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = aws_ssm_parameter.github_runner_pat[0].arn
      },
      {
        Sid      = "AssignIpv6"
        Effect   = "Allow"
        Action   = [
          "ec2:AssignIpv6Addresses",
          "ec2:DescribeNetworkInterfaces"
        ]
        Resource = "*"
      }
    ]
  })
}

resource "aws_iam_instance_profile" "runner" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-runner-profile"
  role  = aws_iam_role.runner[0].name
}

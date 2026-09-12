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

# Security group - SSH only from within the runner VPC (use SSM from outside), outbound for internet
resource "aws_security_group" "runner" {
  count       = var.enable_github_runner ? 1 : 0
  name        = "github-runner-sg"
  description = "GitHub runner - SSH + outbound internet"
  vpc_id      = aws_vpc.runner[0].id

  ingress {
    from_port = 22
    to_port   = 22
    protocol  = "tcp"
    cidr_blocks = [
      aws_vpc.runner[0].cidr_block,                 # intra-VPC runner-to-runner
      "${aws_eip.jumpbox[0].public_ip}/32",         # jumpbox (management host)
      "${aws_eip.firecracker_dev[0].public_ip}/32", # fcvm-metal-arm dev server
      "${aws_eip.x86_dev[0].public_ip}/32",         # fcvm-metal-x86 dev server
    ]
    ipv6_cidr_blocks = [aws_vpc.runner[0].ipv6_cidr_block]
    description      = "SSH from the runner VPC + operator EIPs (jumpbox, dev servers); SSM elsewhere"
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

# Job-host credentials: SSM agent connectivity and an own-instance bootstrap
# credential, never the reusable runner PAT. Publish only after live job/drain gates.
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
  policy_arn = aws_iam_policy.ssm_managed_instance.arn
  lifecycle {
    create_before_destroy = true
  }
  # Establish the explicit payload denies before the old broad Core attachment
  # is replaced; the overlapping attachments must not reopen PAT access.
  depends_on = [aws_iam_role_policy.runner]
}

resource "aws_iam_role_policy" "runner" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-runner-policy"
  role  = aws_iam_role.runner[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid         = "DenyReusableParameterPayloads"
        Effect      = "Deny"
        Action      = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParameterHistory", "ssm:GetParametersByPath"]
        NotResource = "arn:aws:ssm:us-west-1:${data.aws_caller_identity.current.account_id}:parameter/github-runner/bootstrap/*"
      },
      {
        # An ancestor-path request can expose a denied child parameter. Bootstrap
        # needs only a single current value, so deny all batch/history/path reads,
        # including /, /github-runner and the bootstrap parent path, everywhere.
        Sid      = "DenyBulkAndHistoricalParameterPayloads"
        Effect   = "Deny"
        Action   = ["ssm:GetParameters", "ssm:GetParameterHistory", "ssm:GetParametersByPath"]
        Resource = "*"
      },
      {
        Sid      = "DescribeIpv6Interfaces"
        Effect   = "Allow"
        Action   = "ec2:DescribeNetworkInterfaces"
        Resource = "*"
      },
      {
        Sid      = "AssignRunnerIpv6"
        Effect   = "Allow"
        Action   = "ec2:AssignIpv6Addresses"
        Resource = "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:network-interface/*"
        Condition = {
          StringEquals = { "aws:ResourceTag/Role" = "github-runner" }
          ArnEquals    = { "ec2:Subnet" = aws_subnet.runner[0].arn }
        }
      }
    ]
  })
  # Own-bootstrap read/delete and the null-guarded own-ARN DynamoDB claim remain
  # in the unchanged additive runner_bootstrap policy. Do not depend on user data
  # here: that document already depends on this grant; runtime gates are separate.
}

resource "aws_iam_instance_profile" "runner" {
  count = var.enable_github_runner ? 1 : 0
  name  = "github-runner-profile"
  role  = aws_iam_role.runner[0].name
}

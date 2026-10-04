# ohio.tf -- the Ohio (us-east-2) region, PREPARED and idle.
#
# WHY: us-west-1 is the most expensive US region (on-demand and EBS about 17-20% over the others) and its spot prices for
# the metal and big instances we actually run are 15-65% above Ohio's (the 2026-10-03 cost review: c7gd.metal $1.16/h in
# us-west-1 against $0.38 in us-east-2; m7gd.metal $1.00 against $0.37; c5d.metal $1.58 against $0.75). Spot placement
# scores for the same types were no worse in Ohio, and Ohio has three AZs that score.
#
# WHAT THIS FILE IS: the foundation for the GitHub runners and the metal dev box, built NEXT TO what runs today and
# switched on NOTHING. No launcher, no box and no Elastic IP points here yet (an unattached Elastic IP is a charge).
# The cutover is a separate, scheduled act by the owner; everything it needs that does not interrupt anything lives
# here, ahead of time: the network, the security groups, flow logs, and the runner AMIs (copied and kept current).
#
# The runner side is below. The dev-box side is ohio-dev.tf.

provider "aws" {
  alias  = "ohio"
  region = "us-east-2"
}

locals {
  ohio_azs = ["us-east-2a", "us-east-2b", "us-east-2c"]
}

# ============================================================ runner network
# Mirrors runner-vpc.tf: an isolated VPC, internet only, IPv6 on (the runner user data refuses to register without it).
# 10.11.0.0/16 so it never overlaps the main VPC (10.0), the us-west-1 runner VPC (10.1) or the dev boxes' Ohio VPC.
resource "aws_vpc" "ohio_runner" {
  provider                         = aws.ohio
  count                            = var.enable_github_runner ? 1 : 0
  cidr_block                       = "10.11.0.0/16"
  enable_dns_hostnames             = true
  enable_dns_support               = true
  assign_generated_ipv6_cidr_block = true

  tags = { Name = "github-runner-vpc-ohio" }
}

resource "aws_internet_gateway" "ohio_runner" {
  provider = aws.ohio
  count    = var.enable_github_runner ? 1 : 0
  vpc_id   = aws_vpc.ohio_runner[0].id

  tags = { Name = "github-runner-igw-ohio" }
}

resource "aws_route_table" "ohio_runner" {
  provider = aws.ohio
  count    = var.enable_github_runner ? 1 : 0
  vpc_id   = aws_vpc.ohio_runner[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.ohio_runner[0].id
  }

  route {
    ipv6_cidr_block = "::/0"
    gateway_id      = aws_internet_gateway.ohio_runner[0].id
  }

  tags = { Name = "github-runner-rt-ohio" }
}

# One subnet per AZ. A spot pool is one instance type in one AZ, so more AZs is more chances (us-west-1 has two,
# and 78% of ARM launches in 1a once got nothing). Ohio's three all score for the metal types.
resource "aws_subnet" "ohio_runner" {
  provider                        = aws.ohio
  count                           = var.enable_github_runner ? length(local.ohio_azs) : 0
  vpc_id                          = aws_vpc.ohio_runner[0].id
  cidr_block                      = "10.11.${count.index + 1}.0/24"
  availability_zone               = local.ohio_azs[count.index]
  map_public_ip_on_launch         = true
  ipv6_cidr_block                 = cidrsubnet(aws_vpc.ohio_runner[0].ipv6_cidr_block, 8, count.index + 1)
  assign_ipv6_address_on_creation = true

  # Not "github-runner-subnet": fcvm's build-ami.sh refuses to run unless that exact Name matches ONE subnet, and the
  # AMI builder stays in us-west-1a.
  tags = { Name = "github-runner-subnet-ohio-${substr(local.ohio_azs[count.index], -1, 1)}" }
}

resource "aws_route_table_association" "ohio_runner" {
  provider       = aws.ohio
  count          = var.enable_github_runner ? length(local.ohio_azs) : 0
  subnet_id      = aws_subnet.ohio_runner[count.index].id
  route_table_id = aws_route_table.ohio_runner[0].id
}

# Same rules as aws_security_group.runner. The operator addresses are the CURRENT ones; the metal dev box gets a new
# Ohio address at its cutover and its rule is updated then.
resource "aws_security_group" "ohio_runner" {
  provider    = aws.ohio
  count       = var.enable_github_runner ? 1 : 0
  name        = "github-runner-sg"
  description = "GitHub runner - SSH + outbound internet"
  vpc_id      = aws_vpc.ohio_runner[0].id

  ingress {
    from_port = 22
    to_port   = 22
    protocol  = "tcp"
    self      = true
    cidr_blocks = [
      "${aws_eip.jumpbox[0].public_ip}/32",
      "${aws_eip.firecracker_dev[0].public_ip}/32",
      "${aws_eip.x86_dev[0].public_ip}/32",
    ]
    description = "SSH from fcvm runners (self) + operator EIPs (jumpbox, dev servers); SSM elsewhere"
  }

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
    description      = "Internet access"
  }

  tags = { Name = "github-runner-sg" }
}

# The app runners run other repos' code: no inbound at all, as in runner-app.tf.
resource "aws_security_group" "ohio_runner_app" {
  provider    = aws.ohio
  count       = var.enable_github_runner ? 1 : 0
  name        = "github-app-runner-sg"
  description = "Ephemeral app-repo runners: no inbound, outbound internet"
  vpc_id      = aws_vpc.ohio_runner[0].id

  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
    description      = "Internet access"
  }

  tags = { Name = "github-app-runner-sg" }
}

# The security design logs every VPC's traffic to the audit bucket (security-monitoring.tf); Ohio's runner VPC too.
resource "aws_flow_log" "security_ohio_runner" {
  provider                 = aws.ohio
  count                    = var.enable_github_runner ? 1 : 0
  vpc_id                   = aws_vpc.ohio_runner[0].id
  traffic_type             = "ALL"
  log_destination_type     = "s3"
  log_destination          = "${aws_s3_bucket.security_audit.arn}/vpc-flow"
  max_aggregation_interval = 600
  tags                     = { Name = "security-ohio-runner-flow-log", Managed = "terraform" }
  depends_on               = [aws_s3_bucket_policy.security_audit]
}

# The runner launcher says KeyName='fcvm-ec2' on every launch, and a key pair is regional. The SAME public key as the one in
# us-west-1 and the parallel box's (fingerprint 5V+NL76VshPSFc9AHLAwWeVQ3qVUQv7M5CSzncf6k2Y= in all three).
resource "aws_key_pair" "ohio_runner" {
  provider   = aws.ohio
  count      = var.enable_github_runner ? 1 : 0
  key_name   = "fcvm-ec2"
  public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINwtXjjTCVgT9OR3qrnz3zDkV2GveuCBlWFXSOBG2joe fcvm-ec2"
  tags       = { Name = "fcvm-ec2" }
}

# ============================================================ runner AMIs
# fcvm's pipeline publishes the runner AMIs in us-west-1 only, and the launcher picks its AMI by tag in its own region.
# This keeps the newest two of each architecture copied to Ohio, tags included, so the launcher finds the current image
# the day it is pointed there. It only copies: it never deregisters and never touches the source.
data "archive_file" "ami_replicator" {
  type        = "zip"
  output_path = "${path.module}/.terraform/ami-replicator.zip"
  source {
    filename = "index.py"
    content  = file("${path.module}/scripts/ami-replicator.py")
  }
}

resource "aws_iam_role" "ami_replicator" {
  count = var.enable_github_runner ? 1 : 0
  name  = "ami-replicator"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ami_replicator_basic" {
  count      = var.enable_github_runner ? 1 : 0
  role       = aws_iam_role.ami_replicator[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "ami_replicator" {
  count = var.enable_github_runner ? 1 : 0
  name  = "ami-replicator"
  role  = aws_iam_role.ami_replicator[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Read-only, and Describe* cannot be resource scoped.
        Sid      = "SeeTheImages"
        Effect   = "Allow"
        Action   = "ec2:DescribeImages"
        Resource = "*"
      },
      {
        # The only writes: make an image in OHIO and tag it. Never in any other region, never a deregister, never
        # a modify of the source.
        Sid      = "CopyIntoOhioOnly"
        Effect   = "Allow"
        Action   = ["ec2:CopyImage", "ec2:CreateTags"]
        Resource = ["arn:aws:ec2:us-east-2::image/*", "arn:aws:ec2:us-east-2::snapshot/*"]
        Condition = {
          StringEquals = { "aws:RequestedRegion" = "us-east-2" }
        }
      },
      {
        # CopyImage is also authorized against the SOURCE image and snapshot (AWS: copy-ami-permissions); without these
        # every copy is denied. Read-side only, and still only for a request made to Ohio.
        Sid      = "NameTheSourceImagesTheCopyReads"
        Effect   = "Allow"
        Action   = "ec2:CopyImage"
        Resource = ["arn:aws:ec2:${var.aws_region}::image/*", "arn:aws:ec2:${var.aws_region}::snapshot/*"]
        Condition = {
          StringEquals = { "aws:RequestedRegion" = "us-east-2" }
        }
      },
      {
        Sid      = "SayWhenACopyFails"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      },
    ]
  })
}

resource "aws_lambda_function" "ami_replicator" {
  count            = var.enable_github_runner ? 1 : 0
  function_name    = "ami-replicator"
  role             = aws_iam_role.ami_replicator[0].arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 120
  filename         = data.archive_file.ami_replicator.output_path
  source_code_hash = data.archive_file.ami_replicator.output_base64sha256

  environment {
    variables = {
      SOURCE_REGION = var.aws_region
      TARGET_REGION = "us-east-2"
      KEEP_PER_ARCH = "2"
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
    }
  }

  tags = { Name = "ami-replicator" }
}

resource "aws_cloudwatch_event_rule" "ami_replicator" {
  count               = var.enable_github_runner ? 1 : 0
  name                = "ami-replicator-hourly"
  description         = "Copy new runner AMIs to Ohio"
  schedule_expression = "rate(1 hour)"
}

resource "aws_cloudwatch_event_target" "ami_replicator" {
  count = var.enable_github_runner ? 1 : 0
  rule  = aws_cloudwatch_event_rule.ami_replicator[0].name
  arn   = aws_lambda_function.ami_replicator[0].arn
}

resource "aws_lambda_permission" "ami_replicator" {
  count         = var.enable_github_runner ? 1 : 0
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ami_replicator[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ami_replicator[0].arn
}

resource "aws_lambda_function_event_invoke_config" "ami_replicator" {
  count                  = var.enable_github_runner ? 1 : 0
  function_name          = aws_lambda_function.ami_replicator[0].function_name
  maximum_retry_attempts = 0
}

resource "aws_cloudwatch_metric_alarm" "ami_replicator_errors" {
  count               = var.enable_github_runner ? 1 : 0
  alarm_name          = "ami-replicator-errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 3600
  statistic           = "Sum"
  threshold           = 0
  alarm_description   = "A runner AMI could not be copied to Ohio: the Ohio launcher would find a stale image at cutover."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  treat_missing_data  = "notBreaching"
  dimensions          = { FunctionName = aws_lambda_function.ami_replicator[0].function_name }
}

# Spot vCPU quota. Ohio's default (640) was exactly full with 8 metal runners on 2026-10-04, and the launcher
# (which has no fallback to another region) logged 18 MaxSpotInstanceCountExceeded errors an hour. us-west-1's is 1148,
# which is what the runners alone needed there; the parallel boxes (2 x 192 vCPU, moving here) add 384.
# L-34B43A08 = "All Standard (A, C, D, H, I, M, R, T, Z) Spot Instance Requests".
resource "aws_servicequotas_service_quota" "ohio_spot_standard" {
  provider     = aws.ohio
  service_code = "ec2"
  quota_code   = "L-34B43A08"
  value        = 1536
}

# boot-race.tf
#
# Resources for one measurement campaign in fcvm (bench/boot-race): the time from asking for compute to a small
# workload answering an HTTP request from outside it, for an fcvm snapshot restore and for the AWS ways of getting the
# same workload running. The lanes that need AWS resources:
#
#   EC2 launch           RunInstances from an AMI holding the workload (made by the driver with CreateImage)
#   EC2 start            StartInstances of a stopped instance of that AMI
#   EC2 hibernate        StartInstances of an instance stopped with Hibernate=true (c8g.medium supports hibernation)
#   ECS on Fargate       RunTask of a one-container task definition
#   ECS on EC2           RunTask on a container instance that is already running, image already pulled
#   Lambda, cold         a function URL whose handler ends its execution environment after every response
#   Lambda, warm         the same code without the exit, for reference
#
# Everything is named boot-race and lives in us-east-2, in the parallel-box VPC and subnet (ohio-pbox.tf), because the
# driver runs on a parallel box and reaches each workload over the VPC. Nothing is public: the workload group admits
# TCP 8080 (the workload) and 22 (to install the workload on the AMI builder) from the parallel-box group only, and
# the function URLs take IAM auth only. Every instance and task is started by the driver and terminated or stopped by
# it when its samples are taken, and boot-race-reaper expires whatever a failed or interrupted campaign leaves (instances
# after 3 hours, tasks after 1 hour, images and snapshots after a day). The two ECR interface endpoints bill while they
# exist (about $0.02 an hour together).
#
# The driver uses boot-race-driver, which the dev role assumes. Its grants are scoped to these resources by ARN or by
# the Project=boot-race tag, and RunInstances is pinned to the two launch templates below, in the same shape as
# parallel-box-control (parallel-box-launch.tf). ECS tasks are run and stopped only by boot-race-launcher, a
# Terraform-owned function whose code fixes the cluster, the task definitions and the Fargate network (the
# parallel-box subnet, the boot-race group, no public IP); Fargate pulls the image through ECR endpoints in the VPC.

locals {
  boot_race_region  = "us-east-2"
  boot_race_account = data.aws_caller_identity.current.account_id
  boot_race_tag     = { Project = "boot-race" }

  # The base image for the EC2 lanes: Amazon Linux 2023 arm64 (al2023-ami-2023.12.20261102.0-kernel-6.1-arm64), which
  # supports hibernation. The driver makes the workload AMI from an instance of it.
  boot_race_base_ami = "ami-0e59f38361eb8490c"

  boot_race_lambdas = {
    cold = "1" # the handler exits its runtime after each response
    warm = "0"
  }

  boot_race_ec2_arn = "arn:aws:ec2:${local.boot_race_region}:${local.boot_race_account}"
  boot_race_ecs_arn = "arn:aws:ecs:${local.boot_race_region}:${local.boot_race_account}"
}

# The ECS-optimized Amazon Linux 2023 arm64 image, for the ECS-on-EC2 container instance
# (al2023-ami-ecs-hvm-2023.0.20261031-kernel-6.1-arm64). Pinned, not read from AWS's "recommended" parameter: the
# launch template and the driver's RunInstances grant both name this one value, so an AWS release cannot move the
# grant to an image the template does not launch. A new image is a change to this line.
variable "boot_race_ecs_ami" {
  description = "ECS-optimized Amazon Linux 2023 arm64 image in us-east-2 for the boot-race container instance"
  type        = string
  default     = "ami-0e5deb4438b0e4cdc"
}

# ============================================================ network
resource "aws_security_group" "boot_race" {
  provider    = aws.ohio
  name_prefix = "boot-race-"
  description = "boot-race workloads: TCP 8080 and SSH from the parallel boxes only"
  vpc_id      = aws_vpc.ohio_pbox.id

  ingress {
    description     = "The workload, from the parallel boxes"
    from_port       = 8080
    to_port         = 8080
    protocol        = "tcp"
    security_groups = [aws_security_group.ohio_pbox.id]
  }

  ingress {
    description     = "SSH, from the parallel boxes (installing the workload on the AMI builder)"
    from_port       = 22
    to_port         = 22
    protocol        = "tcp"
    security_groups = [aws_security_group.ohio_pbox.id]
  }

  # Fargate tasks reach ECR through the endpoints below; the container instance reaches the ECS endpoint over the
  # internet through its public address (the launch template), which this group closes to inbound traffic.
  egress {
    from_port        = 0
    to_port          = 0
    protocol         = "-1"
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }

  tags = merge(local.boot_race_tag, { Name = "boot-race" })

  lifecycle {
    create_before_destroy = true
  }
}

# ============================================================ EC2 lanes
resource "aws_launch_template" "boot_race_ec2" {
  provider      = aws.ohio
  name          = "boot-race-ec2"
  description   = "boot-race EC2 lanes: c8g.medium, hibernation configured, encrypted root"
  image_id      = local.boot_race_base_ami
  instance_type = "c8g.medium"
  key_name      = aws_key_pair.ohio_pbox.key_name

  hibernation_options {
    configured = true
  }

  # Hibernation needs an encrypted root volume large enough for the instance's memory (2 GiB).
  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 8
      volume_type           = "gp3"
      encrypted             = true
      delete_on_termination = true
    }
  }

  network_interfaces {
    subnet_id                   = aws_subnet.ohio_pbox.id
    security_groups             = [aws_security_group.boot_race.id]
    associate_public_ip_address = true
  }

  metadata_options {
    http_tokens = "required"
  }

  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.boot_race_tag, { Name = "boot-race-ec2" })
  }
  tag_specifications {
    resource_type = "volume"
    tags          = merge(local.boot_race_tag, { Name = "boot-race-ec2" })
  }
  tag_specifications {
    resource_type = "network-interface"
    tags          = merge(local.boot_race_tag, { Name = "boot-race-ec2" })
  }

  tags = local.boot_race_tag
}

# ============================================================ ECS lanes
resource "aws_ecr_repository" "boot_race" {
  provider             = aws.ohio
  name                 = "boot-race"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
  tags                 = local.boot_race_tag
}

resource "aws_ecs_cluster" "boot_race" {
  provider = aws.ohio
  name     = "boot-race"
  tags     = local.boot_race_tag

  setting {
    name  = "containerInsights"
    value = "disabled"
  }
}

resource "aws_iam_role" "boot_race_task_execution" {
  name = "boot-race-task-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.boot_race_tag
}

resource "aws_iam_role_policy" "boot_race_task_execution" {
  name = "boot-race-pull"
  role = aws_iam_role.boot_race_task_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "PullWorkloadImage"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = aws_ecr_repository.boot_race.arn
      },
    ]
  })
}


resource "aws_ecs_task_definition" "boot_race_fargate" {
  provider                 = aws.ohio
  family                   = "boot-race-fargate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.boot_race_task_execution.arn

  container_definitions = jsonencode([{
    name                   = "workload"
    image                  = "${aws_ecr_repository.boot_race.repository_url}:workload"
    essential              = true
    readonlyRootFilesystem = true
    portMappings           = [{ containerPort = 8080, hostPort = 8080, protocol = "tcp" }]
  }])

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  tags = local.boot_race_tag
}

# Host networking on the container instance: no ENI to attach, which is the fastest ECS-on-EC2 path.
resource "aws_ecs_task_definition" "boot_race_ec2" {
  provider                 = aws.ohio
  family                   = "boot-race-ec2"
  requires_compatibilities = ["EC2"]
  network_mode             = "host"
  cpu                      = "1024"
  memory                   = "1536"
  execution_role_arn       = aws_iam_role.boot_race_task_execution.arn

  container_definitions = jsonencode([{
    name                   = "workload"
    image                  = "${aws_ecr_repository.boot_race.repository_url}:workload"
    essential              = true
    readonlyRootFilesystem = true
    portMappings           = [{ containerPort = 8080, hostPort = 8080, protocol = "tcp" }]
  }])

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  tags = local.boot_race_tag
}

resource "aws_iam_role" "boot_race_ecs_host" {
  name = "boot-race-ecs-host"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.boot_race_tag
}

# What the ECS agent on the one container instance needs, held to the boot-race cluster, instead of the managed
# AmazonEC2ContainerServiceforEC2Role (which reaches every cluster and every repository in the account).
resource "aws_iam_role_policy" "boot_race_ecs_host" {
  name = "boot-race-ecs-agent"
  role = aws_iam_role.boot_race_ecs_host.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # DiscoverPollEndpoint has no resource type.
        Sid      = "DiscoverPollEndpoint"
        Effect   = "Allow"
        Action   = "ecs:DiscoverPollEndpoint"
        Resource = "*"
      },
      {
        Sid    = "RegisterInTheBootRaceCluster"
        Effect = "Allow"
        Action = [
          "ecs:RegisterContainerInstance",
          "ecs:DeregisterContainerInstance",
          "ecs:SubmitContainerStateChange",
          "ecs:SubmitTaskStateChange",
          "ecs:SubmitAttachmentStateChanges",
        ]
        Resource = aws_ecs_cluster.boot_race.arn
      },
      {
        Sid      = "PollAsABootRaceContainerInstance"
        Effect   = "Allow"
        Action   = ["ecs:Poll", "ecs:StartTelemetrySession", "ecs:UpdateContainerInstancesState"]
        Resource = "${local.boot_race_ecs_arn}:container-instance/boot-race/*"
        Condition = {
          ArnEquals = { "ecs:cluster" = aws_ecs_cluster.boot_race.arn }
        }
      },
      {
        # The container-instance role page says the instance role pulls ECR images on the EC2 launch type; the
        # task-execution role page lists ECR pulls for Fargate, Managed Instances and external instances only. So the
        # host may pull, but only the workload image.
        Sid      = "EcrToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid      = "PullTheWorkloadImageOnly"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:BatchCheckLayerAvailability"]
        Resource = aws_ecr_repository.boot_race.arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "boot_race_ecs_host" {
  name = "boot-race-ecs-host"
  role = aws_iam_role.boot_race_ecs_host.name
}

resource "aws_launch_template" "boot_race_ecs_host" {
  provider      = aws.ohio
  name          = "boot-race-ecs-host"
  description   = "boot-race ECS-on-EC2 lane: one container instance in the boot-race cluster"
  image_id      = var.boot_race_ecs_ami
  instance_type = "c8g.medium"
  key_name      = aws_key_pair.ohio_pbox.key_name

  iam_instance_profile {
    name = aws_iam_instance_profile.boot_race_ecs_host.name
  }

  network_interfaces {
    subnet_id                   = aws_subnet.ohio_pbox.id
    security_groups             = [aws_security_group.boot_race.id]
    associate_public_ip_address = true
  }

  metadata_options {
    http_tokens = "required"
  }

  user_data = base64encode(<<-EOT
    #!/bin/bash
    cat >> /etc/ecs/ecs.config <<'CFG'
    ECS_CLUSTER=boot-race
    ECS_IMAGE_PULL_BEHAVIOR=prefer-cached
    CFG
  EOT
  )

  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.boot_race_tag, { Name = "boot-race-ecs-host" })
  }
  tag_specifications {
    resource_type = "volume"
    tags          = merge(local.boot_race_tag, { Name = "boot-race-ecs-host" })
  }
  tag_specifications {
    resource_type = "network-interface"
    tags          = merge(local.boot_race_tag, { Name = "boot-race-ecs-host" })
  }

  tags = local.boot_race_tag
}

# ============================================================ Lambda lanes
# A custom runtime (provided.al2023): the bootstrap is the workload binary itself, uploaded by the driver with
# UpdateFunctionCode. The placeholder below only exists so the functions can be created.
data "archive_file" "boot_race_placeholder" {
  type        = "zip"
  output_path = "${path.module}/.terraform/boot-race-placeholder.zip"

  source {
    content  = "#!/bin/sh\necho 'boot-race placeholder: the driver uploads the workload' >&2\nexit 1\n"
    filename = "bootstrap"
  }
}

resource "aws_iam_role" "boot_race_lambda" {
  name = "boot-race-lambda"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.boot_race_tag
}

resource "aws_cloudwatch_log_group" "boot_race" {
  for_each          = local.boot_race_lambdas
  provider          = aws.ohio
  name              = "/aws/lambda/boot-race-${each.key}"
  retention_in_days = 7
  tags              = local.boot_race_tag
}

resource "aws_iam_role_policy" "boot_race_lambda" {
  name = "boot-race-logs"
  role = aws_iam_role.boot_race_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "OwnLogGroups"
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = [for g in aws_cloudwatch_log_group.boot_race : "${g.arn}:*"]
    }]
  })
}

resource "aws_lambda_function" "boot_race" {
  for_each         = local.boot_race_lambdas
  provider         = aws.ohio
  function_name    = "boot-race-${each.key}"
  role             = aws_iam_role.boot_race_lambda.arn
  runtime          = "provided.al2023"
  handler          = "bootstrap"
  architectures    = ["arm64"]
  memory_size      = 2048
  timeout          = 10
  filename         = data.archive_file.boot_race_placeholder.output_path
  source_code_hash = data.archive_file.boot_race_placeholder.output_base64sha256

  environment {
    variables = {
      BOOT_RACE_EXIT_AFTER_RESPONSE = each.value
    }
  }

  tags = local.boot_race_tag

  # The driver uploads the workload, and may change the environment between invocations to force a new environment.
  lifecycle {
    ignore_changes = [filename, source_code_hash, environment]
  }

  depends_on = [aws_cloudwatch_log_group.boot_race, aws_iam_role_policy.boot_race_lambda]
}

resource "aws_lambda_function_url" "boot_race" {
  for_each           = local.boot_race_lambdas
  provider           = aws.ohio
  function_name      = aws_lambda_function.boot_race[each.key].function_name
  authorization_type = "AWS_IAM"
  invoke_mode        = "BUFFERED"
}

# ============================================================ the task launcher
# The only caller of RunTask and StopTask for boot-race tasks. Its code (boot-race/launcher.py) fixes the cluster, the
# task definitions and the Fargate network configuration: the parallel-box subnet, the boot-race group and no public
# IP. The driver may invoke it and nothing more; it cannot change its code, configuration or role.
data "archive_file" "boot_race_launcher" {
  type        = "zip"
  source_file = "${path.module}/boot-race/launcher.py"
  output_path = "${path.module}/.terraform/boot-race-launcher.zip"
}

resource "aws_cloudwatch_log_group" "boot_race_launcher" {
  provider          = aws.ohio
  name              = "/aws/lambda/boot-race-launcher"
  retention_in_days = 7
  tags              = local.boot_race_tag
}

resource "aws_iam_role" "boot_race_launcher" {
  name        = "boot-race-launcher"
  description = "boot-race-launcher Lambda: RunTask and StopTask of the two boot-race task definitions"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.boot_race_tag
}

data "aws_iam_policy_document" "boot_race_launcher" {
  statement {
    sid     = "RunBootRaceTasks"
    effect  = "Allow"
    actions = ["ecs:RunTask"]
    resources = [
      "${local.boot_race_ecs_arn}:task-definition/boot-race-fargate:*",
      "${local.boot_race_ecs_arn}:task-definition/boot-race-ec2:*",
    ]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.boot_race.arn]
    }
  }

  statement {
    sid       = "StopBootRaceTasks"
    effect    = "Allow"
    actions   = ["ecs:StopTask"]
    resources = ["${local.boot_race_ecs_arn}:task/boot-race/*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.boot_race.arn]
    }
  }

  statement {
    sid       = "PassTaskExecutionRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.boot_race_task_execution.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  statement {
    sid       = "OwnLogGroup"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.boot_race_launcher.arn}:*"]
  }
}

resource "aws_iam_role_policy" "boot_race_launcher" {
  name   = "run-and-stop-boot-race-tasks"
  role   = aws_iam_role.boot_race_launcher.id
  policy = data.aws_iam_policy_document.boot_race_launcher.json
}

resource "aws_lambda_function" "boot_race_launcher" {
  provider         = aws.ohio
  function_name    = "boot-race-launcher"
  role             = aws_iam_role.boot_race_launcher.arn
  handler          = "launcher.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 30
  memory_size      = 256
  filename         = data.archive_file.boot_race_launcher.output_path
  source_code_hash = data.archive_file.boot_race_launcher.output_base64sha256

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.boot_race_launcher.name
  }

  environment {
    variables = {
      CLUSTER        = aws_ecs_cluster.boot_race.name
      FARGATE_FAMILY = aws_ecs_task_definition.boot_race_fargate.family
      EC2_FAMILY     = aws_ecs_task_definition.boot_race_ec2.family
      SUBNET         = aws_subnet.ohio_pbox.id
      SECURITY_GROUP = aws_security_group.boot_race.id
    }
  }

  tags = local.boot_race_tag

  depends_on = [aws_iam_role_policy.boot_race_launcher]
}

# ============================================================ the reaper
# The server-side expiry: every 15 minutes, terminate any Project=boot-race instance whose current run began more than
# 3 hours ago, stop any boot-race task older than 1 hour, and remove Project=boot-race images and snapshots older than
# a day. The campaign cleans up after itself; this bounds what a failed or interrupted one leaves billing.
data "archive_file" "boot_race_reaper" {
  type        = "zip"
  source_file = "${path.module}/boot-race/reaper.py"
  output_path = "${path.module}/.terraform/boot-race-reaper.zip"
}

resource "aws_cloudwatch_log_group" "boot_race_reaper" {
  provider          = aws.ohio
  name              = "/aws/lambda/boot-race-reaper"
  retention_in_days = 14
  tags              = local.boot_race_tag
}

resource "aws_iam_role" "boot_race_reaper" {
  name        = "boot-race-reaper"
  description = "boot-race-reaper Lambda: expire boot-race instances, tasks, images and snapshots"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.boot_race_tag
}

data "aws_iam_policy_document" "boot_race_reaper" {
  statement {
    sid       = "Describe"
    effect    = "Allow"
    actions   = ["ec2:DescribeInstances", "ec2:DescribeImages", "ec2:DescribeSnapshots"]
    resources = ["*"]
  }

  statement {
    sid       = "TerminateTaggedInstances"
    effect    = "Allow"
    actions   = ["ec2:TerminateInstances"]
    resources = ["${local.boot_race_ec2_arn}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Project"
      values   = ["boot-race"]
    }
  }

  statement {
    sid     = "RemoveTaggedImages"
    effect  = "Allow"
    actions = ["ec2:DeregisterImage", "ec2:DeleteSnapshot"]
    resources = [
      "arn:aws:ec2:${local.boot_race_region}::image/*",
      "arn:aws:ec2:${local.boot_race_region}::snapshot/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Project"
      values   = ["boot-race"]
    }
  }

  statement {
    sid       = "ListBootRaceTasks"
    effect    = "Allow"
    actions   = ["ecs:ListTasks"]
    resources = ["${local.boot_race_ecs_arn}:container-instance/boot-race/*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.boot_race.arn]
    }
  }

  statement {
    sid       = "StopOldBootRaceTasks"
    effect    = "Allow"
    actions   = ["ecs:DescribeTasks", "ecs:StopTask"]
    resources = ["${local.boot_race_ecs_arn}:task/boot-race/*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.boot_race.arn]
    }
  }

  statement {
    sid       = "OwnLogGroup"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.boot_race_reaper.arn}:*"]
  }
}

resource "aws_iam_role_policy" "boot_race_reaper" {
  name   = "expire-boot-race-compute"
  role   = aws_iam_role.boot_race_reaper.id
  policy = data.aws_iam_policy_document.boot_race_reaper.json
}

resource "aws_lambda_function" "boot_race_reaper" {
  provider         = aws.ohio
  function_name    = "boot-race-reaper"
  role             = aws_iam_role.boot_race_reaper.arn
  handler          = "reaper.lambda_handler"
  runtime          = "python3.12"
  architectures    = ["arm64"]
  timeout          = 120
  memory_size      = 128
  filename         = data.archive_file.boot_race_reaper.output_path
  source_code_hash = data.archive_file.boot_race_reaper.output_base64sha256

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.boot_race_reaper.name
  }

  environment {
    variables = {
      CLUSTER          = aws_ecs_cluster.boot_race.name
      INSTANCE_LIMIT_S = "10800"
      TASK_LIMIT_S     = "3600"
      IMAGE_LIMIT_S    = "86400"
    }
  }

  tags = local.boot_race_tag

  depends_on = [aws_iam_role_policy.boot_race_reaper]
}

resource "aws_iam_role" "boot_race_reaper_scheduler" {
  name = "boot-race-reaper-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.boot_race_account } }
    }]
  })
  tags = local.boot_race_tag
}

resource "aws_iam_role_policy" "boot_race_reaper_scheduler" {
  name = "invoke-boot-race-reaper"
  role = aws_iam_role.boot_race_reaper_scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.boot_race_reaper.arn
    }]
  })
}

resource "aws_scheduler_schedule" "boot_race_reaper" {
  provider   = aws.ohio
  name       = "boot-race-reaper"
  group_name = "default"

  flexible_time_window { mode = "OFF" }

  schedule_expression = "rate(15 minutes)"

  target {
    arn      = aws_lambda_function.boot_race_reaper.arn
    role_arn = aws_iam_role.boot_race_reaper_scheduler.arn

    # The next run is 15 minutes away; a retry of a stale one would only overlap it.
    retry_policy {
      maximum_retry_attempts       = 0
      maximum_event_age_in_seconds = 300
    }
  }
}

# ============================================================ private ECR for Fargate
# A Fargate task with no public IP reaches ECR through these: the two ECR interface endpoints in the parallel-box
# subnet and an S3 gateway endpoint for the image layers. Private DNS points every client in the VPC at them, so the
# endpoints' group admits the parallel boxes as well as the boot-race workloads.
resource "aws_security_group" "boot_race_endpoints" {
  provider    = aws.ohio
  name_prefix = "boot-race-endpoints-"
  description = "ECR interface endpoints in the parallel-box VPC: HTTPS from the boot-race workloads and parallel boxes"
  vpc_id      = aws_vpc.ohio_pbox.id

  ingress {
    description     = "HTTPS to ECR, from the boot-race workloads and the parallel boxes"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = [aws_security_group.boot_race.id, aws_security_group.ohio_pbox.id]
  }

  tags = merge(local.boot_race_tag, { Name = "boot-race-endpoints" })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_endpoint" "boot_race_ecr" {
  for_each            = toset(["ecr.api", "ecr.dkr"])
  provider            = aws.ohio
  vpc_id              = aws_vpc.ohio_pbox.id
  service_name        = "com.amazonaws.${local.boot_race_region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.ohio_pbox.id]
  security_group_ids  = [aws_security_group.boot_race_endpoints.id]
  private_dns_enabled = true
  tags                = merge(local.boot_race_tag, { Name = "boot-race-${each.key}" })
}

resource "aws_vpc_endpoint" "boot_race_s3" {
  provider          = aws.ohio
  vpc_id            = aws_vpc.ohio_pbox.id
  service_name      = "com.amazonaws.${local.boot_race_region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.ohio_pbox.id]
  tags              = merge(local.boot_race_tag, { Name = "boot-race-s3" })
}

# ============================================================ the driver's role
resource "aws_iam_role" "boot_race_driver" {
  name                 = "boot-race-driver"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.dev_server.arn }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.boot_race_tag
}

data "aws_iam_policy_document" "boot_race_driver" {
  # The instance, its volume and its interface: created only through the two templates, carrying Project=boot-race.
  statement {
    sid     = "RunTaggedBootRaceOnly"
    effect  = "Allow"
    actions = ["ec2:RunInstances"]
    resources = [
      "${local.boot_race_ec2_arn}:instance/*",
      "${local.boot_race_ec2_arn}:volume/*",
      "${local.boot_race_ec2_arn}:network-interface/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = ["boot-race"]
    }
    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = [aws_launch_template.boot_race_ec2.arn, aws_launch_template.boot_race_ecs_host.arn]
    }
  }

  # The templates set c8g.medium, but RunInstances can override the type: hold the instance to it.
  statement {
    sid       = "DenyOtherInstanceTypes"
    effect    = "Deny"
    actions   = ["ec2:RunInstances", "ec2:StartInstances"]
    resources = ["${local.boot_race_ec2_arn}:instance/*"]
    condition {
      test     = "StringNotEquals"
      variable = "ec2:InstanceType"
      values   = ["c8g.medium"]
    }
  }

  # What a launch references, by exact ARN: subnet, group, key pair, templates, and the two public base images.
  statement {
    sid     = "RunInstancesReferencedResources"
    effect  = "Allow"
    actions = ["ec2:RunInstances"]
    resources = [
      aws_subnet.ohio_pbox.arn,
      aws_security_group.boot_race.arn,
      aws_key_pair.ohio_pbox.arn,
      aws_launch_template.boot_race_ec2.arn,
      aws_launch_template.boot_race_ecs_host.arn,
      "arn:aws:ec2:${local.boot_race_region}::image/${local.boot_race_base_ami}",
      "arn:aws:ec2:${local.boot_race_region}::image/${var.boot_race_ecs_ami}",
    ]
    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = [aws_launch_template.boot_race_ec2.arn, aws_launch_template.boot_race_ecs_host.arn]
    }
  }

  # The workload AMI the driver makes: an image of this account carrying Project=boot-race.
  statement {
    sid       = "RunInstancesWorkloadImage"
    effect    = "Allow"
    actions   = ["ec2:RunInstances"]
    resources = ["arn:aws:ec2:${local.boot_race_region}::image/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Project"
      values   = ["boot-race"]
    }
    condition {
      test     = "StringEquals"
      variable = "ec2:Owner"
      values   = [local.boot_race_account]
    }
    condition {
      test     = "ArnEquals"
      variable = "ec2:LaunchTemplate"
      values   = [aws_launch_template.boot_race_ec2.arn]
    }
  }

  statement {
    sid     = "TagAtCreateOnly"
    effect  = "Allow"
    actions = ["ec2:CreateTags"]
    resources = [
      "${local.boot_race_ec2_arn}:instance/*",
      "${local.boot_race_ec2_arn}:volume/*",
      "${local.boot_race_ec2_arn}:network-interface/*",
      "arn:aws:ec2:${local.boot_race_region}::image/*",
      "arn:aws:ec2:${local.boot_race_region}::snapshot/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "ec2:CreateAction"
      values   = ["RunInstances", "CreateImage"]
    }
  }

  # Start, stop (with or without hibernation) and terminate only the instances these templates made.
  statement {
    sid       = "ManageTaggedBootRaceInstances"
    effect    = "Allow"
    actions   = ["ec2:StartInstances", "ec2:StopInstances", "ec2:TerminateInstances"]
    resources = ["${local.boot_race_ec2_arn}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Project"
      values   = ["boot-race"]
    }
  }

  statement {
    sid       = "ImageFromTaggedInstance"
    effect    = "Allow"
    actions   = ["ec2:CreateImage"]
    resources = ["${local.boot_race_ec2_arn}:instance/*"]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Project"
      values   = ["boot-race"]
    }
  }

  statement {
    sid     = "ImageCarriesTag"
    effect  = "Allow"
    actions = ["ec2:CreateImage"]
    resources = [
      "arn:aws:ec2:${local.boot_race_region}::image/*",
      "arn:aws:ec2:${local.boot_race_region}::snapshot/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "aws:RequestTag/Project"
      values   = ["boot-race"]
    }
  }

  statement {
    sid     = "RemoveTaggedImages"
    effect  = "Allow"
    actions = ["ec2:DeregisterImage", "ec2:DeleteSnapshot"]
    resources = [
      "arn:aws:ec2:${local.boot_race_region}::image/*",
      "arn:aws:ec2:${local.boot_race_region}::snapshot/*",
    ]
    condition {
      test     = "StringEquals"
      variable = "ec2:ResourceTag/Project"
      values   = ["boot-race"]
    }
  }

  statement {
    sid    = "Ec2Describe"
    effect = "Allow"
    actions = [
      "ec2:DescribeInstances",
      "ec2:DescribeInstanceStatus",
      "ec2:DescribeImages",
      "ec2:DescribeSnapshots",
      "ec2:DescribeNetworkInterfaces",
      "ec2:DescribeLaunchTemplates",
      "ec2:DescribeLaunchTemplateVersions",
    ]
    resources = ["*"]
  }

  # Tasks are run and stopped only by the launcher function, whose network configuration is fixed in its code: RunTask
  # has no condition key for the subnet or a public IP, so the driver holds neither RunTask nor StopTask.
  statement {
    sid       = "InvokeTheLauncher"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.boot_race_launcher.arn]
  }

  statement {
    sid     = "ReadBootRaceTasks"
    effect  = "Allow"
    actions = ["ecs:DescribeTasks", "ecs:ListTasks", "ecs:ListContainerInstances", "ecs:DescribeContainerInstances"]
    resources = [
      aws_ecs_cluster.boot_race.arn,
      "${local.boot_race_ecs_arn}:task/boot-race/*",
      "${local.boot_race_ecs_arn}:container-instance/boot-race/*",
    ]
  }

  statement {
    sid       = "DescribeTaskDefinitions"
    effect    = "Allow"
    actions   = ["ecs:DescribeTaskDefinition"]
    resources = ["*"]
  }

  statement {
    sid       = "PassEcsHostRole"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.boot_race_ecs_host.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ec2.amazonaws.com"]
    }
  }

  statement {
    sid       = "EcrToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "PushWorkloadImage"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:InitiateLayerUpload",
      "ecr:UploadLayerPart",
      "ecr:CompleteLayerUpload",
      "ecr:PutImage",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:DescribeImages",
      "ecr:BatchDeleteImage",
    ]
    resources = [aws_ecr_repository.boot_race.arn]
  }

  statement {
    sid    = "DriveBootRaceFunctions"
    effect = "Allow"
    actions = [
      "lambda:InvokeFunction",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:GetFunctionUrlConfig",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
    ]
    resources = [for f in aws_lambda_function.boot_race : f.arn]
  }

  statement {
    sid       = "InvokeBootRaceUrls"
    effect    = "Allow"
    actions   = ["lambda:InvokeFunctionUrl"]
    resources = [for f in aws_lambda_function.boot_race : f.arn]
    condition {
      test     = "StringEquals"
      variable = "lambda:FunctionUrlAuthType"
      values   = ["AWS_IAM"]
    }
  }

  # The REPORT lines (Init Duration) of the two functions.
  statement {
    sid       = "ReadBootRaceLogs"
    effect    = "Allow"
    actions   = ["logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams"]
    resources = [for g in aws_cloudwatch_log_group.boot_race : "${g.arn}:*"]
  }
}

resource "aws_iam_role_policy" "boot_race_driver" {
  name   = "boot-race-driver"
  role   = aws_iam_role.boot_race_driver.id
  policy = data.aws_iam_policy_document.boot_race_driver.json
}

# The dev role may assume the driver role and nothing more.
resource "aws_iam_role_policy" "dev_server_boot_race" {
  name = "boot-race-assume-driver"
  role = aws_iam_role.dev_server.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "AssumeBootRaceDriver"
      Effect   = "Allow"
      Action   = "sts:AssumeRole"
      Resource = aws_iam_role.boot_race_driver.arn
    }]
  })
}

output "boot_race" {
  description = "What the boot-race driver needs: its role, the function URLs, the image repository"
  value = {
    driver_role_arn = aws_iam_role.boot_race_driver.arn
    function_urls   = { for k, u in aws_lambda_function_url.boot_race : k => u.function_url }
    repository_url  = aws_ecr_repository.boot_race.repository_url
  }
}

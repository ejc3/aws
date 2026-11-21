# GPU Lambda spike for the fleet wall. A capacity provider is configuration only until a project
# operator publishes a function version. Pin the size here: IAM cannot cap MaxVCpuCount in a
# CreateCapacityProvider call. GPU versions default to one minimum execution environment.
locals {
  fcvm_lambda_region        = "us-east-1"
  fcvm_lambda_instance_type = "g4dn.xlarge"
  fcvm_lambda_subnets = [
    for az, id in module.fcvm_fleet_east1.subnet_ids_by_az : id
    if contains(data.aws_availability_zones.fcvm_lambda.names, az) && contains(data.aws_ec2_instance_type_offerings.fcvm_lambda_gpu.locations, az)
  ]
  fcvm_lambda_name         = "fcvm-lambda-tile"
  fcvm_lambda_function_arn = "arn:aws:lambda:${local.fcvm_lambda_region}:${local.fcvm_fleet_account_id}:function:${local.fcvm_lambda_name}"
  fcvm_lambda_repo_name    = "fcvm-fleet/lambda-tile"
  fcvm_lambda_repo_arn     = "arn:aws:ecr:${local.fcvm_lambda_region}:${local.fcvm_fleet_account_id}:repository/${local.fcvm_lambda_repo_name}"
  fcvm_lambda_logs_arn     = "arn:aws:logs:${local.fcvm_lambda_region}:${local.fcvm_fleet_account_id}:log-group:/aws/lambda/${local.fcvm_lambda_name}"
  fcvm_fleet_gpu_lambda = {
    region                = local.fcvm_lambda_region
    function_name         = local.fcvm_lambda_name
    repository_name       = local.fcvm_lambda_repo_name
    execution_role_arn    = aws_iam_role.fcvm_fleet_lambda_tile.arn
    capacity_provider_arn = aws_lambda_capacity_provider.fcvm_fleet_spike.arn
  }
}

# The execution role writes its own log streams and permits same-account Lambda image retrieval
# from the one spike repository. ARM has no permission to change a repository policy.
resource "aws_iam_role" "fcvm_fleet_lambda_tile" {
  provider = aws.fcvm_fleet
  name     = local.fcvm_lambda_name
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = local.fcvm_lambda_name, Project = "fcvm-fleet" }
}

resource "aws_iam_role_policy" "fcvm_fleet_lambda_tile_runtime" {
  provider = aws.fcvm_fleet
  role     = aws_iam_role.fcvm_fleet_lambda_tile.name
  name     = "tile-logs-and-image"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${local.fcvm_lambda_logs_arn}:log-stream:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = local.fcvm_lambda_repo_arn
      },
    ]
  })
}

# The capacity provider's operator role: Lambda manages the GPU instances with it (AWS's documented managed policy).
resource "aws_iam_role" "fcvm_fleet_lambda_operator" {
  provider = aws.fcvm_fleet
  name     = "fcvm-lambda-operator"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "fcvm-lambda-operator", Project = "fcvm-fleet" }
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_lambda_operator" {
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_lambda_operator.name
  policy_arn = "arn:aws:iam::aws:policy/AWSLambdaManagedEC2ResourceOperator"
}

# The invocation-only spike needs HTTPS for image/runtime services. It has no ingress rules.
resource "aws_security_group" "fcvm_fleet_lambda_tile" {
  provider    = aws.fcvm_fleet_east1
  name        = "fcvm-lambda-tile"
  description = "GPU Lambda spike outbound HTTPS"
  vpc_id      = module.fcvm_fleet_east1.vpc_id
  egress {
    protocol         = "tcp"
    from_port        = 443
    to_port          = 443
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
}

# CreateCapacityProvider rejects use1-az3 (us-east-1e in this account). Use the physical AZ ID
# because accounts can map that zone to different names. EC2 offerings are for the selected GPU.
data "aws_availability_zones" "fcvm_lambda" {
  provider         = aws.fcvm_fleet_east1
  state            = "available"
  exclude_zone_ids = ["use1-az3"]
}

data "aws_ec2_instance_type_offerings" "fcvm_lambda_gpu" {
  provider      = aws.fcvm_fleet_east1
  location_type = "availability-zone"
  filter {
    name   = "instance-type"
    values = [local.fcvm_lambda_instance_type]
  }
}

resource "aws_lambda_capacity_provider" "fcvm_fleet_spike" {
  provider = aws.fcvm_fleet_east1
  name     = "fcvm-lambda-spike"
  vpc_config {
    subnet_ids         = local.fcvm_lambda_subnets
    security_group_ids = [aws_security_group.fcvm_fleet_lambda_tile.id]
  }
  permissions_config {
    capacity_provider_operator_role_arn = aws_iam_role.fcvm_fleet_lambda_operator.arn
  }
  instance_requirements {
    architectures          = ["x86_64"]
    allowed_instance_types = [local.fcvm_lambda_instance_type]
  }
  capacity_provider_scaling_config {
    scaling_mode   = "Auto"
    max_vcpu_count = 4 # One 4-vCPU T4 host for this spike.
  }
  lifecycle {
    precondition {
      condition     = length(local.fcvm_lambda_subnets) > 0
      error_message = "The fleet has no Lambda-supported subnet offering the GPU Lambda instance type."
    }
  }
  depends_on = [aws_iam_role_policy_attachment.fcvm_fleet_lambda_operator]
}

resource "aws_iam_policy" "fcvm_fleet_terraform_lambda" {
  provider    = aws.fcvm_fleet
  name        = "fcvm-lambda-tile-deploy"
  description = "Project deployment for one private function and fixed GPU capacity provider"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DeployAndInvokeTile"
        Effect = "Allow"
        Action = [
          "lambda:CreateFunction", "lambda:GetFunction", "lambda:GetFunctionConfiguration", "lambda:UpdateFunctionCode",
          "lambda:UpdateFunctionConfiguration", "lambda:DeleteFunction", "lambda:PublishVersion", "lambda:InvokeFunction",
          "lambda:ListVersionsByFunction", "lambda:GetFunctionConcurrency", "lambda:GetFunctionCodeSigningConfig",
          "lambda:GetFunctionScalingConfig", "lambda:PutFunctionScalingConfig", "lambda:ListTags", "lambda:TagResource", "lambda:UntagResource",
        ]
        Resource = [local.fcvm_lambda_function_arn, "${local.fcvm_lambda_function_arn}:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:GetCapacityProvider", "lambda:PassCapacityProvider"]
        Resource = aws_lambda_capacity_provider.fcvm_fleet_spike.arn
      },
      {
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.fcvm_fleet_lambda_tile.arn
        Condition = { StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" } }
      },
      {
        Effect = "Allow"
        Action = [
          "ecr:CreateRepository", "ecr:DescribeRepositories", "ecr:DeleteRepository", "ecr:ListTagsForResource", "ecr:TagResource", "ecr:UntagResource",
          "ecr:GetLifecyclePolicy", "ecr:PutLifecyclePolicy", "ecr:DeleteLifecyclePolicy", "ecr:PutImageTagMutability",
          "ecr:PutImageScanningConfiguration", "ecr:GetRepositoryPolicy", "ecr:DescribeImages", "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload", "ecr:PutImage", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer",
        ]
        Resource = local.fcvm_lambda_repo_arn
      },
      {
        Effect    = "Allow"
        Action    = ["ecr:GetAuthorizationToken", "logs:DescribeLogGroups"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.fcvm_lambda_region } }
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup", "logs:DeleteLogGroup", "logs:PutRetentionPolicy", "logs:DeleteRetentionPolicy",
          "logs:ListTagsForResource", "logs:TagResource", "logs:UntagResource", "logs:FilterLogEvents", "logs:GetLogEvents", "logs:DescribeLogStreams",
        ]
        Resource = [local.fcvm_lambda_logs_arn, "${local.fcvm_lambda_logs_arn}:*"]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_terraform_lambda" {
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_terraform.name
  policy_arn = aws_iam_policy.fcvm_fleet_terraform_lambda.arn
}

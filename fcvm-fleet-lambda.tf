# The fleet wall's Lambda tiles (ejc3/fcvm fleet/PROTOCOL.md, "Lambda tile"): some of the AWS half's tiles run as GPU
# Lambda functions on Lambda Managed Instances instead of EC2 tiles. Per stack (staging, and prod while it exists), in
# us-east-1: a capacity provider, the functions' execution role, a security group with no ingress, the functions' log
# groups, and the coordinator's grants to create, publish and delete its own functions. The coordinator makes one function
# per tile, fcvm-lam-<fleet>-<n> (ejc3/fcvm src/fleet/coord/lambda.rs), publishes a new version for every launch, and
# deletes them when the fleet sleeps; no function exists while it sleeps, and an empty capacity provider runs no instance.
#
# This file grants and creates; it does not turn the lane on. The coordinator runs Lambda tiles only with --lambda aws and
# --lambda-tiles, which fcvm-fleet.tf and fcvm-fleet-prod.tf do not pass yet (the next PR, staging first).
#
# Cost while a tile is assigned: Lambda keeps the tile's previous instance running beside its current one (measured
# 2026-11-22, ejc3/fcvm lambda lane step 0), so two g4dn.xlarge per tile at the on-demand price plus Lambda's management fee.
locals {
  fcvm_lambda_region        = "us-east-1"
  fcvm_lambda_instance_type = "g4dn.xlarge"
  # The most Lambda tiles a stack runs: half of the AWS half's 8 tiles (owner, 2026-11-22). A tile holds up to three
  # instances (the current one, the one Lambda keeps, and a new one while a race's version comes up), the count the
  # coordinator's launch cap uses (ejc3/fcvm src/fleet/coord/spend.rs), at 4 vCPUs each: 48 vCPUs a stack.
  fcvm_lambda_max_tiles = 4
  fcvm_lambda_max_vcpus = local.fcvm_lambda_max_tiles * 3 * 4
  # Log groups exist for the first 8 tile numbers (the AWS half's size); the execution role cannot create others.
  fcvm_lambda_log_tiles = range(1, 9)

  fcvm_lambda_repo_name = "fcvm-fleet/lambda-tile"
  fcvm_lambda_repo_arn  = "arn:aws:ecr:${local.fcvm_lambda_region}:${local.fcvm_fleet_account_id}:repository/${local.fcvm_lambda_repo_name}"

  # Each stack's VPC in us-east-1, its coordinator task role and its certificate secret (fcvm-fleet.tf, fcvm-fleet-prod.tf).
  fcvm_lambda_stacks = merge(
    {
      stage = {
        fleet            = local.fcvm_fleet_stage.fleet
        vpc_id           = module.fcvm_fleet_east1.vpc_id
        subnet_ids_by_az = module.fcvm_fleet_east1.subnet_ids_by_az
        coordinator_role = aws_iam_role.fcvm_fleet_coordinator_task.name
        tls_secret_arn   = aws_secretsmanager_secret.fcvm_fleet_stage_tls.arn
      }
    },
    {
      for m in module.fcvm_fleet_prod_east1 : "prod" => {
        fleet            = local.fcvm_fleet_prod_stack.fleet
        vpc_id           = m.vpc_id
        subnet_ids_by_az = m.subnet_ids_by_az
        coordinator_role = aws_iam_role.fcvm_fleet_prod_coordinator_task[0].name
        tls_secret_arn   = aws_secretsmanager_secret.fcvm_fleet_prod_tls[0].arn
      }
    },
  )
  # Both AWS Budgets filters use these cost-allocation tags on the billed resources.
  fcvm_lambda_billing_tags = {
    for k, s in local.fcvm_lambda_stacks : k => [
      { Key = "Purpose", Value = "fcvm-fleet-tile" },
      { Key = "Fleet", Value = s.fleet },
    ]
  }
  # A stack's functions, by name prefix: fcvm-lam-<fleet>-<n>, and their versions.
  fcvm_lambda_function_arns = {
    for k, s in local.fcvm_lambda_stacks : k => "arn:aws:lambda:${local.fcvm_lambda_region}:${local.fcvm_fleet_account_id}:function:fcvm-lam-${s.fleet}-*"
  }
  # Each stack's tile role, which its certificate secret's policy admits (fcvm-fleet-stage-cert.tf, fcvm-fleet-prod.tf).
  fcvm_lambda_tile_role_arns = { for k, r in aws_iam_role.fcvm_lambda_tile : k => r.arn }
  fcvm_lambda_subnets = {
    for k, s in local.fcvm_lambda_stacks : k => [
      for az, id in s.subnet_ids_by_az : id
      if contains(data.aws_availability_zones.fcvm_lambda.names, az) && contains(data.aws_ec2_instance_type_offerings.fcvm_lambda_gpu.locations, az)
    ]
  }
  # What the coordinator's --lambda-role, --lambda-capacity-provider and --lambda-image take (the next PR), and where
  # fcvm-arm pushes the lane image (/infra/fcvm-fleet, gpu_lambda).
  fcvm_fleet_gpu_lambda = {
    region          = local.fcvm_lambda_region
    repository_name = local.fcvm_lambda_repo_name
    repository_url  = aws_ecr_repository.fcvm_fleet_lambda_tile.repository_url
    stacks = {
      for k, s in local.fcvm_lambda_stacks : k => {
        execution_role_arn    = aws_iam_role.fcvm_lambda_tile[k].arn
        capacity_provider_arn = aws_cloudformation_stack.fcvm_lambda[k].outputs["CapacityProviderArn"]
      }
    }
  }
}

# ------------------------------------------------------------------------------------------------ the lane image
# Created by fcvm-arm for the spike (2026-11-21) and adopted here with the settings it has: immutable tags, scanned on push.
import {
  to = aws_ecr_repository.fcvm_fleet_lambda_tile
  id = "fcvm-fleet/lambda-tile"
}

resource "aws_ecr_repository" "fcvm_fleet_lambda_tile" {
  provider             = aws.fcvm_fleet_east1
  name                 = local.fcvm_lambda_repo_name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  tags = { Name = "fcvm-lambda-tile", Project = "fcvm-fleet" }
}

resource "aws_ecr_lifecycle_policy" "fcvm_fleet_lambda_tile" {
  provider   = aws.fcvm_fleet_east1
  repository = aws_ecr_repository.fcvm_fleet_lambda_tile.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the newest 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

# CreateFunction refuses an image unless the repository policy lets the Lambda service pull it for that function. Only the
# stacks' tile functions (fcvm-lam-<fleet>-*, any version) qualify.
resource "aws_ecr_repository_policy" "fcvm_fleet_lambda_tile" {
  provider   = aws.fcvm_fleet_east1
  repository = aws_ecr_repository.fcvm_fleet_lambda_tile.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "LambdaPullsTheTileImage"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
      Condition = { StringLike = { "aws:sourceArn" = values(local.fcvm_lambda_function_arns) } }
    }]
  })
}

# fcvm-arm builds the lane image (ejc3/fcvm fleet/lambda) and pushes it here through fcvm-fleet-terraform, as it does the
# coordinator's. Push and list only: the repository, its policies and the functions are not its to change.
resource "aws_iam_policy" "fcvm_fleet_terraform_lambda" {
  provider    = aws.fcvm_fleet
  name        = "fcvm-lambda-tile-push"
  description = "Push the fleet's Lambda tile image to its one repository"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ecr:BatchCheckLayerAvailability", "ecr:InitiateLayerUpload", "ecr:UploadLayerPart", "ecr:CompleteLayerUpload",
        "ecr:PutImage", "ecr:DescribeImages", "ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = aws_ecr_repository.fcvm_fleet_lambda_tile.arn
      },
      {
        Effect    = "Allow"
        Action    = "ecr:GetAuthorizationToken"
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.fcvm_lambda_region } }
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "fcvm_fleet_terraform_lambda" {
  provider   = aws.fcvm_fleet
  role       = aws_iam_role.fcvm_fleet_terraform.name
  policy_arn = aws_iam_policy.fcvm_fleet_terraform_lambda.arn
}

# ------------------------------------------------------------------------------------------------ the functions' role
# A tile's environment writes its log streams, pulls the lane image, and reads its stack's certificate secret (cert, key and
# scene token: TLS ends in the renderer, ejc3/fcvm fleet/PROTOCOL.md "Lambda tile"). The secret's own policy also names this
# role (fcvm-fleet-stage-cert.tf, fcvm-fleet-prod.tf).
resource "aws_iam_role" "fcvm_lambda_tile" {
  for_each = local.fcvm_lambda_stacks
  provider = aws.fcvm_fleet
  name     = "fcvm-lam-${each.value.fleet}"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = { Name = "fcvm-lam-${each.value.fleet}", Project = "fcvm-fleet", Fleet = each.value.fleet }
}

resource "aws_iam_role_policy" "fcvm_lambda_tile" {
  for_each = local.fcvm_lambda_stacks
  provider = aws.fcvm_fleet
  role     = aws_iam_role.fcvm_lambda_tile[each.key].name
  name     = "tile"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "arn:aws:logs:${local.fcvm_lambda_region}:${local.fcvm_fleet_account_id}:log-group:/aws/lambda/fcvm-lam-${each.value.fleet}-*:log-stream:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = aws_ecr_repository.fcvm_fleet_lambda_tile.arn
      },
      {
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = each.value.tls_secret_arn
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "fcvm_lambda_tile" {
  for_each = { for p in setproduct(keys(local.fcvm_lambda_stacks), local.fcvm_lambda_log_tiles) : "${p[0]}-${p[1]}" => local.fcvm_lambda_stacks[p[0]].fleet }
  provider = aws.fcvm_fleet_east1
  # each.key is <stack>-<n>; the function is fcvm-lam-<fleet>-<n>.
  name              = "/aws/lambda/fcvm-lam-${each.value}-${split("-", each.key)[1]}"
  retention_in_days = 14
  tags              = { Project = "fcvm-fleet", Fleet = each.value }
}

# ------------------------------------------------------------------------------------------------ capacity
# The capacity providers' operator role: Lambda manages the GPU instances with it (AWS's documented managed policy).
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

# A tile only dials out: its tunnel to the coordinator, the image and runtime services, Secrets Manager, all on 443 over
# IPv4 or IPv6. No ingress rule: a viewer reaches a tile through the coordinator's relay, never directly.
resource "aws_security_group" "fcvm_lambda_tile" {
  for_each    = local.fcvm_lambda_stacks
  provider    = aws.fcvm_fleet_east1
  name        = "fcvm-lam-${each.value.fleet}"
  description = "Fleet Lambda tiles - outbound HTTPS only"
  vpc_id      = each.value.vpc_id
  egress {
    protocol         = "tcp"
    from_port        = 443
    to_port          = 443
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = ["::/0"]
  }
  tags = { Name = "fcvm-lam-${each.value.fleet}", Project = "fcvm-fleet", Fleet = each.value.fleet }
}

# CreateCapacityProvider rejects use1-az3 (us-east-1e in this account). Use the physical AZ ID because accounts can map that
# zone to different names. EC2 offerings are for the selected GPU.
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

# One per stack, so neither stack's tiles can take the other's capacity. The ceiling is set here because IAM cannot cap
# MaxVCpuCount in a CreateCapacityProvider call, and the coordinators hold no grant to create or change a provider.
# The pinned AWS provider does not expose PropagateTags. CloudFormation owns the provider so the
# existing daily budgets count its managed EC2 instances, disks and interfaces from their creation.
resource "aws_cloudformation_stack" "fcvm_lambda" {
  for_each = local.fcvm_lambda_stacks
  provider = aws.fcvm_fleet_east1
  name     = "fcvm-lam-${each.value.fleet}"

  template_body = jsonencode({
    Resources = {
      Provider = {
        Type = "AWS::Lambda::CapacityProvider"
        Properties = {
          CapacityProviderName = "fcvm-lam-${each.value.fleet}"
          VpcConfig = {
            SubnetIds        = local.fcvm_lambda_subnets[each.key]
            SecurityGroupIds = [aws_security_group.fcvm_lambda_tile[each.key].id]
          }
          PermissionsConfig = {
            CapacityProviderOperatorRoleArn = aws_iam_role.fcvm_fleet_lambda_operator.arn
          }
          InstanceRequirements = {
            Architectures        = ["x86_64"]
            AllowedInstanceTypes = [local.fcvm_lambda_instance_type]
          }
          CapacityProviderScalingConfig = {
            ScalingMode  = "Auto"
            MaxVCpuCount = local.fcvm_lambda_max_vcpus
          }
          Tags = local.fcvm_lambda_billing_tags[each.key]
          PropagateTags = {
            Mode         = "Explicit"
            ExplicitTags = local.fcvm_lambda_billing_tags[each.key]
          }
        }
      }
    }
    Outputs = {
      CapacityProviderArn = { Value = { "Fn::GetAtt" = ["Provider", "Arn"] } }
    }
  })

  lifecycle {
    precondition {
      condition     = length(local.fcvm_lambda_subnets[each.key]) > 0
      error_message = "The ${each.key} fleet has no Lambda-supported subnet offering the GPU Lambda instance type."
    }
  }
  depends_on = [aws_iam_role_policy_attachment.fcvm_fleet_lambda_operator]
}

# ------------------------------------------------------------------------------------------------ the coordinators' grants
# What ejc3/fcvm's LambdaDriver calls (src/fleet/coord/lambda.rs): GetFunctionConfiguration, CreateFunction (with tags, so
# TagResource), UpdateFunctionConfiguration, PublishVersion and DeleteFunction, on its own stack's function names only.
# CreateFunction also checks the caller's pull rights on the image and its right to pass the role and the capacity provider:
# each stack passes only its own execution role and provider. No function URL, resource policy or invoke grant.
# A separate managed policy per stack, because the coordinators' inline policies are at their size limit.
resource "aws_iam_policy" "fcvm_lambda_coordinator" {
  for_each    = local.fcvm_lambda_stacks
  provider    = aws.fcvm_fleet
  name        = "fcvm-fleet-${each.key}-lambda"
  description = "The ${each.key} fleet coordinator runs its Lambda tiles, fcvm-lam-${each.value.fleet}-*, on its own capacity provider"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ItsTileFunctions"
        Effect = "Allow"
        Action = ["lambda:GetFunctionConfiguration", "lambda:CreateFunction", "lambda:UpdateFunctionConfiguration",
        "lambda:PublishVersion", "lambda:DeleteFunction", "lambda:TagResource"]
        Resource = [local.fcvm_lambda_function_arns[each.key]]
      },
      {
        Sid      = "ItsCapacityProvider"
        Effect   = "Allow"
        Action   = "lambda:PassCapacityProvider"
        Resource = aws_cloudformation_stack.fcvm_lambda[each.key].outputs["CapacityProviderArn"]
      },
      {
        Sid       = "ItsExecutionRole"
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = aws_iam_role.fcvm_lambda_tile[each.key].arn
        Condition = { StringEquals = { "iam:PassedToService" = "lambda.amazonaws.com" } }
      },
      {
        Sid      = "PullTheTileImage"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
        Resource = aws_ecr_repository.fcvm_fleet_lambda_tile.arn
      },
    ]
  })
  tags = { Fleet = each.value.fleet }
}

resource "aws_iam_role_policy_attachment" "fcvm_lambda_coordinator" {
  for_each   = local.fcvm_lambda_stacks
  provider   = aws.fcvm_fleet
  role       = each.value.coordinator_role
  policy_arn = aws_iam_policy.fcvm_lambda_coordinator[each.key].arn
  # A stack whose cutoff is already attached must deny Lambda launches before it gains the grant.
  depends_on = [aws_iam_policy.fcvm_fleet_spend_cap]
}

# ------------------------------------------------------------------------------------------------ the spike, gone
# The spike's capacity provider, function role and security group (2026-11-21) are destroyed; its policy is replaced above.
# AWS refuses to delete a capacity provider with function versions on it: the spike's function was deleted on 2026-11-22.

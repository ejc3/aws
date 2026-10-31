# A fixed image factory. GitHub can request work; it cannot launch EC2, change a
# launch template, read state, or obtain the factory's worker credentials.
variable "fcvm_ami_pipeline_enabled" {
  description = "Enable automatic factory requests only after the main-only consumer and Default-version coordinator are deployed"
  type        = bool
  default     = false
}

locals {
  fcvm_ami_project       = "fcvm-fleet-image-factory"
  fcvm_ami_region        = "us-east-1"
  fcvm_ami_state_prefix  = "/fcvm-fleet/ami-pipeline"
  fcvm_ami_builder_tag   = "fcvm-fleet-ami-builder"
  fcvm_ami_source        = "https://github.com/ejc3/fcvm.git"
  fcvm_ami_guard_sha     = filesha256("${path.module}/scripts/cve-2026-98432-mitigation.py")
  fcvm_ami_build_minutes = 150
  fcvm_ami_max_usd       = 25
}

resource "aws_s3_bucket" "fcvm_ami_pipeline" {
  provider = aws.ohio
  bucket   = "fcvm-fleet-image-factory-${data.aws_caller_identity.current.account_id}"
  tags     = { Purpose = "fcvm-fleet-image-factory" }
}

resource "aws_s3_bucket_public_access_block" "fcvm_ami_pipeline" {
  provider                = aws.ohio
  bucket                  = aws_s3_bucket.fcvm_ami_pipeline.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "fcvm_ami_pipeline" {
  provider = aws.ohio
  bucket   = aws_s3_bucket.fcvm_ami_pipeline.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_versioning" "fcvm_ami_pipeline" {
  provider = aws.ohio
  bucket   = aws_s3_bucket.fcvm_ami_pipeline.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "fcvm_ami_pipeline" {
  provider = aws.ohio
  bucket   = aws_s3_bucket.fcvm_ami_pipeline.id
  rule {
    id     = "expire-private-build-artifacts"
    status = "Enabled"
    filter { prefix = "" }
    expiration { days = 7 }
    noncurrent_version_expiration { noncurrent_days = 7 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}

resource "aws_ssm_parameter" "fcvm_ami_control" {
  name  = "${local.fcvm_ami_state_prefix}/control"
  type  = "String"
  value = jsonencode({ version = 1, active = null, pending = null, last_normal_day = "", last_input = "" })
  lifecycle { ignore_changes = [value] }
}

data "aws_ami" "fcvm_ami_canonical" {
  provider    = aws.fcvm_fleet_east1
  most_recent = true
  owners      = ["099720109477"]
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
  filter {
    name   = "state"
    values = ["available"]
  }
}

resource "aws_security_group" "fcvm_ami_builder" {
  provider    = aws.fcvm_fleet_east1
  name        = "fcvm-fleet-ami-builder"
  description = "Image factory: no inbound listeners; outbound package/API access and DNS only"
  vpc_id      = module.fcvm_fleet_east1.vpc_id
  egress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "udp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  egress {
    from_port   = 53
    to_port     = 53
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = { Purpose = local.fcvm_ami_builder_tag }
}

resource "aws_iam_role" "fcvm_ami_builder" {
  provider = aws.fcvm_fleet_east1
  name     = "fcvm-fleet-ami-builder"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "ec2.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}

resource "aws_iam_instance_profile" "fcvm_ami_builder" {
  provider = aws.fcvm_fleet_east1
  name     = "fcvm-fleet-ami-builder"
  role     = aws_iam_role.fcvm_ami_builder.name
}

resource "aws_iam_role" "fcvm_ami_dispatch" {
  name = "fcvm-fleet-ami-dispatch"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}

resource "aws_iam_role" "fcvm_ami_codebuild" {
  name = "fcvm-fleet-ami-codebuild"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Service = "codebuild.amazonaws.com" }, Action = "sts:AssumeRole",
    Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }, ArnEquals = { "aws:SourceArn" = "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:project/${local.fcvm_ami_project}" } }
  }] })
}

resource "aws_iam_role" "fcvm_ami_broker_fleet" {
  provider = aws.fcvm_fleet_east1
  name     = "fcvm-fleet-ami-broker"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { AWS = aws_iam_role.fcvm_ami_dispatch.arn }, Action = "sts:AssumeRole"
  }] })
}

resource "aws_iam_role" "fcvm_ami_worker_fleet" {
  provider = aws.fcvm_fleet_east1
  name     = "fcvm-fleet-ami-worker"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { AWS = aws_iam_role.fcvm_ami_dispatch.arn }, Action = ["sts:AssumeRole", "sts:TagSession"],
    Condition = { Null = { "aws:RequestTag/FactoryBuild" = "false" }, "ForAllValues:StringEquals" = { "aws:TagKeys" = ["FactoryBuild"] } }
  }] })
}

# These are factory-only test identities, never the fleet's production TLS key.
# Values are generated in the bounded executor and kept out of Terraform state.
resource "aws_secretsmanager_secret" "fcvm_ami_tls" {
  provider                = aws.fcvm_fleet_east1
  for_each                = toset(["build", "test"])
  name                    = "fcvm-fleet/ami-pipeline/tls-${each.key}"
  recovery_window_in_days = 7
  tags                    = { Purpose = "fcvm-fleet-image-factory" }
}

resource "aws_ssm_document" "fcvm_ami_build" {
  provider        = aws.fcvm_fleet_east1
  name            = "fcvm-fleet-ami-build"
  document_type   = "Command"
  document_format = "JSON"
  content = jsonencode({ schemaVersion = "2.2", description = "Factory worker commands, only on isolated owned builders", parameters = {
    commands = { type = "StringList" }, executionTimeout = { type = "String", default = "9000", allowedPattern = "^([1-9][0-9]{0,2}|[1-8][0-9]{3}|9000)$" }
  }, mainSteps = [{ action = "aws:runShellScript", name = "factory", inputs = { runCommand = "{{ commands }}", timeoutSeconds = "{{ executionTimeout }}" } }] })
}

resource "aws_cloudwatch_log_group" "fcvm_ami_dispatch" {
  name              = "/aws/lambda/fcvm-fleet-ami-dispatch"
  retention_in_days = 14
}

resource "aws_cloudwatch_log_group" "fcvm_ami_codebuild" {
  name              = "/aws/codebuild/fcvm-fleet-image-factory"
  retention_in_days = 14
}

data "archive_file" "fcvm_ami_dispatch" {
  type        = "zip"
  source_dir  = "${path.module}/fcvm-fleet-ami-pipeline"
  output_path = "${path.module}/.terraform/fcvm-fleet-ami-pipeline.zip"
}

resource "aws_lambda_function" "fcvm_ami_dispatch" {
  function_name                  = "fcvm-fleet-ami-dispatch"
  role                           = aws_iam_role.fcvm_ami_dispatch.arn
  runtime                        = "python3.12"
  handler                        = "dispatch.lambda_handler"
  timeout                        = 180
  memory_size                    = 256
  reserved_concurrent_executions = 1
  filename                       = data.archive_file.fcvm_ami_dispatch.output_path
  source_code_hash               = data.archive_file.fcvm_ami_dispatch.output_base64sha256
  publish                        = true
  environment {
    variables = {
      ENABLED           = tostring(var.fcvm_ami_pipeline_enabled)
      PROJECT_NAME      = local.fcvm_ami_project
      CONTROL_REGION    = var.aws_region
      CONTROL_ACCOUNT   = data.aws_caller_identity.current.account_id
      PUBLISH_FUNCTION  = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:fcvm-fleet-ami-publish"
      COMPUTE_REGION    = local.fcvm_ami_region
      STATE_PREFIX      = local.fcvm_ami_state_prefix
      FLEET_ROLE        = aws_iam_role.fcvm_ami_broker_fleet.arn
      WORKER_ROLE       = aws_iam_role.fcvm_ami_worker_fleet.arn
      FLEET_ACCOUNT     = local.fcvm_fleet_account_id
      BUILDER_PROFILE   = aws_iam_instance_profile.fcvm_ami_builder.arn
      SUBNET_ID         = sort(module.fcvm_fleet_east1.host_subnet_ids)[0]
      SECURITY_GROUP    = aws_security_group.fcvm_ami_builder.id
      CANONICAL_AMI     = data.aws_ami.fcvm_ami_canonical.id
      GUARD_SHA256      = local.fcvm_ami_guard_sha
      PIPELINE_BUCKET   = aws_s3_bucket.fcvm_ami_pipeline.id
      RELEASE_PARAMETER = "/fcvm-fleet/ami-release"
      LOCK_TABLE        = "ejc3-terraform-locks"
      LOCK_ID           = local.fcvm_stage_lock_id
      TEMPLATES_JSON    = jsonencode(local.fcvm_ami_templates)
    }
  }
}

resource "aws_lambda_alias" "fcvm_ami_request" {
  name             = "request"
  function_name    = aws_lambda_function.fcvm_ami_dispatch.function_name
  function_version = aws_lambda_function.fcvm_ami_dispatch.version
}

resource "aws_lambda_alias" "fcvm_ami_build" {
  name             = "build"
  function_name    = aws_lambda_function.fcvm_ami_dispatch.function_name
  function_version = aws_lambda_function.fcvm_ami_dispatch.version
}

resource "aws_codebuild_project" "fcvm_ami" {
  name                   = local.fcvm_ami_project
  description            = "Fixed four-slot GPU image factory; no Terraform or production credentials"
  service_role           = aws_iam_role.fcvm_ami_codebuild.arn
  build_timeout          = local.fcvm_ami_build_minutes
  queued_timeout         = 5
  concurrent_build_limit = 1
  artifacts { type = "NO_ARTIFACTS" }
  cache { type = "NO_CACHE" }
  source {
    type            = "GITHUB"
    location        = local.fcvm_ami_source
    git_clone_depth = 1
    buildspec       = file("${path.module}/fcvm-fleet-ami-pipeline/buildspec.yml")
  }
  environment {
    type            = "LINUX_CONTAINER"
    compute_type    = "BUILD_GENERAL1_SMALL"
    image           = "aws/codebuild/standard:7.0"
    privileged_mode = false
    environment_variable {
      name  = "FLEET_AMI_BROKER_ARN"
      value = aws_lambda_alias.fcvm_ami_build.arn
    }
    environment_variable {
      name  = "FLEET_AMI_WORKER_ROLE_ARN"
      value = aws_iam_role.fcvm_ami_worker_fleet.arn
    }
    environment_variable {
      name  = "FLEET_AMI_DOCUMENT"
      value = aws_ssm_document.fcvm_ami_build.name
    }
    environment_variable {
      name  = "FLEET_AMI_BUCKET"
      value = aws_s3_bucket.fcvm_ami_pipeline.id
    }
    environment_variable {
      name  = "FLEET_AMI_REGION"
      value = local.fcvm_ami_region
    }
    environment_variable {
      name  = "FLEET_AMI_SECRET_REGION"
      value = local.fcvm_ami_region
    }
    environment_variable {
      name  = "FLEET_AMI_ASSET_REGION"
      value = local.fcvm_fleet_home_region
    }
    environment_variable {
      name  = "FLEET_AMI_PUBLISHED_ASSET_BUCKET"
      value = aws_s3_bucket.fcvm_fleet_assets.id
    }
    environment_variable {
      name  = "FLEET_AMI_PUBLISHED_ASSET_REGION"
      value = local.fcvm_fleet_home_region
    }
    environment_variable {
      name  = "FLEET_AMI_TLS_BUILD_ARN"
      value = aws_secretsmanager_secret.fcvm_ami_tls["build"].arn
    }
    environment_variable {
      name  = "FLEET_AMI_TLS_TEST_ARN"
      value = aws_secretsmanager_secret.fcvm_ami_tls["test"].arn
    }
  }
  logs_config {
    cloudwatch_logs { group_name = aws_cloudwatch_log_group.fcvm_ami_codebuild.name }
  }
}

resource "aws_iam_role_policy" "fcvm_ami_builder_ssm" {
  provider = aws.fcvm_fleet_east1
  name     = "managed-instance-without-parameters"
  role     = aws_iam_role.fcvm_ami_builder.id
  policy   = aws_iam_policy.ssm_managed_instance.policy
}

resource "aws_iam_role_policy" "fcvm_ami_builder_data" {
  provider = aws.fcvm_fleet_east1
  name     = "own-instance-artifacts-and-dummy-tls"
  role     = aws_iam_role.fcvm_ami_builder.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], Resource = "${aws_s3_bucket.fcvm_ami_pipeline.arn}/builders/$${ec2:SourceInstanceARN}/*" },
    { Effect = "Allow", Action = "secretsmanager:GetSecretValue", Resource = [for s in aws_secretsmanager_secret.fcvm_ami_tls : s.arn] },
    { Effect = "Deny", Action = ["s3:DeleteObject", "s3:DeleteObjectVersion", "iam:*", "sts:AssumeRole*", "ssm:GetParameter*", "secretsmanager:BatchGetSecretValue"], Resource = "*" },
    { Effect = "Deny", Action = "secretsmanager:GetSecretValue", NotResource = [for s in aws_secretsmanager_secret.fcvm_ami_tls : s.arn] },
    { Effect = "Deny", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], NotResource = "${aws_s3_bucket.fcvm_ami_pipeline.arn}/builders/$${ec2:SourceInstanceARN}/*" }
  ] })
}

resource "aws_secretsmanager_secret_policy" "fcvm_ami_tls" {
  provider   = aws.fcvm_fleet_east1
  for_each   = aws_secretsmanager_secret.fcvm_ami_tls
  secret_arn = each.value.arn
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Deny", Principal = "*", Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"], Resource = each.value.arn,
    Condition = { ArnNotEquals = { "aws:PrincipalArn" = [aws_iam_role.fcvm_ami_builder.arn, aws_iam_role.fcvm_ami_worker_fleet.arn] } }
  }] })
}

resource "aws_iam_role_policy" "fcvm_ami_worker" {
  provider = aws.fcvm_fleet_east1
  name     = "sealed-build-session-only"
  role     = aws_iam_role.fcvm_ami_worker_fleet.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "OwnRunObjects", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], Resource = "${aws_s3_bucket.fcvm_ami_pipeline.arn}/runs/$${aws:PrincipalTag/FactoryBuild}/*" },
    # The broker's mandatory inline session policy narrows this to the four actual instance ARNs.
    { Sid = "ReadOwnedBuilderExports", Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.fcvm_ami_pipeline.arn}/builders/*" },
    { Sid = "OwnBuilderCommands", Effect = "Allow", Action = "ssm:SendCommand", Resource = "arn:aws:ec2:${local.fcvm_ami_region}:${local.fcvm_fleet_account_id}:instance/*",
    Condition = { StringEquals = { "ssm:resourceTag/Purpose" = local.fcvm_ami_builder_tag, "ssm:resourceTag/FactoryBuild" = "$${aws:PrincipalTag/FactoryBuild}" } } },
    { Effect = "Allow", Action = "ssm:SendCommand", Resource = aws_ssm_document.fcvm_ami_build.arn },
    { Effect = "Allow", Action = "ssm:DescribeInstanceInformation", Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = local.fcvm_ami_region } } },
    { Effect = "Allow", Action = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"], Resource = [for s in aws_secretsmanager_secret.fcvm_ami_tls : s.arn] },
    { Effect = "Deny", Action = ["iam:*", "sts:AssumeRole*", "ec2:*", "ssm:GetParameter*", "ssm:GetCommandInvocation", "ssm:ListCommandInvocations", "s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "*" }
  ] })
}

resource "aws_s3_bucket_policy" "fcvm_ami_pipeline" {
  provider = aws.ohio
  bucket   = aws_s3_bucket.fcvm_ami_pipeline.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "TLSOnly", Effect = "Deny", Principal = "*", Action = "s3:*", Resource = [aws_s3_bucket.fcvm_ami_pipeline.arn, "${aws_s3_bucket.fcvm_ami_pipeline.arn}/*"], Condition = { Bool = { "aws:SecureTransport" = "false" } } },
    { Sid = "FleetWorkers", Effect = "Allow", Principal = { AWS = [aws_iam_role.fcvm_ami_builder.arn, aws_iam_role.fcvm_ami_worker_fleet.arn, aws_iam_role.fcvm_ami_publish_fleet.arn] },
    Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject"], Resource = ["${aws_s3_bucket.fcvm_ami_pipeline.arn}/runs/*", "${aws_s3_bucket.fcvm_ami_pipeline.arn}/builders/*"] }
  ] })
}

locals {
  fcvm_ami_instance_arn = "arn:aws:ec2:${local.fcvm_ami_region}:${local.fcvm_fleet_account_id}:instance/*"
  fcvm_ami_created_arns = [for kind in ["instance", "volume", "network-interface"] : "arn:aws:ec2:${local.fcvm_ami_region}:${local.fcvm_fleet_account_id}:${kind}/*"]
  fcvm_ami_image_arns   = ["arn:aws:ec2:${local.fcvm_ami_region}::image/*", "arn:aws:ec2:${local.fcvm_ami_region}::snapshot/*"]
  fcvm_ami_state_arn    = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.fcvm_ami_state_prefix}/*"
}

resource "aws_iam_role_policy" "fcvm_ami_broker_fleet" {
  provider = aws.fcvm_fleet_east1
  name     = "four-fixed-builder-slots"
  role     = aws_iam_role.fcvm_ami_broker_fleet.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Sid = "Metadata", Effect = "Allow", Action = ["ec2:DescribeImages", "ec2:DescribeInstances", "ec2:DescribeSnapshots", "ec2:DescribeLaunchTemplateVersions"], Resource = "*" },
    { Sid = "CanonicalBase", Effect = "Allow", Action = "ec2:RunInstances", Resource = "arn:aws:ec2:${local.fcvm_ami_region}::image/${data.aws_ami.fcvm_ami_canonical.id}" },
    { Sid = "PrivateFactoryImages", Effect = "Allow", Action = "ec2:RunInstances", Resource = "arn:aws:ec2:${local.fcvm_ami_region}::image/*",
    Condition = { StringEquals = { "ec2:Owner" = local.fcvm_fleet_account_id, "ec2:ResourceTag/Purpose" = "fcvm-fleet", "ec2:ResourceTag/PipelineSanitized" = "true", "ec2:ResourceTag/GuardHelperSHA256" = local.fcvm_ami_guard_sha }, Bool = { "ec2:Public" = "false" } } },
    { Sid = "FixedNetwork", Effect = "Allow", Action = "ec2:RunInstances", Resource = ["arn:aws:ec2:${local.fcvm_ami_region}:${local.fcvm_fleet_account_id}:subnet/${sort(module.fcvm_fleet_east1.host_subnet_ids)[0]}", aws_security_group.fcvm_ami_builder.arn] },
    { Sid = "FixedInstance", Effect = "Allow", Action = "ec2:RunInstances", Resource = local.fcvm_ami_instance_arn,
    Condition = { StringEquals = { "aws:RequestTag/Purpose" = local.fcvm_ami_builder_tag, "ec2:InstanceType" = ["g4dn.metal", "g4dn.xlarge"], "ec2:InstanceProfile" = aws_iam_instance_profile.fcvm_ami_builder.arn, "ec2:MetadataHttpTokens" = "required" }, NumericEquals = { "ec2:MetadataHttpPutResponseHopLimit" = 1 } } },
    { Sid = "FixedRoot", Effect = "Allow", Action = "ec2:RunInstances", Resource = "arn:aws:ec2:${local.fcvm_ami_region}:${local.fcvm_fleet_account_id}:volume/*",
    Condition = { StringEquals = { "aws:RequestTag/Purpose" = local.fcvm_ami_builder_tag, "ec2:VolumeType" = "gp3" }, Bool = { "ec2:Encrypted" = "true" }, NumericLessThanEquals = { "ec2:VolumeSize" = 64 } } },
    { Sid = "TaggedInterface", Effect = "Allow", Action = "ec2:RunInstances", Resource = "arn:aws:ec2:${local.fcvm_ami_region}:${local.fcvm_fleet_account_id}:network-interface/*", Condition = { StringEquals = { "aws:RequestTag/Purpose" = local.fcvm_ami_builder_tag } } },
    { Sid = "TagAtLaunch", Effect = "Allow", Action = "ec2:CreateTags", Resource = local.fcvm_ami_created_arns,
    Condition = { StringEquals = { "ec2:CreateAction" = "RunInstances" }, "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Name", "Purpose", "FactoryBuild", "FactorySlot", "SourceSHA"] } } },
    { Sid = "OwnSlotsOnly", Effect = "Allow", Action = ["ec2:StopInstances", "ec2:StartInstances", "ec2:RebootInstances", "ec2:TerminateInstances", "ec2:CreateImage"], Resource = local.fcvm_ami_instance_arn,
    Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = local.fcvm_ami_builder_tag } } },
    { Sid = "CreateCandidate", Effect = "Allow", Action = "ec2:CreateImage", Resource = local.fcvm_ami_image_arns, Condition = { StringEquals = { "aws:RequestTag/Purpose" = "fcvm-fleet" } } },
    { Sid = "TagCandidateAtCreation", Effect = "Allow", Action = "ec2:CreateTags", Resource = local.fcvm_ami_image_arns,
    Condition = { StringEquals = { "ec2:CreateAction" = "CreateImage" }, "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Purpose", "PipelineBuildId", "FleetImageKind", "PipelineSanitized", "SourceCommit", "InputSHA256", "GuardHelperSHA256"] } } },
    { Sid = "OnlyBuilderProfile", Effect = "Allow", Action = "iam:PassRole", Resource = aws_iam_role.fcvm_ami_builder.arn, Condition = { StringEquals = { "iam:PassedToService" = "ec2.amazonaws.com" } } },
    { Sid = "FactoryImageRetention", Effect = "Allow", Action = ["ec2:DeregisterImage", "ec2:DeleteSnapshot"], Resource = local.fcvm_ami_image_arns,
    Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = "fcvm-fleet", "ec2:ResourceTag/PipelineSanitized" = "true" } } },
    { Sid = "NoOtherProfile", Effect = "Deny", Action = "iam:PassRole", NotResource = aws_iam_role.fcvm_ami_builder.arn },
    # The fixed broker pins InstanceId from an immutable slot; workers never get this unscoped read.
    { Sid = "OwnCommandStatusBroker", Effect = "Allow", Action = "ssm:GetCommandInvocation", Resource = "*", Condition = { StringEquals = { "aws:RequestedRegion" = local.fcvm_ami_region } } },
    { Effect = "Allow", Action = "ssm:GetParameter", Resource = aws_ssm_parameter.fcvm_ami_release.arn },
    { Effect = "Deny", Action = ["secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue", "s3:GetObject*", "sts:AssumeRole*", "iam:Create*", "iam:Put*", "iam:Attach*", "iam:Update*"], Resource = "*" }
  ] })
}

resource "aws_iam_role_policy" "fcvm_ami_dispatch" {
  name = "queue-and-fixed-broker"
  role = aws_iam_role.fcvm_ami_dispatch.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter", "ssm:GetParametersByPath"], Resource = local.fcvm_ami_state_arn },
    { Effect = "Allow", Action = ["codebuild:StartBuild", "codebuild:ListBuildsForProject"], Resource = aws_codebuild_project.fcvm_ami.arn },
    { Effect = "Allow", Action = ["codebuild:BatchGetBuilds", "codebuild:StopBuild"], Resource = "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:build/${local.fcvm_ami_project}:*" },
    { Effect = "Allow", Action = "sts:AssumeRole", Resource = [aws_iam_role.fcvm_ami_broker_fleet.arn, aws_iam_role.fcvm_ami_worker_fleet.arn] },
    { Effect = "Allow", Action = "sts:TagSession", Resource = aws_iam_role.fcvm_ami_worker_fleet.arn },
    { Sid = "RedriveOnlyTheFixedPublisher", Effect = "Allow", Action = "lambda:InvokeFunction", Resource = "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:fcvm-fleet-ami-publish" },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.fcvm_ami_dispatch.arn}:*" }
    , { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:DeleteItem"], Resource = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/ejc3-terraform-locks",
    Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [local.fcvm_stage_lock_id] } } }
  ] })
}

resource "aws_iam_role_policy" "fcvm_ami_codebuild" {
  name = "invoke-fixed-build-alias-only"
  role = aws_iam_role.fcvm_ami_codebuild.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_alias.fcvm_ami_build.arn },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.fcvm_ami_codebuild.arn}:*" },
    { Effect = "Deny", Action = ["iam:*", "ec2:*", "ssm:*", "secretsmanager:*", "sts:AssumeRole*", "s3:*"], Resource = "*" }
  ] })
}

resource "github_repository_environment" "fcvm_ami" {
  provider          = github.agents_md
  repository        = "fcvm"
  environment       = "fleet-ami-publish"
  can_admins_bypass = false
  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "fcvm_ami" {
  provider       = github.agents_md
  repository     = "fcvm"
  environment    = github_repository_environment.fcvm_ami.environment
  branch_pattern = "main"
}

resource "aws_iam_role" "fcvm_ami_ci" {
  name = "fcvm-fleet-ami-request-ci"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect    = "Allow", Principal = { Federated = aws_iam_openid_connect_provider.github.arn }, Action = "sts:AssumeRoleWithWebIdentity",
    Condition = { StringEquals = { "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com", "token.actions.githubusercontent.com:sub" = "repo:ejc3/fcvm:environment:fleet-ami-publish" } }
  }] })
}

resource "aws_iam_role_policy" "fcvm_ami_ci" {
  name = "request-no-aws-administration"
  role = aws_iam_role.fcvm_ami_ci.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_alias.fcvm_ami_request.arn },
    { Effect = "Deny", Action = ["iam:*", "sts:AssumeRole*", "ec2:*", "ssm:*", "secretsmanager:*", "s3:*", "codebuild:*", "logs:*"], Resource = "*" }
  ] })
}

resource "github_actions_environment_secret" "fcvm_ami" {
  provider        = github.agents_md
  for_each        = { AWS_ROLE_ARN = aws_iam_role.fcvm_ami_ci.arn, AMI_PIPELINE_DISPATCH = aws_lambda_alias.fcvm_ami_request.arn }
  repository      = "fcvm"
  environment     = github_repository_environment.fcvm_ami.environment
  secret_name     = each.key
  plaintext_value = each.value
}

resource "aws_cloudwatch_event_rule" "fcvm_ami_reap" {
  name                = "fcvm-fleet-image-factory-expiry"
  description         = "Independently terminate expired/stopped factory slots and advance the bounded queue"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "fcvm_ami_reap" {
  rule = aws_cloudwatch_event_rule.fcvm_ami_reap.name
  arn  = aws_lambda_function.fcvm_ami_dispatch.arn
}

resource "aws_lambda_permission" "fcvm_ami_reap" {
  statement_id  = "FactoryExpirySchedule"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fcvm_ami_dispatch.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.fcvm_ami_reap.arn
}

resource "aws_iam_role" "fcvm_ami_publish" {
  name = "fcvm-fleet-ami-publish"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}

resource "aws_iam_role" "fcvm_ami_publish_fleet" {
  provider = aws.fcvm_fleet_east1
  name     = "fcvm-fleet-ami-publish"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { AWS = aws_iam_role.fcvm_ami_publish.arn }, Action = "sts:AssumeRole"
  }] })
}

resource "aws_cloudwatch_log_group" "fcvm_ami_publish" {
  name              = "/aws/lambda/fcvm-fleet-ami-publish"
  retention_in_days = 14
}

resource "aws_iam_role_policy" "fcvm_ami_publish" {
  name = "verified-build-proof-and-backend-mutex-only"
  role = aws_iam_role.fcvm_ami_publish.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "codebuild:BatchGetBuilds", Resource = "arn:aws:codebuild:${var.aws_region}:${data.aws_caller_identity.current.account_id}:build/${local.fcvm_ami_project}:*" },
    { Effect = "Allow", Action = "ssm:GetParameter", Resource = local.fcvm_ami_state_arn },
    { Effect = "Allow", Action = "ssm:PutParameter", Resource = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.fcvm_ami_state_prefix}/requests/*", "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.fcvm_ami_state_prefix}/publications/*"] },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion", "s3:GetObjectAttributes", "s3:GetObjectVersionAttributes"], Resource = "${aws_s3_bucket.fcvm_ami_pipeline.arn}/runs/*" },
    { Effect = "Allow", Action = "sts:AssumeRole", Resource = aws_iam_role.fcvm_ami_publish_fleet.arn },
    { Effect = "Allow", Action = ["dynamodb:PutItem", "dynamodb:DeleteItem"], Resource = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table/ejc3-terraform-locks", Condition = { "ForAllValues:StringEquals" = { "dynamodb:LeadingKeys" = [local.fcvm_stage_lock_id] } } },
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.fcvm_ami_publish.arn}:*" },
    { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.fcvm_ami_dispatch.arn }
  ] })
}

resource "aws_iam_role_policy" "fcvm_ami_publish_fleet" {
  provider = aws.fcvm_fleet_east1
  name     = "image-only-four-template-release"
  role     = aws_iam_role.fcvm_ami_publish_fleet.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["ec2:DescribeImages", "ec2:DescribeSnapshots", "ec2:DescribeInstances", "ec2:DescribeSpotInstanceRequests", "ec2:DescribeLaunchTemplateVersions", "ec2:DescribeLaunchTemplates"], Resource = "*" },
    { Effect = "Allow", Action = ["ec2:CreateLaunchTemplateVersion", "ec2:ModifyLaunchTemplate", "ec2:DeleteLaunchTemplateVersions"],
    Resource = [for template in local.fcvm_ami_templates : "arn:aws:ec2:${template.region}:${local.fcvm_fleet_account_id}:launch-template/${template.id}"] },
    { Effect = "Allow", Action = "ec2:CreateTags", Resource = local.fcvm_ami_image_arns,
    Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = "fcvm-fleet", "ec2:ResourceTag/PipelineSanitized" = "true", "aws:RequestTag/FleetImageValidated" = "true" }, "ForAllValues:StringEquals" = { "aws:TagKeys" = ["FleetImageValidated"] } } },
    { Effect = "Allow", Action = "ec2:TerminateInstances", Resource = local.fcvm_ami_instance_arn,
    Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = ["fcvm-fleet-host", "fcvm-fleet-tile"], "ec2:InstanceType" = ["g4dn.metal", "g4dn.xlarge"], "aws:RequestedRegion" = local.fcvm_ami_region } } },
    { Sid      = "CancelPersistentTileRequestsBeforeRetirement", Effect = "Allow", Action = "ec2:CancelSpotInstanceRequests",
      Resource = "arn:aws:ec2:${local.fcvm_ami_region}:${local.fcvm_fleet_account_id}:spot-instances-request/*",
    Condition = { StringEquals = { "ec2:ResourceTag/Purpose" = "fcvm-fleet-tile", "aws:RequestedRegion" = local.fcvm_ami_region } } },
    { Effect = "Allow", Action = ["ssm:GetParameter", "ssm:PutParameter"], Resource = aws_ssm_parameter.fcvm_ami_release.arn },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"], Resource = "${aws_s3_bucket.fcvm_ami_pipeline.arn}/runs/*" },
    { Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:GetObjectAttributes"], Resource = "${aws_s3_bucket.fcvm_fleet_assets.arn}/factory/*" },
    { Effect = "Allow", Action = "s3:ListBucket", Resource = aws_s3_bucket.fcvm_fleet_assets.arn, Condition = { StringLike = { "s3:prefix" = "factory/*" } } },
    { Effect = "Deny", Action = ["iam:*", "sts:AssumeRole*", "secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue", "ssm:SendCommand", "ssm:GetCommandInvocation", "s3:DeleteObject", "s3:DeleteObjectVersion"], Resource = "*" }
  ] })
}

resource "aws_lambda_function" "fcvm_ami_publish" {
  function_name                  = "fcvm-fleet-ami-publish"
  role                           = aws_iam_role.fcvm_ami_publish.arn
  runtime                        = "python3.12"
  handler                        = "publish.lambda_handler"
  timeout                        = 900
  memory_size                    = 512
  reserved_concurrent_executions = 1
  filename                       = data.archive_file.fcvm_ami_dispatch.output_path
  source_code_hash               = data.archive_file.fcvm_ami_dispatch.output_base64sha256
  environment {
    variables = {
      PROJECT_NAME      = local.fcvm_ami_project
      CONTROL_REGION    = var.aws_region
      COMPUTE_REGION    = local.fcvm_ami_region
      CONTROL_ACCOUNT   = data.aws_caller_identity.current.account_id
      FLEET_ACCOUNT     = local.fcvm_fleet_account_id
      FLEET_ROLE        = aws_iam_role.fcvm_ami_publish_fleet.arn
      STATE_PREFIX      = local.fcvm_ami_state_prefix
      PIPELINE_BUCKET   = aws_s3_bucket.fcvm_ami_pipeline.id
      PIPELINE_REGION   = local.fcvm_fleet_home_region
      RELEASE_PARAMETER = aws_ssm_parameter.fcvm_ami_release.name
      GUARD_SHA256      = local.fcvm_ami_guard_sha
      TEMPLATES_JSON    = jsonencode(local.fcvm_ami_templates)
      LOCK_TABLE        = "ejc3-terraform-locks"
      LOCK_ID           = local.fcvm_stage_lock_id
      ASSET_BUCKET      = aws_s3_bucket.fcvm_fleet_assets.id
      ASSET_REGION      = local.fcvm_fleet_home_region
      DISPATCH_FUNCTION = aws_lambda_function.fcvm_ami_dispatch.arn
    }
  }
}

resource "aws_cloudwatch_event_rule" "fcvm_ami_success" {
  name = "fcvm-fleet-image-factory-success"
  event_pattern = jsonencode({ source = ["aws.codebuild"], "detail-type" = ["CodeBuild Build State Change"],
  account = [data.aws_caller_identity.current.account_id], detail = { "build-status" = ["SUCCEEDED"], "project-name" = [local.fcvm_ami_project] } })
}

resource "aws_cloudwatch_event_target" "fcvm_ami_success" {
  rule = aws_cloudwatch_event_rule.fcvm_ami_success.name
  arn  = aws_lambda_function.fcvm_ami_publish.arn
  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 185
  }
}

resource "aws_lambda_permission" "fcvm_ami_success" {
  statement_id  = "OnlyFactorySuccessfulBuilds"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.fcvm_ami_publish.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.fcvm_ami_success.arn
}

# Owner-authorized critical-patch administration, 2026-11-01.
# The model has no direct SSM/state/IAM permissions. The broker independently
# verifies current CRITICAL findings before accepting a target and journals commands.
# IMPORTANT: root commands on either jumpbox are administration authority, not
# a "narrow" host grant. This dedicated control plane never delegates to dev roles.
locals {
  patchdoctor_regions = ["us-west-1", "us-west-2", "us-east-1"]
  patchdoctor_targets = ["jumpbox", "jumpbox-2", "fcvm-metal-arm", "fcvm-metal-x86", "nextjs-ct", "nextjs-dev", "claude-master-server"]
  patchdoctor_model   = "us.anthropic.claude-opus-5-5"
  patchdoctor_env = {
    TABLE_NAME            = aws_dynamodb_table.patchdoctor.name
    PROJECT_NAME          = "patchdoctor"
    BUCKET_NAME           = aws_s3_bucket.patchdoctor.id
    SNS_TOPIC_ARN         = aws_sns_topic.cost_alerts.arn
    ACCOUNT_ID            = data.aws_caller_identity.current.account_id
    ALLOWED_REGIONS_JSON  = jsonencode(local.patchdoctor_regions)
    TARGET_NAMES_JSON     = jsonencode(local.patchdoctor_targets)
    COMMAND_DOCUMENT_NAME = "PatchDoctor-RunCommand"
    INSPECT_DOCUMENT_NAME = "PatchDoctor-Inspect"
  }
  patchdoctor_log_names = {
    control   = "/aws/lambda/patchdoctor"
    admission = "/aws/lambda/patchdoctor-admit"
    broker    = "/aws/lambda/patchdoctor-broker"
    worker    = "/aws/codebuild/patchdoctor"
  }
  patchdoctor_run_document = jsonencode({
    schemaVersion = "2.2"
    description   = "PatchDoctor root commands: only the administration broker may invoke this document on verified critical targets."
    parameters = {
      commands         = { type = "StringList", description = "Incident-scoped script recorded durably before execution." }
      executionTimeout = { type = "String", default = "900", allowedPattern = "([1-9][0-9]{0,1}|[1-8][0-9]{2}|900)" }
    }
    mainSteps = [{
      action       = "aws:runShellScript", name = "criticalPatch",
      precondition = { StringEquals = ["platformType", "Linux"] },
      inputs       = { runCommand = "{{ commands }}", timeoutSeconds = "{{ executionTimeout }}" }
    }]
  })
  patchdoctor_inspect_document = jsonencode({
    schemaVersion = "2.2"
    description   = "Read-only Linux patch baseline; no credentials or user files."
    mainSteps = [{
      action       = "aws:runShellScript", name = "baseline",
      precondition = { StringEquals = ["platformType", "Linux"] },
      inputs = {
        timeoutSeconds = "60"
        runCommand = [
          "set -eu",
          "uname -r",
          "if command -v dpkg-query >/dev/null 2>&1; then dpkg-query -W 'linux-image*' 'openssl' 'libc6' 2>/dev/null || true; fi",
          "if [ -x /usr/local/sbin/cve-2026-98432-mitigation ]; then /usr/local/sbin/cve-2026-98432-mitigation --check; fi",
          "test ! -e /var/run/reboot-required || echo reboot-required",
        ]
      }
    }]
  })
}

resource "aws_dynamodb_table" "patchdoctor" {
  name                        = "patchdoctor"
  billing_mode                = "PAY_PER_REQUEST"
  hash_key                    = "pk"
  deletion_protection_enabled = true
  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "record_kind"
    type = "S"
  }
  attribute {
    name = "created_at"
    type = "N"
  }
  global_secondary_index {
    name            = "active-cases"
    hash_key        = "record_kind"
    range_key       = "created_at"
    projection_type = "KEYS_ONLY"
  }
  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true }
  tags = { Managed = "terraform", Purpose = "critical-patch-administration" }
}

resource "aws_kms_key" "patchdoctor" {
  description             = "Private PatchDoctor workspaces, command receipts and potentially sensitive agent transcripts"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}
resource "aws_kms_alias" "patchdoctor" {
  name          = "alias/patchdoctor"
  target_key_id = aws_kms_key.patchdoctor.key_id
}
resource "aws_s3_bucket" "patchdoctor" {
  bucket = format("ejc3-patchdoctor-%s", data.aws_caller_identity.current.account_id)
  tags   = { Managed = "terraform", Purpose = "critical-patch-administration" }
}
resource "aws_s3_bucket_public_access_block" "patchdoctor" {
  bucket                  = aws_s3_bucket.patchdoctor.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_versioning" "patchdoctor" {
  bucket = aws_s3_bucket.patchdoctor.id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "patchdoctor" {
  bucket = aws_s3_bucket.patchdoctor.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.patchdoctor.arn
    }
    bucket_key_enabled = true
  }
}
resource "aws_s3_bucket_policy" "patchdoctor" {
  bucket = aws_s3_bucket.patchdoctor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "TLSOnly", Effect = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [aws_s3_bucket.patchdoctor.arn, format("%s/*", aws_s3_bucket.patchdoctor.arn)]
      Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
    }]
  })
}

data "archive_file" "patchdoctor_control" {
  type        = "zip"
  output_path = format("%s/.terraform/patchdoctor-control.zip", path.module)
  source {
    filename = "index.py"
    content  = file(format("%s/patchdoctor/control.py", path.module))
  }
  source {
    filename = "triage.py"
    content  = file(format("%s/patchdoctor/triage.py", path.module))
  }
}
data "archive_file" "patchdoctor_worker" {
  type        = "zip"
  output_path = format("%s/.terraform/patchdoctor-worker.zip", path.module)
  source {
    filename = "worker.py"
    content  = file(format("%s/patchdoctor/worker.py", path.module))
  }
  source {
    filename = "host.py"
    content  = file(format("%s/patchdoctor/host.py", path.module))
  }
  source {
    filename = "prompt.txt"
    content  = file(format("%s/patchdoctor/prompt.txt", path.module))
  }
  source {
    filename = "install-claude.sh"
    content  = file(format("%s/patchdoctor/install-claude.sh", path.module))
  }
}
resource "aws_s3_object" "patchdoctor_worker" {
  bucket                 = aws_s3_bucket.patchdoctor.id
  key                    = format("runtime/%s.zip", data.archive_file.patchdoctor_worker.output_sha256)
  source                 = data.archive_file.patchdoctor_worker.output_path
  source_hash            = data.archive_file.patchdoctor_worker.output_base64sha256
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.patchdoctor.arn
  bucket_key_enabled     = true
  depends_on             = [aws_s3_bucket_versioning.patchdoctor, aws_s3_bucket_server_side_encryption_configuration.patchdoctor]
}

resource "aws_cloudwatch_log_group" "patchdoctor" {
  for_each          = local.patchdoctor_log_names
  name              = each.value
  retention_in_days = 30
}
resource "aws_iam_role" "patchdoctor" {
  for_each = { control = "lambda.amazonaws.com", broker = "lambda.amazonaws.com", admission = "lambda.amazonaws.com", worker = "codebuild.amazonaws.com" }
  name     = format("patchdoctor-%s", each.key)
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Service = each.value }, Action = "sts:AssumeRole"
      Condition = each.key == "worker" ? {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
        ArnEquals    = { "aws:SourceArn" = format("arn:aws:codebuild:%s:%s:project/patchdoctor", var.aws_region, data.aws_caller_identity.current.account_id) }
      } : {}
    }]
  })
}
resource "aws_iam_role_policy" "patchdoctor_common" {
  for_each = aws_iam_role.patchdoctor
  name     = "private-patchdoctor-records"
  role     = each.value.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Effect   = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = format("%s:*", trimsuffix(aws_cloudwatch_log_group.patchdoctor[each.key].arn, ":*"))
      },
      ], [for statement in [
        {
          Effect   = "Allow", Action = ["s3:GetObject", "s3:GetObjectVersion"]
          Resource = [for prefix in(each.key == "worker" ? ["runtime", "cases"] : ["cases", "commands"]) : format("%s/%s/*", aws_s3_bucket.patchdoctor.arn, prefix)]
        },
        {
          Effect   = "Allow", Action = ["s3:PutObject"]
          Resource = [for prefix in ["cases", "commands"] : format("%s/%s/*", aws_s3_bucket.patchdoctor.arn, prefix) if each.key != "worker" || prefix == "cases"]
        },
        {
          Effect   = "Allow", Action = ["kms:Decrypt", "kms:GenerateDataKey"]
          Resource = aws_kms_key.patchdoctor.arn
          Condition = { StringEquals = {
            "kms:ViaService"                   = format("s3.%s.amazonaws.com", var.aws_region)
            "kms:EncryptionContext:aws:s3:arn" = aws_s3_bucket.patchdoctor.arn
          } }
        }
        ] : statement if each.key != "admission"], [for statement in [
        {
          Effect = "Allow", Action = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
          Resource = [
            aws_dynamodb_table.patchdoctor.arn,
            format("%s/index/active-cases", aws_dynamodb_table.patchdoctor.arn)
          ]
        },
        { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.cost_alerts.arn },
        {
          Effect    = "Allow", Action = ["inspector2:ListFindings", "securityhub:GetFindings", "ec2:DescribeInstances"]
          Resource  = "*"
          Condition = { StringEquals = { "aws:RequestedRegion" = local.patchdoctor_regions } }
        },
        ] : statement if each.key != "worker"], [for statement in [
        {
          Effect = "Allow", Action = ["codebuild:BatchGetBuilds", "codebuild:ListBuildsForProject"]
          Resource = [
            format("arn:aws:codebuild:%s:%s:project/patchdoctor", var.aws_region, data.aws_caller_identity.current.account_id),
            format("arn:aws:codebuild:%s:%s:build/patchdoctor:*", var.aws_region, data.aws_caller_identity.current.account_id)
          ]
        }
    ] : statement if contains(["control", "broker"], each.key)])
  })
}
resource "aws_iam_role_policy" "patchdoctor_start" {
  role = aws_iam_role.patchdoctor["control"].id
  name = "start-fixed-doctor"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow", Action = ["codebuild:StartBuild", "codebuild:StopBuild"],
        Resource = format("arn:aws:codebuild:%s:%s:project/patchdoctor", var.aws_region, data.aws_caller_identity.current.account_id)
      },
      {
        # Reconcile existing commands after their worker dies; this role cannot send commands.
        Effect    = "Allow", Action = ["ssm:GetCommandInvocation", "ssm:ListCommands", "ssm:ListCommandInvocations"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.patchdoctor_regions } }
      }
    ]
  })
}

# Haiku is advisory only. The serial controller alone invokes it; admission and
# dev hosts cannot use this identity or acquire host-command authority.
resource "aws_iam_role_policy" "patchdoctor_triage" {
  role = aws_iam_role.patchdoctor["control"].id
  name = "advisory-haiku-triage"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Action = ["bedrock:InvokeModel"]
      Resource = [
        format("arn:aws:bedrock:us-east-1:%s:inference-profile/us.anthropic.claude-haiku-5-5", data.aws_caller_identity.current.account_id),
        "arn:aws:bedrock:us-*::foundation-model/anthropic.claude-haiku-5-5"
      ]
    }]
  })
}
resource "aws_iam_role_policy" "patchdoctor_worker" {
  role = aws_iam_role.patchdoctor["worker"].id
  name = "opus-and-incident-broker"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.patchdoctor_broker.arn },
      {
        Effect = "Allow", Action = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
        Resource = [
          format("arn:aws:bedrock:us-east-1:%s:inference-profile/us.anthropic.claude-opus-5-5", data.aws_caller_identity.current.account_id),
          "arn:aws:bedrock:us-*::foundation-model/anthropic.claude-opus-5-5"
        ]
      },
      {
        Effect    = "Allow", Action = "aws-marketplace:Subscribe", Resource = "*"
        Condition = { "ForAnyValue:StringEquals" = { "aws-marketplace:ProductId" = ["prod-e5xo6z26ie45g"] } }
      },
      { Effect = "Allow", Action = "aws-marketplace:ViewSubscriptions", Resource = "*" }
    ]
  })
}
resource "aws_iam_role_policy" "patchdoctor_commands" {
  role = aws_iam_role.patchdoctor["broker"].id
  name = "verified-critical-host-commands"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow", Action = "ssm:SendCommand"
        Resource = flatten([for region in local.patchdoctor_regions : [
          format("arn:aws:ssm:%s:%s:document/PatchDoctor-RunCommand", region, data.aws_caller_identity.current.account_id),
          format("arn:aws:ssm:%s:%s:document/PatchDoctor-Inspect", region, data.aws_caller_identity.current.account_id)
        ]])
      },
      {
        Effect    = "Allow", Action = "ssm:SendCommand"
        Resource  = [for region in local.patchdoctor_regions : format("arn:aws:ec2:%s:%s:instance/*", region, data.aws_caller_identity.current.account_id)]
        Condition = { StringEquals = { "ssm:resourceTag/Name" = local.patchdoctor_targets } }
      },
      {
        Effect    = "Allow", Action = ["ssm:GetCommandInvocation", "ssm:ListCommands", "ssm:ListCommandInvocations"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.patchdoctor_regions } }
      }
    ]
  })
}

resource "aws_ssm_document" "patchdoctor" {
  for_each      = { run = local.patchdoctor_run_document, inspect = local.patchdoctor_inspect_document }
  name          = each.key == "run" ? "PatchDoctor-RunCommand" : "PatchDoctor-Inspect"
  document_type = "Command"
  content       = each.value
}
resource "aws_ssm_document" "patchdoctor_west2" {
  provider      = aws.west2
  for_each      = { run = local.patchdoctor_run_document, inspect = local.patchdoctor_inspect_document }
  name          = each.key == "run" ? "PatchDoctor-RunCommand" : "PatchDoctor-Inspect"
  document_type = "Command"
  content       = each.value
}
resource "aws_ssm_document" "patchdoctor_east1" {
  provider      = aws.dr
  for_each      = { run = local.patchdoctor_run_document, inspect = local.patchdoctor_inspect_document }
  name          = each.key == "run" ? "PatchDoctor-RunCommand" : "PatchDoctor-Inspect"
  document_type = "Command"
  content       = each.value
}

resource "aws_lambda_function" "patchdoctor_broker" {
  function_name                  = "patchdoctor-broker"
  role                           = aws_iam_role.patchdoctor["broker"].arn
  runtime                        = "python3.12"
  handler                        = "index.broker"
  filename                       = data.archive_file.patchdoctor_control.output_path
  source_code_hash               = data.archive_file.patchdoctor_control.output_base64sha256
  timeout                        = 60
  memory_size                    = 256
  reserved_concurrent_executions = 2
  environment { variables = local.patchdoctor_env }
  depends_on = [aws_iam_role_policy.patchdoctor_common, aws_iam_role_policy.patchdoctor_commands, aws_cloudwatch_log_group.patchdoctor]
}
resource "aws_lambda_function" "patchdoctor" {
  function_name                  = "patchdoctor"
  role                           = aws_iam_role.patchdoctor["control"].arn
  runtime                        = "python3.12"
  handler                        = "index.handler"
  filename                       = data.archive_file.patchdoctor_control.output_path
  source_code_hash               = data.archive_file.patchdoctor_control.output_base64sha256
  timeout                        = 60
  memory_size                    = 256
  reserved_concurrent_executions = 1
  environment { variables = merge(local.patchdoctor_env, { BROKER_ARN = aws_lambda_function.patchdoctor_broker.arn }) }
  depends_on = [aws_iam_role_policy.patchdoctor_common, aws_iam_role_policy.patchdoctor_start, aws_iam_role_policy.patchdoctor_triage, aws_cloudwatch_log_group.patchdoctor]
}

resource "aws_iam_role_policy" "patchdoctor_wake" {
  role = aws_iam_role.patchdoctor["admission"].id
  name = "wake-serial-controller"
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.patchdoctor.arn }]
  })
}
resource "aws_lambda_function" "patchdoctor_admission" {
  function_name                  = "patchdoctor-admit"
  role                           = aws_iam_role.patchdoctor["admission"].arn
  runtime                        = "python3.12"
  handler                        = "index.admission"
  filename                       = data.archive_file.patchdoctor_control.output_path
  source_code_hash               = data.archive_file.patchdoctor_control.output_base64sha256
  timeout                        = 60
  memory_size                    = 256
  reserved_concurrent_executions = 10
  environment { variables = merge(local.patchdoctor_env, { CONTROL_ARN = aws_lambda_function.patchdoctor.arn }) }
  depends_on = [aws_iam_role_policy.patchdoctor_common, aws_iam_role_policy.patchdoctor_queue, aws_iam_role_policy.patchdoctor_wake, aws_cloudwatch_log_group.patchdoctor]
}

resource "aws_codebuild_project" "patchdoctor" {
  name                   = "patchdoctor"
  service_role           = aws_iam_role.patchdoctor["worker"].arn
  build_timeout          = 60
  queued_timeout         = 5
  concurrent_build_limit = 1
  artifacts { type = "NO_ARTIFACTS" }
  environment {
    compute_type                = "BUILD_GENERAL1_SMALL"
    image                       = "aws/codebuild/standard:7.0"
    type                        = "LINUX_CONTAINER"
    privileged_mode             = false
    image_pull_credentials_type = "CODEBUILD"
    dynamic "environment_variable" {
      for_each = {
        PATCHDOCTOR_BROKER_ARN                   = aws_lambda_function.patchdoctor_broker.arn
        PATCHDOCTOR_BUCKET_NAME                  = aws_s3_bucket.patchdoctor.id
        PATCHDOCTOR_CONTROL_REGION               = var.aws_region
        PATCHDOCTOR_BUNDLE_KEY                   = aws_s3_object.patchdoctor_worker.key
        PATCHDOCTOR_BUNDLE_VERSION               = aws_s3_object.patchdoctor_worker.version_id
        PATCHDOCTOR_BUNDLE_SHA256                = data.archive_file.patchdoctor_worker.output_sha256
        CLAUDE_VERSION                           = "2.1.316"
        CLAUDE_SHA256                            = "fd13c70000fec3f2116dfbcbb6121210b4a29328bf0e46210a59dd594a67d824"
        CLAUDE_CODE_USE_BEDROCK                  = "1"
        DISABLE_AUTOUPDATER                      = "1"
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
      }
      content {
        name  = environment_variable.key
        value = environment_variable.value
      }
    }
  }
  source {
    type = "NO_SOURCE"
    buildspec = yamlencode({
      version = "0.2"
      env     = { shell = "bash" }
      phases = {
        install = { commands = [
          "set -euo pipefail",
          "umask 077",
          "aws s3api get-object --bucket \"$PATCHDOCTOR_BUCKET_NAME\" --key \"$PATCHDOCTOR_BUNDLE_KEY\" --version-id \"$PATCHDOCTOR_BUNDLE_VERSION\" /tmp/patchdoctor.zip >/dev/null",
          "printf '%s  %s\\n' \"$PATCHDOCTOR_BUNDLE_SHA256\" /tmp/patchdoctor.zip | sha256sum --check --status",
          "install -d -m 0755 /opt/patchdoctor",
          "python3 -m zipfile -e /tmp/patchdoctor.zip /opt/patchdoctor",
          "chmod 0755 /opt/patchdoctor",
          "chmod 0644 /opt/patchdoctor/worker.py /opt/patchdoctor/host.py /opt/patchdoctor/prompt.txt /opt/patchdoctor/install-claude.sh",
          "(umask 022; python3 -m pip install --disable-pip-version-check 'boto3==1.43.123' >/dev/null)",
          "bash /opt/patchdoctor/install-claude.sh /opt/patchdoctor/bin/claude",
          "id doctor >/dev/null 2>&1 || useradd --create-home --home-dir /tmp/doctor-home --shell /bin/bash doctor",
          "install -d -o doctor -g doctor -m 0700 /tmp/doctor-work /tmp/doctor-home",
          "chmod 0755 /opt/patchdoctor/host.py",
          "ln -s /opt/patchdoctor/host.py /usr/local/bin/patchdoctor-host",
        ] }
        build = { commands = [
          "runuser -u doctor -- env HOME=/tmp/doctor-home CLAUDE_CONFIG_DIR=/tmp/doctor-home/.claude CLAUDE_BINARY=/opt/patchdoctor/bin/claude PATCHDOCTOR_WORKSPACE=/tmp/doctor-work python3 /opt/patchdoctor/worker.py"
        ] }
      }
    })
  }
  logs_config {
    cloudwatch_logs {
      group_name  = aws_cloudwatch_log_group.patchdoctor["worker"].name
      stream_name = "typed-progress"
    }
  }
  depends_on = [aws_iam_role_policy.patchdoctor_common, aws_iam_role_policy.patchdoctor_worker]
}

resource "aws_sqs_queue" "patchdoctor_dlq" {
  name                      = "patchdoctor-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}
resource "aws_sqs_queue" "patchdoctor" {
  name                       = "patchdoctor"
  message_retention_seconds  = 1209600
  visibility_timeout_seconds = 420
  sqs_managed_sse_enabled    = true
  redrive_policy             = jsonencode({ deadLetterTargetArn = aws_sqs_queue.patchdoctor_dlq.arn, maxReceiveCount = 30 })
}
resource "aws_cloudwatch_event_rule" "patchdoctor" {
  name           = "patchdoctor-critical"
  event_bus_name = aws_cloudwatch_event_bus.security_alerts.name
  event_pattern = jsonencode({
    account = [data.aws_caller_identity.current.account_id]
    region  = local.patchdoctor_regions
    "$or" = [
      { source = ["aws.inspector2"], "detail-type" = ["Inspector2 Finding"], detail = { severity = ["CRITICAL"], status = ["ACTIVE"] } },
      { source = ["aws.securityhub"], "detail-type" = ["Security Hub Findings - Imported"], detail = { findings = { Severity = { Label = ["CRITICAL"] }, RecordState = ["ACTIVE"] } } }
    ]
  })
}
resource "aws_sqs_queue_policy" "patchdoctor" {
  queue_url = aws_sqs_queue.patchdoctor.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sqs:SendMessage"
      Resource  = aws_sqs_queue.patchdoctor.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.patchdoctor.arn } }
    }]
  })
}
resource "aws_cloudwatch_event_target" "patchdoctor" {
  event_bus_name = aws_cloudwatch_event_bus.security_alerts.name
  rule           = aws_cloudwatch_event_rule.patchdoctor.name
  target_id      = "durable-doctor-inbox"
  arn            = aws_sqs_queue.patchdoctor.arn
  depends_on     = [aws_sqs_queue_policy.patchdoctor]
}
resource "aws_iam_role_policy" "patchdoctor_queue" {
  role = aws_iam_role.patchdoctor["admission"].id
  name = "critical-inbox"
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"], Resource = aws_sqs_queue.patchdoctor.arn }]
  })
}
resource "aws_lambda_event_source_mapping" "patchdoctor" {
  event_source_arn = aws_sqs_queue.patchdoctor.arn
  function_name    = aws_lambda_function.patchdoctor_admission.arn
  batch_size       = 1
  scaling_config { maximum_concurrency = 2 }
  function_response_types = ["ReportBatchItemFailures"]
  depends_on              = [aws_iam_role_policy.patchdoctor_queue]
}
resource "aws_cloudwatch_event_rule" "patchdoctor_reconcile" {
  name                = "patchdoctor-reconcile"
  schedule_expression = "rate(1 minute)"
}
resource "aws_cloudwatch_event_target" "patchdoctor_reconcile" {
  rule = aws_cloudwatch_event_rule.patchdoctor_reconcile.name
  arn  = aws_lambda_function.patchdoctor.arn
}
resource "aws_lambda_permission" "patchdoctor_reconcile" {
  statement_id  = "ScheduledRecovery"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.patchdoctor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.patchdoctor_reconcile.arn
}
resource "aws_cloudwatch_event_rule" "patchdoctor_complete" {
  name = "patchdoctor-worker-complete"
  event_pattern = jsonencode({
    source = ["aws.codebuild"], "detail-type" = ["CodeBuild Build State Change"],
    detail = { "project-name" = ["patchdoctor"], "build-status" = ["SUCCEEDED", "FAILED", "FAULT", "STOPPED", "TIMED_OUT"] }
  })
}
resource "aws_cloudwatch_event_target" "patchdoctor_complete" {
  rule = aws_cloudwatch_event_rule.patchdoctor_complete.name
  arn  = aws_lambda_function.patchdoctor.arn
}
resource "aws_lambda_permission" "patchdoctor_complete" {
  statement_id  = "WorkerCompletion"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.patchdoctor.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.patchdoctor_complete.arn
}
resource "aws_cloudwatch_metric_alarm" "patchdoctor_dlq" {
  alarm_name          = "patchdoctor-undelivered-critical"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.patchdoctor_dlq.name }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

output "patchdoctor" {
  value = {
    control_function = aws_lambda_function.patchdoctor.function_name
    incident_table   = aws_dynamodb_table.patchdoctor.name
    private_bucket   = aws_s3_bucket.patchdoctor.id
    model            = local.patchdoctor_model
    scope            = "Verified CRITICAL CVEs on the named main-account Linux fleet; root on jumpboxes is administration authority."
    activation       = "Run the real Opus self_test, verify its durable result, then invoke the administration-only arm action."
  }
}

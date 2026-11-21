# fcvm-fleet-prod-drills.tf
#
# The production fleet's Spot reclaim drill (ejc3/aws#942): an AWS FIS experiment that sends one running prod GPU host
# (Purpose=fcvm-fleet-host, Fleet=prod, us-east-1) the real two-minute Spot interruption notice, so the coordinator's
# replace-at-notice path and the second awake host are exercised end to end. The ARM dev box invokes the drill
# function. FIS assumes the experiment role below to interrupt the selected host.

# FIS needs this account-wide service role on the very first experiment. The dev role cannot create IAM roles.
resource "aws_iam_service_linked_role" "fcvm_fleet_fis" {
  count            = local.fcvm_fleet_prod
  provider         = aws.fcvm_fleet
  aws_service_name = "fis.amazonaws.com"
}

resource "aws_iam_role" "fcvm_fleet_prod_spot_drill" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-prod-spot-drill"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "fis.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.fcvm_fleet_account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:fis:us-east-1:${local.fcvm_fleet_account_id}:experiment/*" }
      }
    }]
  })
  tags = local.fcvm_fleet_prod_tags
}

resource "aws_iam_role_policy" "fcvm_fleet_prod_spot_drill" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "interrupt-prod-hosts-only"
  role     = aws_iam_role.fcvm_fleet_prod_spot_drill[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "InterruptOnlyProdHosts"
        Effect   = "Allow"
        Action   = "ec2:SendSpotInstanceInterruptions"
        Resource = "arn:aws:ec2:us-east-1:${local.fcvm_fleet_account_id}:instance/*"
        Condition = { StringEquals = {
          "ec2:ResourceTag/Fleet"   = "prod"
          "ec2:ResourceTag/Purpose" = "fcvm-fleet-host"
        } }
      },
      { Sid = "FindThem", Effect = "Allow", Action = "ec2:DescribeInstances", Resource = "*" },
    ]
  })
}

resource "aws_fis_experiment_template" "fcvm_fleet_prod_spot_drill" {
  count       = local.fcvm_fleet_prod
  provider    = aws.fcvm_fleet_east1
  description = "fcvm prod: one running GPU host gets the two-minute Spot interruption notice"
  role_arn    = aws_iam_role.fcvm_fleet_prod_spot_drill[0].arn
  depends_on = [
    aws_iam_service_linked_role.fcvm_fleet_fis,
    aws_iam_role_policy.fcvm_fleet_prod_spot_drill,
  ]

  stop_condition {
    source = "none"
  }

  target {
    name           = "one-prod-host"
    resource_type  = "aws:ec2:spot-instance"
    selection_mode = "COUNT(1)"
    resource_tag {
      key   = "Fleet"
      value = "prod"
    }
    resource_tag {
      key   = "Purpose"
      value = "fcvm-fleet-host"
    }
    filter {
      path   = "State.Name"
      values = ["running"]
    }
  }

  action {
    name      = "interrupt"
    action_id = "aws:ec2:send-spot-instance-interruptions"
    parameter {
      key   = "durationBeforeInterruption"
      value = "PT2M"
    }
    target {
      key   = "SpotInstances"
      value = "one-prod-host"
    }
  }

  tags = merge(local.fcvm_fleet_prod_tags, { Name = "fcvm-fleet-prod-spot-drill" })
}

# ARM may invoke this serialized guard. Only its role can submit the fixed template or stop its own runs.
locals {
  fcvm_prod_drill_function = "fcvm-fleet-prod-spot-drill"
  fcvm_prod_drill_state    = "drills/prod-spot.json"
  fcvm_prod_drill_exp_arn  = "arn:aws:fis:us-east-1:${local.fcvm_fleet_account_id}:experiment/*"
}

resource "aws_s3_object" "fcvm_fleet_prod_spot_drill" {
  count        = local.fcvm_fleet_prod
  provider     = aws.fcvm_fleet
  bucket       = aws_s3_bucket.fcvm_fleet_coordinator_state.id
  key          = local.fcvm_prod_drill_state
  content      = "{}"
  content_type = "application/json"
  lifecycle {
    ignore_changes = [content]
  }
}

resource "aws_cloudwatch_log_group" "fcvm_fleet_prod_spot_drill" {
  count             = local.fcvm_fleet_prod
  provider          = aws.fcvm_fleet_east1
  name              = "/aws/lambda/${local.fcvm_prod_drill_function}"
  retention_in_days = 30
  tags              = local.fcvm_fleet_prod_tags
}

resource "aws_iam_role" "fcvm_fleet_prod_spot_drill_guard" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "fcvm-fleet-prod-spot-drill-guard"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
  tags = local.fcvm_fleet_prod_tags
}

resource "aws_iam_role_policy" "fcvm_fleet_prod_spot_drill_guard" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  role     = aws_iam_role.fcvm_fleet_prod_spot_drill_guard[0].id
  name     = "run-the-guarded-spot-drill"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = "fis:StartExperiment"
        Effect   = "Allow"
        Resource = "arn:aws:fis:us-east-1:${local.fcvm_fleet_account_id}:experiment-template/${aws_fis_experiment_template.fcvm_fleet_prod_spot_drill[0].id}"
      },
      {
        Effect   = "Allow"
        Action   = ["fis:StartExperiment", "fis:TagResource"]
        Resource = local.fcvm_prod_drill_exp_arn
        Condition = {
          StringEquals                = { "aws:RequestTag/Drill" = "fcvm-fleet-prod-spot" }
          "ForAllValues:StringEquals" = { "aws:TagKeys" = ["Drill", "Request"] }
        }
      },
      {
        Effect    = "Allow"
        Action    = ["fis:GetExperiment", "fis:ListExperimentResolvedTargets", "fis:StopExperiment"]
        Resource  = local.fcvm_prod_drill_exp_arn
        Condition = { StringEquals = { "aws:ResourceTag/Drill" = "fcvm-fleet-prod-spot" } }
      },
      {
        Effect    = "Allow"
        Action    = ["fis:ListExperiments", "ec2:DescribeInstances", "ec2:DescribeInstanceStatus"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = "us-east-1" } }
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/${local.fcvm_prod_drill_state}"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.fcvm_fleet_prod_spot_drill[0].arn}:*"
      },
    ]
  })
}

data "archive_file" "fcvm_fleet_prod_spot_drill" {
  count       = local.fcvm_fleet_prod
  type        = "zip"
  output_path = "${path.module}/.terraform/fcvm-fleet-prod-spot-drill.zip"
  source {
    content  = file("${path.module}/scripts/fcvm-prod-spot-drill.py")
    filename = "drill.py"
  }
}

resource "aws_lambda_function" "fcvm_fleet_prod_spot_drill" {
  count                          = local.fcvm_fleet_prod
  provider                       = aws.fcvm_fleet_east1
  function_name                  = local.fcvm_prod_drill_function
  role                           = aws_iam_role.fcvm_fleet_prod_spot_drill_guard[0].arn
  filename                       = data.archive_file.fcvm_fleet_prod_spot_drill[0].output_path
  source_code_hash               = data.archive_file.fcvm_fleet_prod_spot_drill[0].output_base64sha256
  reserved_concurrent_executions = 1
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  handler                        = "drill.handler"
  timeout                        = 60
  memory_size                    = 128
  environment {
    variables = {
      TEMPLATE_ID  = aws_fis_experiment_template.fcvm_fleet_prod_spot_drill[0].id
      STATE_REGION = local.fcvm_fleet_home_region
      STATE_BUCKET = aws_s3_bucket.fcvm_fleet_coordinator_state.bucket
      STATE_KEY    = aws_s3_object.fcvm_fleet_prod_spot_drill[0].key
    }
  }
  depends_on = [aws_iam_role_policy.fcvm_fleet_prod_spot_drill_guard]
  tags       = local.fcvm_fleet_prod_tags
}

# The live ops setting is a UTC day or "off". After provisioning the marker, fcvm-arm owns its value.
# The coordinator recognizes today's date only, and its durable spending ledger still controls wake admission.
resource "aws_s3_object" "fcvm_fleet_prod_budget_day" {
  count        = local.fcvm_fleet_prod
  provider     = aws.fcvm_fleet
  bucket       = aws_s3_bucket.fcvm_fleet_coordinator_state.id
  key          = "drills/prod-budget-day"
  content      = "off"
  content_type = "text/plain"
  lifecycle {
    ignore_changes = all
  }
}

resource "aws_iam_role_policy" "fcvm_fleet_operate_budget_drill" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "operate-prod-budget-drill"
  role     = aws_iam_role.fcvm_fleet_terraform.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject", "s3:PutObject"]
      Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/${aws_s3_object.fcvm_fleet_prod_budget_day[0].key}"
    }]
  })
}

resource "aws_iam_role_policy" "fcvm_fleet_read_budget_drill" {
  count    = local.fcvm_fleet_prod
  provider = aws.fcvm_fleet
  name     = "read-prod-budget-drill"
  role     = aws_iam_role.fcvm_fleet_prod_coordinator_task[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "s3:GetObject"
      Resource = "${aws_s3_bucket.fcvm_fleet_coordinator_state.arn}/${aws_s3_object.fcvm_fleet_prod_budget_day[0].key}"
    }]
  })
}

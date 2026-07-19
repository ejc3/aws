# mac-dev-teardown.tf
#
# Automatic teardown for the temporary EC2 Mac box.
#
# WHY THIS RELEASES THE HOST RATHER THAN STOPPING THE INSTANCE:
# EC2 Mac bills the DEDICATED HOST continuously, whether the instance is running or
# stopped. Stopping the instance saves nothing. The only way to stop paying is to
# terminate the instance and RELEASE the host -- which AWS refuses until 24h after
# allocation. So this is a one-shot scheduled teardown, not an idle auto-stop.
#
# Drift note: this Lambda deletes resources terraform manages. That is deliberate (it is
# a cost backstop that must work even if nobody is around). Because enable_mac_dev
# defaults to false, the next `terraform apply` reconciles state cleanly.

variable "mac_teardown_at" {
  description = "One-time UTC teardown time. MUST be >= 24h after host allocation or AWS rejects the release."
  type        = string
  default     = "2026-07-20T18:30:00" # host allocated 2026-07-19T18:09:05Z -> +24h21m
}

locals {
  mac_teardown_code = <<-PY
import os, time, boto3

REGION = os.environ["REGION"]
HOST_ID = os.environ["HOST_ID"]
INSTANCE_ID = os.environ.get("INSTANCE_ID", "")
EIP_ALLOC = os.environ.get("EIP_ALLOC", "")
SNS_TOPIC = os.environ.get("SNS_TOPIC_ARN", "")
SNS_REGION = os.environ.get("SNS_REGION", "us-west-1")

ec2 = boto3.client("ec2", region_name=REGION)


def notify(subject, body):
    if not SNS_TOPIC:
        return
    try:
        boto3.client("sns", region_name=SNS_REGION).publish(
            TopicArn=SNS_TOPIC, Subject=subject[:100], Message=body
        )
    except Exception as e:
        print("sns publish failed: %s" % e)


def lambda_handler(event, context):
    steps = []

    # 1. Terminate the Mac instance (a host cannot be released while it holds one).
    if INSTANCE_ID:
        try:
            ec2.terminate_instances(InstanceIds=[INSTANCE_ID])
            steps.append("terminate requested: " + INSTANCE_ID)
            waiter = ec2.get_waiter("instance_terminated")
            waiter.wait(InstanceIds=[INSTANCE_ID],
                        WaiterConfig={"Delay": 15, "MaxAttempts": 40})
            steps.append("instance terminated")
        except Exception as e:
            steps.append("terminate failed: %s" % e)

    # 2. Release the dedicated host -- this is what actually stops the billing.
    released = False
    for attempt in range(10):
        try:
            r = ec2.release_hosts(HostIds=[HOST_ID])
            if HOST_ID in r.get("Successful", []):
                steps.append("host released: " + HOST_ID)
                released = True
                break
            err = r.get("Unsuccessful", [])
            steps.append("release attempt %d unsuccessful: %s" % (attempt + 1, err))
        except Exception as e:
            steps.append("release attempt %d error: %s" % (attempt + 1, e))
        time.sleep(30)

    # 3. Release the Elastic IP so it stops costing while unattached.
    if EIP_ALLOC:
        try:
            ec2.release_address(AllocationId=EIP_ALLOC)
            steps.append("eip released: " + EIP_ALLOC)
        except Exception as e:
            steps.append("eip release failed: %s" % e)

    body = "Mac dev box teardown\n\n" + "\n".join(steps)
    if not released:
        body += "\n\nWARNING: host NOT released -- it is STILL BILLING. Check manually."
    print(body)
    notify("Mac dev teardown: %s" % ("OK" if released else "FAILED"), body)
    return {"released": released, "steps": steps}
  PY
}

data "archive_file" "mac_teardown" {
  count       = var.enable_mac_dev ? 1 : 0
  type        = "zip"
  output_path = "${path.module}/.terraform/mac-teardown.zip"
  source {
    content  = local.mac_teardown_code
    filename = "index.py"
  }
}

resource "aws_iam_role" "mac_teardown" {
  count    = var.enable_mac_dev ? 1 : 0
  provider = aws.mac
  name     = "mac-dev-teardown"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "mac_teardown_basic" {
  count      = var.enable_mac_dev ? 1 : 0
  provider   = aws.mac
  role       = aws_iam_role.mac_teardown[0].name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "mac_teardown" {
  count    = var.enable_mac_dev ? 1 : 0
  provider = aws.mac
  name     = "mac-dev-teardown-policy"
  role     = aws_iam_role.mac_teardown[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances", "ec2:DescribeHosts", "ec2:DescribeAddresses",
          "ec2:TerminateInstances", "ec2:ReleaseHosts", "ec2:ReleaseAddress"
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      }
    ]
  })
}

resource "aws_lambda_function" "mac_teardown" {
  count            = var.enable_mac_dev ? 1 : 0
  provider         = aws.mac
  function_name    = "mac-dev-teardown"
  role             = aws_iam_role.mac_teardown[0].arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 900
  filename         = data.archive_file.mac_teardown[0].output_path
  source_code_hash = data.archive_file.mac_teardown[0].output_base64sha256

  environment {
    variables = {
      REGION        = "us-west-2"
      HOST_ID       = aws_ec2_host.mac[0].id
      INSTANCE_ID   = aws_instance.mac[0].id
      EIP_ALLOC     = aws_eip.mac[0].id
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
      SNS_REGION    = var.aws_region
    }
  }

  tags = { Name = "mac-dev-teardown", Temporary = "true" }
}

# One-shot schedule (EventBridge Scheduler supports at(); classic rules only do cron/rate).
resource "aws_iam_role" "mac_teardown_scheduler" {
  count    = var.enable_mac_dev ? 1 : 0
  provider = aws.mac
  name     = "mac-dev-teardown-scheduler"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "mac_teardown_scheduler" {
  count    = var.enable_mac_dev ? 1 : 0
  provider = aws.mac
  name     = "invoke-teardown"
  role     = aws_iam_role.mac_teardown_scheduler[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.mac_teardown[0].arn
    }]
  })
}

resource "aws_scheduler_schedule" "mac_teardown" {
  count      = var.enable_mac_dev ? 1 : 0
  provider   = aws.mac
  name       = "mac-dev-teardown"
  group_name = "default"

  flexible_time_window { mode = "OFF" }

  schedule_expression          = "at(${var.mac_teardown_at})"
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.mac_teardown[0].arn
    role_arn = aws_iam_role.mac_teardown_scheduler[0].arn
  }
}

output "mac_dev_teardown_at" {
  description = "When the Mac box will be auto-torn-down (UTC)"
  value       = var.enable_mac_dev ? "${var.mac_teardown_at}Z (host releasable from 2026-07-20T18:09:05Z)" : null
}

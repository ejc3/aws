# Lambda-based auto-stop for dev servers (works with spot instances)
# CloudWatch EC2 actions don't work reliably with spot instances

locals {
  auto_stop_lambda_code = <<-PYTHON
import boto3
import json
import os

ec2 = boto3.client('ec2')
sns = boto3.client('sns')
cloudwatch = boto3.client('cloudwatch')

SNS_TOPIC = os.environ.get('SNS_TOPIC_ARN', '')
IDLE_HOURS = int(os.environ.get('IDLE_HOURS', '8'))

def lambda_handler(event, context):
    """
    Check dev server CPU utilization and stop if idle.
    Called by CloudWatch Events rule every hour.
    """
    instance_ids = os.environ.get('INSTANCE_IDS', '').split(',')
    instance_ids = [i.strip() for i in instance_ids if i.strip()]

    if not instance_ids:
        print("No instance IDs configured")
        return {'statusCode': 200, 'body': 'No instances to check'}

    results = []
    for instance_id in instance_ids:
        result = check_and_stop_instance(instance_id)
        results.append(result)

    return {'statusCode': 200, 'body': json.dumps(results)}

def check_and_stop_instance(instance_id):
    """Check if instance is idle and stop it if so."""
    try:
        # Get instance state
        response = ec2.describe_instances(InstanceIds=[instance_id])
        if not response['Reservations']:
            return {'instance': instance_id, 'status': 'not_found'}

        instance = response['Reservations'][0]['Instances'][0]
        state = instance['State']['Name']

        if state != 'running':
            return {'instance': instance_id, 'status': f'already_{state}'}

        # Get instance name
        name = 'unknown'
        for tag in instance.get('Tags', []):
            if tag['Key'] == 'Name':
                name = tag['Value']
                break

        # Check CPU utilization over last IDLE_HOURS (but only since instance started)
        import datetime
        end_time = datetime.datetime.utcnow()
        start_time = end_time - datetime.timedelta(hours=IDLE_HOURS)

        # Get instance launch time - only count metrics since instance started
        launch_time = instance.get('LaunchTime')
        if launch_time:
            # LaunchTime is timezone-aware, convert to naive UTC for comparison
            launch_time_utc = launch_time.replace(tzinfo=None)
            if launch_time_utc > start_time:
                # Instance started less than IDLE_HOURS ago
                hours_running = (end_time - launch_time_utc).total_seconds() / 3600
                return {'instance': instance_id, 'name': name, 'status': 'too_new',
                        'hours_running': round(hours_running, 1), 'required_hours': IDLE_HOURS}
            start_time = max(start_time, launch_time_utc)

        metrics = cloudwatch.get_metric_statistics(
            Namespace='AWS/EC2',
            MetricName='CPUUtilization',
            Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
            StartTime=start_time,
            EndTime=end_time,
            Period=3600,  # 1 hour
            Statistics=['Maximum']  # Use max, not avg - a 5min compile burst should count
        )

        datapoints = metrics.get('Datapoints', [])
        if len(datapoints) < IDLE_HOURS:
            return {'instance': instance_id, 'name': name, 'status': 'insufficient_data',
                    'datapoints': len(datapoints)}

        # Check if ANY hour had CPU >= 5% (using hourly max, not avg)
        max_per_hour = [d['Maximum'] for d in datapoints]
        peak_cpu = max(max_per_hour)

        if peak_cpu >= 5.0:
            return {'instance': instance_id, 'name': name, 'status': 'active',
                    'peak_cpu': peak_cpu}

        # Instance is idle - stop it (no hour had even 5% peak CPU)
        print(f"Stopping idle instance {instance_id} ({name}), peak CPU: {peak_cpu:.2f}%")

        try:
            ec2.stop_instances(InstanceIds=[instance_id])
            notify(f"Auto-stopped {name}",
                   f"Instance {instance_id} ({name}) was idle for {IDLE_HOURS}+ hours (peak CPU: {peak_cpu:.2f}%) and has been stopped.")
            return {'instance': instance_id, 'name': name, 'status': 'stopped', 'peak_cpu': peak_cpu}
        except Exception as e:
            error_msg = str(e)
            print(f"Failed to stop {instance_id}: {error_msg}")
            notify(f"FAILED to auto-stop {name}",
                   f"Instance {instance_id} ({name}) is idle but FAILED to stop!\n\nError: {error_msg}\n\nPlease stop it manually.")
            return {'instance': instance_id, 'name': name, 'status': 'stop_failed', 'error': error_msg}

    except Exception as e:
        print(f"Error checking {instance_id}: {e}")
        return {'instance': instance_id, 'status': 'error', 'error': str(e)}

def notify(subject, message):
    """Send SNS notification."""
    if not SNS_TOPIC:
        print(f"No SNS topic configured. Would send: {subject}")
        return

    try:
        sns.publish(
            TopicArn=SNS_TOPIC,
            Subject=subject,
            Message=message
        )
        print(f"Sent notification: {subject}")
    except Exception as e:
        print(f"Failed to send notification: {e}")
PYTHON
}

# IAM role for Lambda
resource "aws_iam_role" "dev_auto_stop" {
  name = "dev-auto-stop-lambda-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
    }]
  })

  tags = { Name = "dev-auto-stop-lambda-role" }
}

# Lambda basic execution (CloudWatch Logs)
resource "aws_iam_role_policy_attachment" "dev_auto_stop_basic" {
  role       = aws_iam_role.dev_auto_stop.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Policy for EC2 and SNS access
resource "aws_iam_role_policy" "dev_auto_stop" {
  name = "dev-auto-stop-policy"
  role = aws_iam_role.dev_auto_stop.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EC2Describe"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus"
        ]
        Resource = "*"
      },
      {
        Sid    = "EC2StopDevServers"
        Effect = "Allow"
        Action = "ec2:StopInstances"
        Resource = [
          "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*"
        ]
        Condition = {
          StringLike = {
            "ec2:ResourceTag/Name" = ["fcvm-metal-*", "x86-*"]
          }
        }
      },
      {
        Sid    = "CloudWatchMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:GetMetricData"
        ]
        Resource = "*"
      },
      {
        Sid    = "SNSPublish"
        Effect = "Allow"
        Action = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      }
    ]
  })
}

# Lambda function
resource "aws_lambda_function" "dev_auto_stop" {
  function_name = "dev-auto-stop"
  role          = aws_iam_role.dev_auto_stop.arn
  handler       = "index.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 128

  filename         = data.archive_file.dev_auto_stop.output_path
  source_code_hash = data.archive_file.dev_auto_stop.output_base64sha256

  environment {
    variables = {
      INSTANCE_IDS  = join(",", compact([
        var.enable_firecracker_instance ? aws_instance.firecracker_dev[0].id : "",
        var.enable_x86_dev_instance ? aws_instance.x86_dev[0].id : ""
      ]))
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
      IDLE_HOURS    = "12"
    }
  }

  tags = { Name = "dev-auto-stop" }
}

# Package Lambda code
data "archive_file" "dev_auto_stop" {
  type        = "zip"
  output_path = "${path.module}/.terraform/dev-auto-stop.zip"

  source {
    content  = local.auto_stop_lambda_code
    filename = "index.py"
  }
}

# CloudWatch Events rule - run every hour
resource "aws_cloudwatch_event_rule" "dev_auto_stop" {
  name                = "dev-auto-stop-hourly"
  description         = "Check dev servers for idle and auto-stop"
  schedule_expression = "rate(1 hour)"

  tags = { Name = "dev-auto-stop-hourly" }
}

resource "aws_cloudwatch_event_target" "dev_auto_stop" {
  rule      = aws_cloudwatch_event_rule.dev_auto_stop.name
  target_id = "dev-auto-stop-lambda"
  arn       = aws_lambda_function.dev_auto_stop.arn
}

resource "aws_lambda_permission" "dev_auto_stop" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.dev_auto_stop.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.dev_auto_stop.arn
}

# Output for testing
output "dev_auto_stop_lambda_arn" {
  description = "ARN of the dev auto-stop Lambda"
  value       = aws_lambda_function.dev_auto_stop.arn
}

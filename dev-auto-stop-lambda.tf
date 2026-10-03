# Lambda-based auto-stop for dev servers (works with spot instances)
# CloudWatch EC2 actions don't work reliably with spot instances

locals {
  auto_stop_lambda_code = <<-PYTHON
import boto3
import json
import os

SNS_TOPIC = os.environ.get('SNS_TOPIC_ARN', '')
SNS_REGION = os.environ.get('SNS_REGION', os.environ.get('AWS_REGION', 'us-west-1'))
IDLE_HOURS = int(os.environ.get('IDLE_HOURS', '8'))
# A five-minute window with CPU at or above this keeps the instance alive. 5 suits the metal boxes (a short compile is
# a real burst). An instance with a high IDLE floor needs more: an idle Windows desktop with its streaming agent sits
# at about 6% and peaks near 12%, so at 5 it can never be called idle (wbox ran 4 days).
CPU_THRESHOLD = float(os.environ.get('CPU_THRESHOLD', '5.0'))
CHECK_IO = os.environ.get('CHECK_IO', 'false').lower() == 'true'
# Instance-store activity is direct evidence that the scratch disk is in use, so a
# single MiB in any five-minute period is meaningful. Network needs a higher bar:
# Ubuntu's apt timers can download package indexes in the background, and that must
# not keep an otherwise idle box alive forever. NFS work also hits the disk unless a
# read is fully cached, where 64 MiB of egress is still meaningful activity.
DISK_ACTIVITY_BYTES = int(os.environ.get('DISK_ACTIVITY_BYTES', '1048576'))
NETWORK_ACTIVITY_BYTES = int(os.environ.get('NETWORK_ACTIVITY_BYTES', '67108864'))

ec2 = boto3.client('ec2')
sns = boto3.client('sns', region_name=SNS_REGION)
cloudwatch = boto3.client('cloudwatch')

def lambda_handler(event, context):
    """
    Check dev server activity and stop if idle.
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
            Period=300,  # 5 minutes - catches short compile bursts on 64-core metal
            Statistics=['Maximum']  # Use max, not avg - a 5min compile burst should count
        )

        datapoints = metrics.get('Datapoints', [])
        # With 5-min periods over IDLE_HOURS, expect IDLE_HOURS*12 datapoints
        # Require at least IDLE_HOURS worth (one per hour minimum)
        if len(datapoints) < IDLE_HOURS:
            return {'instance': instance_id, 'name': name, 'status': 'insufficient_data',
                    'datapoints': len(datapoints)}

        # Check if ANY 5-min window had CPU >= CPU_THRESHOLD
        max_per_period = [d['Maximum'] for d in datapoints]
        peak_cpu = max(max_per_period)

        if peak_cpu >= CPU_THRESHOLD:
            return {'instance': instance_id, 'name': name, 'status': 'active',
                    'peak_cpu': peak_cpu}

        # The ordinary metal dev boxes preserve their established CPU-only behavior.
        # The shared I/O box opts into network + instance-store activity checks: NFS can
        # move real data while using less than 5% of two CPUs, so CPU alone is unsafe.
        peak_io_bytes = 0
        io_peaks = {}
        if CHECK_IO:
            metric_thresholds = {
                'NetworkIn': NETWORK_ACTIVITY_BYTES,
                'NetworkOut': NETWORK_ACTIVITY_BYTES,
                'DiskReadBytes': DISK_ACTIVITY_BYTES,
                'DiskWriteBytes': DISK_ACTIVITY_BYTES,
            }
            active_metrics = []
            for metric_name, threshold in metric_thresholds.items():
                io_metrics = cloudwatch.get_metric_statistics(
                    Namespace='AWS/EC2',
                    MetricName=metric_name,
                    Dimensions=[{'Name': 'InstanceId', 'Value': instance_id}],
                    StartTime=start_time,
                    EndTime=end_time,
                    Period=300,
                    Statistics=['Sum']
                )
                points = io_metrics.get('Datapoints', [])
                peak = max([d['Sum'] for d in points], default=0)
                io_peaks[metric_name] = round(peak)
                peak_io_bytes = max(peak_io_bytes, peak)
                if peak >= threshold:
                    active_metrics.append(metric_name)

            if active_metrics:
                return {'instance': instance_id, 'name': name, 'status': 'active_io',
                        'peak_cpu': peak_cpu, 'peak_io_bytes_5m': round(peak_io_bytes),
                        'active_metrics': active_metrics, 'io_peaks': io_peaks}

        # Instance is idle - no five-minute window crossed either activity threshold.
        print(f"Stopping idle instance {instance_id} ({name}), peak CPU: {peak_cpu:.2f}%, peak I/O: {peak_io_bytes:.0f} bytes/5m")

        try:
            ec2.stop_instances(InstanceIds=[instance_id])
            notify(f"Auto-stopped {name}",
                   f"Instance {instance_id} ({name}) was idle for {IDLE_HOURS}+ hours "
                   f"(peak CPU: {peak_cpu:.2f}%, peak I/O: {peak_io_bytes:.0f} bytes/5m) and has been stopped.")
            return {'instance': instance_id, 'name': name, 'status': 'stopped',
                    'peak_cpu': peak_cpu, 'peak_io_bytes_5m': round(peak_io_bytes)}
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
          "arn:aws:ec2:us-west-1:${data.aws_caller_identity.current.account_id}:instance/*",
          "arn:aws:ec2:us-west-2:${data.aws_caller_identity.current.account_id}:instance/*",
        ]
        Condition = {
          StringLike = {
            "ec2:ResourceTag/Name" = ["fcvm-metal-*", "x86-*", "io-box", "wbox"]
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
        Sid      = "SNSPublish"
        Effect   = "Allow"
        Action   = "sns:Publish"
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
      INSTANCE_IDS = join(",", compact([
        var.enable_firecracker_instance ? aws_instance.firecracker_dev[0].id : "",
        var.enable_x86_dev_instance ? aws_instance.x86_dev[0].id : ""
      ]))
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
      SNS_REGION    = var.aws_region
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

# The same 12-hour stop lifecycle for the us-west-2 I/O box. Lambda's EC2 and
# CloudWatch clients are regional, so this must run beside the instance rather than
# adding a west-2 ID to the west-1 function. The shared source opts into network and
# instance-store byte checks here; the existing metal boxes remain CPU-only.
resource "aws_lambda_function" "io_box_auto_stop" {
  provider      = aws.west2
  function_name = "io-box-auto-stop"
  role          = aws_iam_role.dev_auto_stop.arn
  handler       = "index.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 128

  filename         = data.archive_file.dev_auto_stop.output_path
  source_code_hash = data.archive_file.dev_auto_stop.output_base64sha256

  environment {
    variables = {
      INSTANCE_IDS           = aws_instance.io_box.id
      SNS_TOPIC_ARN          = aws_sns_topic.cost_alerts.arn
      SNS_REGION             = var.aws_region
      IDLE_HOURS             = "12"
      CHECK_IO               = "true"
      DISK_ACTIVITY_BYTES    = "1048576"
      NETWORK_ACTIVITY_BYTES = "67108864"
    }
  }

  tags = { Name = "io-box-auto-stop" }
}

resource "aws_cloudwatch_event_rule" "io_box_auto_stop" {
  provider            = aws.west2
  name                = "io-box-auto-stop-hourly"
  description         = "Stop the shared I/O box after 12 hours without CPU, network, or disk activity"
  schedule_expression = "rate(1 hour)"

  tags = { Name = "io-box-auto-stop-hourly" }
}

resource "aws_cloudwatch_event_target" "io_box_auto_stop" {
  provider  = aws.west2
  rule      = aws_cloudwatch_event_rule.io_box_auto_stop.name
  target_id = "io-box-auto-stop-lambda"
  arn       = aws_lambda_function.io_box_auto_stop.arn
}

resource "aws_lambda_permission" "io_box_auto_stop" {
  provider      = aws.west2
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.io_box_auto_stop.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.io_box_auto_stop.arn
}

# Output for testing
output "dev_auto_stop_lambda_arn" {
  description = "ARN of the dev auto-stop Lambda"
  value       = aws_lambda_function.dev_auto_stop.arn
}

output "io_box_auto_stop_lambda_arn" {
  description = "ARN of the us-west-2 I/O box auto-stop Lambda"
  value       = aws_lambda_function.io_box_auto_stop.arn
}

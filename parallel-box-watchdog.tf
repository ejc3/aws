# parallel-box-watchdog.tf
#
# Aggressive idle watchdog for the on-demand parallel box.
#
# WHY A SEPARATE LAMBDA FROM dev-auto-stop-lambda.tf: the semantics genuinely differ on
# three axes, and folding them together would mean parameterising the function that
# protects the dev boxes -- risk to working code for little gain.
#
#   dev boxes            parallel box
#   ---------            ------------
#   idle window: HOURS   idle window: MINUTES  (it costs ~$3.14/hr, ~50x a dev box)
#   action: stop         action: TERMINATE     (a one-time spot cannot be restarted, and
#                                               the root disk is disposable by design)
#   found by: instance   found by: TAG         (the instance ID changes on every launch,
#             ID                                so a baked-in ID goes stale immediately)
#   checked: hourly      checked: every 5 min
#
# Terminating is safe precisely because all work lives on the persistent 100GB volume,
# which is a separate resource with prevent_destroy. Losing the instance loses nothing.

variable "parallel_box_idle_minutes" {
  description = "Terminate the parallel box after this many minutes below the CPU threshold."
  type        = number
  default     = 30
}

variable "parallel_box_idle_cpu_pct" {
  description = "Peak CPU below which the box counts as idle. Embarrassingly parallel work pegs all cores, so idle is unambiguous."
  type        = number
  default     = 5
}

locals {
  parallel_watchdog_code = <<-PY
import os, json, datetime, boto3

IDLE_MINUTES = int(os.environ.get("IDLE_MINUTES", "30"))
IDLE_CPU     = float(os.environ.get("IDLE_CPU_PCT", "5"))
TAG_NAME     = os.environ.get("TAG_NAME", "parallel-box")
SNS_TOPIC    = os.environ.get("SNS_TOPIC_ARN", "")

ec2 = boto3.client("ec2")
cw  = boto3.client("cloudwatch")


def notify(subject, body):
    if not SNS_TOPIC:
        return
    try:
        boto3.client("sns").publish(TopicArn=SNS_TOPIC, Subject=subject[:100], Message=body)
    except Exception as e:
        print("sns publish failed: %s" % e)


def lambda_handler(event, context):
    # Find by TAG, not by a baked-in instance id: this box is created and destroyed
    # repeatedly, so its id is different every time it comes up.
    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [TAG_NAME]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    instances = [i for r in resp["Reservations"] for i in r["Instances"]]
    if not instances:
        return {"checked": 0, "note": "no running parallel box"}

    results = []
    now = datetime.datetime.now(datetime.timezone.utc)

    for inst in instances:
        iid = inst["InstanceId"]
        launch = inst["LaunchTime"]
        age_min = (now - launch).total_seconds() / 60.0

        # Grace period: never reap a box that has not yet had a full idle window to
        # prove itself. Bootstrapping (apt, cloning data) can look idle at the start.
        if age_min < IDLE_MINUTES:
            results.append({"id": iid, "action": "too_young", "age_min": round(age_min, 1)})
            continue

        stats = cw.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName="CPUUtilization",
            Dimensions=[{"Name": "InstanceId", "Value": iid}],
            StartTime=now - datetime.timedelta(minutes=IDLE_MINUTES),
            EndTime=now,
            Period=300,
            Statistics=["Maximum"],
        )
        points = stats.get("Datapoints", [])

        # Missing metrics must NOT be read as idle -- a metric outage would otherwise
        # terminate a box doing real work. Require real evidence of idleness.
        if len(points) < 2:
            results.append({"id": iid, "action": "insufficient_metrics", "points": len(points)})
            continue

        peak = max(p["Maximum"] for p in points)
        if peak >= IDLE_CPU:
            results.append({"id": iid, "action": "busy", "peak_cpu": round(peak, 1)})
            continue

        print("terminating idle parallel box %s (peak CPU %.1f%% over %dmin)" % (iid, peak, IDLE_MINUTES))
        try:
            ec2.terminate_instances(InstanceIds=[iid])
            results.append({"id": iid, "action": "terminated", "peak_cpu": round(peak, 1)})
            notify(
                "parallel-box auto-terminated (idle)",
                "Instance %s was below %.1f%% CPU for %d minutes (peak %.1f%%) and was terminated.\n\n"
                "The 100GB work volume is untouched -- bring the box back with:\n"
                "  scripts/parallel-box.sh up\n" % (iid, IDLE_CPU, IDLE_MINUTES, peak),
            )
        except Exception as e:
            results.append({"id": iid, "action": "terminate_failed", "error": str(e)})

    print(json.dumps(results))
    return {"checked": len(instances), "results": results}
  PY
}

data "archive_file" "parallel_watchdog" {
  type        = "zip"
  output_path = "${path.module}/.terraform/parallel-watchdog.zip"
  source {
    content  = local.parallel_watchdog_code
    filename = "index.py"
  }
}

resource "aws_iam_role" "parallel_watchdog" {
  name = "parallel-box-watchdog"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "parallel_watchdog_basic" {
  role       = aws_iam_role.parallel_watchdog.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "parallel_watchdog" {
  name = "parallel-box-watchdog-policy"
  role = aws_iam_role.parallel_watchdog.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances", "cloudwatch:GetMetricStatistics"]
        Resource = "*"
      },
      {
        # Scoped by tag so this role can only ever terminate the parallel box, never a
        # dev box or the jumpbox.
        Effect   = "Allow"
        Action   = "ec2:TerminateInstances"
        Resource = "*"
        Condition = {
          StringEquals = { "ec2:ResourceTag/Name" = "parallel-box" }
        }
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      }
    ]
  })
}

resource "aws_lambda_function" "parallel_watchdog" {
  function_name    = "parallel-box-watchdog"
  role             = aws_iam_role.parallel_watchdog.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 60
  filename         = data.archive_file.parallel_watchdog.output_path
  source_code_hash = data.archive_file.parallel_watchdog.output_base64sha256

  environment {
    variables = {
      IDLE_MINUTES  = tostring(var.parallel_box_idle_minutes)
      IDLE_CPU_PCT  = tostring(var.parallel_box_idle_cpu_pct)
      TAG_NAME      = "parallel-box"
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
    }
  }

  tags = { Name = "parallel-box-watchdog" }
}

# Every 5 minutes: at ~$3.14/hr, a missed reap costs real money, and the check is free.
resource "aws_cloudwatch_event_rule" "parallel_watchdog" {
  name                = "parallel-box-watchdog"
  description         = "Terminate the parallel box when idle"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "parallel_watchdog" {
  rule      = aws_cloudwatch_event_rule.parallel_watchdog.name
  target_id = "parallel-box-watchdog"
  arn       = aws_lambda_function.parallel_watchdog.arn
}

resource "aws_lambda_permission" "parallel_watchdog" {
  statement_id  = "AllowExecutionFromCloudWatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.parallel_watchdog.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.parallel_watchdog.arn
}

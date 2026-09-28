# dev-diagnostics.tf
#
# WHY THIS EXISTS: on 2026-08-08 fcvm-metal-arm hung. The alarm said "status check
# failed" and nothing else. The reason it hung -- a kernel BUG in the nested-virt KVM
# path -- existed in exactly one place, the EC2 serial console ring buffer, which:
#
#   1. is archived NOWHERE, and
#   2. is DESTROYED by the standard remedy. A stop/start clears the buffer, so the act
#      of recovering the box erases the only evidence of why it died, and
#   3. is invisible to the obvious API call: `get-console-output` returns a CACHED
#      snapshot that can be hours stale (it returned 05:37 data during a 12:19 incident);
#      only `--latest` reads the live buffer. During the incident that difference cost
#      twenty minutes and nearly cost the trace entirely -- the capture happened to be
#      taken seconds before the stop that would have wiped it.
#
# THE FIX: capture automatically, before anyone can destroy it. This Lambda subscribes to
# the alert topic, and whenever a status-check alarm fires it snapshots the live console
# to CloudWatch Logs (durable, searchable, survives stop/start) and re-notifies with the
# panic signature extracted, so the alert says WHY the box is down rather than only that
# it is.
#
# It deliberately takes no recovery action. Recovery is stop/start, which destroys local
# NVMe scratch; that stays a human decision. This only preserves the evidence so the
# decision is informed.

resource "aws_cloudwatch_log_group" "dev_console_capture" {
  name              = "/dev-servers/console-capture"
  retention_in_days = 90

  tags = { Name = "dev-console-capture" }
}

locals {
  console_capture_code = <<-PY
import os, json, re, datetime, boto3

SNS_TOPIC = os.environ.get("SNS_TOPIC_ARN", "")
LOG_GROUP = os.environ.get("LOG_GROUP", "/dev-servers/console-capture")

ec2 = boto3.client("ec2")
logs = boto3.client("logs")
sns = boto3.client("sns")

# Lines worth putting in front of a human. A kernel BUG/Oops names the exact source
# location, which is the difference between "it hung" and a filed upstream bug.
SIGNATURE = re.compile(
    r"(kernel BUG at |Internal error: Oops|Unable to handle kernel|"
    r"watchdog: BUG: soft lockup|INFO: task .* blocked for more than|"
    r"Out of memory: Killed process|Kernel panic)"
)

# Boot-time xtrace has printed private keys onto instance consoles (dev-hop-key.tf and
# jumpbox2-user-data.tf both did), and this function copies the console into CloudWatch
# Logs and an email that cannot be recalled. Key blocks are removed before the text is
# matched, logged or published. The buffer is a window onto a ring buffer, so it can also
# start or end inside a key; those partial blocks go too.
_KEY_LABEL = r"[A-Z0-9 ]*PRIVATE KEY(?: BLOCK)?-----"
# Starts inside a key: everything up to the first END marker with no BEGIN before it. The
# marker must follow key material, whitespace or a literal \n escape, so an END marker
# quoted in a traced command cannot wipe the head of the buffer.
KEY_HEAD = re.compile(r"\A(?:(?!-----BEGIN ).)*?(?<=[\sA-Za-z0-9+/=])-----END " + _KEY_LABEL, re.S)
KEY_BLOCK = re.compile(r"-----BEGIN " + _KEY_LABEL + r".*?-----END " + _KEY_LABEL, re.S)
# Ends inside a key: a BEGIN marker with no END after it, taken to the end of the buffer
# only when what follows could be key material. A marker quoted in a traced grep therefore
# cannot swallow the panic after it, and the end of the buffer is the part that matters.
KEY_TAIL = re.compile(r"-----BEGIN " + _KEY_LABEL + r"(?=[\s\\A-Za-z0-9+/=]|\Z).*\Z", re.S)
REDACTED = "[redacted private key]"


def redact(text):
    """The console text with every whole or partial private key block replaced."""
    for pattern in (KEY_HEAD, KEY_BLOCK, KEY_TAIL):
        text = pattern.sub(REDACTED, text)
    return text


def instance_ids_from_alarm(msg):
    """InstanceId out of the alarm's dimensions; [] when it is not an EC2 alarm."""
    ids = []
    for dim in (msg.get("Trigger", {}) or {}).get("Dimensions", []) or []:
        if dim.get("name") == "InstanceId" and dim.get("value"):
            ids.append(dim["value"])
    return ids


def capture(instance_id):
    # Latest=True is the whole point: without it the API answers with a cached
    # snapshot that can predate the incident by hours.
    resp = ec2.get_console_output(InstanceId=instance_id, Latest=True)
    # Redacted here, first: the signature match, the archive and the email all use this.
    output = redact(resp.get("Output") or "")
    if not output:
        return None, [], None
    lines = output.splitlines()
    hits = [l for l in lines if SIGNATURE.search(l)]
    return output, hits, resp.get("Timestamp")


def to_logs(instance_id, output, when):
    stream = "%s/%s" % (instance_id, datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ"))
    try:
        logs.create_log_stream(logGroupName=LOG_GROUP, logStreamName=stream)
    except logs.exceptions.ResourceAlreadyExistsException:
        pass
    now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    # PutLogEvents caps a batch at 10k events / ~1MB; console buffers are ~64KB, but
    # chunk anyway so a big buffer cannot silently drop the tail (which is the part
    # that matters -- the panic is at the END of the buffer).
    lines = output.splitlines() or [""]
    for i in range(0, len(lines), 1000):
        chunk = lines[i:i + 1000]
        logs.put_log_events(
            logGroupName=LOG_GROUP, logStreamName=stream,
            logEvents=[{"timestamp": now_ms, "message": l[:8000] or " "} for l in chunk],
        )
    return stream


def lambda_handler(event, context):
    results = []
    for record in event.get("Records", []):
        try:
            msg = json.loads(record["Sns"]["Message"])
        except Exception:
            continue
        if msg.get("NewStateValue") != "ALARM":
            continue
        name = msg.get("AlarmName", "")
        # Only status-check alarms carry a wedged-instance meaning. Everything else on
        # this topic (cost, starvation, backups) has nothing to snapshot.
        if "status" not in name.lower():
            continue

        for iid in instance_ids_from_alarm(msg):
            try:
                output, hits, when = capture(iid)
            except Exception as e:
                results.append({"instance": iid, "error": "console read failed: %s" % e})
                continue
            if not output:
                results.append({"instance": iid, "note": "console buffer empty"})
                continue
            stream = to_logs(iid, output, when)
            tail = "\n".join(output.splitlines()[-40:])
            body = (
                "%s is failing its status check.\n\n"
                "Console captured to CloudWatch Logs:\n  group:  %s\n  stream: %s\n"
                "Buffer timestamp: %s\n\n"
                "%s\n\nLast 40 console lines:\n%s\n\n"
                "The console buffer is DESTROYED by stop/start. It is archived above, so\n"
                "recovery is now safe to perform.\n"
            ) % (
                iid, LOG_GROUP, stream, when,
                ("PANIC SIGNATURE FOUND:\n  " + "\n  ".join(hits[:10])) if hits
                else "No panic signature matched; the box may have hung without logging.",
                tail,
            )
            if SNS_TOPIC:
                sns.publish(TopicArn=SNS_TOPIC, Subject=("console captured: %s" % iid)[:100], Message=body)
            results.append({"instance": iid, "stream": stream, "signatures": len(hits)})

    print(json.dumps(results))
    return {"captured": results}
  PY
}

data "archive_file" "console_capture" {
  type        = "zip"
  output_path = "${path.module}/.terraform/console-capture.zip"
  source {
    content  = local.console_capture_code
    filename = "index.py"
  }
}

resource "aws_iam_role" "console_capture" {
  name = "dev-console-capture"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "console_capture_basic" {
  role       = aws_iam_role.console_capture.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "console_capture" {
  name = "dev-console-capture"
  role = aws_iam_role.console_capture.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Read-only on EC2 by nature: this role can look at consoles, never touch an
        # instance. Recovery stays a human action.
        Effect   = "Allow"
        Action   = ["ec2:GetConsoleOutput", "ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.dev_console_capture.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      }
    ]
  })
}

resource "aws_lambda_function" "console_capture" {
  function_name    = "dev-console-capture"
  role             = aws_iam_role.console_capture.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 60
  filename         = data.archive_file.console_capture.output_path
  source_code_hash = data.archive_file.console_capture.output_base64sha256

  environment {
    variables = {
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
      LOG_GROUP     = aws_cloudwatch_log_group.dev_console_capture.name
    }
  }

  tags = { Name = "dev-console-capture" }
}

# Subscribed to the shared alert topic rather than given its own alarm: every current and
# future status-check alarm publishes here, so a new instance's alarm is covered the day
# it is created, with no second place to remember to wire up. The handler ignores every
# message that is not a status alarm entering ALARM.
resource "aws_sns_topic_subscription" "console_capture" {
  topic_arn = aws_sns_topic.cost_alerts.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.console_capture.arn
}

resource "aws_lambda_permission" "console_capture" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.console_capture.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.cost_alerts.arn
}

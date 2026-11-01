# parallel-box-watchdog.tf
#
# Aggressive idle watchdog for the on-demand parallel box.
#
# Runs in us-west-2, alongside the box it watches (the dev auto-stop lambda is in
# us-west-1 with the dev boxes). CloudWatch metrics and ec2:TerminateInstances are both
# region-scoped, so a us-west-1 lambda simply would not see this instance.
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
# Terminating is safe precisely because all work lives on the persistent work volume,
# which is a separate resource with prevent_destroy. Losing the instance loses nothing.

variable "parallel_box_idle_minutes" {
  description = "Terminate the parallel box after this many minutes below the CPU threshold."
  type        = number
  default     = 30
}

variable "parallel_box_idle_cpu_pct" {
  description = "Peak CPU below which the box counts as idle. Embarrassingly parallel work pegs all cores; work that does not (one thread, a disk-bound job) is caught by the I/O thresholds below."
  type        = number
  default     = 5
}

# Disk and network activity that keeps a CPU-idle box alive (#532): one busy thread is about 3% of a 32-vCPU box and
# 0.5% of a 192, and a disk-bound job on a storage flavor can sit under 5% CPU while it writes gigabytes. Any five-minute
# window of the idle window at or above a threshold counts as work. Measured on the Ohio parallel boxes reaped
# 2026-10-18/19: an idle box writes a steady 40-115 MiB per five minutes to EBS (peaks of 176 right after a job) and
# moves 0.1-3 MiB on the network; the jobs that were reaped as idle moved 1.7-13 GB per window to EBS or the instance
# store, or 180 MiB and more on the network. So 512 MiB for each disk direction and 64 MiB for each network direction
# (the I/O box's network bar, dev-auto-stop-lambda.tf), well clear of the idle floor and far under real work. A series
# that is missing counts as no activity: DiskRead/WriteBytes (the instance store) exist only on types that have one, and
# CPU already refuses to reap without data. EBSRead/WriteBytes are documented only for Nitro instances that are not bare
# metal, so where either is missing the code reads the attached volumes' AWS/EBS VolumeRead/WriteBytes instead (the role's
# cloudwatch:GetMetricStatistics on * and DescribeInstances' block device mappings already cover it). The members'
# Windows boxes (member-wbox.tf) keep CPU alone.
locals {
  parallel_watchdog_io_thresholds = jsonencode({
    EBSReadBytes   = 512 * 1048576
    EBSWriteBytes  = 512 * 1048576
    DiskReadBytes  = 512 * 1048576
    DiskWriteBytes = 512 * 1048576
    NetworkIn      = 64 * 1048576
    NetworkOut     = 64 * 1048576
  })
}

locals {
  parallel_watchdog_code = <<-PY
import os, json, datetime, boto3

IDLE_MINUTES = int(os.environ.get("IDLE_MINUTES", "30"))
IDLE_CPU     = float(os.environ.get("IDLE_CPU_PCT", "5"))
# Comma-separated: both parallel boxes (parallel-box2.tf) and the GPU test boxes' four
# slots (gpu-box.tf) share this one watchdog, with the members' gboxes (member-gbox.tf); the
# Ohio copy watches the parallel boxes and the members' pboxes (member-pbox.tf), and the
# members' Windows boxes (member-wbox.tf) run the same code as their own function in us-west-1.
TAG_NAMES    = os.environ.get("TAG_NAMES", "parallel-box").split(",")
SNS_TOPIC    = os.environ.get("SNS_TOPIC_ARN", "")
# The SNS topic is in us-west-1; this lambda runs in us-west-2 next to the instance.
SNS_REGION   = os.environ.get("SNS_REGION", "us-west-1")
# Hard lifetime per box name, in minutes, enforced from LaunchTime whatever the box is doing.
# The GPU box (gpu-box.tf) also arms a shutdown timer on itself, but that timer lives on the
# box -- a reboot, `shutdown -c` or a launch-time --user-data override disarms it -- so this
# is the lifetime that cannot be talked out of.
MAX_AGE      = json.loads(os.environ.get("MAX_AGE_MINUTES", "{}"))
# Names of which at most ONE instance may run: each member's own box (member-boxes.tf). IAM cannot
# cap RunInstances' --count, so a launch of several (or a second `up`) is cut back here to the
# oldest, and the rest are terminated with an alert.
SINGLE_NAMES = set(n for n in os.environ.get("SINGLE_NAMES", "").split(",") if n)
# Bytes per five-minute window, by AWS/EC2 metric, at or above which a CPU-idle box is still working (#532): disk and
# network. Empty (the members' Windows boxes) keeps CPU alone. A missing series is no activity.
IO_THRESHOLDS = json.loads(os.environ.get("IO_THRESHOLDS", "{}"))
# Names whose PboxLeaseUntil tag counts: the owner's parallel boxes (`pbox up ... --lease`, scripts/parallel-box.sh). Until
# the lease ends such a box is not terminated for idleness; then the normal rule applies. Empty (the members' boxes, the
# GPU test boxes) means no box has a lease. A missing or unparseable tag is no lease, and a lease past LaunchTime +
# LEASE_MAX_HOURS ends there, so a tag edited by hand cannot keep a box forever.
LEASE_NAMES = set(n for n in os.environ.get("LEASE_NAMES", "").split(",") if n)
LEASE_MAX_HOURS = 12

ec2 = boto3.client("ec2")
cw  = boto3.client("cloudwatch")


def notify(subject, body):
    if not SNS_TOPIC:
        return
    try:
        boto3.client("sns", region_name=SNS_REGION).publish(TopicArn=SNS_TOPIC, Subject=subject[:100], Message=body)
    except Exception as e:
        print("sns publish failed: %s" % e)


def lease_until(inst):
    """When the box's lease ends (aware UTC datetime), capped at LaunchTime + LEASE_MAX_HOURS, or None for no lease."""
    raw = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "PboxLeaseUntil"), "")
    if not (raw.isascii() and raw.isdigit()) or len(raw) > 12:
        return None
    try:
        until = datetime.datetime.fromtimestamp(int(raw), datetime.timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None
    return min(until, inst["LaunchTime"] + datetime.timedelta(hours=LEASE_MAX_HOURS))


def lambda_handler(event, context):
    # Find by TAG, not by a baked-in instance id: this box is created and destroyed
    # repeatedly, so its id is different every time it comes up.
    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": TAG_NAMES},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    instances = [i for r in resp["Reservations"] for i in r["Instances"]]
    if not instances:
        return {"checked": 0, "note": "no running parallel box"}

    results = []
    now = datetime.datetime.now(datetime.timezone.utc)

    def name_of(inst):
        return next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), "parallel-box")

    # One instance per single name: keep the oldest, terminate the rest, and say so.
    groups = {}
    for inst in instances:
        if name_of(inst) in SINGLE_NAMES:
            groups.setdefault(name_of(inst), []).append(inst)
    extra = set()
    for name, group in sorted(groups.items()):
        if len(group) < 2:
            continue
        group.sort(key=lambda i: (i["LaunchTime"], i["InstanceId"]))
        keep, ids = group[0]["InstanceId"], [i["InstanceId"] for i in group[1:]]
        extra.update(ids)
        print("terminating extra %s %s: only one may run (keeping the oldest, %s)" % (name, ",".join(ids), keep))
        try:
            ec2.terminate_instances(InstanceIds=ids)
            results.extend({"id": i, "action": "terminated_duplicate", "kept": keep} for i in ids)
            notify("%s: %d extra instance(s) terminated" % (name, len(ids)),
                   "%d instances named %s were running; only one may. Kept the oldest, %s, and terminated %s.\n"
                   "Someone launched more than one (a --count, an overridden template, or two launches at once)."
                   % (len(group), name, keep, ", ".join(ids)))
        except Exception as e:
            results.extend({"id": i, "action": "terminate_failed", "error": str(e)} for i in ids)
            notify("%s: FAILED to terminate extra instances" % name,
                   "%d instances named %s are running; only one may. TerminateInstances failed for %s: %s\n"
                   "They keep costing money until someone terminates them." % (len(group), name, ", ".join(ids), e))
    instances = [i for i in instances if i["InstanceId"] not in extra]

    for inst in instances:
        iid = inst["InstanceId"]
        launch = inst["LaunchTime"]
        age_min = (now - launch).total_seconds() / 60.0
        name = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), "parallel-box")

        max_age = MAX_AGE.get(name)
        if max_age is not None and age_min >= max_age:
            print("terminating %s %s: lifetime %d min reached (age %.0f min)" % (name, iid, max_age, age_min))
            try:
                ec2.terminate_instances(InstanceIds=[iid])
                results.append({"id": iid, "action": "terminated_lifetime", "age_min": round(age_min, 1)})
                notify("%s auto-terminated (lifetime)" % name,
                       "Instance %s (%s) reached its %d-minute lifetime and was terminated." % (iid, name, max_age))
            except Exception as e:
                results.append({"id": iid, "action": "terminate_failed", "error": str(e)})
                notify("%s FAILED to terminate (lifetime)" % name,
                       "Instance %s (%s) is past its %d-minute lifetime but TerminateInstances failed: %s\n"
                       "It keeps costing money until someone terminates it." % (iid, name, max_age, e))
            continue

        # A lease (`pbox up ... --lease`) keeps the box from the idle rule until it ends. The lifetime above still applies.
        lease = lease_until(inst) if name in LEASE_NAMES else None
        if lease is not None and now < lease:
            left = (lease - now).total_seconds() / 60.0
            print("keeping %s %s: leased until %s UTC (%.0f min left), not checked for idleness until then"
                  % (name, iid, lease.strftime("%Y-%m-%d %H:%M"), left))
            results.append({"id": iid, "action": "leased", "until": lease.isoformat(), "left_min": round(left, 1)})
            continue

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

        # Under the CPU bar, but a single thread or a disk-bound job looks exactly like that: any window of disk or
        # network bytes at or above its threshold keeps the box.
        io_active, ebs_missing = {}, set()
        for metric, threshold in sorted(IO_THRESHOLDS.items()):
            io = cw.get_metric_statistics(
                Namespace="AWS/EC2",
                MetricName=metric,
                Dimensions=[{"Name": "InstanceId", "Value": iid}],
                StartTime=now - datetime.timedelta(minutes=IDLE_MINUTES),
                EndTime=now,
                Period=300,
                Statistics=["Sum"],
            )
            points = io.get("Datapoints", [])
            if metric.startswith("EBS") and not points:
                ebs_missing.add(metric)
            io_peak = max((p.get("Sum", 0) for p in points), default=0)
            if io_peak >= threshold:
                io_active[metric] = int(io_peak)
        # AWS publishes the instance-level EBS series only for Nitro instances that are not bare metal (the kvm
        # flavors are all .metal): where one is missing, read the attached volumes' own AWS/EBS series instead,
        # summed across the volumes per window, against the same threshold.
        volumes = [m["Ebs"]["VolumeId"] for m in inst.get("BlockDeviceMappings", []) if m.get("Ebs", {}).get("VolumeId")]
        for metric in sorted(ebs_missing):
            if io_active or not volumes:
                break
            per_window = {}
            for vol in volumes:
                io = cw.get_metric_statistics(
                    Namespace="AWS/EBS",
                    MetricName=metric.replace("EBS", "Volume", 1),
                    Dimensions=[{"Name": "VolumeId", "Value": vol}],
                    StartTime=now - datetime.timedelta(minutes=IDLE_MINUTES),
                    EndTime=now,
                    Period=300,
                    Statistics=["Sum"],
                )
                for p in io.get("Datapoints", []):
                    per_window[p.get("Timestamp")] = per_window.get(p.get("Timestamp"), 0) + p.get("Sum", 0)
            io_peak = max(per_window.values(), default=0)
            if io_peak >= IO_THRESHOLDS[metric]:
                io_active[metric.replace("EBS", "Volume", 1)] = int(io_peak)
        if io_active:
            results.append({"id": iid, "action": "busy_io", "peak_cpu": round(peak, 1), "io_bytes_5m": io_active})
            continue

        if name.split("-", 1)[0] in ("wbox", "pbox", "gbox"):
            # A member's own box (member-boxes.tf), "<kind>-<person>": the person runs the plain
            # command from their own account, which takes no slot.
            kind = name.split("-", 1)[0]
            # A pbox takes a flavor (pbox-flavors.tf), which the watchdog does not know: it names the type the box ran as.
            flavor = " FLAVOR" if kind == "pbox" else ""
            up_cmd = "%s up%s   (run by %s)" % (kind, flavor, name.split("-", 1)[1])
            if kind == "pbox":
                up_cmd += "\n  (it ran as %s; `pbox flavors` lists the flavors)" % inst.get("InstanceType", "?")
            kept = {"wbox": "Its D: disk (Roblox and its caches) is untouched",
                    "pbox": "Its work volume is untouched",
                    "gbox": "Nothing on it persists"}[kind]
        elif name.startswith("gpu-box"):
            # gpu-box is slot 1; gpu-box-N is slot N.
            up_cmd = "gbox up" + ("" if name == "gpu-box" else " " + name.rsplit("-", 1)[1])
            kept = "Nothing on it persists"
        else:
            # parallel-box is slot 1; parallel-box-N is slot N (the same shape as gbox).
            up_cmd = ("pbox up" + ("" if name == "parallel-box" else " " + name.rsplit("-", 1)[1]) + " FLAVOR"
                      + "\n  (it ran as %s; `pbox flavors` lists the flavors)" % inst.get("InstanceType", "?"))
            fallback = any(t["Key"] == "Fallback" and t["Value"] == "true" for t in inst.get("Tags", []))
            # A fallback box (pbox-fallback.tf) ran on a fresh volume that dies with it; only the home volume persists.
            kept = ("It was a fallback box: its fresh work volume was deleted with it (the home work volume is untouched)"
                    if fallback else "Its work volume is untouched")
        io_note = " and under its disk and network thresholds" if IO_THRESHOLDS else ""
        lease_note = " Its lease ended at %s UTC." % lease.strftime("%Y-%m-%d %H:%M") if lease is not None else ""
        print("terminating idle %s %s (peak CPU %.1f%% over %dmin%s)%s" % (name, iid, peak, IDLE_MINUTES, io_note, lease_note))
        try:
            ec2.terminate_instances(InstanceIds=[iid])
            results.append({"id": iid, "action": "terminated", "peak_cpu": round(peak, 1)})
            notify(
                "%s auto-terminated (idle)" % name,
                "Instance %s (%s) was below %.1f%% CPU for %d minutes (peak %.1f%%)%s and was terminated.%s\n\n"
                "%s -- bring the box back with:\n"
                "  %s\n" % (iid, name, IDLE_CPU, IDLE_MINUTES, peak, io_note, lease_note, kept, up_cmd),
            )
        except Exception as e:
            results.append({"id": iid, "action": "terminate_failed", "error": str(e)})
            # Silent failure here is how a box outlives its watchdog (termination protection,
            # a policy change): say so, loudly.
            notify("%s FAILED to terminate (idle)" % name,
                   "Instance %s (%s) is idle but TerminateInstances failed: %s\n"
                   "It keeps costing money until someone terminates it." % (iid, name, e))

    print(json.dumps(results))
    return {"checked": len(instances), "results": results}
  PY
}

# Every box this watchdog reaps, by exact Name tag: what it describes (TAG_NAMES) and what its
# role may terminate are the same list, so a new GPU slot cannot be found but unkillable.
locals {
  parallel_watchdog_tag_names = concat(local.parallel_box_names, local.gpu_box_names, local.member_gbox_names, local.member_pbox_names)
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
  provider = aws.west2
  name     = "parallel-box-watchdog"
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
  provider   = aws.west2
  role       = aws_iam_role.parallel_watchdog.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "parallel_watchdog" {
  provider = aws.west2
  name     = "parallel-box-watchdog-policy"
  role     = aws_iam_role.parallel_watchdog.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ec2:DescribeInstances", "cloudwatch:GetMetricStatistics"]
        Resource = "*"
      },
      {
        # Scoped by tag so this role can only ever terminate the parallel boxes and the GPU
        # boxes, never a dev box or the jumpbox. Explicit list, not a wildcard, so nothing
        # else that happens to share a prefix can ever match.
        Effect   = "Allow"
        Action   = "ec2:TerminateInstances"
        Resource = "*"
        Condition = {
          StringEquals = { "ec2:ResourceTag/Name" = local.parallel_watchdog_tag_names }
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
  provider         = aws.west2
  function_name    = "parallel-box-watchdog"
  role             = aws_iam_role.parallel_watchdog.arn
  handler          = "index.lambda_handler"
  runtime          = "python3.12"
  timeout          = 60
  filename         = data.archive_file.parallel_watchdog.output_path
  source_code_hash = data.archive_file.parallel_watchdog.output_base64sha256

  environment {
    variables = {
      IDLE_MINUTES = tostring(var.parallel_box_idle_minutes)
      IDLE_CPU_PCT = tostring(var.parallel_box_idle_cpu_pct)
      TAG_NAMES    = join(",", local.parallel_watchdog_tag_names)
      # Native diagnostics have a fixed 30-minute lifetime; existing GPU slots/members keep theirs.
      MAX_AGE_MINUTES = jsonencode({ for n in concat(local.gpu_box_names, local.member_gbox_names) : n => n == local.gpu_box_slots["native"] ? 30 : var.gpu_box_max_hours * 60 })
      # One native diagnostic, as well as one instance per member gbox.
      SINGLE_NAMES  = join(",", concat(local.member_gbox_names, [local.gpu_box_slots["native"]]))
      IO_THRESHOLDS = local.parallel_watchdog_io_thresholds
      # Only the owner's parallel boxes honour a lease (`pbox up --lease`): not the GPU test boxes or the members' boxes.
      LEASE_NAMES   = join(",", local.parallel_box_names)
      SNS_REGION    = var.aws_region
      SNS_TOPIC_ARN = aws_sns_topic.cost_alerts.arn
    }
  }

  tags = { Name = "parallel-box-watchdog" }
}

# Every 5 minutes: at ~$3.14/hr, a missed reap costs real money, and the check is free.
resource "aws_cloudwatch_event_rule" "parallel_watchdog" {
  provider            = aws.west2
  name                = "parallel-box-watchdog"
  description         = "Terminate the parallel box when idle"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "parallel_watchdog" {
  provider  = aws.west2
  rule      = aws_cloudwatch_event_rule.parallel_watchdog.name
  target_id = "parallel-box-watchdog"
  arn       = aws_lambda_function.parallel_watchdog.arn
}

resource "aws_lambda_permission" "parallel_watchdog" {
  provider      = aws.west2
  statement_id  = "AllowExecutionFromCloudWatch"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.parallel_watchdog.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.parallel_watchdog.arn
}

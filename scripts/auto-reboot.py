"""auto-reboot: reboot a persistent box that has wedged. See auto-reboot.tf for the why.

Runs every five minutes. For each RUNNING instance whose Name is on the list it asks CloudWatch three questions:

  * has the instance status check been failing for STATUS_MINUTES in a row?  (the guest OS is unreachable)
  * has NetworkOut stayed under NETWORK_FLOOR_BYTES for NETWORK_MINUTES in a row?  (the guest is alive to EC2
    and dead to everyone else: 2026-10-01 nextjs-dev sat like this for 20 hours; 2026-07-25 and 2026-08-16 were
    the same with the status check still "ok")
  * has the box been PAGING for THRASH_MINUTES in a row: EBS reads at the volume's cap with writes near zero?
    (2026-10-10 nextjs-dev: SSH timed out and both tunnels were down for 20+ minutes while the status check
    read ok and NetworkOut fell to 27 KB per five minutes, above even the floor, so neither rule above fired. A 14-day
    backtest on every box here found this signature only in that wedge and in 2026-10-01's, never in real work)

A FLOOR, not exactly zero, because of 2026-10-08: the jumpbox took an RCU stall (`rcu_sched detected stalls`),
which starves userspace while leaving the kernel's TCP stack answering. Both vCPUs pinned at 99%, SSH accepted
the connection and never sent a banner, SSM went Delayed then ConnectionLost -- and NetworkOut fell to 11,468
bytes per five minutes but NEVER to zero, because bare ARP and SYN-ACK still go out. The status check stayed
"ok" the whole time, so neither signal fired and the box would have sat wedged indefinitely. A trickle is not
life; measure against the floor.

Any one makes it wedged. A wedged box gets its console snapshotted FIRST (the existing redacting capture
Lambda), then an OS reboot, then a notification. Never stop/start: a reboot keeps the console buffer and the
instance-store disks. A box that is not running is never touched, so a deliberate stop stays a stop.

Brakes: at most one reboot per COOLDOWN_SECONDS and MAX_PER_DAY a day per instance. Past that it only alerts,
because a box that wedges again straight after a reboot needs a person, not a loop.

A SYSTEM status check failure (the host, not the guest) is alert-only: a reboot does not fix it.

Invoke with {"dry_run": true} to see what it would do without doing it.
"""
import json
import os
import time

import boto3

STATUS_MINUTES = 15
NETWORK_MINUTES = 20
THRASH_MINUTES = 20
THRASH_READ_BYTES = 30 * 1024 ** 3  # per five minutes: about 107 MB/s, 86% of a gp3 volume's default 125 MB/s
THRASH_WRITE_SHARE = 0.1           # paging reads pages back and writes almost nothing; a build or a copy writes
# Below this many bytes of NetworkOut per five-minute bucket the box is "silent". Measured over the four days to
# 2026-10-08, the quietest HEALTHY bucket of any box on the list was jumpbox-2 at 34,909 bytes (jumpbox 68,259,
# claude-master-server 152,048, nextjs-dev 684,878), while the jumpbox's RCU-stall wedge sat at 11,468. 20 KiB is
# the geometric midpoint of that gap: ~1.8x above the wedge, ~1.7x below the quietest healthy bucket. Re-measure
# before trusting it on a box quieter than jumpbox-2; the 20-minute window and the brakes below are what keep a
# mis-set floor from becoming a reboot loop.
NETWORK_FLOOR_BYTES = 20 * 1024
MIN_AGE_SECONDS = 30 * 60        # a box still booting fails its checks; so does one that was just started
COOLDOWN_SECONDS = 3 * 3600
MAX_PER_DAY = 3
ALERT_EVERY_SECONDS = 3 * 3600   # a still-wedged box is mentioned this often, not every five minutes

NAMES = [n for n in os.environ.get("TARGET_NAMES", "").split(",") if n]
REGIONS = [r for r in os.environ.get("REGIONS", "us-west-1").split(",") if r]
TABLE = os.environ.get("STATE_TABLE", "")
TOPIC = os.environ.get("SNS_TOPIC_ARN", "")
CAPTURE_FUNCTION = os.environ.get("CONSOLE_CAPTURE_FUNCTION", "")
HOME_REGION = os.environ.get("AWS_REGION", "us-west-1")


# ------------------------------------------------------------------ decisions (pure)

def failing_for(points, minutes, period_seconds, now):
    """True when the last `minutes` of a series are ALL bad, in CONSECUTIVE buckets that END RECENTLY.
    points: [(bucket_start_epoch, bad)] in time order. Fewer buckets than the window, a gap, or a newest bucket
    that is old (CloudWatch stopped delivering, or the box was healthy since) is not evidence: old zeros must never
    reboot a box that is fine now."""
    need = max(1, (minutes * 60) // period_seconds)
    # Only COMPLETED buckets count: the bucket CloudWatch is still filling is partial, and counting it would
    # fire the 20-minute rule after fifteen.
    points = [p for p in points if p[0] + period_seconds <= now]
    if len(points) < need:
        return False
    tail = points[-need:]
    if now - (tail[-1][0] + period_seconds) > 2 * period_seconds:
        return False
    if any(b[0] - a[0] != period_seconds for a, b in zip(tail, tail[1:])):
        return False
    return all(bad for _, bad in tail)


def host_failing_now(system_failed, now):
    """The newest COMPLETED system-status bucket is failing and recent: any current host fault, however young.
    It vetoes the guest recovery path, which cannot fix AWS hardware."""
    done = [p for p in system_failed if p[0] + 60 <= now]
    return bool(done) and now - (done[-1][0] + 60) <= 3 * 60 and done[-1][1] >= 1


def paging(ebs_read, ebs_write):
    """[(ts, bad)] per five minutes: bad when that bucket read at least THRASH_READ_BYTES and wrote at most
    THRASH_WRITE_SHARE of what it read. A bucket with no write sample is not evidence and is never bad: missing data
    must not reboot a box."""
    writes = dict(ebs_write)
    return [(t, t in writes and r >= THRASH_READ_BYTES and writes[t] <= r * THRASH_WRITE_SHARE) for t, r in ebs_read]


def wedged(status_failed, network_out, now, ebs_read=(), ebs_write=()):
    """(reason or None). status_failed: [(ts, StatusCheckFailed_Instance)] per minute (1 failed). network_out,
    ebs_read, ebs_write: [(ts, bytes)] per five minutes."""
    if failing_for([(t, v >= 1) for t, v in status_failed], STATUS_MINUTES, 60, now):
        return "the instance status check has failed for %d minutes" % STATUS_MINUTES
    if failing_for([(t, v < NETWORK_FLOOR_BYTES) for t, v in network_out], NETWORK_MINUTES, 300, now):
        return "no network traffic out for %d minutes" % NETWORK_MINUTES
    if failing_for(paging(ebs_read, ebs_write), THRASH_MINUTES, 300, now):
        return "paging for %d minutes (disk reads at the volume's cap, writes near zero)" % THRASH_MINUTES
    return None


def may_reboot(history, now):
    """(ok, why not). history: epoch seconds of earlier automatic reboots."""
    recent = [t for t in history if now - t < 86400]
    if history and now - max(history) < COOLDOWN_SECONDS:
        return False, "rebooted %d minutes ago; waiting for the cooldown" % ((now - max(history)) // 60)
    if len(recent) >= MAX_PER_DAY:
        return False, "already rebooted %d times in 24 hours" % len(recent)
    return True, ""


# ------------------------------------------------------------------ AWS

def series(cw, instance_id, metric, stat, period, minutes, now):
    start = now - (minutes + 10) * 60
    resp = cw.get_metric_statistics(
        Namespace="AWS/EC2", MetricName=metric, Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
        StartTime=start, EndTime=now + 60, Period=period, Statistics=[stat])
    points = sorted(resp.get("Datapoints", []), key=lambda d: d["Timestamp"])
    return [(int(p["Timestamp"].timestamp()) if hasattr(p["Timestamp"], "timestamp") else int(p["Timestamp"]), p[stat]) for p in points]


def load_history(ddb, instance_id):
    item = ddb.get_item(TableName=TABLE, Key={"instance_id": {"S": instance_id}}).get("Item", {})
    return ([int(x["N"]) for x in item.get("reboots", {}).get("L", [])], int(item.get("last_alert", {}).get("N", "0")))


def save_history(ddb, instance_id, reboots, last_alert, reserve_after=None):
    """Writes the item. With reserve_after it is CONDITIONAL: it succeeds only if no reboot was recorded since that
    time, so two overlapping runs cannot both reboot a box. Returns False when the condition failed."""
    item = {
        "instance_id": {"S": instance_id},
        "reboots": {"L": [{"N": str(t)} for t in reboots[-10:]]},
        "last_alert": {"N": str(last_alert)},
        "last_reboot": {"N": str(max(reboots) if reboots else 0)},
    }
    kwargs = {}
    if reserve_after is not None:
        kwargs = {"ConditionExpression": "attribute_not_exists(last_reboot) OR last_reboot < :cutoff",
                  "ExpressionAttributeValues": {":cutoff": {"N": str(reserve_after)}}}
    try:
        ddb.put_item(TableName=TABLE, Item=item, **kwargs)
    except Exception as exc:
        if exc.__class__.__name__ == "ConditionalCheckFailedException":
            return False
        raise
    return True


def record_alert(ddb, instance_id, when):
    """Notes that a box was just mentioned, touching ONLY last_alert: rewriting the whole item from state read
    earlier could erase a reboot reservation another run has just made."""
    ddb.update_item(TableName=TABLE, Key={"instance_id": {"S": instance_id}},
                    UpdateExpression="SET last_alert = :a", ExpressionAttributeValues={":a": {"N": str(when)}})


def notify(sns, subject, body):
    print(subject + " | " + body.replace("\n", " "))
    if TOPIC:
        sns.publish(TopicArn=TOPIC, Subject=subject[:100], Message=body)


def capture_console(lam, instance_id, region):
    """The existing capture Lambda reads the live console, redacts private keys and archives it. It is in the home
    region and reads only its own region's instances. It reports a failed read or an empty buffer in its RESULT,
    not as a function error, so the result is read before anything is claimed."""
    if not CAPTURE_FUNCTION or region != HOME_REGION:
        return "not captured (the capture function reads %s only)" % HOME_REGION
    event = {"Records": [{"Sns": {"Message": json.dumps({
        "NewStateValue": "ALARM", "AlarmName": "auto-reboot status capture",
        "Trigger": {"Dimensions": [{"name": "InstanceId", "value": instance_id}]}})}}]}
    try:
        resp = lam.invoke(FunctionName=CAPTURE_FUNCTION, InvocationType="RequestResponse", Payload=json.dumps(event).encode())
        if resp.get("FunctionError"):
            return "capture function failed"
        body = resp.get("Payload")
        result = json.loads(body.read() if hasattr(body, "read") else (body or "{}"))
        entries = [e for e in result.get("captured", []) if e.get("instance") == instance_id]
        if entries and entries[0].get("stream"):
            return "console archived to /dev-servers/console-capture (%s)" % entries[0]["stream"]
        detail = entries[0].get("error") or entries[0].get("note") if entries else "no result for this instance"
        return "NOT archived: %s" % detail
    except Exception as exc:  # never let a failed snapshot keep a wedged box down
        return "capture failed: %s" % exc


def handle_instance(inst, region, clients, now, dry_run):
    iid = inst["InstanceId"]
    name = next((t["Value"] for t in inst.get("Tags", []) if t["Key"] == "Name"), iid)
    out = {"instance": iid, "name": name, "region": region}
    age = now - int(inst["LaunchTime"].timestamp())
    if age < MIN_AGE_SECONDS:
        return dict(out, action="skip", why="started %d minutes ago" % (age // 60))
    cw = clients["cw"][region]
    history, last_alert = load_history(clients["ddb"], iid)

    # A HOST problem first, and as a veto: a reboot does not fix AWS hardware, and a dead host also zeroes the
    # network, which would otherwise read as a guest wedge.
    system = series(cw, iid, "StatusCheckFailed_System", "Maximum", 60, STATUS_MINUTES, now)
    if host_failing_now(system, now):
        if now - last_alert >= ALERT_EVERY_SECONDS and not dry_run:
            notify(clients["sns"], "%s: AWS host problem" % name,
                   "%s (%s) is failing its SYSTEM status check. A reboot does not fix a host problem; stop/start "
                   "moves it to new hardware, which is a decision for you (it clears instance-store disks)." % (name, iid))
            record_alert(clients["ddb"], iid, now)
        return dict(out, action="alert", why="system status check failing")

    status = series(cw, iid, "StatusCheckFailed_Instance", "Maximum", 60, STATUS_MINUTES, now)
    network = series(cw, iid, "NetworkOut", "Sum", 300, NETWORK_MINUTES, now)
    ebs_read = series(cw, iid, "EBSReadBytes", "Sum", 300, THRASH_MINUTES, now)
    ebs_write = series(cw, iid, "EBSWriteBytes", "Sum", 300, THRASH_MINUTES, now)
    reason = wedged(status, network, now, ebs_read, ebs_write)
    if not reason:
        return dict(out, action="none")
    ok, why_not = may_reboot(history, now)
    if not ok:
        if now - last_alert >= ALERT_EVERY_SECONDS and not dry_run:
            notify(clients["sns"], "%s is still wedged; NOT rebooting it again" % name,
                   "%s (%s): %s, and I will not reboot it again: %s. It needs a person." % (name, iid, reason, why_not))
            record_alert(clients["ddb"], iid, now)
        return dict(out, action="blocked", reason=reason, why=why_not)
    if dry_run:
        return dict(out, action="would-reboot", reason=reason)

    # RESERVE the reboot durably before doing it. If the write fails, nothing is rebooted (the error is raised);
    # if another run got there first, the conditional write fails and this one stands down. Otherwise a reboot
    # followed by a failed bookkeeping write would be repeated every five minutes, past both brakes.
    if not save_history(clients["ddb"], iid, history + [now], last_alert, reserve_after=now - COOLDOWN_SECONDS):
        return dict(out, action="blocked", reason=reason, why="another run has just rebooted it")
    snapshot = capture_console(clients["lambda"], iid, region)
    try:
        clients["ec2"][region].reboot_instances(InstanceIds=[iid])
    except Exception:
        save_history(clients["ddb"], iid, history, last_alert)       # it was not rebooted: give the reservation back
        raise
    notify(clients["sns"], "%s wedged: rebooting it" % name,
           "%s (%s) in %s: %s.\n\nConsole: %s.\nAction: OS reboot (not stop/start: the console buffer and any "
           "instance-store disks survive). It will not be rebooted again for %d hours; at most %d times a day."
           % (name, iid, region, reason, snapshot, COOLDOWN_SECONDS // 3600, MAX_PER_DAY))
    return dict(out, action="rebooted", reason=reason, console=snapshot)


def make_clients():
    return {
        "ec2": {r: boto3.client("ec2", region_name=r) for r in REGIONS},
        "cw": {r: boto3.client("cloudwatch", region_name=r) for r in REGIONS},
        "ddb": boto3.client("dynamodb", region_name=HOME_REGION),
        "sns": boto3.client("sns", region_name=HOME_REGION),
        "lambda": boto3.client("lambda", region_name=HOME_REGION),
    }


def lambda_handler(event, context, clients=None, now=None):
    dry_run = bool((event or {}).get("dry_run"))
    clients = clients or make_clients()
    now = int(time.time()) if now is None else now
    results = []
    for region in REGIONS:
        resp = clients["ec2"][region].describe_instances(Filters=[
            {"Name": "tag:Name", "Values": NAMES}, {"Name": "instance-state-name", "Values": ["running"]}])
        for reservation in resp.get("Reservations", []):
            for inst in reservation.get("Instances", []):
                try:
                    results.append(handle_instance(inst, region, clients, now, dry_run))
                except Exception as exc:  # one bad box must not stop the others being checked
                    results.append({"instance": inst.get("InstanceId"), "action": "error", "why": str(exc)})
                    print("error on %s: %s" % (inst.get("InstanceId"), exc))
    print(json.dumps(results, default=str))
    errors = [r for r in results if r.get("action") == "error"]
    if errors and not dry_run:
        # Every box has been looked at; NOW fail, so AWS/Lambda Errors counts it and the alarm fires. Without this a
        # permission or API fault would disable the watch for a box while the invocation reported success.
        notify(clients["sns"], "auto-reboot: %d instance check(s) failed" % len(errors),
               "\n".join("%s: %s" % (e.get("instance"), e.get("why")) for e in errors))
        raise RuntimeError("%d instance check(s) failed: %s" % (len(errors), "; ".join(str(e.get("why")) for e in errors)))
    return {"dry_run": dry_run, "results": results}

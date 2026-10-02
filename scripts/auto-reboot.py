"""auto-reboot: reboot a persistent box that has wedged. See auto-reboot.tf for the why.

Runs every five minutes. For each RUNNING instance whose Name is on the list it asks CloudWatch two questions:

  * has the instance status check been failing for STATUS_MINUTES in a row?  (the guest OS is unreachable)
  * has NetworkOut been exactly zero for NETWORK_MINUTES in a row?           (the guest is alive to EC2 and
    dead to everyone else: 2026-10-01 nextjs-dev sat like this for 20 hours; 2026-07-25 and 2026-08-16 were
    the same with the status check still "ok")

Either one makes it wedged. A wedged box gets its console snapshotted FIRST (the existing redacting capture
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

def failing_for(points, minutes, period_seconds):
    """True when the last `minutes` of a 0/1 or sum series are ALL bad. `points` is a list of values in time
    order, one per period. A missing period is not evidence: fewer points than needed means no."""
    need = max(1, (minutes * 60) // period_seconds)
    return len(points) >= need and all(points[-need:])


def wedged(status_failed, network_out):
    """(reason or None). status_failed: StatusCheckFailed_Instance per minute (1 failed). network_out: bytes
    sent per five minutes."""
    if failing_for([v >= 1 for v in status_failed], STATUS_MINUTES, 60):
        return "the instance status check has failed for %d minutes" % STATUS_MINUTES
    if failing_for([v == 0 for v in network_out], NETWORK_MINUTES, 300):
        return "no network traffic out for %d minutes" % NETWORK_MINUTES
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
    return [p[stat] for p in points]


def load_history(ddb, instance_id):
    item = ddb.get_item(TableName=TABLE, Key={"instance_id": {"S": instance_id}}).get("Item", {})
    return ([int(x["N"]) for x in item.get("reboots", {}).get("L", [])], int(item.get("last_alert", {}).get("N", "0")))


def save_history(ddb, instance_id, reboots, last_alert):
    ddb.put_item(TableName=TABLE, Item={
        "instance_id": {"S": instance_id},
        "reboots": {"L": [{"N": str(t)} for t in reboots[-10:]]},
        "last_alert": {"N": str(last_alert)},
    })


def notify(sns, subject, body):
    print(subject + " | " + body.replace("\n", " "))
    if TOPIC:
        sns.publish(TopicArn=TOPIC, Subject=subject[:100], Message=body)


def capture_console(lam, instance_id, region):
    """The existing capture Lambda reads the live console, redacts private keys and archives it. It is
    in the home region and reads only its own region's instances."""
    if not CAPTURE_FUNCTION or region != HOME_REGION:
        return "not captured (the capture function reads %s only)" % HOME_REGION
    event = {"Records": [{"Sns": {"Message": json.dumps({
        "NewStateValue": "ALARM", "AlarmName": "auto-reboot status capture",
        "Trigger": {"Dimensions": [{"name": "InstanceId", "value": instance_id}]}})}}]}
    try:
        resp = lam.invoke(FunctionName=CAPTURE_FUNCTION, InvocationType="RequestResponse", Payload=json.dumps(event).encode())
        if resp.get("FunctionError"):
            return "capture function failed"
        return "console archived to /dev-servers/console-capture"
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
    status = series(cw, iid, "StatusCheckFailed_Instance", "Maximum", 60, STATUS_MINUTES, now)
    network = series(cw, iid, "NetworkOut", "Sum", 300, NETWORK_MINUTES, now)
    reason = wedged(status, network)
    if not reason:
        system = series(cw, iid, "StatusCheckFailed_System", "Maximum", 60, STATUS_MINUTES, now)
        if failing_for([v >= 1 for v in system], STATUS_MINUTES, 60):
            reason = None
            history, last_alert = load_history(clients["ddb"], iid)
            if now - last_alert >= ALERT_EVERY_SECONDS and not dry_run:
                notify(clients["sns"], "%s: AWS host problem" % name,
                       "%s (%s) has failed its SYSTEM status check for %d minutes. A reboot does not fix a host "
                       "problem; stop/start moves it to new hardware, which is a decision for you (it clears "
                       "instance-store disks)." % (name, iid, STATUS_MINUTES))
                save_history(clients["ddb"], iid, history, now)
            return dict(out, action="alert", why="system status check failing")
        return dict(out, action="none")
    history, last_alert = load_history(clients["ddb"], iid)
    ok, why_not = may_reboot(history, now)
    if not ok:
        if now - last_alert >= ALERT_EVERY_SECONDS and not dry_run:
            notify(clients["sns"], "%s is still wedged; NOT rebooting it again" % name,
                   "%s (%s): %s, and I will not reboot it again: %s. It needs a person." % (name, iid, reason, why_not))
            save_history(clients["ddb"], iid, history, now)
        return dict(out, action="blocked", reason=reason, why=why_not)
    if dry_run:
        return dict(out, action="would-reboot", reason=reason)
    snapshot = capture_console(clients["lambda"], iid, region)
    clients["ec2"][region].reboot_instances(InstanceIds=[iid])
    save_history(clients["ddb"], iid, history + [now], last_alert)
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
    return {"dry_run": dry_run, "results": results}

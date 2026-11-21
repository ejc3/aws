"""Guard the production Spot drill. Only this serialized Lambda writes its S3 receipt."""

import json
import os
import re
import time
import uuid

OWNER = {"Drill": "fcvm-fleet-prod-spot"}
TERMINAL = {"completed", "stopped", "failed", "cancelled"}
# FIS stop does not retract an interruption notice already sent to EC2.
RECOVERY_SECONDS = 4 * 60
HISTORY_SECONDS = 120 * 24 * 60 * 60  # FIS retires terminal experiment records after 120 days.


def pages(call, collection, token_key="nextToken", **params):
    while True:
        page = call(**params)
        yield from page.get(collection, [])
        if not page.get(token_key):
            return
        params[token_key] = page[token_key]


def healthy_hosts(ec2):
    filters = {"tag:Fleet": "prod", "tag:Purpose": "fcvm-fleet-host", "instance-state-name": "running"}
    reservations = pages(
        ec2.describe_instances,
        "Reservations",
        "NextToken",
        Filters=[{"Name": key, "Values": [value]} for key, value in filters.items()],
    )
    hosts = {i["InstanceId"]: i for r in reservations for i in r["Instances"]}
    if not hosts:
        raise ValueError("No running production hosts.")
    healthy = set()
    # DescribeInstanceStatus accepts at most 100 IDs per call.
    ids = sorted(hosts)
    for offset in range(0, len(ids), 100):
        for status in pages(
            ec2.describe_instance_status,
            "InstanceStatuses",
            "NextToken",
            InstanceIds=ids[offset : offset + 100],
        ):
            if all(status[key]["Status"] == "ok" for key in ("SystemStatus", "InstanceStatus")):
                healthy.add(status["InstanceId"])
    zones = {key: i["Placement"]["AvailabilityZone"] for key, i in hosts.items()}
    spot = {key for key, i in hosts.items() if i.get("InstanceLifecycle") == "spot"}
    if not spot or not spot <= healthy or len({zones[key] for key in healthy}) < 2:
        raise ValueError(
            "Every eligible Spot host must be healthy, with healthy production hosts in two AZs."
        )
    return zones


def process(event, fis, ec2, read, save, template, now):
    action = event.get("action")
    if action not in ("start", "status", "stop"):
        raise ValueError("Expected start, status or stop.")
    allowed = {
        "start": {"action", "request_id"},
        "status": {"action", "experiment_id"},
        "stop": {"action", "experiment_id"},
    }
    if set(event) - allowed[action]:
        raise ValueError("Unexpected drill argument.")
    state = read()
    receipts = state.setdefault("requests", {})

    def owned(eid):
        exp = fis.get_experiment(id=eid)["experiment"]
        if exp["experimentTemplateId"] != template or exp.get("tags", {}).get("Drill") != OWNER["Drill"]:
            raise ValueError("Experiment does not belong to this drill.")
        return exp

    def selected(exp, receipt):
        state_name = exp["state"]["status"]
        azs = set(receipt.get("target_azs", []))
        if state_name not in ("pending", "initiating", "expired") and not azs:
            resolved = pages(
                fis.list_experiment_resolved_targets,
                "resolvedTargets",
                experimentId=exp["id"],
                targetName="one-prod-host",
            )
            for target in resolved:
                for value in target.get("targetInformation", {}).values():
                    instance = str(value).rsplit("/", 1)[-1]
                    if re.fullmatch(r"i-[0-9a-f]{8}(?:[0-9a-f]{9})?", instance):
                        az = receipt.get("hosts", {}).get(instance)
                        if az:
                            azs.add(az)
        started = exp["startTime"].isoformat() if exp.get("startTime") else receipt.get("started_at")
        azs = sorted(azs)
        if receipt and (started != receipt.get("started_at") or azs != receipt.get("target_azs", [])):
            receipt.update(started_at=started, target_azs=azs)
            save(state)
        return {"experiment_id": exp["id"], "state": state_name, "started_at": started, "target_azs": azs}

    def recorded(record):
        try:
            return owned(record["id"])
        except fis.exceptions.ClientError as error:
            if error.response["Error"]["Code"] not in {"ResourceNotFoundException", "AccessDeniedException"}:
                raise
            if now < record.get("accepted_at", record["requested_at"]) + HISTORY_SECONDS:
                raise
            # FIS drops terminal records after 120 days. A deleted experiment has no Drill
            # tag, so the scoped Get can return AccessDenied. Confirm absence in FIS first.
            if any(exp["id"] == record["id"] for exp in observed()):
                raise
            return {"id": record["id"], "state": {"status": "expired"}}

    def observed():
        return [
            x for x in pages(fis.list_experiments, "experiments") if x["experimentTemplateId"] == template
        ]

    def accept(record, exp):
        record["id"] = exp["id"]
        record["accepted_at"] = exp["creationTime"].timestamp()
        save(state)

    latest = state.get("latest")
    last = receipts.get(latest, {})
    experiments = None
    if last and not last.get("id") and not last.get("expired"):
        # A crash may have fallen between accepting the FIS run and saving its ID.
        experiments = observed()
        for exp in experiments:
            if exp.get("tags", {}).get("Request") == latest and exp["tags"].get("Drill") == OWNER["Drill"]:
                accept(last, exp)
                break
        else:
            if now >= last["last_attempted_at"] + HISTORY_SECONDS and all(
                e["state"]["status"] in TERMINAL for e in experiments
            ):
                last["expired"] = True
                save(state)

    if action != "start":
        eid = event.get("experiment_id")
        if not eid and action == "stop":
            raise ValueError("Pass the experiment_id to stop.")
        record = last if not eid else next((r for r in receipts.values() if r.get("id") == eid), {})
        eid = eid or record.get("id")
        if not eid:
            if record.get("expired"):
                return {"state": "expired", "request_id": latest}
            return {"state": "pending request" if latest else "no drill", "request_id": latest}
        exp = recorded(record) if record else owned(eid)
        if action == "stop" and exp["state"]["status"] not in TERMINAL | {"expired", "stopping"}:
            exp = fis.stop_experiment(id=eid)["experiment"]
        return selected(exp, record)

    request = str(uuid.UUID(event["request_id"]))
    record = receipts.get(request)
    if record and record.get("expired"):
        return {"state": "expired", "request_id": request}
    if record and record.get("id"):
        return selected(recorded(record), record)
    if latest and latest != request and not last.get("id") and not last.get("expired"):
        raise ValueError("Pending drill reservation. Check status and retry with request_id " + latest)
    if experiments is None:
        experiments = observed()
    if any(e["state"]["status"] not in TERMINAL for e in experiments):
        raise ValueError("A production Spot drill is already active.")
    if latest:
        last_id = receipts[latest].get("id")
        if last_id:
            prev = recorded(receipts[latest])
            if prev["state"]["status"] not in TERMINAL | {"expired"}:
                raise ValueError("A production Spot drill is already active.")
            if prev["state"]["status"] != "expired":
                end = prev.get("endTime")
                if end is None or now < end.timestamp() + RECOVERY_SECONDS:
                    raise ValueError("The preceding interruption is still in its recovery window.")

    hosts = healthy_hosts(ec2)
    if record is None:
        record = receipts[request] = {"hosts": {}, "requested_at": now}
        state["latest"] = request
    record["hosts"].update(hosts)
    record["last_attempted_at"] = now
    save(state)
    exp = fis.start_experiment(
        experimentTemplateId=template, clientToken=request, tags={**OWNER, "Request": request}
    )["experiment"]
    accept(record, exp)
    return selected(exp, record)


def handler(event, context):
    import boto3

    s3 = boto3.client("s3", region_name=os.environ["STATE_REGION"])
    obj = {"Bucket": os.environ["STATE_BUCKET"], "Key": os.environ["STATE_KEY"]}

    def read():
        return json.loads(s3.get_object(**obj)["Body"].read())

    def save(state):
        s3.put_object(**obj, Body=json.dumps(state).encode(), ContentType="application/json")

    return process(
        event, boto3.client("fis"), boto3.client("ec2"), read, save, os.environ["TEMPLATE_ID"], time.time()
    )

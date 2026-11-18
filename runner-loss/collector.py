"""Retain sanitized observations of fcvm CI runner terminations."""

import base64
import gzip
import json
import os
import re
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError


RUNNER_NAMES = {"github-runner-arm64": "arm64", "github-runner-x86_64": "x86_64"}
END_STATES = ["shutting-down", "terminated"]
GROUPS = {f"/aws/lambda/github-runner-{controller}": controller
          for controller in ("cleanup", "webhook", "reuse")}
REASONS = {
    "bootstrap-failure", "stalled-launch", "hard-ceiling", "unregistered-lease-expired",
    "drained-soft-cap", "lease-expired", "job-runtime-limit", "idle-no-demand",
}


def emit(**fields):
    print(json.dumps(fields, separators=(",", ":")))


def matches(pattern, value):
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def instance_id(value):
    return matches(r"i-(?:[0-9a-f]{8}|[0-9a-f]{17})", value)


def timestamp(value):
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(timezone.utc).isoformat()
    return None


def controller_events(batch, regions):
    try:
        payload = json.loads(gzip.decompress(base64.b64decode(batch["data"], validate=True)))
    except (KeyError, OSError, EOFError, ValueError, TypeError):
        raise ValueError("Malformed CloudWatch Logs subscription batch") from None
    if not isinstance(payload, dict):
        raise ValueError("Malformed CloudWatch Logs subscription envelope")
    if payload.get("messageType") != "DATA_MESSAGE" or payload.get("logGroup") not in GROUPS:
        return
    for record in payload["logEvents"]:
        try:
            entry = json.loads(record["message"])
        except (KeyError, ValueError, TypeError):
            continue
        if not isinstance(entry, dict) or entry.get("event") != "runner_termination_accepted":
            continue
        reason, region = entry.get("reason"), entry.get("region")
        if (not isinstance(reason, str) or reason not in REASONS or region not in regions
                or not instance_id(entry.get("instance_id"))):
            continue
        millis = record.get("timestamp")
        if type(millis) is not int or not 0 <= millis < 253402300800000:
            continue
        emit(event="runner_termination_accepted", controller=GROUPS[payload["logGroup"]],
             instance_id=entry["instance_id"], region=region, reason=reason,
             accepted_at=timestamp(datetime.fromtimestamp(millis / 1000, timezone.utc)))


def observation(instance, region):
    tags = {t["Key"]: t.get("Value") for t in instance.get("Tags", [])}
    arch = RUNNER_NAMES.get(tags.get("Name"))
    iid = instance.get("InstanceId")
    state = instance.get("State", {}).get("Name")
    if tags.get("Role") != "github-runner" or not arch or not instance_id(iid) or state not in END_STATES:
        return None
    code = instance.get("StateReason", {}).get("Code")
    shape = instance.get("InstanceType")
    zone = instance.get("Placement", {}).get("AvailabilityZone")
    lifecycle = instance.get("InstanceLifecycle")
    market = "on-demand" if lifecycle is None else lifecycle
    result = {
        "event": "runner_instance_end_observed" if state == "terminated" else "runner_instance_shutdown_observed",
        "instance_id": iid, "region": region, "architecture": arch, "state": state,
        "state_reason_code": code if matches(r"(?:Server|Client)\.[A-Za-z0-9.]{1,80}", code) else None,
        "zone": zone if matches(re.escape(region) + r"(?:-[a-z0-9-]+)?[a-z]", zone) else None,
        "instance_type": shape if matches(r"[a-z0-9-]{1,25}\.[a-z0-9-]{1,25}", shape) else None,
        "launch_time": timestamp(instance.get("LaunchTime")),
        "market": market if market in ("on-demand", "spot", "capacity-block", "scheduled") else "unknown",
    }
    if lifecycle == "spot":
        request = instance.get("SpotInstanceRequestId")
        result.update(spot_request_id=request if matches(r"sir-[a-z0-9]{8,32}", request) else None,
                      spot_status_code=None, spot_status_at=None)
    return result


def add_spot_status(ec2, observations):
    spot = {obs["instance_id"]: obs for obs in observations if obs["market"] == "spot"}
    if not spot:
        return
    pages = ec2.get_paginator("describe_spot_instance_requests").paginate(
        Filters=[{"Name": "instance-id", "Values": list(spot)}])
    for page in pages:
        for request in page.get("SpotInstanceRequests", []):
            obs = spot.get(request.get("InstanceId"))
            if not obs or not obs["spot_request_id"] or request.get("SpotInstanceRequestId") != obs["spot_request_id"]:
                continue
            status = request.get("Status", {})
            code = status.get("Code")
            obs["spot_status_code"] = code if matches(r"[a-z0-9-]{1,80}", code) else None
            obs["spot_status_at"] = timestamp(status.get("UpdateTime"))


def scan(region):
    ec2 = boto3.client("ec2", region_name=region)
    pages = ec2.get_paginator("describe_instances").paginate(Filters=[
        {"Name": "tag:Role", "Values": ["github-runner"]},
        {"Name": "tag:Name", "Values": list(RUNNER_NAMES)},
        {"Name": "instance-state-name", "Values": END_STATES},
    ], PaginationConfig={"PageSize": 100})
    success = True
    for page in pages:
        observations = []
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                obs = observation(instance, region)
                if obs:
                    observations.append(obs)
        try:
            add_spot_status(ec2, observations)
        except (BotoCoreError, ClientError):
            # Preserve the terminal EC2 record even when enrichment is unavailable.
            emit(event="runner_loss_scan_error", region=region, operation="DescribeSpotInstanceRequests")
            success = False
        observed_at = timestamp(datetime.now(timezone.utc))
        for obs in observations:
            emit(**obs, observed_at=observed_at)
    return success


def handler(event, context):
    regions = json.loads(os.environ["RUNNER_REGIONS"])
    if not isinstance(regions, list) or not regions or not all(matches(r"[a-z0-9-]+", r) for r in regions):
        raise ValueError("RUNNER_REGIONS must list the runner regions")
    if "awslogs" in event:
        controller_events(event["awslogs"], regions)
        return
    if event.get("source") != "aws.events" or event.get("detail-type") != "Scheduled Event":
        raise ValueError("Expected a scheduled event or a CloudWatch Logs subscription batch")
    failed = False
    for region in regions:
        try:
            if not scan(region):
                failed = True
        except (BotoCoreError, ClientError):
            emit(event="runner_loss_scan_error", region=region, operation="DescribeInstances")
            failed = True
    if failed:
        # Lambda retries asynchronous invocations. Repeated observations are expected.
        raise RuntimeError("Runner loss scan incomplete")
    emit(event="runner_loss_scan_complete")

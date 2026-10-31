"""Release only expired macpbox leases; a fresh invocation retries long Mac scrubs."""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone


INSTANCE_TYPE = "mac-m4pro.metal"
LEASE = timedelta(hours=24)


def tags(resource):
    return {item["Key"]: item["Value"] for item in resource.get("Tags", [])}


def lease_id(value):
    try:
        parsed = uuid.UUID(value)
        return parsed.version == 4 and str(parsed) == value
    except (ValueError, TypeError, AttributeError):
        return False


def pages(client, operation, **kwargs):
    while True:
        result = getattr(client, operation)(**kwargs)
        yield result
        token = result.get("NextToken")
        if not token:
            return
        kwargs["NextToken"] = token


def cleanup(ec2, now):
    result = {"within_lease": 0, "waiting": 0, "terminating": 0,
              "released": 0, "errors": 0}
    filters = [{"Name": "tag:Name", "Values": ["macpbox"]},
               {"Name": "tag:Purpose", "Values": ["macpbox"]}]
    for page in pages(ec2, "describe_hosts", Filter=filters, MaxResults=100):
        for host in page.get("Hosts", []):
            if host.get("State") in ("released", "released-permanent-failure"):
                continue
            label = tags(host)
            allocated = host.get("AllocationTime")
            if (label.get("Name") != "macpbox" or label.get("Purpose") != "macpbox"
                    or label.get("LeaseHours") != "24" or not lease_id(label.get("LeaseId"))
                    or host.get("HostProperties", {}).get("InstanceType") != INSTANCE_TYPE
                    or not isinstance(allocated, datetime) or allocated.tzinfo is None
                    or allocated > now):
                result["errors"] += 1
                continue  # Missing or inconsistent ownership metadata never authorizes deletion.
            deadline = allocated + LEASE
            if now < deadline:
                result["within_lease"] += 1
                continue
            instances = []
            for ipage in pages(ec2, "describe_instances", Filters=[{
                    "Name": "host-id", "Values": [host["HostId"]]}], MaxResults=1000):
                for reservation in ipage.get("Reservations", []):
                    instances.extend(reservation.get("Instances", []))
            live = [item for item in instances if item.get("State", {}).get("Name") != "terminated"]
            if any(tags(item).get("Name") != "macpbox"
                   or tags(item).get("Purpose") != "macpbox"
                   or tags(item).get("LeaseId") != label["LeaseId"]
                   or item.get("InstanceType") != INSTANCE_TYPE
                   or item.get("Placement", {}).get("HostId") != host["HostId"] for item in live):
                result["errors"] += 1
                continue  # Even an owned host cannot authorize touching a different occupant.
            terminate = [item["InstanceId"] for item in live
                         if item["State"]["Name"] != "shutting-down"]
            if terminate:
                ec2.terminate_instances(InstanceIds=terminate)
                result["terminating"] += 1
                continue  # Never call this released; stop/termination starts the scrub.
            if live or host.get("State") in ("pending", "under-assessment"):
                result["waiting"] += 1
                if now > deadline + timedelta(hours=6):
                    result["errors"] += 1
                continue
            if host.get("State") != "available":
                result["errors"] += 1
                continue
            response = ec2.release_hosts(HostIds=[host["HostId"]])
            if host["HostId"] in response.get("Successful", []):
                result["released"] += 1
            else:
                result["errors"] += 1
    return result


def lambda_handler(event, context):
    import boto3

    try:
        result = cleanup(boto3.client("ec2", region_name="us-west-2"), datetime.now(timezone.utc))
    except Exception:
        # Vendor errors can contain request details. Emit only a fixed operational category.
        result = {"errors": 1, "category": "cleanup_api_failed"}
    print(json.dumps(result, sort_keys=True))
    if result["errors"]:
        boto3.client("sns", region_name=os.environ["SNS_REGION"]).publish(
            TopicArn=os.environ["SNS_TOPIC_ARN"], Subject="Mac pbox cleanup needs attention",
            Message="Mac pbox cleanup did not confirm a safe release. Inspect /aws/lambda/macpbox-cleanup; "
                    "an expired host can resume billing after scrubbing. Summary: " + json.dumps(result))
        raise RuntimeError("macpbox cleanup incomplete")
    return result

"""Operate the single Terraform-owned benchmark worker interface."""
import datetime
import os

import boto3

EC2 = boto3.client("ec2")
NAME = "benchmark-worker"
MAX_AGE = datetime.timedelta(hours=6)


def current():
    eni = EC2.describe_network_interfaces(NetworkInterfaceIds=[os.environ["ENI"]])["NetworkInterfaces"][0]
    attachment = eni.get("Attachment") or {}
    iid = attachment.get("InstanceId")
    if not iid:
        return None
    if attachment.get("DeviceIndex") != 0:
        raise RuntimeError("benchmark interface is not the primary interface")
    [[instance]] = [r["Instances"] for r in EC2.describe_instances(InstanceIds=[iid])["Reservations"]]
    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    if tags.get("Name") != NAME or tags.get("Project") != "benchmark-worker":
        raise RuntimeError("benchmark interface is attached to an unexpected instance")
    return instance


def lambda_handler(event, _context):
    action = event.get("action")
    if action == "down":
        expected = event.get("instance")
        if event != {"action": "down", "instance": expected} or not isinstance(expected, str):
            raise ValueError("down requires the specific worker instance ID")
    elif event != {"action": action} or action not in {"up", "status", "reap"}:
        raise ValueError("expected exactly one action: up, status, down or reap")
    instance = current()
    if action == "down" and instance and instance["InstanceId"] != expected:
        raise ValueError("the worker changed; refusing to terminate a different instance")
    launched = False
    if action == "up" and not instance:
        instance = EC2.run_instances(
            LaunchTemplate={"LaunchTemplateId": os.environ["TEMPLATE"], "Version": os.environ["VERSION"]},
            MinCount=1,
            MaxCount=1,
        )["Instances"][0]
        launched = True
    if not instance:
        return {"state": "absent"}
    iid = instance["InstanceId"]
    state = instance["State"]["Name"]
    if action == "down" or (
        action == "reap" and datetime.datetime.now(datetime.timezone.utc) - instance["LaunchTime"] >= MAX_AGE
    ):
        if state not in {"shutting-down", "terminated"}:
            EC2.terminate_instances(InstanceIds=[iid])
            state = "shutting-down"
    result = {"state": state, "instance": iid, "public_ipv4": os.environ["PUBLIC_IPV4"]}
    if launched:
        result["launched"] = True
    return result

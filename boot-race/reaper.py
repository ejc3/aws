"""boot-race-reaper: the server-side expiry for the boot-race campaign's compute, run every 15 minutes.

The campaign driver cleans up after itself; this catches what a failed or interrupted campaign leaves behind:
- any instance tagged Project=boot-race whose current run began more than INSTANCE_LIMIT_S ago is terminated
  (a stop/start sample restarts the clock, a forgotten running or stopped instance does not);
- any task in the boot-race cluster created more than TASK_LIMIT_S ago is stopped;
- any image tagged Project=boot-race created more than IMAGE_LIMIT_S ago is deregistered, and its snapshots, and
  any other Project=boot-race snapshot that old, are deleted.
"""

import datetime
import os

import boto3
from botocore.exceptions import ClientError

INSTANCE_LIMIT_S = int(os.environ.get("INSTANCE_LIMIT_S", 3 * 3600))
TASK_LIMIT_S = int(os.environ.get("TASK_LIMIT_S", 3600))
IMAGE_LIMIT_S = int(os.environ.get("IMAGE_LIMIT_S", 24 * 3600))
CLUSTER = os.environ.get("CLUSTER", "boot-race")
TAG = [{"Name": "tag:Project", "Values": ["boot-race"]}]


def age_s(when, now):
    if isinstance(when, str):
        when = datetime.datetime.fromisoformat(when.replace("Z", "+00:00"))
    return (now - when).total_seconds()


def reap_instances(ec2, now):
    stale = []
    for page in ec2.get_paginator("describe_instances").paginate(
        Filters=TAG + [{"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}]
    ):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                if age_s(inst["LaunchTime"], now) > INSTANCE_LIMIT_S:
                    stale.append(inst["InstanceId"])
    if stale:
        ec2.terminate_instances(InstanceIds=stale)
    return stale


def reap_tasks(ecs, now):
    arns = []
    for page in ecs.get_paginator("list_tasks").paginate(cluster=CLUSTER, desiredStatus="RUNNING"):
        arns += page["taskArns"]
    stale = []
    for i in range(0, len(arns), 100):
        for task in ecs.describe_tasks(cluster=CLUSTER, tasks=arns[i:i + 100])["tasks"]:
            if age_s(task["createdAt"], now) > TASK_LIMIT_S:
                stale.append(task["taskArn"])
    for arn in stale:
        ecs.stop_task(cluster=CLUSTER, task=arn, reason="boot-race-reaper: older than the task limit")
    return stale


def reap_images(ec2, now):
    images, snapshots = [], []
    for img in ec2.describe_images(Owners=["self"], Filters=TAG)["Images"]:
        if age_s(img["CreationDate"], now) > IMAGE_LIMIT_S:
            try:
                ec2.deregister_image(ImageId=img["ImageId"])
                images.append(img["ImageId"])
            except ClientError as e:
                print("boot-race-reaper: could not deregister", img["ImageId"], e)
    for page in ec2.get_paginator("describe_snapshots").paginate(OwnerIds=["self"], Filters=TAG):
        for snap in page["Snapshots"]:
            if age_s(snap["StartTime"], now) > IMAGE_LIMIT_S:
                try:
                    ec2.delete_snapshot(SnapshotId=snap["SnapshotId"])
                    snapshots.append(snap["SnapshotId"])
                except ClientError as e:  # still in use by an image this run could not deregister
                    print("boot-race-reaper: could not delete", snap["SnapshotId"], e)
    return images, snapshots


def lambda_handler(_event, _context, ec2=None, ecs=None, now=None):
    ec2 = ec2 or boto3.client("ec2")
    ecs = ecs or boto3.client("ecs")
    now = now or datetime.datetime.now(datetime.timezone.utc)
    instances = reap_instances(ec2, now)
    tasks = reap_tasks(ecs, now)
    images, snapshots = reap_images(ec2, now)
    result = {"instances": instances, "tasks": tasks, "images": images, "snapshots": snapshots}
    print("boot-race-reaper", result)
    return result

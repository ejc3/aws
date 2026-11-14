"""boot-race-launcher: the only caller of RunTask and StopTask for the boot-race campaign's ECS lanes.

The cluster, the task definitions and the Fargate network configuration come from this function's environment,
which Terraform sets; the caller chooses only the lane and, for the EC2 lane, which boot-race container instance.

    {"action": "run", "lane": "fargate"}
    {"action": "run", "lane": "ec2", "instance": "i-..."}
    {"action": "stop", "task": "<task ARN in the boot-race cluster>"}

A run answers with the wall-clock times just before and just after its RunTask call, so the caller can time a lane
from the moment RunTask was asked rather than from its own invoke.
"""

import os
import re
import time

import boto3

ECS = boto3.client("ecs")
CLUSTER = os.environ["CLUSTER"]
FARGATE_FAMILY = os.environ["FARGATE_FAMILY"]
EC2_FAMILY = os.environ["EC2_FAMILY"]
SUBNET = os.environ.get("SUBNET", "")
SECURITY_GROUP = os.environ["SECURITY_GROUP"]

INSTANCE = re.compile(r"^i-[0-9a-f]{8,17}$")
TASK = re.compile(r"^arn:aws:ecs:[a-z0-9-]+:\d{12}:task/%s/[0-9a-f]{32}$" % re.escape(CLUSTER))


def run_arguments(event):
    lane = event.get("lane")
    if lane == "fargate":
        if not SUBNET:
            raise ValueError("Fargate benchmarking is disabled: no campaign subnet is configured")
        return {
            "taskDefinition": FARGATE_FAMILY,
            "launchType": "FARGATE",
            "networkConfiguration": {
                "awsvpcConfiguration": {
                    "subnets": [SUBNET],
                    "securityGroups": [SECURITY_GROUP],
                    "assignPublicIp": "DISABLED",
                }
            },
        }
    if lane == "ec2":
        instance = event.get("instance")
        if not isinstance(instance, str) or not INSTANCE.match(instance):
            raise ValueError("the ec2 lane needs an instance id")
        return {
            "taskDefinition": EC2_FAMILY,
            "launchType": "EC2",
            "placementConstraints": [{"type": "memberOf", "expression": f"ec2InstanceId == {instance}"}],
        }
    raise ValueError("lane must be fargate or ec2")


def lambda_handler(event, _context):
    action = event.get("action")
    if action == "run":
        kwargs = run_arguments(event)
        before = time.time()
        r = ECS.run_task(cluster=CLUSTER, count=1, **kwargs)
        after = time.time()
        return {
            "before_unix_ms": before * 1000,
            "after_unix_ms": after * 1000,
            "task_arn": r["tasks"][0]["taskArn"] if r.get("tasks") else None,
            "failures": [{"reason": f.get("reason"), "detail": f.get("detail")} for f in r.get("failures", [])],
        }
    if action == "stop":
        task = event.get("task")
        if not isinstance(task, str) or not TASK.match(task):
            raise ValueError("stop needs a task ARN in the boot-race cluster")
        ECS.stop_task(cluster=CLUSTER, task=task, reason="boot-race sample taken")
        return {"stopped": task}
    raise ValueError("action must be run or stop")

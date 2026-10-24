"""Change only the staging coordinator image, serialized with Terraform's backend lock."""
import copy
import datetime
import json
import os
import re
import time
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


def task_input(response, image):
    """Preserve the service's task configuration; replace exactly one container image."""
    old = response["taskDefinition"]
    if old["family"] != "fcvm-fleet-coordinator":
        raise ValueError("unexpected task family")
    containers = copy.deepcopy(old["containerDefinitions"])
    if sorted(c["name"] for c in containers) != ["cloudflared", "coordinator"]:
        raise ValueError("unexpected containers")
    if old.get("runtimePlatform", {}).get("cpuArchitecture") != "ARM64":
        raise ValueError("unexpected architecture")
    for container in containers:
        if container["name"] == "coordinator":
            container["image"] = image
    fields = ("family", "taskRoleArn", "executionRoleArn", "networkMode", "volumes",
              "placementConstraints", "requiresCompatibilities", "cpu", "memory",
              "pidMode", "ipcMode", "proxyConfiguration", "ephemeralStorage",
              "runtimePlatform", "enableFaultInjection")
    result = {k: copy.deepcopy(old[k]) for k in fields if k in old}
    result["containerDefinitions"] = containers
    result["tags"] = response.get("tags", [])
    return result


def image_of(response):
    return next(c["image"] for c in response["taskDefinition"]["containerDefinitions"]
                if c["name"] == "coordinator")


def service(ecs):
    result = ecs.describe_services(cluster="fcvm-fleet", services=["fcvm-fleet-coordinator"])
    if result.get("failures") or len(result.get("services", [])) != 1:
        raise RuntimeError("staging service unavailable")
    return result["services"][0]


def wait_for(ecs, arn, deadline):
    while time.monotonic() < deadline:
        current = service(ecs)
        if current["taskDefinition"] != arn:
            raise RuntimeError("staging service changed during release")
        deployments = current.get("deployments", [])
        if any(d.get("rolloutState") == "FAILED" for d in deployments):
            raise RuntimeError("ECS rollout failed")
        if (len(deployments) == 1 and deployments[0].get("rolloutState") == "COMPLETED"
                and current["runningCount"] == current["desiredCount"] == 1
                and current["pendingCount"] == 0):
            return
        time.sleep(5)
    raise TimeoutError("ECS rollout did not become stable")


def deploy(event, ddb, ecs, ssm, ecr, *, remaining_ms=lambda: 850_000):
    commit, digest = event.get("commit", ""), event.get("digest", "")
    if not re.fullmatch(r"[0-9a-f]{40}", commit) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("a full commit and image digest are required")
    images = ecr.describe_images(repositoryName="fcvm-fleet/coordinator",
                                 imageIds=[{"imageTag": commit}])["imageDetails"]
    if len(images) != 1 or images[0]["imageDigest"] != digest:
        raise ValueError("digest does not match the immutable commit tag")
    image = os.environ["REPOSITORY_URL"] + "@" + digest
    table, lock_id, parameter = (os.environ[k] for k in ("LOCK_TABLE", "LOCK_ID", "IMAGE_PARAMETER"))
    token = str(uuid.uuid4())
    info = json.dumps({"ID": token, "Operation": "fcvm coordinator release", "Info": commit,
                       "Who": "fleet-stage-ci", "Version": "1", "Path": lock_id,
                       "Created": datetime.datetime.now(datetime.timezone.utc).isoformat()})
    try:
        ddb.put_item(TableName=table, Item={"LockID": {"S": lock_id}, "Info": {"S": info}},
                     ConditionExpression="attribute_not_exists(LockID)")
    except ClientError as error:
        if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return {"status": "busy"}
        raise
    new_arn, previous = None, None
    try:
        current = service(ecs)
        if current["desiredCount"] != 1 or len(current.get("deployments", [])) != 1:
            raise RuntimeError("staging is disabled or already rolling out")
        old_arn = current["taskDefinition"]
        old = ecs.describe_task_definition(taskDefinition=old_arn, include=["TAGS"])
        previous = ssm.get_parameter(Name=parameter)["Parameter"]["Value"]
        if image_of(old) != previous:
            raise RuntimeError("release pointer and running task disagree")
        if previous == image:
            wait_for(ecs, old_arn, time.monotonic() + 60)
            return {"status": "unchanged", "taskDefinition": old_arn, "digest": digest}
        new_arn = ecs.register_task_definition(**task_input(old, image))["taskDefinition"]["taskDefinitionArn"]
        ecs.update_service(cluster="fcvm-fleet", service="fcvm-fleet-coordinator", taskDefinition=new_arn)
        # Reserve time for a rollback and releasing the backend lock on every invocation.
        wait_for(ecs, new_arn, time.monotonic() + min(480, max(5, remaining_ms() / 1000 - 240)))
        ssm.put_parameter(Name=parameter, Value=image, Type="String", Overwrite=True)
        return {"status": "deployed", "taskDefinition": new_arn, "digest": digest}
    except Exception:
        if new_arn:
            # Never overwrite a deployment another operator made while we were running.
            if service(ecs)["taskDefinition"] in (old_arn, new_arn):
                ecs.update_service(cluster="fcvm-fleet", service="fcvm-fleet-coordinator", taskDefinition=old_arn)
                wait_for(ecs, old_arn, time.monotonic() + 150)
                ssm.put_parameter(Name=parameter, Value=previous, Type="String", Overwrite=True)
                ecs.deregister_task_definition(taskDefinition=new_arn)
        raise
    finally:
        # The exact Info value is the ownership token; a stale invocation cannot unlock another owner.
        ddb.delete_item(TableName=table, Key={"LockID": {"S": lock_id}},
                        ConditionExpression="Info = :owner", ExpressionAttributeValues={":owner": {"S": info}})


def lambda_handler(event, context):
    config = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2})
    credentials = boto3.client("sts", config=config).assume_role(
        RoleArn=os.environ["RELEASE_ROLE"], RoleSessionName="fcvm-stage-release")["Credentials"]
    session = boto3.Session(aws_access_key_id=credentials["AccessKeyId"],
                            aws_secret_access_key=credentials["SecretAccessKey"],
                            aws_session_token=credentials["SessionToken"], region_name="us-east-2")
    try:
        result = deploy(event, boto3.client("dynamodb", config=config), session.client("ecs", config=config),
                        session.client("ssm", config=config), session.client("ecr", config=config),
                        remaining_ms=context.get_remaining_time_in_millis)
        print(json.dumps(result))
        return result
    except Exception as error:
        # Never return SDK responses or task definitions: configuration can contain credentials.
        print(json.dumps({"status": "failed", "errorType": type(error).__name__}))
        return {"status": "failed", "errorType": type(error).__name__}

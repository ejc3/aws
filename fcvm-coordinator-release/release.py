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

CONDITION_FAILED = "ConditionalCheckFailedException"


class Refused(RuntimeError):
    """A release check that failed; `code` names the check in the failure result."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def error_code(error):
    """The AWS error code or the release check that failed, never a message or a payload."""
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code", "")
    elif isinstance(error, Refused):
        code = error.code
    else:
        return None
    return code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9.:_-]{1,64}", code) else None


def lock_holder(error):
    """The lock's Info when a conditional write was refused (ALL_OLD), or None when no lock exists."""
    return error.response.get("Item", {}).get("Info", {}).get("S")


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
        raise Refused("service-unavailable", "staging service unavailable")
    return result["services"][0]


def wait_for(ecs, arn, deadline):
    while time.monotonic() < deadline:
        current = service(ecs)
        if current["taskDefinition"] != arn:
            raise Refused("service-changed", "staging service changed during release")
        deployments = current.get("deployments", [])
        if any(d.get("rolloutState") == "FAILED" for d in deployments):
            raise Refused("rollout-failed", "ECS rollout failed")
        if (len(deployments) == 1 and deployments[0].get("rolloutState") == "COMPLETED"
                and current["runningCount"] == current["desiredCount"] == 1
                and current["pendingCount"] == 0):
            return
        time.sleep(5)
    raise TimeoutError("ECS rollout did not become stable")


def deploy(event, ddb, ecs, ssm, ecr, *, remaining_ms=lambda: 850_000, progress=None):
    """Release one image. `progress["step"]` names the step that raised, for the failure result."""
    progress = {} if progress is None else progress
    progress["step"] = "input"
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
    progress["step"] = "lock"
    try:
        # ALL_OLD: a refusal carries the lock it was checked against, so a retry of a write that
        # already landed (the SDK retries a timed-out request) can tell its own lock from another's.
        ddb.put_item(TableName=table, Item={"LockID": {"S": lock_id}, "Info": {"S": info}},
                     ConditionExpression="attribute_not_exists(LockID)",
                     ReturnValuesOnConditionCheckFailure="ALL_OLD")
    except ClientError as error:
        if error_code(error) != CONDITION_FAILED:
            raise
        if lock_holder(error) != info:
            return {"status": "busy"}
        # Our own token: an earlier attempt of this request took the lock and its reply was lost.
    new_arn, previous = None, None
    try:
        progress["step"] = "inspect"
        current = service(ecs)
        if current["desiredCount"] != 1 or len(current.get("deployments", [])) != 1:
            raise Refused("not-idle", "staging is disabled or already rolling out")
        old_arn = current["taskDefinition"]
        old = ecs.describe_task_definition(taskDefinition=old_arn, include=["TAGS"])
        previous = ssm.get_parameter(Name=parameter)["Parameter"]["Value"]
        if image_of(old) != previous:
            raise Refused("pointer-mismatch", "release pointer and running task disagree")
        if previous == image:
            progress["step"] = "rollout"
            wait_for(ecs, old_arn, time.monotonic() + 60)
            return {"status": "unchanged", "taskDefinition": old_arn, "digest": digest}
        definition = task_input(old, image)
        # Bump the parameter version BEFORE the first ECS change. The ephemeral
        # Terraform guard then rejects older saved plans, even after rollback or
        # a failed registration that leaves the image value unchanged.
        progress["step"] = "register"
        ssm.put_parameter(Name=parameter, Value=previous, Type="String", Overwrite=True)
        new_arn = ecs.register_task_definition(**definition)["taskDefinition"]["taskDefinitionArn"]
        progress["step"] = "update"
        ecs.update_service(cluster="fcvm-fleet", service="fcvm-fleet-coordinator", taskDefinition=new_arn)
        # Reserve time for a rollback and releasing the backend lock on every invocation.
        progress["step"] = "rollout"
        wait_for(ecs, new_arn, time.monotonic() + min(480, max(5, remaining_ms() / 1000 - 240)))
        progress["step"] = "pointer"
        ssm.put_parameter(Name=parameter, Value=image, Type="String", Overwrite=True)
        return {"status": "deployed", "taskDefinition": new_arn, "digest": digest}
    except Exception:
        if new_arn:
            failed_step, progress["step"] = progress["step"], "rollback"
            # Never overwrite a deployment another operator made while we were running.
            if service(ecs)["taskDefinition"] in (old_arn, new_arn):
                ecs.update_service(cluster="fcvm-fleet", service="fcvm-fleet-coordinator", taskDefinition=old_arn)
                wait_for(ecs, old_arn, time.monotonic() + 150)
                ssm.put_parameter(Name=parameter, Value=previous, Type="String", Overwrite=True)
                ecs.deregister_task_definition(taskDefinition=new_arn)
            progress["step"] = failed_step
        raise
    finally:
        unlock(ddb, table, lock_id, info, progress)


def unlock(ddb, table, lock_id, info, progress):
    """Delete the lock only while it holds this release's exact Info, the ownership token.

    A refusal means the lock is not ours: an earlier attempt of this same delete removed it and its
    reply was lost, or it was force-released (and possibly taken) by someone else. Either way there
    is nothing of ours left to delete, and the release's own outcome stands.
    """
    failed_step, progress["step"] = progress.get("step"), "unlock"
    try:
        ddb.delete_item(TableName=table, Key={"LockID": {"S": lock_id}},
                        ConditionExpression="#info = :owner", ExpressionAttributeNames={"#info": "Info"},
                        ExpressionAttributeValues={":owner": {"S": info}},
                        ReturnValuesOnConditionCheckFailure="ALL_OLD")
    except ClientError as error:
        if error_code(error) != CONDITION_FAILED:
            raise
        print(json.dumps({"step": "unlock", "code": CONDITION_FAILED,
                          "lock": "held by another owner" if lock_holder(error) else "absent"}))
    progress["step"] = failed_step


def lambda_handler(event, context):
    progress = {"step": "credentials"}
    try:
        config = Config(connect_timeout=5, read_timeout=15, retries={"max_attempts": 2})
        credentials = boto3.client("sts", config=config).assume_role(
            RoleArn=os.environ["RELEASE_ROLE"], RoleSessionName="fcvm-stage-release")["Credentials"]
        session = boto3.Session(aws_access_key_id=credentials["AccessKeyId"],
                                aws_secret_access_key=credentials["SecretAccessKey"],
                                aws_session_token=credentials["SessionToken"], region_name="us-east-2")
        result = deploy(event, boto3.client("dynamodb", config=config), session.client("ecs", config=config),
                        session.client("ssm", config=config), session.client("ecr", config=config),
                        remaining_ms=context.get_remaining_time_in_millis, progress=progress)
        print(json.dumps(result))
        return result
    except Exception as error:
        # Never return messages, SDK responses or task definitions: configuration can contain
        # credentials. The step and the AWS error code (or the release check's code) are fixed tokens.
        result = {"status": "failed", "errorType": type(error).__name__, "step": progress["step"]}
        code = error_code(error)
        if code:
            result["code"] = code
        print(json.dumps(result))
        return result

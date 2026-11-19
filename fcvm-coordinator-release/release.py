"""Change only one fleet stack's coordinator image, serialized with Terraform's backend lock.

One function per stack (STACK, CLUSTER, SERVICE, FAMILY): staging's (fcvm-fleet-ci.tf) and
production's (fcvm-fleet-prod-ci.tf). Production adds:
- a viewer hold: a release goes ahead only while the coordinator's ActiveViewers metric for
  Fleet=prod has been 0 for the last 5 minutes, otherwise it returns `held` and changes nothing
  (the caller asks again; a Lambda runs at most 15 minutes);
- `force`, for rollback only: it skips the hold and is accepted only for an image the prod
  pointer has held before (its parameter history);
- read-only `quiet` and `candidate`, and `record`, which writes the release record
  (RELEASE_RECORD): {coordinator: {commit, digest}, web_commit, worker_version, generation, build_id,
  smoke_run, previous}, with a compare-and-swap on its predecessor.
"""
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
COMMIT = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
VERSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The window ActiveViewers must have been 0 for, and the most parameter versions read for `force`.
QUIET_SECONDS = 300
HISTORY_PAGES = 4
RECORD_BYTES = 4000


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


def stack():
    """This function's stack, from its environment: name, cluster, service and task family."""
    name = os.environ["STACK"]
    if name not in ("stage", "prod"):
        raise ValueError("unknown stack")
    return name, os.environ["CLUSTER"], os.environ["SERVICE"], os.environ["FAMILY"]


def task_input(response, image):
    """Preserve the service's task configuration; replace exactly one container image."""
    old = response["taskDefinition"]
    if old["family"] != stack()[3]:
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
    _, cluster, name, _ = stack()
    result = ecs.describe_services(cluster=cluster, services=[name])
    if result.get("failures") or len(result.get("services", [])) != 1:
        raise Refused("service-unavailable", "service unavailable")
    return result["services"][0]


def update(ecs, arn):
    _, cluster, name, _ = stack()
    ecs.update_service(cluster=cluster, service=name, taskDefinition=arn)


def wait_for(ecs, arn, deadline):
    while time.monotonic() < deadline:
        current = service(ecs)
        if current["taskDefinition"] != arn:
            raise Refused("service-changed", "service changed during release")
        deployments = current.get("deployments", [])
        if any(d.get("rolloutState") == "FAILED" for d in deployments):
            raise Refused("rollout-failed", "ECS rollout failed")
        if (len(deployments) == 1 and deployments[0].get("rolloutState") == "COMPLETED"
                and current["runningCount"] == current["desiredCount"] == 1
                and current["pendingCount"] == 0):
            return
        time.sleep(5)
    raise TimeoutError("ECS rollout did not become stable")


def held(cloudwatch, now=None):
    """None when ActiveViewers{Fleet=<METRIC_FLEET>} was 0 over the last QUIET_SECONDS, else the
    `held` result. No datapoint holds too: a coordinator that reports nothing proves nothing."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    result = cloudwatch.get_metric_data(
        MetricDataQueries=[{"Id": "viewers", "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": os.environ["METRICS_NAMESPACE"], "MetricName": "ActiveViewers",
                       "Dimensions": [{"Name": "Fleet", "Value": os.environ["METRIC_FLEET"]}]},
            "Period": 60, "Stat": "Maximum"}}],
        StartTime=now - datetime.timedelta(seconds=QUIET_SECONDS), EndTime=now,
        ScanBy="TimestampDescending")["MetricDataResults"]
    values = result[0].get("Values", []) if len(result) == 1 and result[0].get("Id") == "viewers" else []
    if len(result) != 1 or result[0].get("StatusCode") != "Complete" or not values:
        return {"status": "held", "reason": "no-viewer-data"}
    if any(not isinstance(v, (int, float)) or v != 0 for v in values):
        return {"status": "held", "reason": "viewers"}
    return None


def pointer_digest(value):
    """The digest of a pointer value (REPOSITORY_URL@sha256:...), or a refusal."""
    prefix = os.environ["REPOSITORY_URL"] + "@"
    if not value.startswith(prefix) or not DIGEST.fullmatch(value[len(prefix):]):
        raise Refused("pointer-invalid", "release pointer is not an image digest in its repository")
    return value[len(prefix):]


def commits_of(ecr, digest):
    """The commit tags of one image in the coordinator repository."""
    rows = ecr.describe_images(repositoryName="fcvm-fleet/coordinator",
                               imageIds=[{"imageDigest": digest}])["imageDetails"]
    if len(rows) != 1 or rows[0]["imageDigest"] != digest:
        raise Refused("image-missing", "image is not in the coordinator repository")
    return sorted(t for t in rows[0].get("imageTags", []) if COMMIT.fullmatch(t))


def ran_before(ssm, parameter, image):
    """Whether the pointer has held `image` before: what a forced rollback may return to."""
    arguments = {"Name": parameter, "WithDecryption": False, "MaxResults": 50}
    for _ in range(HISTORY_PAGES):
        page = ssm.get_parameter_history(**arguments)
        if any(item.get("Value") == image for item in page.get("Parameters", [])):
            return True
        if not page.get("NextToken"):
            return False
        arguments["NextToken"] = page["NextToken"]
    return False


def load_record(ssm):
    value = json.loads(ssm.get_parameter(Name=os.environ["RELEASE_RECORD"])["Parameter"]["Value"])
    if value == {}:
        return None
    if not isinstance(value, dict):
        raise Refused("record-invalid", "release record is not an object")
    return value


def checked_release(value):
    """The shape of one release record entry (without its own `previous`)."""
    coordinator = value.get("coordinator") if isinstance(value, dict) else None
    ok = (isinstance(value, dict)
          and set(value) == {"coordinator", "web_commit", "worker_version", "generation", "build_id", "smoke_run"}
          and isinstance(coordinator, dict) and set(coordinator) == {"commit", "digest"}
          and isinstance(coordinator["commit"], str) and COMMIT.fullmatch(coordinator["commit"])
          and isinstance(coordinator["digest"], str) and DIGEST.fullmatch(coordinator["digest"])
          and (value["web_commit"] is None or (isinstance(value["web_commit"], str) and COMMIT.fullmatch(value["web_commit"])))
          and (value["worker_version"] is None
               or (isinstance(value["worker_version"], str) and VERSION_ID.fullmatch(value["worker_version"])))
          and type(value["generation"]) is int and value["generation"] >= 0
          and isinstance(value["build_id"], str) and len(value["build_id"]) <= 200
          and (value["smoke_run"] is None or (type(value["smoke_run"]) is int and value["smoke_run"] > 0)))
    if not ok:
        raise ValueError("a release record names a coordinator commit and digest, a web commit, a Worker version, "
                         "a generation, a build id and a smoke run")
    return value


def record(event, ssm):
    """Write the release record: the release now running, and the one it replaced."""
    release = event.get("release")
    if set(event) != {"action", "release"} or not isinstance(release, dict) or set(release) != {
            "coordinator", "web_commit", "worker_version", "generation", "build_id", "smoke_run", "previous"}:
        raise ValueError("record takes one release and its previous")
    current = checked_release({k: v for k, v in release.items() if k != "previous"})
    previous = release["previous"]
    if previous is not None:
        checked_release(previous)
    running = pointer_digest(ssm.get_parameter(Name=os.environ["IMAGE_PARAMETER"])["Parameter"]["Value"])
    if current["coordinator"]["digest"] != running:
        raise Refused("not-running", "the recorded coordinator is not the one prod runs")
    stored = load_record(ssm)
    if stored is not None and {k: v for k, v in stored.items() if k != "previous"} == current:
        return {"status": "unchanged"}
    if stored is not None and {k: v for k, v in stored.items() if k != "previous"} != previous:
        raise Refused("record-moved", "the release record changed since this release read it")
    raw = json.dumps({**current, "previous": previous}, sort_keys=True, separators=(",", ":"))
    if len(raw.encode()) > RECORD_BYTES:
        raise ValueError("release record exceeds its fixed bound")
    ssm.put_parameter(Name=os.environ["RELEASE_RECORD"], Value=raw, Type="String", Overwrite=True)
    return {"status": "recorded"}


def candidate(ssm, ecr):
    """Staging's and prod's coordinator images and the release record, for the promotion."""
    out = {"status": "ok"}
    for name, parameter in (("stage", os.environ["STAGE_IMAGE_PARAMETER"]), ("prod", os.environ["IMAGE_PARAMETER"])):
        value = ssm.get_parameter(Name=parameter)["Parameter"]["Value"]
        digest = pointer_digest(value)
        out[name] = {"digest": digest, "commits": commits_of(ecr, digest)}
    out["release"] = load_record(ssm)
    return out


def deploy(event, ddb, ecs, ssm, ecr, cloudwatch=None, *, remaining_ms=lambda: 850_000, progress=None):
    """Release one image. `progress["step"]` names the step that raised, for the failure result."""
    progress = {} if progress is None else progress
    progress["step"] = "input"
    name = stack()[0]
    action = event.get("action")
    if name == "prod" and action == "quiet":
        if set(event) != {"action"}:
            raise ValueError("quiet takes no arguments")
        return held(cloudwatch) or {"status": "quiet"}
    if name == "prod" and action == "candidate":
        if set(event) != {"action"}:
            raise ValueError("candidate takes no arguments")
        return candidate(ssm, ecr)
    if name == "prod" and action == "record":
        return record(event, ssm)
    allowed = {"commit", "digest", "force"} if name == "prod" else {"commit", "digest"}
    if not set(event) <= allowed or "force" in event and event["force"] is not True:
        raise ValueError("a release takes a commit and a digest (and, in prod, force for a rollback)")
    force = event.get("force") is True
    commit, digest = event.get("commit", ""), event.get("digest", "")
    if not isinstance(commit, str) or not isinstance(digest, str) \
            or not COMMIT.fullmatch(commit) or not DIGEST.fullmatch(digest):
        raise ValueError("a full commit and image digest are required")
    images = ecr.describe_images(repositoryName="fcvm-fleet/coordinator",
                                 imageIds=[{"imageTag": commit}])["imageDetails"]
    if len(images) != 1 or images[0]["imageDigest"] != digest:
        raise ValueError("digest does not match the immutable commit tag")
    image = os.environ["REPOSITORY_URL"] + "@" + digest
    table, lock_id, parameter = (os.environ[k] for k in ("LOCK_TABLE", "LOCK_ID", "IMAGE_PARAMETER"))
    if name == "prod":
        progress["step"] = "hold"
        if force:
            if not ran_before(ssm, parameter, image):
                raise Refused("not-a-rollback", "force returns only to an image prod ran before")
        else:
            waiting = held(cloudwatch)
            if waiting:
                return waiting
    token = str(uuid.uuid4())
    info = json.dumps({"ID": token, "Operation": "fcvm coordinator release", "Info": commit,
                       "Who": f"fleet-{name}-ci", "Version": "1", "Path": lock_id,
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
            raise Refused("not-idle", "the service is disabled or already rolling out")
        old_arn = current["taskDefinition"]
        old = ecs.describe_task_definition(taskDefinition=old_arn, include=["TAGS"])
        previous = ssm.get_parameter(Name=parameter)["Parameter"]["Value"]
        if image_of(old) != previous:
            raise Refused("pointer-mismatch", "release pointer and running task disagree")
        if previous == image:
            progress["step"] = "rollout"
            wait_for(ecs, old_arn, time.monotonic() + 60)
            return {"status": "unchanged", "taskDefinition": old_arn, "digest": digest}
        if name == "prod" and not force:
            # Again under the lock, just before the change: the window to UpdateService is seconds.
            progress["step"] = "hold"
            waiting = held(cloudwatch)
            if waiting:
                return waiting
        definition = task_input(old, image)
        # Bump the parameter version BEFORE the first ECS change. The ephemeral
        # Terraform guard then rejects older saved plans, even after rollback or
        # a failed registration that leaves the image value unchanged.
        progress["step"] = "register"
        ssm.put_parameter(Name=parameter, Value=previous, Type="String", Overwrite=True)
        new_arn = ecs.register_task_definition(**definition)["taskDefinition"]["taskDefinitionArn"]
        progress["step"] = "update"
        update(ecs, new_arn)
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
                update(ecs, old_arn)
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
            RoleArn=os.environ["RELEASE_ROLE"], RoleSessionName=f"fcvm-{stack()[0]}-release")["Credentials"]
        session = boto3.Session(aws_access_key_id=credentials["AccessKeyId"],
                                aws_secret_access_key=credentials["SecretAccessKey"],
                                aws_session_token=credentials["SessionToken"], region_name="us-east-2")
        result = deploy(event, boto3.client("dynamodb", config=config), session.client("ecs", config=config),
                        session.client("ssm", config=config), session.client("ecr", config=config),
                        session.client("cloudwatch", config=config),
                        remaining_ms=context.get_remaining_time_in_millis, progress=progress)
        print(json.dumps({k: result[k] for k in ("status", "reason") if k in result}))
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

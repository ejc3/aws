"""Change only one fleet stack's coordinator image, serialized with Terraform's backend lock.

A release may also carry `lambda_digest`, the Lambda tiles' lane image the stack's CI selected (ejc3/fcvm
fleet/coord/publish.py, fleet/lambda/release.py). The function sets the coordinator's FLEET_LAMBDA_IMAGE to
LANE_REPOSITORY_URL@lambda_digest only when the lane repository's keep-<stack>-current tag holds that digest, so CI
cannot make a coordinator run an image nothing protects; when the repository cannot be read or the tag holds another
digest, it refuses and changes nothing. A release without `lambda_digest` is the release without a lane. Read-only
`lane` returns the lane digest the coordinator runs and the commit tags of its coordinator image, which CI uses to
skip a release of an ancestor of what runs.

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
from botocore.exceptions import BotoCoreError, ClientError

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


LANE_VARIABLE = "FLEET_LAMBDA_IMAGE"
LANE_REPOSITORY = "fcvm-fleet/lambda-tile"


def lane_of(response):
    """The lane digest the coordinator's FLEET_LAMBDA_IMAGE names, None without one, or a refusal."""
    container = next(c for c in response["taskDefinition"]["containerDefinitions"] if c["name"] == "coordinator")
    values = [e["value"] for e in container.get("environment", []) if e.get("name") == LANE_VARIABLE]
    if not values:
        return None
    prefix = os.environ["LANE_REPOSITORY_URL"] + "@"
    if len(values) != 1 or not values[0].startswith(prefix) or not DIGEST.fullmatch(values[0][len(prefix):]):
        raise Refused("lane-invalid", "the coordinator's lane image is not a digest in the lane repository")
    return values[0][len(prefix):]


def lane_selected(lane_ecr, name, digest):
    """Refuse unless the lane repository's keep-<stack>-current holds `digest`."""
    rows = lane_ecr.describe_images(repositoryName=LANE_REPOSITORY,
                                    imageIds=[{"imageTag": f"keep-{name}-current"}])["imageDetails"]
    if len(rows) != 1 or rows[0].get("imageDigest") != digest:
        raise Refused("lane-not-selected", f"keep-{name}-current does not hold the lane digest")


def task_input(response, image, lane=None, clear=False):
    """Preserve the service's task configuration; replace exactly one container image, and with `lane`, the
    coordinator's FLEET_LAMBDA_IMAGE."""
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
            if lane is not None or clear:
                env = [e for e in container.get("environment", []) if e.get("name") != LANE_VARIABLE]
                container["environment"] = env + ([] if clear else [
                    {"name": LANE_VARIABLE, "value": os.environ["LANE_REPOSITORY_URL"] + "@" + lane}])
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
    start = now.replace(second=0, microsecond=0) - datetime.timedelta(seconds=QUIET_SECONDS)
    result = cloudwatch.get_metric_data(
        MetricDataQueries=[{"Id": "viewers", "ReturnData": True, "MetricStat": {
            "Metric": {"Namespace": os.environ["METRICS_NAMESPACE"], "MetricName": "ActiveViewers",
                       "Dimensions": [{"Name": "Fleet", "Value": os.environ["METRIC_FLEET"]}]},
            "Period": 60, "Stat": "Maximum"}}],
        StartTime=start, EndTime=now,
        ScanBy="TimestampDescending")["MetricDataResults"]
    values = result[0].get("Values", []) if len(result) == 1 and result[0].get("Id") == "viewers" else []
    if len(result) != 1 or result[0].get("StatusCode") != "Complete" or not values:
        return {"status": "held", "reason": "no-viewer-data"}
    if any(not isinstance(v, (int, float)) or v != 0 for v in values):
        return {"status": "held", "reason": "viewers"}
    # Require every completed minute, and count any reported viewers in the current minute too.
    stamps = result[0].get("Timestamps", [])
    required = {start + datetime.timedelta(seconds=i) for i in range(0, QUIET_SECONDS, 60)}
    if len(stamps) != len(values) or not required.issubset(stamps):
        return {"status": "held", "reason": "no-viewer-data"}
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
    base = {"coordinator", "web_commit", "worker_version", "generation", "build_id", "smoke_run"}
    ok = (isinstance(value, dict)
          and set(value) in (base, base | {"lambda_digest"})
          # The set's lane image, when ejc3/fcvm's prod lane is on (FLEET_LAMBDA_LANE_PROD): a rollback returns to it.
          and ("lambda_digest" not in value or (isinstance(value["lambda_digest"], str)
                                                and DIGEST.fullmatch(value["lambda_digest"])))
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
                         "a generation, a build id and a smoke run, and may name a lane image digest")
    return value


def record(event, ssm):
    """Write the release record: the release now running, and the one it replaced."""
    release = event.get("release")
    fields = {"coordinator", "web_commit", "worker_version", "generation", "build_id", "smoke_run", "previous"}
    if set(event) != {"action", "release"} or not isinstance(release, dict) or set(release) not in (
            fields, fields | {"lambda_digest"}):
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


def running_lane(ecs, cluster, name, image):
    """The lane digest a stack's running coordinator task names (FLEET_LAMBDA_IMAGE), or None. The service must
    run one settled deployment whose coordinator image is the one the stack's pointer names (`image`)."""
    result = ecs.describe_services(cluster=cluster, services=[name])
    if result.get("failures") or len(result.get("services", [])) != 1:
        raise Refused("service-unavailable", "service unavailable")
    current = result["services"][0]
    deployments = current.get("deployments", [])
    if len(deployments) != 1 or deployments[0].get("rolloutState") != "COMPLETED":
        raise Refused("not-idle", "the service is rolling out")
    definition = ecs.describe_task_definition(taskDefinition=current["taskDefinition"])
    if image_of(definition) != image:
        raise Refused("pointer-mismatch", "release pointer and running task disagree")
    return lane_of(definition)


def candidate(ssm, ecr, ecs):
    """Staging's and prod's coordinator images, the lane image each running task names, and the release record,
    for the promotion. What a stack runs comes from its task; the lane repository's tags only protect images."""
    out = {"status": "ok"}
    services = {"stage": (os.environ["STAGE_CLUSTER"], os.environ["STAGE_SERVICE"]),
                "prod": (os.environ["CLUSTER"], os.environ["SERVICE"])}
    for name, parameter in (("stage", os.environ["STAGE_IMAGE_PARAMETER"]), ("prod", os.environ["IMAGE_PARAMETER"])):
        value = ssm.get_parameter(Name=parameter)["Parameter"]["Value"]
        digest = pointer_digest(value)
        out[name] = {"digest": digest, "commits": commits_of(ecr, digest)}
        # Per stack, so the record (which the manual rollback reads with the prod lane off) never depends on it:
        # ejc3/fcvm's decide refuses on a lane_error. A timeout or endpoint error (BotoCoreError) and a task
        # definition without a coordinator container (StopIteration) or a field (KeyError) are "unreadable".
        try:
            out[name]["lambda_digest"] = running_lane(ecs, *services[name], value)
        except (Refused, ClientError, BotoCoreError, StopIteration, KeyError) as error:
            out[name].update({"lambda_digest": None, "lane_error": error_code(error) or "unreadable"})
    out["release"] = load_record(ssm)
    return out


def deploy(event, ddb, ecs, ssm, ecr, cloudwatch=None, *, remaining_ms=lambda: 850_000, progress=None, lane_ecr=None):
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
        return candidate(ssm, ecr, ecs)
    if name == "prod" and action == "record":
        return record(event, ssm)
    if action == "lane":
        if set(event) != {"action"}:
            raise ValueError("lane takes no arguments")
        progress["step"] = "lane"
        running = ecs.describe_task_definition(taskDefinition=service(ecs)["taskDefinition"])
        return {"status": "ok", "lambda_digest": lane_of(running),
                "commits": commits_of(ecr, pointer_digest(image_of(running)))}
    allowed = {"commit", "digest", "lambda_digest"} | ({"force"} if name == "prod" else set())
    if not set(event) <= allowed or "force" in event and event["force"] is not True:
        raise ValueError("a release takes a commit and a digest (and, in prod, force for a rollback)")
    lane = event.get("lambda_digest")
    # lambda_digest null: return to running no lane image (ejc3/fcvm's rollback to a set that ran none).
    clear = "lambda_digest" in event and lane is None
    if "lambda_digest" in event and not clear and not (isinstance(lane, str) and DIGEST.fullmatch(lane)):
        raise ValueError("lambda_digest is an image digest, or null to remove the lane image")
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
        if lane is not None:
            # Under the lock and before any change: a lane digest CI selected, and nothing else.
            progress["step"] = "lane"
            lane_selected(lane_ecr, name, lane)
            progress["step"] = "inspect"
        if previous == image and (lane is None and not clear or lane_of(old) == lane):
            progress["step"] = "rollout"
            wait_for(ecs, old_arn, time.monotonic() + 60)
            return {"status": "unchanged", "taskDefinition": old_arn, "digest": digest, **lane_result(lane, clear)}
        if name == "prod" and not force:
            # Again under the lock, just before the change: the window to UpdateService is seconds.
            progress["step"] = "hold"
            waiting = held(cloudwatch)
            if waiting:
                return waiting
        definition = task_input(old, image, lane, clear)
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
        return {"status": "deployed", "taskDefinition": new_arn, "digest": digest, **lane_result(lane, clear)}
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


def lane_result(lane, clear=False):
    return {"lambda_digest": None} if clear else {} if lane is None else {"lambda_digest": lane}


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
                        remaining_ms=context.get_remaining_time_in_millis, progress=progress,
                        lane_ecr=session.client("ecr", config=config, region_name="us-east-1"))
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

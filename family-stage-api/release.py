"""Promote one immutable commit image and roll only the Family staging service."""
import os
import re
import time

REPO = "family/api"
STAGE_TAG = "family-stage"
ECS_PROPAGATION_SECONDS = 300
SERVICE = {"cluster": "family", "service": "family"}


def primary(service):
    deployments = [d for d in service["deployments"] if d["status"] == "PRIMARY"]
    if len(deployments) != 1:
        raise ValueError("Expected one primary Family staging deployment")
    return deployments[0]


def release(event, ecr, ecs, receipts, repo_uri, *, now=time.time, sleep=time.sleep):
    if not isinstance(event, dict) or event.keys() != {"commit", "digest", "run_number"}:
        raise ValueError("Expected only commit, digest and run_number")
    commit, digest, run_number = event["commit"], event["digest"], event["run_number"]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected a full Git commit SHA")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Expected a SHA-256 image digest")
    if type(run_number) is not int or not 0 < run_number < 2**63:
        raise ValueError("Expected a positive GitHub workflow run number")

    key = {"stage": "family"}
    prior = receipts.get_item(Key=key, ConsistentRead=True).get("Item", {})
    if run_number < prior.get("run_number", 0):
        raise ValueError("A newer workflow run has already claimed this staging service")
    same_run = run_number == prior.get("run_number")
    if same_run and (commit != prior["commit"] or digest != prior["digest"]):
        raise ValueError("The same workflow run cannot change its image")

    def image(tag):
        result = ecr.batch_get_image(repositoryName=REPO, imageIds=[{"imageTag": tag}])
        if result.get("failures") or len(result["images"]) != 1:
            raise ValueError(f"Expected exactly one Family API image tagged {tag}")
        return result["images"][0]

    source = image(commit)
    if source["imageId"]["imageDigest"] != digest:
        raise ValueError("The requested digest does not match the immutable commit tag")
    previous_digest = image(STAGE_TAG)["imageId"]["imageDigest"]

    def service_state():
        result = ecs.describe_services(cluster=SERVICE["cluster"], services=[SERVICE["service"]])
        if result.get("failures") or len(result["services"]) != 1:
            raise ValueError("Could not find the Family staging service")
        service = result["services"][0]
        if service["status"] != "ACTIVE" or service["desiredCount"] != 1:
            raise ValueError("Family staging must be active with one desired task")
        return service

    service = service_state()
    definition = ecs.describe_task_definition(taskDefinition=service["taskDefinition"])["taskDefinition"]
    api_images = [c["image"] for c in definition["containerDefinitions"] if c["name"] == "api"]
    if definition["family"] != "family" or api_images != [f"{repo_uri}:{STAGE_TAG}"]:
        raise ValueError("Apply the Family staging task's rolling tag before enabling releases")

    current = primary(service)
    record = {**key, **event}
    if same_run and "deployment_id" in prior:
        if previous_digest != digest:
            raise ValueError("The staging tag moved after this release; run the latest main workflow")
        # UpdateService can succeed before DescribeServices begins returning its new
        # PRIMARY. A later administrative rollout can also supersede it. In both cases
        # replay the recorded submission and let the caller verify the actual origin.
        if current["id"] != prior["deployment_id"] or current.get("rolloutState") != "FAILED":
            return {**event, "deployment_id": prior["deployment_id"]}
    if (prior.get("digest") == digest and previous_digest == digest
            and prior.get("deployment_id") == current["id"] and current.get("rolloutState") != "FAILED"):
        # A retry, or another run for the same live image, needs no new deployment.
        record["deployment_id"] = current["id"]
    else:
        resuming = "baseline" in prior and prior["digest"] == digest and (same_run or previous_digest == digest)
        if not same_run:
            # A newer run for the same image adopts an ambiguous earlier submission.
            # Fence the earlier run while keeping its persisted observation window.
            pending = {k: prior[k] for k in ("baseline", "attempted_at")} if resuming else {}
            receipts.put_item(Item={**record, **pending})
        if previous_digest != digest:
            if resuming:
                raise ValueError("The staging tag moved during the release; run the latest main workflow")
            ecr.put_image(repositoryName=REPO, imageTag=STAGE_TAG, imageDigest=digest,
                          imageManifest=source["imageManifest"], imageManifestMediaType=source["imageManifestMediaType"])

        # UpdateService has no idempotency token and DescribeServices is eventually
        # consistent. Reconcile an unacknowledged submission before considering a
        # second force deployment, including after a Lambda timeout or restart.
        baseline = prior["baseline"] if resuming else current["id"]
        if resuming and current["id"] == baseline:
            for delay in (1, 2, 4):
                sleep(delay)
                observed = service_state()
                if observed["taskDefinition"] != service["taskDefinition"]:
                    raise RuntimeError("The staging task definition changed during reconciliation; retry")
                current = primary(observed)
                if current["id"] != baseline:
                    break
            if current["id"] == baseline and now() - prior["attempted_at"] < ECS_PROPAGATION_SECONDS:
                raise RuntimeError("The ECS submission is uncertain; retry after its five-minute propagation window")
        if current["id"] == baseline or current.get("rolloutState") == "FAILED":
            baseline = current["id"]
            receipts.put_item(Item={**record, "baseline": baseline, "attempted_at": int(now())})
            started = ecs.update_service(**SERVICE, forceNewDeployment=True)["service"]
            current = primary(started)
            if current["id"] == baseline:
                raise RuntimeError("ECS did not yet report a new primary deployment; retry the workflow")
        record["deployment_id"] = current["id"]

    receipts.put_item(Item=record)
    return {**event, "deployment_id": record["deployment_id"]}


def handler(event, context):
    import boto3
    from botocore.config import Config

    # UpdateService has no client token. The journal handles retries across invocations.
    ecs = boto3.client("ecs", config=Config(retries={"total_max_attempts": 1}))
    table = boto3.resource("dynamodb").Table(os.environ["RECEIPT_TABLE"])
    return release(event, boto3.client("ecr"), ecs, table, os.environ["REPOSITORY_URI"])

"""Promote one immutable commit image and roll only the Family staging service."""
import os
import re

REPO = "family/api"
STAGE_TAG = "family-stage"
SERVICE = {"cluster": "family", "service": "family"}


def primary(service):
    deployments = [d for d in service["deployments"] if d["status"] == "PRIMARY"]
    if len(deployments) != 1:
        raise ValueError("Expected one primary Family staging deployment")
    return deployments[0]


def release(event, ecr, ecs, receipts, repo_uri):
    if not isinstance(event, dict) or event.keys() != {"commit", "digest", "run_id"}:
        raise ValueError("Expected only commit, digest and run_id")
    commit, digest, run_id = event["commit"], event["digest"], event["run_id"]
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("Expected a full Git commit SHA")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("Expected a SHA-256 image digest")
    if type(run_id) is not int or not 0 < run_id < 2**63:
        raise ValueError("Expected a positive GitHub run ID")

    key = {"stage": "family"}
    prior = receipts.get_item(Key=key, ConsistentRead=True).get("Item", {})
    if run_id < prior.get("run_id", 0):
        raise ValueError("A newer workflow run has already claimed this staging service")
    same_run = run_id == prior.get("run_id")
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
    result = ecs.describe_services(cluster=SERVICE["cluster"], services=[SERVICE["service"]])
    if result.get("failures") or len(result["services"]) != 1:
        raise ValueError("Could not find the Family staging service")
    service = result["services"][0]
    if service["status"] != "ACTIVE" or service["desiredCount"] != 1:
        raise ValueError("Family staging must be active with one desired task")
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
        if not same_run:
            receipts.put_item(Item=record)
        if previous_digest != digest:
            if same_run and "baseline" in prior:
                raise ValueError("The staging tag moved during the release; run the latest main workflow")
            ecr.put_image(repositoryName=REPO, imageTag=STAGE_TAG, imageDigest=digest,
                          imageManifest=source["imageManifest"], imageManifestMediaType=source["imageManifestMediaType"])

        # Persist the previous deployment before the non-idempotent ECS call. If its
        # acknowledgement is lost, a retry can recognize the new primary deployment.
        baseline = prior.get("baseline") if same_run else None
        if baseline is None or current.get("rolloutState") == "FAILED":
            baseline = current["id"]
            receipts.put_item(Item={**record, "baseline": baseline})
        if current["id"] == baseline:
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

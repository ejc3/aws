"""ami-replicator: keep the runner AMIs available in the Ohio region.

The runner launcher picks its AMI by tag in its OWN region (Purpose=github-runner, newest per architecture, owned by
this account). fcvm's pipeline publishes those AMIs in the source region only, so a launcher pointed at Ohio would find
nothing. This copies the newest KEEP_PER_ARCH images of each architecture to the target region, tags and all, on a
schedule, so that the day the launcher is switched Ohio already has the current AMI.

It only ever COPIES. It never deregisters, never touches the source, and never copies anything that is not a
tagged runner image. Idempotent: a copy is recorded by a SourceImageId tag in the target, so a re-run (or an overlap)
finds it and does nothing. Invoke with {"dry_run": true} to see what it would copy.
"""
import json
import os

import boto3

SOURCE = os.environ.get("SOURCE_REGION", "us-west-1")
TARGET = os.environ.get("TARGET_REGION", "us-east-2")
KEEP_PER_ARCH = int(os.environ.get("KEEP_PER_ARCH", "2"))
TOPIC = os.environ.get("SNS_TOPIC_ARN", "")
PURPOSE = {"Name": "tag:Purpose", "Values": ["github-runner"]}


def newest_per_arch(images, keep):
    """The `keep` newest available images of each architecture."""
    by_arch = {}
    for image in images:
        if image.get("State") == "available":
            by_arch.setdefault(image["Architecture"], []).append(image)
    chosen = []
    for arch in sorted(by_arch):
        chosen += sorted(by_arch[arch], key=lambda i: i["CreationDate"], reverse=True)[:keep]
    return chosen


def _source_of(image):
    return next((t["Value"] for t in image.get("Tags", []) if t["Key"] == "SourceImageId"), None)


def already_copied(target_images):
    """Source image ids with a copy that is done or on its way (pending or available). A copy is asynchronous: the
    call returns while the image is pending and it can still end up failed, and a FAILED copy is not a copy."""
    return {_source_of(i) for i in target_images if _source_of(i) and i.get("State") in ("pending", "available")}


def failed_copies(target_images):
    """(source id, target id) of copies that ended failed."""
    return [(_source_of(i), i["ImageId"]) for i in target_images if _source_of(i) and i.get("State") in ("failed", "error")]


def plan(source_images, target_images, keep=KEEP_PER_ARCH):
    copied = already_copied(target_images)
    return [i for i in newest_per_arch(source_images, keep) if i["ImageId"] not in copied]


def lambda_handler(event, context, clients=None):
    dry_run = bool((event or {}).get("dry_run"))
    src = (clients or {}).get("src") or boto3.client("ec2", region_name=SOURCE)
    dst = (clients or {}).get("dst") or boto3.client("ec2", region_name=TARGET)
    sns = (clients or {}).get("sns") or (boto3.client("sns") if TOPIC else None)
    source_images = src.describe_images(Owners=["self"], Filters=[PURPOSE])["Images"]
    target_images = dst.describe_images(Owners=["self"], Filters=[PURPOSE])["Images"]
    todo = plan(source_images, target_images)
    results, errors = [], []
    wanted = {i["ImageId"] for i in todo}
    for source_id, target_id in failed_copies(target_images):
        if source_id in wanted:
            # It will be copied again below; but say so, and fail the run, so a copy that keeps failing is seen
            # (the invocation used to succeed and the error alarm never fired).
            errors.append("an earlier copy of %s (%s) FAILED; copying it again" % (source_id, target_id))
    for image in todo:
        entry = {"source": image["ImageId"], "name": image["Name"], "arch": image["Architecture"]}
        if dry_run:
            results.append(dict(entry, action="would-copy"))
            continue
        try:
            copy = dst.copy_image(Name=image["Name"], SourceImageId=image["ImageId"], SourceRegion=SOURCE,
                                  CopyImageTags=True, Description="copy of %s from %s (ami-replicator)" % (image["ImageId"], SOURCE))
            new = copy["ImageId"]
            # The marker that makes the next run a no-op. Without it a retry would copy the same image again.
            dst.create_tags(Resources=[new], Tags=[{"Key": "SourceImageId", "Value": image["ImageId"]},
                                                  {"Key": "SourceRegion", "Value": SOURCE}])
            results.append(dict(entry, action="copied", target=new))
        except Exception as exc:
            errors.append("%s: %s" % (image["ImageId"], exc))
            results.append(dict(entry, action="error", why=str(exc)))
    print(json.dumps({"dry_run": dry_run, "results": results}))
    if errors and not dry_run:
        if sns and TOPIC:
            sns.publish(TopicArn=TOPIC, Subject="ami-replicator: %d copy(ies) failed" % len(errors), Message="\n".join(errors))
        raise RuntimeError("; ".join(errors))
    return {"dry_run": dry_run, "results": results}

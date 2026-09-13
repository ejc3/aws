"""Reconcile only the configured fleet's backup copies; never alter source volumes.

The hourly pass repairs lost EventBridge deliveries. Unencrypted sources copy directly;
only the explicitly allowed encrypted sources use the customer-key intermediary.
The recovery account owns GFS history. Cleanup is separately gated and requires exact
copy lineage; existing primary-vault history is read-only and never removed here.
Restore validation checks detached encrypted EBS metadata, not guest contents/bootability.
Monthly per-volume checks do not rely on best-effort events or a successful scheduler.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError


def pages(client, operation, result_key, **kwargs):
    return [item for page in client.get_paginator(operation).paginate(**kwargs)
            for item in page[result_key]]


def usable(points, allowed):
    return [point for point in points
            if point.get("ResourceType") == "EBS"
            and point.get("Status") == "COMPLETED"
            and point.get("ResourceArn") in allowed]


def latest(points, resource):
    return max((point for point in points if point["ResourceArn"] == resource),
               key=lambda point: point["CreationDate"], default=None)


def volume_creation_times(ec2, resources):
    """CreateTime for each configured volume that still exists.

    Filter rather than VolumeIds, so a deleted volume cannot fail the whole lookup. A
    volume absent from the answer gets no grace: a lost volume still reports missing.
    """
    ids = sorted({resource.rsplit("/", 1)[-1] for resource in resources})
    if not ids:
        return {}
    by_id = {volume["VolumeId"]: volume["CreateTime"]
             for volume in pages(ec2, "describe_volumes", "Volumes",
                                 Filters=[{"Name": "volume-id", "Values": ids}])}
    return {resource: by_id[resource.rsplit("/", 1)[-1]] for resource in resources
            if resource.rsplit("/", 1)[-1] in by_id}


def created_after(created, resource, cutoff):
    """True only when the volume's creation time is known and later than cutoff."""
    when = (created or {}).get(resource)
    return when is not None and when > cutoff


def final_retention(point):
    """GFS is based on capture time, never an intermediate TTL or retry date."""
    captured = point["CreationDate"].astimezone(timezone.utc)
    return 365 if captured.day == 1 else 30 if captured.weekday() == 6 else 7


def verified_copy(point, destination_points, jobs, destination, retention, key=None, source_vault=None, now=None,
                  expired_checkpoint_lineage=False):
    """A completed job alone is not proof that its exact recovery point is usable."""
    # Cleanup can reconstruct an already-copied chain through an EXPIRED CMK
    # checkpoint. This is historical metadata only, never a usable final/copy
    # source; ordinary copy/health callers retain the COMPLETED-only default.
    if expired_checkpoint_lineage and key is None:
        raise ValueError("Expired lineage requires an exact checkpoint key")
    statuses = {"COMPLETED", "EXPIRED"} if expired_checkpoint_lineage else {"COMPLETED"}
    for job in jobs:
        if (job.get("State") != "COMPLETED"
                or job.get("SourceRecoveryPointArn") != point["RecoveryPointArn"]
                or job.get("DestinationBackupVaultArn") != destination
                or (source_vault is not None and job.get("SourceBackupVaultArn") != source_vault)
                or job.get("ResourceArn") != point["ResourceArn"]):
            continue
        copied = next((candidate for candidate in destination_points
                       if candidate["RecoveryPointArn"] == job.get("DestinationRecoveryPointArn")), None)
        if (copied is None or copied.get("Status") not in statuses
                or copied.get("ResourceType") != "EBS" or copied.get("IsEncrypted") is not True
                or copied.get("ResourceArn") != point["ResourceArn"]
                or copied.get("CreationDate") != point["CreationDate"]
                or (key is not None and copied.get("EncryptionKeyArn") != key)):
            continue
        actual_retention = copied.get("Lifecycle", {}).get("DeleteAfterDays")
        if retention is not None and (actual_retention is None or actual_retention < retention):
            continue
        delete_at = copied.get("CalculatedLifecycle", {}).get("DeleteAt")
        if now is not None and ((delete_at is not None and delete_at <= now)
                                or (actual_retention is not None and actual_retention > 0
                                    and copied["CreationDate"] + timedelta(days=actual_retention) <= now)):
            continue
        return copied
    return None


def copy_point(client, point, source_vault, destination, jobs, role, now, retention_days):
    """Only retry a terminal failure, after six hours, at most three times per point."""
    if point.get("Status") != "COMPLETED":
        raise ValueError("Cannot copy a non-completed recovery point: " + point["RecoveryPointArn"])
    matching = [job for job in jobs
                if job.get("SourceRecoveryPointArn") == point["RecoveryPointArn"]
                and job.get("DestinationBackupVaultArn") == destination]
    for job in matching:
        if job["State"] in {"CREATED", "RUNNING"}:
            if job["CreationDate"] < now - timedelta(hours=24):
                raise RuntimeError("Copy pending more than 24h: " + point["ResourceArn"])
            return "in-flight"
        if job["State"] == "COMPLETED":
            # The caller already checked destination metadata. Allow propagation,
            # but never treat missing/incorrect destination data as success.
            if job.get("CompletionDate", job["CreationDate"]) < now - timedelta(hours=2):
                raise RuntimeError("Completed copy has no verified destination: " + point["ResourceArn"])
            return "awaiting-destination"
    if len(matching) >= 3:
        raise RuntimeError("Copy exhausted three attempts for " + point["ResourceArn"])
    if matching and max(job["CreationDate"] for job in matching) > now - timedelta(hours=6):
        return "backoff"
    if retention_days is not None and point["CreationDate"] + timedelta(days=retention_days) <= now:
        raise RuntimeError("Copy retention window already elapsed; retaining pending source: " + point["ResourceArn"])
    token = hashlib.sha256((point["RecoveryPointArn"] + destination + str(len(matching))).encode()).hexdigest()[:50]
    request = dict(
        RecoveryPointArn=point["RecoveryPointArn"], SourceBackupVaultName=source_vault,
        DestinationBackupVaultArn=destination, IamRoleArn=role,
        IdempotencyToken=token)
    if retention_days is not None:
        request["Lifecycle"] = {"DeleteAfterDays": retention_days}
    result = client.start_copy_job(**request)
    jobs.append({"SourceRecoveryPointArn": point["RecoveryPointArn"],
                 "DestinationBackupVaultArn": destination, "State": "CREATED",
                 "CreationDate": now, "CopyJobId": result["CopyJobId"]})
    print(json.dumps({"action": "copy-started", "resource": point["ResourceArn"],
                      "copy_job_id": result["CopyJobId"], "retention_days": retention_days}))
    return "started"


def validate_restore(stage_backup, stage_ec2, job_id, restore_role, account):
    job = stage_backup.describe_restore_job(RestoreJobId=job_id)
    if (job.get("IamRoleArn") != restore_role or job.get("ResourceType") != "EBS"
            or job.get("Status") != "COMPLETED"
            or job.get("ValidationStatus") in {"SUCCESSFUL", "FAILED", "TIMED_OUT"}):
        return
    created = job.get("CreatedResourceArn", "")
    prefix = "arn:aws:ec2:" + stage_ec2.meta.region_name + ":" + account + ":volume/"
    if not created.startswith(prefix):
        raise ValueError("Unexpected resource ARN in restore test")
    volume_id = created[len(prefix):]
    volumes = stage_ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"]
    passed = (len(volumes) == 1 and volumes[0].get("Encrypted") is True
              and volumes[0].get("State") == "available"
              and not volumes[0].get("Attachments")
              and any(tag["Key"] == "awsbackup-restore-test" for tag in volumes[0].get("Tags", [])))
    stage_backup.put_restore_validation_result(
        RestoreJobId=job_id, ValidationStatus="SUCCESSFUL" if passed else "FAILED",
        ValidationStatusMessage=("Detached encrypted test volume restored; guest boot/data not tested."
                                 if passed else "Restored volume failed encryption, isolation, or test-tag checks."))
    if not passed:
        raise RuntimeError("Restore validation failed for " + job_id)


def cleanup_expired_test(stage_ec2, job, restore_role, account, now):
    """Backstop AWS Backup cleanup only for this role's old, detached test volumes."""
    if (job.get("IamRoleArn") != restore_role or job.get("ResourceType") != "EBS"
            or job.get("DeletionStatus") == "SUCCESSFUL"
            or job.get("CompletionDate", now) > now - timedelta(hours=4)):
        return False
    prefix = "arn:aws:ec2:" + stage_ec2.meta.region_name + ":" + account + ":volume/"
    created = job.get("CreatedResourceArn", "")
    if not created.startswith(prefix):
        return False
    volume_id = created[len(prefix):]
    try:
        volume = stage_ec2.describe_volumes(VolumeIds=[volume_id])["Volumes"][0]
    except ClientError as error:
        if error.response["Error"]["Code"] == "InvalidVolume.NotFound":
            return False
        raise
    if (volume.get("State") != "available" or volume.get("Attachments")
            or not any(tag["Key"] == "awsbackup-restore-test" for tag in volume.get("Tags", []))):
        raise RuntimeError("Expired restore-test volume is not safe to clean up: " + volume_id)
    stage_ec2.delete_volume(VolumeId=volume_id)
    print(json.dumps({"action": "expired-test-volume-removed", "volume": volume_id, "restore_job": job["RestoreJobId"]}))
    return True


def monthly_test_start(plan, now):
    """Match the opinionated Terraform schedule; never silently accept schedule drift."""
    if (plan.get("ScheduleExpression") != "cron(0 12 8 * ? *)"
            or plan.get("ScheduleExpressionTimezone", "Etc/UTC") not in {"Etc/UTC", "UTC"}
            or plan.get("StartWindowHours") != 1):
        raise ValueError("Unexpected monthly restore-test schedule")
    scheduled = now.astimezone(timezone.utc).replace(day=8, hour=12, minute=0, second=0, microsecond=0)
    if scheduled > now:
        scheduled = (scheduled.replace(day=1) - timedelta(days=1)).replace(day=8)
    # A newly created plan did not owe a test before it existed. A deleted plan or
    # denied metadata request must raise instead of being interpreted as this grace.
    return scheduled if plan["CreationTime"] <= scheduled else None


def monthly_restore_health(stage, config, now, created=None):
    """Require the latest scheduled test for every allowed source, even with no events.

    Use the per-resource API, not optional newer SourceResourceArn response fields:
    older Lambda SDK models drop those fields, and recovery points may have expired.
    An unrelated plan/role or a successful test of another volume cannot satisfy this.
    A volume created less than 48h before the scheduled test had no copied recovery point
    for that test to restore, so it is first required at the following month's test.
    """
    plan = stage.get_restore_testing_plan(
        RestoreTestingPlanName=config["restore_plan"])["RestoreTestingPlan"]
    if plan["RestoreTestingPlanArn"] != config["restore_plan_arn"]:
        raise ValueError("Unexpected restore-testing plan ARN")
    scheduled = monthly_test_start(plan, now)
    if scheduled is None:
        return []
    errors = []
    for resource in sorted(set(config["volumes"])):
        if created_after(created, resource, scheduled - timedelta(hours=48)):
            continue
        jobs = pages(stage, "list_restore_jobs_by_protected_resource", "RestoreJobs",
                     ResourceArn=resource,
                     ByRecoveryPointCreationDateAfter=scheduled - timedelta(days=36))
        matching = [job for job in jobs
                    if job.get("IamRoleArn") == config["restore_role"]
                    and job.get("ResourceType") == "EBS"
                    and job.get("CreatedBy", {}).get("RestoreTestingPlanArn") == config["restore_plan_arn"]
                    and job["CreationDate"] >= scheduled]
        job = max(matching, key=lambda item: item["CreationDate"], default=None)
        if job is None:
            # AWS may start anywhere in the one-hour window. Allow another hour
            # for metadata propagation, but never wait a month to detect no job.
            if now >= scheduled + timedelta(hours=2):
                errors.append("monthly restore test missing since " + scheduled.isoformat() + ": " + resource)
            continue
        job_id = job["RestoreJobId"]
        if job.get("Status") in {"FAILED", "ABORTED"}:
            errors.append("monthly restore test " + job["Status"] + ": " + resource + " (" + job_id + ")")
        elif job.get("ValidationStatus") in {"FAILED", "TIMED_OUT"}:
            errors.append("monthly restore validation " + job["ValidationStatus"] + ": " + resource + " (" + job_id + ")")
        elif job.get("Status") == "COMPLETED":
            if (job.get("ValidationStatus") != "SUCCESSFUL"
                    and now >= job.get("CompletionDate", job["CreationDate"]) + timedelta(hours=2)):
                errors.append("monthly restore test unvalidated after 2h: " + resource + " (" + job_id + ")")
        elif now >= scheduled + timedelta(hours=24):
            errors.append("monthly restore test unfinished after 24h: " + resource + " (" + job_id + ")")
        if job.get("DeletionStatus") == "FAILED":
            errors.append("AWS Backup restore-test cleanup failed: " + job_id)
    return errors


def failed_job_event(event):
    detail = event.get("detail", {})
    field = "status" if event.get("detail-type") == "Restore Job State Change" else "state"
    return detail.get(field) in {"FAILED", "EXPIRED", "ABORTED", "PARTIAL"}


def canary_test_start(config):
    """A reviewed retry changes the one-off schedule, not past job history."""
    if not config.get("canary_plan_arn"):
        return None
    value = config.get("canary_start_at")
    try:
        scheduled = datetime.strptime(value, "%Y-%m-%dT%H:%M:00Z").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        raise ValueError("Invalid initial restore canary schedule") from None
    if scheduled.strftime("%Y-%m-%dT%H:%M:00Z") != value:
        raise ValueError("Invalid initial restore canary schedule")
    return scheduled


def canary_restore_health(stage, config, now):
    """A retry only clears each source's failure after that source fully passes.

    Attribute jobs with the per-resource API supported by the Lambda SDK. Do not
    filter by recovery-point age: moving the retry date must not hide old failures.
    The initial untried wave gets its one-hour start window plus one hour of
    propagation grace, but a known failure never receives that grace again.
    """
    scheduled = canary_test_start(config)
    accepted = config.get("canary_accepted", False)
    if not isinstance(accepted, bool):
        raise ValueError("Invalid initial restore canary acceptance")
    # Only the reviewed production cutover retires this one-off health check.
    # AWS eventually expires job history; age alone must never count as success.
    # The handler still validates/cleans old jobs and enforces monthly health.
    if scheduled is None or accepted is True:
        return []
    errors = []
    for resource in sorted(set(config["volumes"])):
        jobs = pages(stage, "list_restore_jobs_by_protected_resource", "RestoreJobs",
                     ResourceArn=resource)
        matching = [job for job in jobs
                    if job.get("IamRoleArn") == config["restore_role"]
                    and job.get("ResourceType") == "EBS"
                    and job.get("CreatedBy", {}).get("RestoreTestingPlanArn") == config["canary_plan_arn"]]
        current = max((job for job in matching if job["CreationDate"] >= scheduled),
                      key=lambda item: item["CreationDate"], default=None)
        if (current is not None and current.get("Status") == "COMPLETED"
                and current.get("ValidationStatus") == "SUCCESSFUL"
                and current.get("DeletionStatus") == "SUCCESSFUL"):
            continue
        failed = max((job for job in matching
                      if job.get("Status") in {"FAILED", "ABORTED"}
                      or job.get("ValidationStatus") in {"FAILED", "TIMED_OUT"}
                      or job.get("DeletionStatus") == "FAILED"),
                     key=lambda item: item["CreationDate"], default=None)
        if failed is not None:
            errors.append("Initial restore canary failed; no successful current-wave replacement: "
                          + resource + " (" + failed["RestoreJobId"] + ")")
        elif now >= scheduled + timedelta(hours=2):
            if current is None:
                errors.append("Initial restore canary missing since " + scheduled.isoformat() + ": " + resource)
            else:
                errors.append("Initial restore canary incomplete after 2h: " + resource
                              + " (" + current["RestoreJobId"] + "; restore=" + str(current.get("Status"))
                              + ", validation=" + str(current.get("ValidationStatus"))
                              + ", deletion=" + str(current.get("DeletionStatus")) + ")")
    return errors


def checkpoint_origin(point, jobs, config):
    return next((job for job in jobs
                 if job.get("State") == "COMPLETED"
                 and job.get("DestinationBackupVaultArn") == config["dr_vault_arn"]
                 and job.get("DestinationRecoveryPointArn") == point["RecoveryPointArn"]
                 and job.get("SourceBackupVaultArn") in {
                     config["primary_vault_arn"], config["processing_vault_arn"]}
                 and job.get("SourceRecoveryPointArn")
                 and job.get("ResourceArn") == point["ResourceArn"]), None)


def expired_cleanup_candidates(primary, dr, config, pending_raw, dr_raw, stage_points,
                               primary_jobs, dr_jobs, confirmed_intermediates, now):
    """Retry failed deletion only with independently surviving exact copy proof.

    EXPIRED remains unhealthy while present. Do not copy it, accept it as a final
    destination, or remove a newest/unproven CMK checkpoint to make alarms green.
    """
    cleanup, errors = [], []
    for point in pending_raw:
        if (point.get("Status") != "EXPIRED" or point.get("ResourceType") != "EBS"
                or point.get("ResourceArn") not in config["volumes"]):
            continue
        try:
            final = None
            if point.get("IsEncrypted") is False:
                final = verified_copy(point, stage_points, primary_jobs, config["stage_vault_arn"],
                                      final_retention(point), source_vault=config["processing_vault_arn"], now=now)
            elif (point.get("IsEncrypted") is True and point.get("EncryptionKeyArn")
                  and point["ResourceArn"] in config["cmk_hop_volumes"]):
                intermediate = verified_copy(point, dr_raw, primary_jobs, config["dr_vault_arn"],
                                             None, config["dr_key"], config["processing_vault_arn"], now,
                                             expired_checkpoint_lineage=True)
                if intermediate is not None:
                    final = verified_copy(intermediate, stage_points, dr_jobs, config["stage_vault_arn"],
                                          final_retention(point), source_vault=config["dr_vault_arn"], now=now)
            if final is not None:
                tags = primary.list_tags(ResourceArn=point["RecoveryPointArn"]).get("Tags", {})
                if (tags.get("BackupPipeline") != "fleet-processing-v2"
                        or not tags.get("aws:backup:source-resource")):
                    raise ValueError("Expired processing point lacks pipeline provenance: " + point["RecoveryPointArn"])
                cleanup.append((primary, config["processing_vault"], point))
        except Exception as error:
            errors.append(str(error))
    for point in dr_raw:
        if (point.get("Status") != "EXPIRED" or point.get("ResourceType") != "EBS"
                or point.get("ResourceArn") not in config["cmk_hop_volumes"]
                or point.get("IsEncrypted") is not True or point.get("EncryptionKeyArn") != config["dr_key"]):
            continue
        try:
            newest = latest(dr_raw, point["ResourceArn"])
            successor = latest(confirmed_intermediates, point["ResourceArn"])
            if (point["RecoveryPointArn"] == newest["RecoveryPointArn"] or successor is None
                    or successor["CreationDate"] <= point["CreationDate"]):
                continue
            final = verified_copy(point, stage_points, dr_jobs, config["stage_vault_arn"],
                                  final_retention(point), source_vault=config["dr_vault_arn"], now=now)
            if final is not None and checkpoint_origin(point, primary_jobs, config) is not None:
                cleanup.append((dr, config["dr_vault"], point))
        except Exception as error:
            errors.append(str(error))
    return cleanup, errors


def reconcile(primary, dr, stage, config, now, created=None):
    allowed = set(config["volumes"])
    hop_volumes = set(config["cmk_hop_volumes"])
    if not hop_volumes <= allowed or config["processing_vault"] == config["primary_vault"]:
        raise ValueError("Invalid processing-vault or encrypted-volume boundary")
    primary_points = usable(pages(primary, "list_recovery_points_by_backup_vault", "RecoveryPoints",
                                 BackupVaultName=config["primary_vault"]), allowed)
    pending_raw = pages(primary, "list_recovery_points_by_backup_vault", "RecoveryPoints",
                        BackupVaultName=config["processing_vault"])
    dr_raw = pages(dr, "list_recovery_points_by_backup_vault", "RecoveryPoints",
                   BackupVaultName=config["dr_vault"])
    pending_points = usable(pending_raw, allowed)
    dr_points = usable(dr_raw, allowed)
    stage_points = usable(pages(stage, "list_recovery_points_by_backup_vault", "RecoveryPoints",
                               BackupVaultName=config["stage_vault"]), allowed)
    # AWS retains copy-job history for a finite period. Missing lineage always
    # blocks cleanup; it is not evidence that an old point can be deleted.
    since = now - timedelta(days=90)
    primary_jobs = pages(primary, "list_copy_jobs", "CopyJobs", ByCreatedAfter=since)
    dr_jobs = pages(dr, "list_copy_jobs", "CopyJobs", ByCreatedAfter=since)
    missing = []
    errors = []
    for point in pending_raw + dr_raw:
        if (point.get("ResourceArn") in allowed and point.get("ResourceType") == "EBS"
                and point.get("Status") in {"EXPIRED", "PARTIAL", "DELETING"}):
            # DeleteRecoveryPoint can return 200 but leave EXPIRED behind. Never
            # filter those out silently and report a successful cleanup.
            errors.append("Processing recovery point remains " + point["Status"] + ": " + point["RecoveryPointArn"])

    sources = [(point, config["processing_vault"], True) for point in pending_points]
    for resource in sorted(allowed):
        # Processing points normally disappear after verification. A fresh final
        # point therefore satisfies daily capture health without a retained local
        # snapshot. Only bootstrap a recent legacy point newer than this pipeline.
        source = latest(primary_points + pending_points + dr_points + stage_points, resource)
        destination = latest(stage_points, resource)
        # A volume younger than the 48h window has not had its first scheduled capture
        # and copy yet (moving a dev box gives it a new root volume). Only a known
        # creation time earns this; an unknown or deleted volume still reports missing.
        new_volume = created_after(created, resource, now - timedelta(hours=48))
        if not new_volume and (source is None or source["CreationDate"] < now - timedelta(hours=48)):
            missing.append("daily capture missing/older than 48h: " + resource)
        if not new_volume and (destination is None or destination["CreationDate"] < now - timedelta(hours=48)):
            missing.append("cross-account copy missing/older than 48h: " + resource)
        seed = latest(primary_points, resource)
        current = latest(pending_points + dr_points + stage_points, resource)
        if (seed is not None and seed["CreationDate"] >= now - timedelta(hours=48)
                and (current is None or seed["CreationDate"] > current["CreationDate"])):
            sources.append((seed, config["primary_vault"], False))

    confirmed_sources = []
    for source, vault, processing in sources:
        try:
            source_vault_arn = config["processing_vault_arn"] if processing else config["primary_vault_arn"]
            if source.get("IsEncrypted") is False:
                final = verified_copy(source, stage_points, primary_jobs,
                                      config["stage_vault_arn"], final_retention(source), source_vault=source_vault_arn, now=now)
                if final is None:
                    copy_point(primary, source, vault, config["stage_vault_arn"],
                               primary_jobs, config["copy_role"], now, final_retention(source))
            elif (source.get("IsEncrypted") is True and source["ResourceArn"] in hop_volumes
                  and source.get("EncryptionKeyArn")):
                intermediate = verified_copy(source, dr_points, primary_jobs,
                                             config["dr_vault_arn"], None, config["dr_key"], source_vault_arn, now)
                if intermediate is None:
                    # New pending points have no TTL; inherit it. Legacy seeds
                    # need an explicit bridge TTL so short legacy retention cannot
                    # expire them before the second leg finishes.
                    copy_point(primary, source, vault, config["dr_vault_arn"],
                               primary_jobs, config["copy_role"], now, None if processing else 365)
                    final = None
                else:
                    final = verified_copy(intermediate, stage_points, dr_jobs,
                                          config["stage_vault_arn"], final_retention(source), source_vault=config["dr_vault_arn"], now=now)
            else:
                raise ValueError("Unexpected source encryption; refusing copy: " + source["RecoveryPointArn"])
            if processing and final is not None:
                confirmed_sources.append(source)
        except Exception as error:
            errors.append(str(error))

    confirmed_intermediates = []
    for point in dr_points:
        if (point["ResourceArn"] not in hop_volumes or point.get("IsEncrypted") is not True
                or point.get("EncryptionKeyArn") != config["dr_key"]):
            errors.append("DR recovery point is not encrypted with the expected customer key: " + point["RecoveryPointArn"])
            continue
        try:
            final = verified_copy(point, stage_points, dr_jobs, config["stage_vault_arn"],
                                  final_retention(point), source_vault=config["dr_vault_arn"], now=now)
            if final is not None:
                origin = checkpoint_origin(point, primary_jobs, config)
                if origin is None:
                    errors.append("Missing first-leg lineage; retaining CMK checkpoint: " + point["RecoveryPointArn"])
                else:
                    confirmed_intermediates.append(point)
            else:
                copy_point(dr, point, config["dr_vault"], config["stage_vault_arn"],
                           dr_jobs, config["copy_role"], now, final_retention(point))
        except Exception as error:
            errors.append(str(error))

    if config.get("cleanup_enabled") is True:
        cleanup = [(primary, config["processing_vault"], point) for point in confirmed_sources]
        for point in confirmed_intermediates:
            newest = latest(dr_points, point["ResourceArn"])
            successor = latest(confirmed_intermediates, point["ResourceArn"])
            # Never remove the latest CMK checkpoint. A strictly newer verified
            # successor keeps EBS incremental-copy lineage and the rolling baseline.
            if (point["RecoveryPointArn"] != newest["RecoveryPointArn"]
                    and successor["CreationDate"] > point["CreationDate"]):
                cleanup.append((dr, config["dr_vault"], point))
        expired, expired_errors = expired_cleanup_candidates(
            primary, dr, config, pending_raw, dr_raw, stage_points,
            primary_jobs, dr_jobs, confirmed_intermediates, now)
        cleanup.extend(expired)
        errors.extend(expired_errors)
        for client, vault, point in cleanup:
            try:
                client.delete_recovery_point(BackupVaultName=vault, RecoveryPointArn=point["RecoveryPointArn"])
                # This is a request, not proof of deletion. The next inventory
                # confirms absence and alarms on EXPIRED/PARTIAL/DELETING remnants.
                print(json.dumps({"action": "processing-deletion-requested", "vault": vault,
                                  "recovery_point": point["RecoveryPointArn"]}))
            except Exception as error:
                errors.append(str(error))
    return missing, errors


def handler(event, context):
    config = json.loads(os.environ["CONFIG"])
    now = datetime.now(timezone.utc)
    primary = boto3.client("backup", region_name=config["primary_region"])
    dr = boto3.client("backup", region_name="us-east-1")
    credentials = boto3.client("sts").assume_role(
        RoleArn=config["observer_role"], RoleSessionName="backup-recovery")['Credentials']
    stage_session = boto3.Session(aws_access_key_id=credentials['AccessKeyId'],
                                 aws_secret_access_key=credentials['SecretAccessKey'],
                                 aws_session_token=credentials['SessionToken'])
    stage = stage_session.client("backup", region_name=config["stage_region"])
    stage_ec2 = stage_session.client("ec2", region_name=config["stage_region"])
    sns = boto3.client("sns", region_name=config["primary_region"])
    if failed_job_event(event):
        sns.publish(TopicArn=config["topic"], Subject="AWS Backup job requires attention",
                    Message=json.dumps(event, default=str))
    try:
        canary_test_start(config)
        created = volume_creation_times(
            boto3.client("ec2", region_name=config["primary_region"]), config["volumes"])
        missing, errors = reconcile(primary, dr, stage, config, now, created)
        # The periodic pass also handles a missed restore-completed event.
        restore_jobs = []
        restore_errors = []
        plans = {config["restore_plan_arn"]}
        if config.get("canary_plan_arn"):
            plans.add(config["canary_plan_arn"])
        for plan in sorted(plans):
            restore_jobs.extend(job for job in pages(stage, "list_restore_jobs", "RestoreJobs",
                                ByCreatedAfter=now - timedelta(days=40), ByRestoreTestingPlanArn=plan)
                                if job.get("CreatedBy", {}).get("RestoreTestingPlanArn") == plan)
        for job in restore_jobs:
            if job.get("IamRoleArn") == config["restore_role"]:
                # Prior attempts remain in AWS job history and still receive safe
                # validation/cleanup, independently of current-wave health.
                recent = job.get("CompletionDate", now) > now - timedelta(hours=4)
                try:
                    if recent and job.get("DeletionStatus") != "SUCCESSFUL":
                        validate_restore(stage, stage_ec2, job["RestoreJobId"], config["restore_role"], config["stage_account"])
                    if recent and job.get("DeletionStatus") == "FAILED":
                        errors.append("AWS Backup restore-test cleanup failed: " + job["RestoreJobId"])
                except Exception as error:
                    errors.append(str(error))
                try:
                    cleanup_expired_test(stage_ec2, job, config["restore_role"], config["stage_account"], now)
                except Exception as error:
                    errors.append(str(error))
        # Query current metadata after validation so a just-validated job can count
        # as healthy. Missing/failed monthly tests remain visible if events vanish.
        restore_errors.extend(canary_restore_health(stage, config, now))
        restore_errors.extend(monthly_restore_health(stage, config, now, created))
        errors.extend(restore_errors)
        boto3.client("cloudwatch", region_name=config["primary_region"]).put_metric_data(
            Namespace="FleetBackup", MetricData=[
                {"MetricName": "MissingRecoveryCopies", "Value": len(missing), "Unit": "Count"},
                {"MetricName": "UnhealthyRestoreTests", "Value": len(restore_errors), "Unit": "Count"},
                {"MetricName": "RecoveryControllerErrors", "Value": len(errors), "Unit": "Count"}])
        print(json.dumps({"missing": missing, "errors": errors}))
        if errors:
            raise RuntimeError("; ".join(errors))
    except Exception:
        # Lambda Errors alarm covers API errors as well as application failures.
        print("Backup recovery reconciliation failed; see the exception and alarm.")
        raise
    return {"missing": len(missing), "errors": len(errors)}

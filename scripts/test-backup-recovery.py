"""Focused read-only unit checks for copy and restore safety decisions."""
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone

class ClientError(Exception):
    """Only the error response shape consumed by production cleanup is needed."""
    def __init__(self, response, operation_name):
        super().__init__(operation_name)
        self.response = response


# Never import an installed SDK or discover credentials. Every client is a fake;
# accidental unmocked construction fails instead of reaching AWS or IMDS.
sdk = ModuleType("boto3")
sdk.client = Mock(side_effect=AssertionError("Unmocked AWS client in unit test"))
sdk.Session = Mock(side_effect=AssertionError("Unmocked AWS session in unit test"))
botocore = ModuleType("botocore")
botocore.exceptions = ModuleType("botocore.exceptions")
botocore.exceptions.ClientError = ClientError
spec = importlib.util.spec_from_file_location("backup_recovery", Path(__file__).with_name("backup-recovery.py"))
module = importlib.util.module_from_spec(spec)
with patch.dict(sys.modules, {"boto3": sdk, "botocore": botocore,
                             "botocore.exceptions": botocore.exceptions}):
    spec.loader.exec_module(module)
NOW = datetime(2026, 9, 7, 15, tzinfo=timezone.utc)
RESOURCE = "arn:aws:ec2:us-west-1:111111111111:volume/vol-example"
POINT = {"ResourceArn": RESOURCE, "RecoveryPointArn": "arn:aws:ec2:us-west-1::snapshot/snap-example",
         "ResourceType": "EBS", "Status": "COMPLETED", "CreationDate": NOW,
         "IsEncrypted": True, "EncryptionKeyArn": "dr-key", "Lifecycle": {"DeleteAfterDays": 365}}
CONFIG = {"volumes": [RESOURCE], "primary_vault": "local", "dr_vault": "dr", "stage_vault": "stage",
          "primary_vault_arn": "local-vault", "processing_vault": "pending", "processing_vault_arn": "pending-vault",
          "stage_region": "us-east-1", "cleanup_enabled": False, "cmk_hop_volumes": [RESOURCE],
          "dr_vault_arn": "dr-vault", "stage_vault_arn": "stage-vault", "dr_key": "dr-key", "copy_role": "copy-role",
          "restore_plan": "fleet", "restore_plan_arn": "plan-arn", "restore_role": "restore-role"}
SCHEDULED = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
PLAN = {"CreationTime": NOW, "RestoreTestingPlanArn": "plan-arn",
        "ScheduleExpression": "cron(0 12 8 * ? *)", "ScheduleExpressionTimezone": "Etc/UTC",
        "StartWindowHours": 1}
RESTORE_JOB = {"RestoreJobId": "restore-job", "IamRoleArn": "restore-role", "ResourceType": "EBS",
               "CreationDate": SCHEDULED + timedelta(minutes=15),
               "CompletionDate": SCHEDULED + timedelta(minutes=30),
               "CreatedBy": {"RestoreTestingPlanArn": "plan-arn"},
               "Status": "COMPLETED", "ValidationStatus": "SUCCESSFUL", "DeletionStatus": "SUCCESSFUL"}


class Backup:
    def __init__(self, points=None, jobs=None, vault_points=None, tags=None):
        self.points = points or []
        self.jobs = jobs or []
        self.vault_points = vault_points
        self.started = []
        self.validation = []
        self.deleted = []
        self.tags = tags or {}
        self.tag_reads = []

    def get_paginator(self, operation):
        def paginate(**kwargs):
            if operation == "list_recovery_points_by_backup_vault":
                return [{"RecoveryPoints": (self.vault_points.get(kwargs["BackupVaultName"], [])
                                             if self.vault_points is not None else self.points)}]
            if operation == "list_copy_jobs":
                return [{"CopyJobs": list(self.jobs)}]
            raise AssertionError("Unexpected paginator " + operation)
        return SimpleNamespace(paginate=paginate)

    def start_copy_job(self, **kwargs):
        self.started.append(kwargs)
        return {"CopyJobId": "job-" + str(len(self.started))}

    def describe_restore_job(self, **kwargs):
        return {"Status": "COMPLETED", "ResourceType": "EBS", "IamRoleArn": "restore-role",
                "CreatedResourceArn": "arn:aws:ec2:us-west-1:222222222222:volume/vol-test"}

    def put_restore_validation_result(self, **kwargs):
        self.validation.append(kwargs)

    def delete_recovery_point(self, **kwargs):
        self.deleted.append(kwargs)

    def list_tags(self, **kwargs):
        self.tag_reads.append(kwargs["ResourceArn"])
        return {"Tags": self.tags.get(kwargs["ResourceArn"], {})}


class RecoveryTests(unittest.TestCase):
    def test_missing_dr_seeds_only_allowed_recent_local_points(self):
        other = dict(POINT, ResourceArn="unrelated-volume")
        primary, dr, stage = Backup(vault_points={"local": [POINT, other]}), Backup(), Backup()
        missing, errors = module.reconcile(primary, dr, stage, CONFIG, NOW)
        self.assertEqual(errors, [])
        self.assertEqual(len(missing), 1)
        self.assertEqual(len(primary.started), 1)
        self.assertEqual(primary.started[0]["DestinationBackupVaultArn"], "dr-vault")
        self.assertEqual(dr.started, [])

    def test_stale_local_is_not_used_to_claim_recovery(self):
        primary = Backup(vault_points={"local": [dict(POINT, CreationDate=NOW - timedelta(days=3))]})
        missing, errors = module.reconcile(primary, Backup(), Backup(), CONFIG, NOW)
        self.assertIn("daily capture missing/older", missing[0])
        self.assertEqual(primary.started, [])

    def test_new_volume_gets_48h_grace_for_first_capture_and_copy(self):
        for created, expected in [({RESOURCE: NOW - timedelta(hours=10)}, 0),
                                  ({RESOURCE: NOW - timedelta(hours=49)}, 2),
                                  ({}, 2), (None, 2)]:
            with self.subTest(created=created):
                missing, errors = module.reconcile(Backup(), Backup(), Backup(), CONFIG, NOW, created)
                self.assertEqual(errors, [])
                self.assertEqual(len(missing), expected)

    def test_volume_creation_times_filters_and_skips_deleted_volumes(self):
        gone = "arn:aws:ec2:us-west-1:111111111111:volume/vol-deleted"
        created = NOW - timedelta(hours=5)
        paginator = SimpleNamespace(paginate=Mock(return_value=[
            {"Volumes": [{"VolumeId": "vol-example", "CreateTime": created}]}]))
        ec2 = SimpleNamespace(get_paginator=Mock(return_value=paginator))
        self.assertEqual(module.volume_creation_times(ec2, [RESOURCE, gone]), {RESOURCE: created})
        ec2.get_paginator.assert_called_once_with("describe_volumes")
        paginator.paginate.assert_called_once_with(
            Filters=[{"Name": "volume-id", "Values": ["vol-deleted", "vol-example"]}])
        self.assertEqual(module.volume_creation_times(ec2, []), {})

    def test_wrong_dr_key_never_crosses_accounts(self):
        dr = Backup([dict(POINT, EncryptionKeyArn="aws/ebs")])
        _, errors = module.reconcile(Backup([POINT]), dr, Backup(), CONFIG, NOW)
        self.assertIn("expected customer key", errors[0])
        self.assertEqual(dr.started, [])

    def test_copy_preserves_annual_retention_and_deduplicates(self):
        client, jobs = Backup(), []
        self.assertEqual(module.copy_point(client, POINT, "dr", "stage", jobs, "role", NOW, 365), "started")
        self.assertEqual(client.started[0]["Lifecycle"], {"DeleteAfterDays": 365})
        self.assertEqual(module.copy_point(client, POINT, "dr", "stage", jobs, "role", NOW, 365), "in-flight")
        self.assertEqual(len(client.started), 1)

    def test_copy_retries_are_bounded(self):
        failed = {"SourceRecoveryPointArn": POINT["RecoveryPointArn"], "DestinationBackupVaultArn": "stage",
                  "State": "FAILED", "CreationDate": NOW - timedelta(hours=7)}
        with self.assertRaisesRegex(RuntimeError, "three attempts"):
            module.copy_point(Backup(), POINT, "dr", "stage", [failed] * 3, "role", NOW, 7)
        self.assertEqual(module.copy_point(Backup(), POINT, "dr", "stage",
                                          [dict(failed, CreationDate=NOW)], "role", NOW, 7), "backoff")

    def test_restore_must_be_encrypted_detached_and_test_tagged(self):
        for encrypted, attachments, tags, expected in [
            (True, [], [{"Key": "awsbackup-restore-test", "Value": "test"}], "SUCCESSFUL"),
            (False, [], [{"Key": "awsbackup-restore-test", "Value": "test"}], "FAILED"),
            (True, [{"InstanceId": "unexpected-host"}], [{"Key": "awsbackup-restore-test", "Value": "test"}], "FAILED"),
            (True, [], [], "FAILED"),
        ]:
            backup = Backup()
            ec2 = SimpleNamespace(meta=SimpleNamespace(region_name="us-west-1"),
                                  describe_volumes=lambda **kwargs: {"Volumes": [{"State": "available",
                                      "Encrypted": encrypted, "Attachments": attachments, "Tags": tags}]})
            if expected == "FAILED":
                with self.assertRaises(RuntimeError):
                    module.validate_restore(backup, ec2, "job", "restore-role", "222222222222")
            else:
                module.validate_restore(backup, ec2, "job", "restore-role", "222222222222")
            self.assertEqual(backup.validation[0]["ValidationStatus"], expected)

    def test_cleanup_only_known_expired_detached_tests(self):
        deleted = []
        job = {"RestoreJobId": "job", "IamRoleArn": "restore-role", "ResourceType": "EBS",
               "CompletionDate": NOW - timedelta(hours=5),
               "CreatedResourceArn": "arn:aws:ec2:us-west-1:222222222222:volume/vol-test"}
        ec2 = SimpleNamespace(meta=SimpleNamespace(region_name="us-west-1"),
                              describe_volumes=lambda **kwargs: {"Volumes": [{"State": "available",
                                  "Attachments": [], "Tags": [{"Key": "awsbackup-restore-test"}]}]},
                              delete_volume=lambda **kwargs: deleted.append(kwargs))
        self.assertFalse(module.cleanup_expired_test(ec2, dict(job, IamRoleArn="other-role"),
                                                    "restore-role", "222222222222", NOW))
        self.assertFalse(module.cleanup_expired_test(ec2, dict(job, CompletionDate=NOW),
                                                    "restore-role", "222222222222", NOW))
        self.assertTrue(module.cleanup_expired_test(ec2, job, "restore-role", "222222222222", NOW))
        self.assertEqual(deleted, [{"VolumeId": "vol-test"}])


def copied(source, suffix, *, key="final-key", retention=None):
    return dict(source, RecoveryPointArn="arn:aws:ec2:us-east-1::snapshot/snap-" + suffix,
                IsEncrypted=True, EncryptionKeyArn=key,
                Lifecycle={"DeleteAfterDays": retention or module.final_retention(source)})


def completed_copy(source, destination_point, destination="stage-vault", source_vault="pending-vault", **changes):
    return dict({"State": "COMPLETED", "SourceRecoveryPointArn": source["RecoveryPointArn"],
                 "DestinationRecoveryPointArn": destination_point["RecoveryPointArn"],
                 "SourceBackupVaultArn": source_vault, "DestinationBackupVaultArn": destination,
                 "ResourceArn": source["ResourceArn"], "CreationDate": NOW,
                 "CompletionDate": NOW}, **changes)


class PipelineTests(unittest.TestCase):
    def direct(self, source=None, final_changes=None, job_changes=None, config_changes=None):
        source = source or dict(POINT, IsEncrypted=False, Lifecycle={})
        final = dict(copied(source, "final"), **(final_changes or {}))
        job = completed_copy(source, final, **(job_changes or {}))
        config = dict(CONFIG, cleanup_enabled=True, **(config_changes or {}))
        primary = Backup(vault_points={"pending": [source]}, jobs=[job])
        dr, stage = Backup(), Backup([final])
        result = module.reconcile(primary, dr, stage, config, NOW)
        return primary, dr, stage, result

    def test_gfs_uses_original_utc_capture_time_not_lifecycle(self):
        for captured, retention in [(NOW, 7), (NOW - timedelta(days=1), 30),
                                    (NOW.replace(day=1), 365),
                                    (datetime(2026, 11, 1, tzinfo=timezone.utc), 365)]:
            self.assertEqual(module.final_retention(dict(POINT, CreationDate=captured, Lifecycle={})), retention)
        # September 1 locally is still August 31 UTC: no accidental monthly class.
        offset = timezone(timedelta(hours=10))
        self.assertEqual(module.final_retention(dict(POINT, CreationDate=datetime(2026, 9, 1, 1, tzinfo=offset))), 7)

    def test_unencrypted_source_copies_directly_with_daily_retention(self):
        source = dict(POINT, IsEncrypted=False, Lifecycle={})
        primary, dr = Backup(vault_points={"pending": [source]}), Backup()
        _, errors = module.reconcile(primary, dr, Backup(), CONFIG, NOW)
        self.assertEqual(errors, [])
        self.assertEqual(primary.started[0]["DestinationBackupVaultArn"], "stage-vault")
        self.assertEqual(primary.started[0]["Lifecycle"], {"DeleteAfterDays": 7})
        self.assertEqual(dr.started, [])

    def test_pending_cmk_copy_inherits_indefinite_ttl_and_seed_explicitly_bridges_365(self):
        for vault, expected in [("pending", None), ("local", {"DeleteAfterDays": 365})]:
            primary = Backup(vault_points={vault: [dict(POINT, Lifecycle={})]})
            module.reconcile(primary, Backup(), Backup(), CONFIG, NOW)
            self.assertEqual(primary.started[0].get("Lifecycle"), expected)
            self.assertEqual(primary.started[0]["DestinationBackupVaultArn"], "dr-vault")

    def test_unknown_encryption_or_unallowlisted_encrypted_source_fails_closed(self):
        for source, hops in [(dict(POINT, IsEncrypted=None), [RESOURCE]),
                             (POINT, []), (dict(POINT, EncryptionKeyArn=""), [RESOURCE])]:
            primary = Backup(vault_points={"pending": [source]})
            _, errors = module.reconcile(primary, Backup(), Backup(), dict(CONFIG, cmk_hop_volumes=hops), NOW)
            self.assertIn("Unexpected source encryption", errors[0])
            self.assertEqual(primary.started + primary.deleted, [])

    def test_direct_cleanup_requires_exact_destination_and_never_deletes_stage(self):
        primary, dr, stage, (_, errors) = self.direct()
        self.assertEqual(errors, [])
        self.assertEqual(primary.deleted, [{"BackupVaultName": "pending", "RecoveryPointArn": POINT["RecoveryPointArn"]}])
        self.assertEqual(primary.started + dr.deleted + stage.deleted, [])

    def test_cleanup_gate_requires_literal_true(self):
        for enabled in [False, None, "true", 1]:
            source = dict(POINT, IsEncrypted=False)
            final = copied(source, "final")
            primary = Backup(vault_points={"pending": [source]}, jobs=[completed_copy(source, final)])
            module.reconcile(primary, Backup(), Backup([final]), dict(CONFIG, cleanup_enabled=enabled), NOW)
            self.assertEqual(primary.deleted, [])

    def test_legacy_points_are_never_deleted_even_with_cleanup_enabled(self):
        source = dict(POINT, IsEncrypted=False)
        final = copied(source, "final")
        primary = Backup(vault_points={"local": [source]}, jobs=[completed_copy(source, final, source_vault="local-vault")])
        module.reconcile(primary, Backup(), Backup([final]), dict(CONFIG, cleanup_enabled=True), NOW)
        self.assertEqual(primary.deleted + primary.started, [])

    def test_fresh_final_satisfies_health_after_processing_point_disappears(self):
        primary = Backup(vault_points={"local": [dict(POINT, CreationDate=NOW - timedelta(days=1))]})
        result = module.reconcile(primary, Backup(), Backup([copied(POINT, "final")]), CONFIG, NOW)
        self.assertEqual(result, ([], []))
        self.assertEqual(primary.started, [])

    def test_completed_job_alone_never_authorizes_deletion(self):
        source = dict(POINT, IsEncrypted=False)
        final = copied(source, "final")
        primary = Backup(vault_points={"pending": [source]}, jobs=[completed_copy(
            source, final, CreationDate=NOW - timedelta(hours=3), CompletionDate=NOW - timedelta(hours=3))])
        _, errors = module.reconcile(primary, Backup(), Backup(), dict(CONFIG, cleanup_enabled=True), NOW)
        self.assertIn("no verified destination", errors[0])
        self.assertEqual(primary.deleted + primary.started, [])

    def test_wrong_destination_metadata_or_job_lineage_cannot_authorize_cleanup(self):
        for changes in [{"CreationDate": NOW - timedelta(seconds=1)}, {"ResourceArn": "unrelated"},
                        {"IsEncrypted": False}, {"Status": "EXPIRED"}, {"ResourceType": "EC2"},
                        {"Lifecycle": {}}, {"Lifecycle": {"DeleteAfterDays": 1}}]:
            with self.subTest(changes=changes):
                primary, _, _, _ = self.direct(final_changes=changes)
                self.assertEqual(primary.deleted, [])
        for changes in [{"State": "RUNNING"}, {"DestinationRecoveryPointArn": "missing"},
                        {"ResourceArn": "unrelated"}, {"SourceRecoveryPointArn": "unrelated"},
                        {"DestinationBackupVaultArn": "other-vault"}]:
            with self.subTest(changes=changes):
                primary, _, _, _ = self.direct(job_changes=changes)
                self.assertEqual(primary.deleted, [])

    def test_hour_old_completed_copy_allows_propagation_without_delete_or_duplicate(self):
        source = dict(POINT, IsEncrypted=False)
        job = completed_copy(source, copied(source, "absent"))
        primary = Backup(vault_points={"pending": [source]}, jobs=[job])
        _, errors = module.reconcile(primary, Backup(), Backup(), dict(CONFIG, cleanup_enabled=True), NOW)
        self.assertEqual(errors, [])
        self.assertEqual(primary.deleted + primary.started, [])

    def test_expired_but_still_completed_destination_does_not_authorize_cleanup(self):
        primary, _, _, _ = self.direct(final_changes={"CalculatedLifecycle": {"DeleteAt": NOW}})
        self.assertEqual(primary.deleted, [])
        source = dict(POINT, IsEncrypted=False, CreationDate=datetime(2026, 8, 29, tzinfo=timezone.utc))
        primary, _, _, _ = self.direct(source=source)
        self.assertEqual(primary.deleted, [])

    def test_stale_inflight_and_elapsed_retention_are_explicit_not_deleted(self):
        source = dict(POINT, IsEncrypted=False)
        job = completed_copy(source, copied(source, "future"), State="RUNNING",
                             CreationDate=NOW - timedelta(hours=25))
        primary = Backup(vault_points={"pending": [source]}, jobs=[job])
        _, errors = module.reconcile(primary, Backup(), Backup(), CONFIG, NOW)
        self.assertIn("pending more than 24h", errors[0])
        expired = dict(source, CreationDate=NOW - timedelta(days=8))  # Sunday gets 30 days.
        expired["CreationDate"] = datetime(2026, 8, 29, tzinfo=timezone.utc)
        primary = Backup(vault_points={"pending": [expired]})
        _, errors = module.reconcile(primary, Backup(), Backup(), CONFIG, NOW)
        self.assertIn("retention window already elapsed", errors[0])
        self.assertEqual(primary.started + primary.deleted, [])

    def test_noncompleted_processing_remnants_alarm_without_further_delete(self):
        for status in ["EXPIRED", "PARTIAL", "DELETING"]:
            primary = Backup(vault_points={"pending": [dict(POINT, Status=status)]})
            _, errors = module.reconcile(primary, Backup(), Backup(), dict(CONFIG, cleanup_enabled=True), NOW)
            self.assertIn(status, errors[0])
            self.assertEqual(primary.deleted, [])

    def test_original_monthly_date_survives_both_legs_and_pending_ttl(self):
        source = dict(POINT, CreationDate=NOW.replace(day=1), Lifecycle={})
        intermediate = copied(source, "cmk", key="dr-key", retention=365)
        primary = Backup(vault_points={"pending": [source]}, jobs=[completed_copy(source, intermediate, "dr-vault")])
        dr = Backup([intermediate])
        module.reconcile(primary, dr, Backup(), CONFIG, NOW)
        self.assertEqual(dr.started[0]["Lifecycle"], {"DeleteAfterDays": 365})
        self.assertEqual(primary.started + primary.deleted, [])

    def cmk_pair(self, *, source_changes=None, first_leg_changes=None, final_changes=None, remove_first=False):
        source = dict(POINT, Lifecycle={})
        intermediate = copied(source, "cmk", key="dr-key", retention=365)
        final = dict(copied(source, "final"), **(final_changes or {}))
        first = completed_copy(source, intermediate, "dr-vault", **(first_leg_changes or {}))
        primary = Backup(vault_points={"pending": [dict(source, **(source_changes or {}))]},
                         jobs=[] if remove_first else [first])
        dr = Backup([intermediate], [completed_copy(intermediate, final, source_vault="dr-vault")])
        result = module.reconcile(primary, dr, Backup([final]), dict(CONFIG, cleanup_enabled=True), NOW)
        return primary, dr, result

    def test_cmk_source_cleanup_requires_both_legs_and_keeps_latest_checkpoint(self):
        primary, dr, (_, errors) = self.cmk_pair()
        self.assertEqual(errors, [])
        self.assertEqual(len(primary.deleted), 1)
        self.assertEqual(dr.deleted, [])

    def test_missing_or_wrong_first_leg_prevents_cmk_source_cleanup(self):
        for changes in [{"State": "RUNNING"}, {"ResourceArn": "wrong"},
                        {"DestinationRecoveryPointArn": "wrong"}]:
            primary, dr, _ = self.cmk_pair(first_leg_changes=changes)
            self.assertEqual(primary.deleted + dr.deleted, [])
        primary, dr, (_, errors) = self.cmk_pair(remove_first=True)
        self.assertEqual(primary.deleted + dr.deleted, [])
        self.assertTrue(any("Missing first-leg lineage" in error for error in errors))

    def test_failed_final_leg_prevents_cmk_source_cleanup(self):
        primary, dr, _ = self.cmk_pair(final_changes={"Lifecycle": {"DeleteAfterDays": 1}})
        self.assertEqual(primary.deleted + dr.deleted, [])

    def test_only_older_checkpoint_with_verified_newer_successor_is_deleted(self):
        old_source = dict(POINT, CreationDate=NOW - timedelta(days=1), RecoveryPointArn="source-old")
        new_source = dict(POINT, RecoveryPointArn="source-new")
        old = copied(old_source, "old", key="dr-key", retention=365)
        new = copied(new_source, "new", key="dr-key", retention=365)
        old_final, new_final = copied(old, "old-final"), copied(new, "new-final")
        first_jobs = [completed_copy(old_source, old, "dr-vault"), completed_copy(new_source, new, "dr-vault")]
        final_jobs = [completed_copy(old, old_final, source_vault="dr-vault"),
                      completed_copy(new, new_final, source_vault="dr-vault")]
        for with_successor in [False, True]:
            primary = Backup(vault_points={}, jobs=first_jobs)
            dr = Backup([old, new], final_jobs if with_successor else final_jobs[:1])
            stage = Backup([old_final, new_final] if with_successor else [old_final])
            module.reconcile(primary, dr, stage, dict(CONFIG, cleanup_enabled=True), NOW)
            expected = [{"BackupVaultName": "dr", "RecoveryPointArn": old["RecoveryPointArn"]}] if with_successor else []
            self.assertEqual(dr.deleted, expected)
            self.assertEqual(primary.deleted, [])

    def test_unrelated_first_leg_vault_cannot_authorize_checkpoint_cleanup(self):
        primary, dr, (_, errors) = self.cmk_pair(first_leg_changes={"SourceBackupVaultArn": "other-vault"})
        self.assertEqual(primary.deleted + dr.deleted, [])
        self.assertTrue(any("Missing first-leg lineage" in error for error in errors))

    def test_invalid_vault_boundary_or_hop_allowlist_fails_closed(self):
        for config in [dict(CONFIG, processing_vault="local"), dict(CONFIG, cmk_hop_volumes=["unrelated"])]:
            with self.assertRaises(ValueError):
                module.reconcile(Backup(), Backup(), Backup(), config, NOW)


class ExpiredCleanupTests(unittest.TestCase):
    provenance = {"BackupPipeline": "fleet-processing-v2", "aws:backup:source-resource": "backup-source-id"}

    def direct(self, *, source_changes=None, final_changes=None, job_changes=None, tags=None, enabled=True):
        source = dict(POINT, IsEncrypted=False, Lifecycle={})
        final = dict(copied(source, "final"), **(final_changes or {}))
        job = completed_copy(source, final, **(job_changes or {}))
        source = dict(dict(source, Status="EXPIRED"), **(source_changes or {}))
        primary = Backup(vault_points={"pending": [source]}, jobs=[job],
                         tags={source["RecoveryPointArn"]: self.provenance if tags is None else tags})
        dr = Backup()
        result = module.reconcile(primary, dr, Backup([final]), dict(CONFIG, cleanup_enabled=enabled), NOW)
        self.assertEqual(primary.started + dr.started, [], "Expired cleanup must never start a copy")
        return primary, result

    def test_proven_expired_processing_retries_but_remains_unhealthy_until_absent(self):
        primary, (_, errors) = self.direct()
        self.assertEqual(primary.deleted, [{"BackupVaultName": "pending", "RecoveryPointArn": POINT["RecoveryPointArn"]}])
        self.assertEqual(primary.tag_reads, [POINT["RecoveryPointArn"]])
        self.assertTrue(any("remains EXPIRED" in error for error in errors))

    def test_disabled_gate_never_reads_tags_or_retries_expired_cleanup(self):
        for enabled in [False, None, "true", 1]:
            primary, (_, errors) = self.direct(enabled=enabled)
            self.assertEqual(primary.deleted + primary.tag_reads, [])
            self.assertTrue(any("EXPIRED" in error for error in errors))

    def test_processing_provenance_requires_both_pipeline_and_reserved_source_tags(self):
        for tags in [{}, {"BackupPipeline": "other", "aws:backup:source-resource": "id"},
                     {"BackupPipeline": "fleet-processing-v2"}, {"aws:backup:source-resource": "id"}]:
            with self.subTest(tags=tags):
                primary, (_, errors) = self.direct(tags=tags)
                self.assertEqual(primary.deleted, [])
                self.assertTrue(any("lacks pipeline provenance" in error for error in errors))

    def test_tag_read_failure_is_fail_closed(self):
        with patch.object(Backup, "list_tags", side_effect=RuntimeError("AccessDenied ListTags")):
            primary, (_, errors) = self.direct()
        self.assertEqual(primary.deleted, [])
        self.assertIn("AccessDenied ListTags", errors)

    def test_partial_and_deleting_are_not_expired_retry_candidates(self):
        for status in ["PARTIAL", "DELETING"]:
            primary, _ = self.direct(source_changes={"Status": status})
            self.assertEqual(primary.deleted + primary.tag_reads, [])

    def test_missing_mismatched_expired_or_time_expired_final_never_authorizes_retry(self):
        for changes in [{"Status": "EXPIRED"}, {"Status": "PARTIAL"}, {"ResourceArn": "other"},
                        {"CreationDate": NOW - timedelta(seconds=1)}, {"IsEncrypted": False},
                        {"Lifecycle": {}}, {"CalculatedLifecycle": {"DeleteAt": NOW}}]:
            with self.subTest(changes=changes):
                primary, _ = self.direct(final_changes=changes)
                self.assertEqual(primary.deleted + primary.tag_reads, [])
        for changes in [{"State": "FAILED"}, {"SourceRecoveryPointArn": "other"},
                        {"SourceBackupVaultArn": "other"}, {"DestinationRecoveryPointArn": "missing"},
                        {"DestinationBackupVaultArn": "other"}, {"ResourceArn": "other"}]:
            with self.subTest(changes=changes):
                primary, _ = self.direct(job_changes=changes)
                self.assertEqual(primary.deleted + primary.tag_reads, [])

    def pair(self, *, old_changes=None, new_changes=None, old_final_changes=None,
             new_final_changes=None, first_changes=None, include_source=False):
        old_source = dict(POINT, CreationDate=NOW - timedelta(days=1), RecoveryPointArn="source-old")
        new_source = dict(POINT, RecoveryPointArn="source-new")
        old = copied(old_source, "old", key="dr-key", retention=365)
        new = dict(copied(new_source, "new", key="dr-key", retention=365), **(new_changes or {}))
        old_final = dict(copied(old, "old-final"), **(old_final_changes or {}))
        new_final = dict(copied(new, "new-final"), **(new_final_changes or {}))
        first_jobs = [completed_copy(old_source, old, "dr-vault", **(first_changes or {})),
                      completed_copy(new_source, new, "dr-vault")]
        final_jobs = [completed_copy(old, old_final, source_vault="dr-vault"),
                      completed_copy(new, new_final, source_vault="dr-vault")]
        old = dict(dict(old, Status="EXPIRED"), **(old_changes or {}))
        primary = Backup(vault_points={"pending": [dict(old_source, Status="EXPIRED")] if include_source else []},
                         jobs=first_jobs, tags={old_source["RecoveryPointArn"]: self.provenance})
        dr = Backup([old, new], final_jobs)
        result = module.reconcile(primary, dr, Backup([old_final, new_final]), dict(CONFIG, cleanup_enabled=True), NOW)
        self.assertEqual(primary.started + dr.started, [])
        return primary, dr, result

    def test_superseded_expired_checkpoint_retries_only_with_its_own_and_newer_final_proof(self):
        primary, dr, (_, errors) = self.pair()
        self.assertEqual(dr.deleted, [{"BackupVaultName": "dr", "RecoveryPointArn": "arn:aws:ec2:us-east-1::snapshot/snap-old"}])
        self.assertEqual(primary.deleted + primary.tag_reads, [])
        self.assertTrue(any("EXPIRED" in error for error in errors))

    def test_expired_processing_can_use_expired_checkpoint_only_as_historical_chain_proof(self):
        primary, dr, (_, errors) = self.pair(include_source=True)
        self.assertEqual(primary.deleted, [{"BackupVaultName": "pending", "RecoveryPointArn": "source-old"}])
        self.assertEqual(len(dr.deleted), 1)
        self.assertEqual(sum("EXPIRED" in error for error in errors), 2)

    def test_newest_or_equal_date_expired_checkpoint_is_never_deleted(self):
        for old_changes in [{"CreationDate": NOW}, {"CreationDate": NOW + timedelta(seconds=1)}]:
            primary, dr, _ = self.pair(old_changes=old_changes)
            self.assertEqual(primary.deleted + dr.deleted, [])

    def test_expired_successor_or_unverified_successor_cannot_authorize_checkpoint_retry(self):
        for new_changes, final_changes in [({"Status": "EXPIRED"}, {}), ({"Status": "PARTIAL"}, {}),
                                          ({}, {"Status": "EXPIRED"}), ({}, {"IsEncrypted": False})]:
            primary, dr, _ = self.pair(new_changes=new_changes, new_final_changes=final_changes)
            self.assertEqual(primary.deleted + dr.deleted, [])

    def test_checkpoint_needs_exact_key_origin_and_unexpired_final(self):
        cases = [{"old_changes": {"EncryptionKeyArn": "wrong-key"}},
                 {"first_changes": {"SourceBackupVaultArn": "legacy-unrelated"}},
                 {"first_changes": {"State": "FAILED"}},
                 {"first_changes": {"ResourceArn": "other"}},
                 {"old_final_changes": {"Status": "EXPIRED"}},
                 {"old_final_changes": {"CalculatedLifecycle": {"DeleteAt": NOW}}}]
        for changes in cases:
            with self.subTest(changes=changes):
                primary, dr, _ = self.pair(**changes)
                self.assertEqual(primary.deleted + dr.deleted, [])

    def test_expired_points_never_become_copy_usable_or_default_accepted_destinations(self):
        expired = dict(POINT, Status="EXPIRED")
        self.assertEqual(module.usable([expired], {RESOURCE}), [])
        self.assertIsNone(module.verified_copy(POINT, [expired], [completed_copy(POINT, expired)],
                                              "stage-vault", 365))
        with self.assertRaisesRegex(ValueError, "exact checkpoint key"):
            module.verified_copy(POINT, [expired], [], "stage-vault", 365, expired_checkpoint_lineage=True)
        for status in ["EXPIRED", "PARTIAL", "DELETING", None]:
            client = Backup()
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "non-completed recovery point"):
                module.copy_point(client, dict(POINT, Status=status), "pending", "stage-vault", [], "role", NOW, 7)
            self.assertEqual(client.started, [])


class MonthlyRestoreTests(unittest.TestCase):
    def stage(self, jobs_by_resource=None, plan=None):
        jobs_by_resource = jobs_by_resource or {}
        paginator = SimpleNamespace(paginate=Mock(side_effect=lambda **kwargs: [
            {"RestoreJobs": jobs_by_resource.get(kwargs["ResourceArn"], [])}]))
        return SimpleNamespace(
            get_restore_testing_plan=Mock(return_value={"RestoreTestingPlan": plan or PLAN}),
            get_paginator=Mock(return_value=paginator))

    def health(self, stage, now=None, config=None, created=None):
        return module.monthly_restore_health(stage, config or CONFIG, now or SCHEDULED + timedelta(hours=3), created)

    def test_cold_bootstrap_does_not_require_a_preexisting_monthly_test(self):
        stage = self.stage()
        self.assertEqual(self.health(stage, NOW), [])
        stage.get_paginator.assert_not_called()

    def test_volume_created_within_48h_of_the_test_is_first_required_next_month(self):
        stage = self.stage()
        for created, expected in [({RESOURCE: SCHEDULED - timedelta(hours=47)}, 0),
                                  ({RESOURCE: SCHEDULED + timedelta(days=2)}, 0),
                                  ({RESOURCE: SCHEDULED - timedelta(hours=49)}, 1),
                                  ({}, 1), (None, 1)]:
            with self.subTest(created=created):
                self.assertEqual(len(self.health(stage, created=created)), expected)

    def test_missing_job_is_detected_without_any_event_after_start_window(self):
        stage = self.stage()
        self.assertEqual(self.health(stage, SCHEDULED + timedelta(hours=1, minutes=59)), [])
        self.assertIn("monthly restore test missing", self.health(stage, SCHEDULED + timedelta(hours=2))[0])
        stage.get_restore_testing_plan.assert_called_with(RestoreTestingPlanName="fleet")

    def test_monthly_boundaries_include_previous_month_and_previous_year(self):
        old_plan = dict(PLAN, CreationTime=datetime(2025, 1, 1, tzinfo=timezone.utc))
        self.assertEqual(module.monthly_test_start(old_plan, NOW),
                         datetime(2026, 8, 8, 12, tzinfo=timezone.utc))
        self.assertEqual(module.monthly_test_start(old_plan, datetime(2026, 1, 1, tzinfo=timezone.utc)),
                         datetime(2025, 12, 8, 12, tzinfo=timezone.utc))

    def test_missing_or_changed_plan_does_not_receive_bootstrap_grace(self):
        stage = self.stage()
        stage.get_restore_testing_plan.side_effect = RuntimeError("ResourceNotFoundException")
        with self.assertRaisesRegex(RuntimeError, "ResourceNotFound"):
            self.health(stage)
        for change in [{"ScheduleExpression": "cron(0 12 9 * ? *)"},
                       {"ScheduleExpressionTimezone": "America/Los_Angeles"},
                       {"StartWindowHours": 24}, {"RestoreTestingPlanArn": "other-plan"}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.health(self.stage(plan=dict(PLAN, **change)))

    def test_every_configured_volume_needs_its_own_successful_test(self):
        stage = self.stage({RESOURCE: [RESTORE_JOB]})
        config = dict(CONFIG, volumes=[RESOURCE, "other-volume"])
        errors = self.health(stage, config=config)
        self.assertEqual(len(errors), 1)
        self.assertIn("other-volume", errors[0])
        self.assertEqual(stage.get_paginator.call_count, 2)

    def test_unrelated_role_plan_or_old_job_cannot_mask_missing_test(self):
        for changes in [{"IamRoleArn": "other-role"}, {"ResourceType": "EC2"},
                        {"CreatedBy": {"RestoreTestingPlanArn": "other-plan"}},
                        {"CreatedBy": {}}, {"CreationDate": SCHEDULED - timedelta(days=31)}]:
            with self.subTest(changes=changes):
                errors = self.health(self.stage({RESOURCE: [dict(RESTORE_JOB, **changes)]}))
                self.assertIn("monthly restore test missing", errors[0])

    def test_failed_aborted_and_validation_timeout_are_detected_without_events(self):
        for changes in [{"Status": "FAILED"}, {"Status": "ABORTED"},
                        {"ValidationStatus": "FAILED"}, {"ValidationStatus": "TIMED_OUT"},
                        {"DeletionStatus": "FAILED"}]:
            with self.subTest(changes=changes):
                errors = self.health(self.stage({RESOURCE: [dict(RESTORE_JOB, **changes)]}))
                self.assertEqual(len(errors), 1)

    def test_completed_but_unvalidated_job_is_not_success(self):
        stage = self.stage({RESOURCE: [dict(RESTORE_JOB, ValidationStatus="VALIDATING")]})
        self.assertEqual(self.health(stage, SCHEDULED + timedelta(hours=2)), [])
        self.assertIn("unvalidated after 2h", self.health(stage)[0])

    def test_running_job_has_bounded_completion_grace(self):
        stage = self.stage({RESOURCE: [dict(RESTORE_JOB, Status="RUNNING", ValidationStatus="")]})
        self.assertEqual(self.health(stage), [])
        self.assertIn("unfinished after 24h", self.health(stage, SCHEDULED + timedelta(days=1))[0])

    def test_new_success_clears_previous_attempt_failure_and_old_failure_does_not_linger(self):
        failed = dict(RESTORE_JOB, Status="FAILED", CreationDate=SCHEDULED)
        stage = self.stage({RESOURCE: [RESTORE_JOB, failed]})
        self.assertEqual(self.health(stage), [])
        next_month = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
        self.assertIn("monthly restore test missing", self.health(stage, next_month + timedelta(hours=3))[0])

    def test_terminal_validation_is_never_rewritten(self):
        backup, ec2 = Mock(), Mock()
        for status in ["SUCCESSFUL", "FAILED", "TIMED_OUT"]:
            backup.describe_restore_job.return_value = dict(RESTORE_JOB, ValidationStatus=status)
            module.validate_restore(backup, ec2, "job", "restore-role", "222222222222")
        backup.put_restore_validation_result.assert_not_called()
        ec2.describe_volumes.assert_not_called()

    def test_restore_event_status_and_backup_copy_state_alert(self):
        for detail_type, field in [("Restore Job State Change", "status"),
                                   ("Backup Job State Change", "state"),
                                   ("Copy Job State Change", "state")]:
            self.assertTrue(module.failed_job_event({"detail-type": detail_type, "detail": {field: "FAILED"}}))
            self.assertFalse(module.failed_job_event({"detail-type": detail_type, "detail": {field: "COMPLETED"}}))
        self.assertFalse(module.failed_job_event({}))


class CanaryRestoreTests(unittest.TestCase):
    config = dict(CONFIG, canary_plan_arn="canary-arn", canary_start_at="2026-09-08T12:00:00Z")

    def job(self, **changes):
        return dict(dict(RESTORE_JOB, CreatedBy={"RestoreTestingPlanArn": "canary-arn"}), **changes)

    def health(self, jobs, now=None, config=None):
        stage = MonthlyRestoreTests().stage(jobs)
        return module.canary_restore_health(stage, config or self.config,
                                            now or SCHEDULED + timedelta(hours=3))

    def test_disabled_canary_does_not_query_restore_jobs(self):
        stage = Mock()
        self.assertEqual(module.canary_restore_health(stage, CONFIG, NOW), [])
        stage.get_paginator.assert_not_called()

    def test_only_explicit_accepted_cutover_retires_one_off_health(self):
        stage = Mock()
        self.assertEqual(module.canary_restore_health(stage, dict(self.config, canary_accepted=True),
                                                      SCHEDULED + timedelta(days=120)), [])
        stage.get_paginator.assert_not_called()
        for value in ["true", "false", 1, 0, None, [], {}]:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "canary acceptance"):
                module.canary_restore_health(stage, dict(self.config, canary_accepted=value), NOW)
        stage.get_paginator.assert_not_called()

    def test_accepted_cutover_does_not_skip_strict_schedule_validation(self):
        with self.assertRaisesRegex(ValueError, "canary schedule"):
            module.canary_restore_health(Mock(), dict(self.config, canary_accepted=True,
                                                      canary_start_at="invalid"), NOW)

    def test_all_five_sources_need_individual_restore_validation_and_deletion_success(self):
        resources = [RESOURCE + str(index) for index in range(5)]
        config = dict(self.config, volumes=resources)
        prior = self.job(Status="FAILED", CreationDate=SCHEDULED - timedelta(minutes=15))
        jobs = {resource: [dict(prior, RestoreJobId="old-" + str(index)),
                           self.job(RestoreJobId="new-" + str(index))]
                for index, resource in enumerate(resources)}
        self.assertEqual(self.health(jobs, config=config), [])
        # Four complete successes cannot clear the fifth source's old failure.
        for changes in [{"Status": "RUNNING"}, {"ValidationStatus": "VALIDATING"},
                        {"DeletionStatus": "DELETING"}]:
            with self.subTest(changes=changes):
                partial = dict(jobs, **{resources[-1]: [prior, self.job(**changes)]})
                errors = self.health(partial, now=SCHEDULED + timedelta(minutes=30), config=config)
                self.assertEqual(len(errors), 1)
                self.assertIn(resources[-1], errors[0])
                self.assertIn("failed", errors[0])
        missing = dict(jobs, **{resources[-1]: []})
        errors = self.health(missing, config=config)
        self.assertEqual(len(errors), 1)
        self.assertIn("missing", errors[0])
        self.assertIn(resources[-1], errors[0])

    def test_prior_failure_remains_unhealthy_before_a_future_retry_or_with_no_retry_job(self):
        prior = self.job(Status="FAILED", CreationDate=SCHEDULED - timedelta(days=60))
        for now in [SCHEDULED - timedelta(hours=1), SCHEDULED + timedelta(minutes=30),
                    SCHEDULED + timedelta(hours=3)]:
            with self.subTest(now=now):
                self.assertIn("failed", self.health({RESOURCE: [prior]}, now=now)[0])

    def test_prior_failure_remains_unhealthy_until_every_current_status_is_successful(self):
        for prior_changes in [{"Status": "FAILED"}, {"Status": "ABORTED"},
                              {"ValidationStatus": "FAILED"}, {"ValidationStatus": "TIMED_OUT"},
                              {"DeletionStatus": "FAILED"}]:
            prior = self.job(CreationDate=SCHEDULED - timedelta(minutes=15), **prior_changes)
            for changes in [{"Status": "PENDING"}, {"Status": "RUNNING"},
                            {"ValidationStatus": "VALIDATING"}, {"ValidationStatus": None},
                            {"DeletionStatus": "DELETING"}, {"DeletionStatus": None}]:
                with self.subTest(prior=prior_changes, current=changes):
                    errors = self.health({RESOURCE: [prior, self.job(**changes)]},
                                         now=SCHEDULED + timedelta(minutes=30))
                    self.assertEqual(len(errors), 1)
                    self.assertIn("failed", errors[0])

    def test_initial_untried_wave_gets_only_two_hours_of_grace(self):
        stage = MonthlyRestoreTests().stage()
        for now in [SCHEDULED - timedelta(hours=1), SCHEDULED + timedelta(hours=2, microseconds=-1)]:
            self.assertEqual(module.canary_restore_health(stage, self.config, now), [])
        errors = module.canary_restore_health(stage, self.config, SCHEDULED + timedelta(hours=2))
        self.assertEqual(len(errors), 1)
        self.assertIn("missing", errors[0])

    def test_initial_nonterminal_or_incomplete_job_alarms_at_the_two_hour_boundary(self):
        for changes in [{"Status": "PENDING"}, {"Status": "RUNNING"}, {"Status": None},
                        {"ValidationStatus": "VALIDATING"}, {"ValidationStatus": None},
                        {"DeletionStatus": "DELETING"}, {"DeletionStatus": None}]:
            with self.subTest(changes=changes):
                jobs = {RESOURCE: [self.job(**changes)]}
                self.assertEqual(self.health(jobs, now=SCHEDULED + timedelta(hours=2, microseconds=-1)), [])
                self.assertIn("incomplete after 2h", self.health(jobs, now=SCHEDULED + timedelta(hours=2))[0])

    def test_stale_success_cannot_satisfy_the_current_wave(self):
        stale = self.job(CreationDate=SCHEDULED - timedelta(microseconds=1))
        self.assertIn("missing", self.health({RESOURCE: [stale]})[0])

    def test_latest_current_job_controls_success_not_any_earlier_success(self):
        success = self.job(CreationDate=SCHEDULED)
        pending = self.job(Status="RUNNING", CreationDate=SCHEDULED + timedelta(minutes=1))
        for jobs in [[success, pending], [pending, success]]:
            self.assertIn("incomplete", self.health({RESOURCE: jobs})[0])
        failed = self.job(Status="FAILED", CreationDate=SCHEDULED)
        self.assertEqual(self.health({RESOURCE: [self.job(), failed]}), [])

    def test_failure_at_retry_start_alarms_immediately_even_inside_grace(self):
        for changes in [{"Status": "FAILED"}, {"Status": "ABORTED"},
                        {"ValidationStatus": "FAILED"}, {"ValidationStatus": "TIMED_OUT"},
                        {"DeletionStatus": "FAILED"}]:
            with self.subTest(changes=changes):
                failed = self.job(CreationDate=SCHEDULED, **changes)
                self.assertIn("failed", self.health({RESOURCE: [failed]}, now=SCHEDULED)[0])

    def test_unrelated_plan_role_type_or_optional_source_field_cannot_supply_coverage(self):
        for changes in [{"IamRoleArn": "other-role"}, {"ResourceType": "EC2"},
                        {"CreatedBy": {"RestoreTestingPlanArn": "plan-arn"}}, {"CreatedBy": {}}]:
            with self.subTest(changes=changes):
                self.assertIn("missing", self.health({RESOURCE: [self.job(**changes)]})[0])
        other = "other-source"
        config = dict(self.config, volumes=[RESOURCE, other])
        errors = self.health({RESOURCE: [self.job(SourceResourceArn=other)]}, config=config)
        self.assertEqual(len(errors), 1)
        self.assertIn(other, errors[0])

    def test_history_is_paginated_per_source_without_backup_age_or_new_sdk_fields(self):
        prior = self.job(Status="FAILED", CreationDate=SCHEDULED - timedelta(days=60))
        paginator = SimpleNamespace(paginate=Mock(return_value=[{"RestoreJobs": []}, {"RestoreJobs": [prior]}]))
        stage = SimpleNamespace(get_paginator=Mock(return_value=paginator))
        self.assertIn("failed", module.canary_restore_health(stage, self.config, SCHEDULED)[0])
        stage.get_paginator.assert_called_once_with("list_restore_jobs_by_protected_resource")
        paginator.paginate.assert_called_once_with(ResourceArn=RESOURCE)


class HandlerTests(unittest.TestCase):
    def run_handler(self, restore_jobs, event=None, expected_error=None, listed_jobs=None, config_changes=None,
                    volume_created=None):
        # The same per-source API returns both monthly and canary jobs. Plan-wide
        # cleanup inventory is separate, as in AWS, and carries no source field.
        resource_jobs = restore_jobs + [job for jobs in (listed_jobs or {}).values() for job in jobs]
        stage = MonthlyRestoreTests().stage({RESOURCE: resource_jobs})
        resource_paginator = stage.get_paginator.return_value
        restore_paginator = SimpleNamespace(paginate=Mock(side_effect=lambda **kwargs: [{
            "RestoreJobs": (listed_jobs or {}).get(kwargs["ByRestoreTestingPlanArn"], [])}]))
        stage.get_paginator.side_effect = lambda operation: (
            resource_paginator if operation == "list_restore_jobs_by_protected_resource"
            else restore_paginator
        )
        clients = {service: Mock() for service in ["backup", "sts", "sns", "cloudwatch", "ec2"]}
        clients["ec2"].get_paginator.return_value.paginate.return_value = [{"Volumes": [
            {"VolumeId": "vol-example", "CreateTime": volume_created or SCHEDULED - timedelta(days=60)}]}]
        clients["sts"].assume_role.return_value = {"Credentials": {
            "AccessKeyId": "mock", "SecretAccessKey": "mock", "SessionToken": "mock"}}
        stage_session = Mock()
        stage_session.client.side_effect = lambda service, **kwargs: stage if service == "backup" else Mock()
        config = dict(CONFIG, primary_region="us-west-1", observer_role="observer-role",
                      stage_account="222222222222", topic="topic")
        config.update(config_changes or {})
        with patch.dict(module.os.environ, {"CONFIG": json.dumps(config)}), \
                patch.object(module.boto3, "client", side_effect=lambda service, **kwargs: clients[service]), \
                patch.object(module.boto3, "Session", return_value=stage_session), \
                patch.object(module, "reconcile", return_value=([], [])), \
                patch.object(module, "datetime", SimpleNamespace(
                    now=lambda tz: SCHEDULED + timedelta(hours=3), strptime=datetime.strptime)):
            if expected_error:
                with self.assertRaisesRegex(RuntimeError, expected_error):
                    module.handler(event or {}, None)
            else:
                self.assertEqual(module.handler(event or {}, None), {"missing": 0, "errors": 0})
        clients.update(stage_session=stage_session, restore_paginator=restore_paginator)
        return clients

    def test_hourly_invocation_alarms_for_missing_test_without_restore_event(self):
        clients = self.run_handler([], expected_error="monthly restore test missing")
        metrics = clients["cloudwatch"].put_metric_data.call_args.kwargs["MetricData"]
        self.assertEqual({metric["MetricName"]: metric["Value"] for metric in metrics},
                         {"MissingRecoveryCopies": 0, "UnhealthyRestoreTests": 1, "RecoveryControllerErrors": 1})

    def test_hourly_invocation_accepts_new_volume_without_monthly_test(self):
        clients = self.run_handler([], volume_created=SCHEDULED + timedelta(hours=1))
        metrics = clients["cloudwatch"].put_metric_data.call_args.kwargs["MetricData"]
        self.assertTrue(all(metric["Value"] == 0 for metric in metrics))
        clients["ec2"].get_paginator.assert_called_with("describe_volumes")

    def test_hourly_invocation_alarms_for_failed_job_even_when_event_was_lost(self):
        self.run_handler([dict(RESTORE_JOB, Status="FAILED")], expected_error="monthly restore test FAILED")

    def test_healthy_test_reports_zero_and_failed_restore_event_still_notifies(self):
        clients = self.run_handler([RESTORE_JOB], event={
            "detail-type": "Restore Job State Change", "detail": {"status": "FAILED"}})
        clients["sns"].publish.assert_called_once()
        metrics = clients["cloudwatch"].put_metric_data.call_args.kwargs["MetricData"]
        self.assertTrue(all(metric["Value"] == 0 for metric in metrics))

    def test_recovery_clients_use_explicit_destination_region(self):
        clients = self.run_handler([RESTORE_JOB])
        self.assertEqual(clients["stage_session"].client.call_args_list[0].kwargs, {"region_name": "us-east-1"})
        self.assertEqual(clients["stage_session"].client.call_args_list[1].kwargs, {"region_name": "us-east-1"})

    def test_initial_canary_is_validated_and_cleaned_without_satisfying_monthly_health(self):
        canary = dict(RESTORE_JOB, RestoreJobId="initial-job", DeletionStatus="DELETING",
                      CreatedBy={"RestoreTestingPlanArn": "canary-arn"})
        with patch.object(module, "validate_restore") as validate, \
                patch.object(module, "cleanup_expired_test") as cleanup:
            clients = self.run_handler([], listed_jobs={"canary-arn": [canary]},
                                       config_changes={"canary_plan": "initial", "canary_plan_arn": "canary-arn",
                                                       "canary_start_at": "2026-09-08T12:00:00Z"},
                                       expected_error="monthly restore test missing")
            validate.assert_called_once()
            cleanup.assert_called_once()
            self.assertEqual(validate.call_args.args[2], "initial-job")
        queried = {call.kwargs["ByRestoreTestingPlanArn"] for call in clients["restore_paginator"].paginate.call_args_list}
        self.assertEqual(queried, {"plan-arn", "canary-arn"})

    def test_failed_initial_canary_is_detected_without_event(self):
        canary = dict(RESTORE_JOB, RestoreJobId="initial-job", Status="FAILED",
                      CreatedBy={"RestoreTestingPlanArn": "canary-arn"})
        with patch.object(module, "cleanup_expired_test"):
            self.run_handler([RESTORE_JOB], listed_jobs={"canary-arn": [canary]},
                             config_changes={"canary_plan_arn": "canary-arn",
                                             "canary_start_at": "2026-09-08T12:00:00Z"},
                             expected_error="Initial restore canary failed")

    def test_prior_failed_canary_does_not_poison_retry_but_still_gets_cleanup(self):
        prior = dict(RESTORE_JOB, RestoreJobId="prior-failed", Status="FAILED",
                     CreationDate=SCHEDULED - timedelta(minutes=15),
                     CreatedBy={"RestoreTestingPlanArn": "canary-arn"})
        retry = dict(RESTORE_JOB, RestoreJobId="successful-retry",
                     CreatedBy={"RestoreTestingPlanArn": "canary-arn"})
        with patch.object(module, "cleanup_expired_test") as cleanup:
            self.run_handler([RESTORE_JOB], listed_jobs={"canary-arn": [prior, retry]},
                             config_changes={"canary_plan_arn": "canary-arn",
                                             "canary_start_at": "2026-09-08T12:00:00Z"})
            self.assertEqual([call.args[1]["RestoreJobId"] for call in cleanup.call_args_list],
                             ["prior-failed", "successful-retry"])

    def test_canary_failure_at_retry_start_is_not_ignored(self):
        retry = dict(RESTORE_JOB, RestoreJobId="failed-retry", Status="FAILED",
                     CreationDate=SCHEDULED, CreatedBy={"RestoreTestingPlanArn": "canary-arn"})
        with patch.object(module, "cleanup_expired_test"):
            self.run_handler([RESTORE_JOB], listed_jobs={"canary-arn": [retry]},
                             config_changes={"canary_plan_arn": "canary-arn",
                                             "canary_start_at": "2026-09-08T12:00:00Z"},
                             expected_error="Initial restore canary failed")

    def test_prior_canary_still_receives_validation(self):
        prior = dict(RESTORE_JOB, RestoreJobId="prior-unvalidated", ValidationStatus="VALIDATING",
                     DeletionStatus="DELETING", CreationDate=SCHEDULED - timedelta(minutes=15),
                     CreatedBy={"RestoreTestingPlanArn": "canary-arn"})
        with patch.object(module, "validate_restore") as validate, \
                patch.object(module, "cleanup_expired_test") as cleanup:
            self.run_handler([RESTORE_JOB], listed_jobs={"canary-arn": [prior]},
                             config_changes={"canary_plan_arn": "canary-arn",
                                             "canary_start_at": "2026-09-08T12:00:00Z"},
                             expected_error="Initial restore canary missing")
            self.assertEqual(validate.call_args.args[2], "prior-unvalidated")
            self.assertEqual(cleanup.call_args.args[1]["RestoreJobId"], "prior-unvalidated")

    def test_future_retry_keeps_failed_health_metric_and_old_job_cleanup(self):
        prior = dict(RESTORE_JOB, RestoreJobId="prior-failed", Status="FAILED",
                     CreationDate=SCHEDULED - timedelta(minutes=15),
                     CreatedBy={"RestoreTestingPlanArn": "canary-arn"})
        with patch.object(module, "cleanup_expired_test") as cleanup:
            clients = self.run_handler([RESTORE_JOB], listed_jobs={"canary-arn": [prior]},
                                       config_changes={"canary_plan_arn": "canary-arn",
                                                       "canary_start_at": "2026-09-08T16:00:00Z"},
                                       expected_error="Initial restore canary failed")
            self.assertEqual(cleanup.call_args.args[1]["RestoreJobId"], "prior-failed")
        metrics = clients["cloudwatch"].put_metric_data.call_args.kwargs["MetricData"]
        self.assertEqual({metric["MetricName"]: metric["Value"] for metric in metrics},
                         {"MissingRecoveryCopies": 0, "UnhealthyRestoreTests": 1, "RecoveryControllerErrors": 1})

    def test_accepted_canary_still_cleans_old_jobs_and_requires_monthly_health(self):
        prior = dict(RESTORE_JOB, RestoreJobId="prior-unvalidated", ValidationStatus="VALIDATING",
                     DeletionStatus="DELETING", CreationDate=SCHEDULED - timedelta(minutes=15),
                     CreatedBy={"RestoreTestingPlanArn": "canary-arn"})
        with patch.object(module, "validate_restore") as validate, \
                patch.object(module, "cleanup_expired_test") as cleanup:
            clients = self.run_handler([], listed_jobs={"canary-arn": [prior]},
                                       config_changes={"canary_plan_arn": "canary-arn", "canary_accepted": True,
                                                       "canary_start_at": "2026-09-08T12:00:00Z"},
                                       expected_error="monthly restore test missing")
            self.assertEqual(validate.call_args.args[2], "prior-unvalidated")
            self.assertEqual(cleanup.call_args.args[1]["RestoreJobId"], "prior-unvalidated")
        metrics = clients["cloudwatch"].put_metric_data.call_args.kwargs["MetricData"]
        self.assertEqual({metric["MetricName"]: metric["Value"] for metric in metrics},
                         {"MissingRecoveryCopies": 0, "UnhealthyRestoreTests": 1, "RecoveryControllerErrors": 1})

    def test_canary_start_is_strict_and_missing_configuration_fails_closed(self):
        self.assertIsNone(module.canary_test_start({"canary_plan_arn": None}))
        for value in (None, "", "2026-09-08T12:00:01Z", "2026-09-08T12:00:00+00:00",
                      "2026-02-30T12:00:00Z", "2026-9-8T12:00:00Z"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "Invalid initial restore"):
                module.canary_test_start({"canary_plan_arn": "canary-arn", "canary_start_at": value})
        self.assertEqual(module.canary_test_start({"canary_plan_arn": "canary-arn",
                                                  "canary_start_at": "2026-09-08T12:00:00Z"}), SCHEDULED)

    def test_null_canary_does_not_query_any_extra_plan(self):
        clients = self.run_handler([RESTORE_JOB], config_changes={"canary_plan_arn": None})
        calls = clients["restore_paginator"].paginate.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs["ByRestoreTestingPlanArn"], "plan-arn")

    def test_unrelated_plan_or_role_jobs_cannot_trigger_validation_or_cleanup(self):
        for changes in [{"CreatedBy": {"RestoreTestingPlanArn": "unrelated"}}, {"IamRoleArn": "other-role"}]:
            with patch.object(module, "validate_restore") as validate, \
                    patch.object(module, "cleanup_expired_test") as cleanup:
                self.run_handler([RESTORE_JOB], listed_jobs={"plan-arn": [dict(RESTORE_JOB, **changes)]})
                validate.assert_not_called()
                cleanup.assert_not_called()


if __name__ == "__main__":
    unittest.main()

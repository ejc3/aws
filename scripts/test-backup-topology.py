"""SDK-free regression checks for the reviewed fleet backup Terraform boundaries.

Run: python3 -S -B scripts/test-backup-topology.py
These check the checked-in topology, not live IAM authorization, delivery, or restore
success. They complement terraform validate and the controller's fake-client tests.
"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
SECURITY = (ROOT / "backup-security.tf").read_text()
CANARY = (ROOT / "backup-restore-canary.tf").read_text()
DEV = (ROOT / "dev-instance-common.tf").read_text()
JUMPBOX = (ROOT / "jumpbox.tf").read_text()
WORKFLOW = (ROOT / ".github/workflows/lambda-tests.yml").read_text()
ALL_TF = "\n".join(path.read_text() for path in sorted(ROOT.glob("*.tf")))


def block(source, kind, *labels):
    """Read one fmt-style top-level block, without interpreting Terraform or IAM.

    Nested blocks close at nonzero indentation; root blocks close at column zero.
    Fail on duplicate/missing blocks instead of silently selecting the wrong one.
    """
    declaration = r"[ \t]+".join([re.escape(kind)] + [re.escape('"' + label + '"') for label in labels])
    matches = re.findall(r"^" + declaration + r"[ \t]*\{\n(.*?)^\}", source, re.M | re.S)
    if len(matches) != 1:
        raise AssertionError("Expected one block: " + " ".join((kind,) + labels))
    return matches[0]


def resource(kind, name, source=SECURITY):
    return block(source, "resource", kind, name)


def compact_list(source, name):
    match = re.search(r"\b" + re.escape(name) + r"\s*=\s*compact\(\[(.*?)\]\)", source, re.S)
    if not match:
        raise AssertionError("Missing compact list: " + name)
    return match[1]


def statement(source, sid):
    matches = re.findall(r'\bSid\s*=\s*"' + re.escape(sid) + r'"(.*?)\n      \},', source, re.S)
    if len(matches) != 1:
        raise AssertionError("Expected one IAM statement: " + sid)
    return matches[0]


class BackupTopologyTests(unittest.TestCase):
    def test_recovery_account_and_region_are_explicit(self):
        self.assertRegex(SECURITY, r'backup_recovery_region\s*=\s*"us-east-1"')
        provider = block(SECURITY, "provider", "aws")
        self.assertRegex(provider, r'alias\s*=\s*"staging_dr"')
        self.assertRegex(provider, r"region\s*=\s*local\.backup_recovery_region")
        self.assertIn("aws_organizations_account.dev_staging[0].id", provider)
        self.assertIn(":role/OrganizationAccountAccessRole", provider)

    def test_new_lag_is_distinct_aws_owned_and_protected(self):
        old = resource("aws_backup_logically_air_gapped_vault", "staging_recovery")
        final = resource("aws_backup_logically_air_gapped_vault", "staging_recovery_dr")
        self.assertRegex(old, r"provider\s*=\s*aws\.staging\b")
        self.assertRegex(final, r"provider\s*=\s*aws\.staging_dr\b")
        for vault in (old, final):
            self.assertRegex(vault, r"min_retention_days\s*=\s*7\b")
            self.assertRegex(vault, r"max_retention_days\s*=\s*366\b")
            self.assertRegex(vault, r"prevent_destroy\s*=\s*true")
            self.assertNotIn("kms_key_arn", vault)

    def test_pending_vault_and_plans_never_expire_uncopied_data(self):
        vault = resource("aws_backup_vault", "processing")
        self.assertIn('"ejc3-backup-processing"', vault)
        self.assertRegex(vault, r"prevent_destroy\s*=\s*true")
        plan = resource("aws_backup_plan", "processing")
        self.assertIn('dev = "cron(0 6 * * ? *)"', plan)
        self.assertIn('jumpbox = "cron(0 5 * * ? *)"', plan)
        self.assertRegex(plan, r"target_vault_name\s*=\s*aws_backup_vault\.processing\.name")
        self.assertNotRegex(plan, r"\b(lifecycle|delete_after|cold_storage_after|copy_action)\s*[={]")
        self.assertIn('BackupPipeline = "fleet-processing-v2"', plan)

    def test_temporary_vaults_cannot_gain_a_retention_lock(self):
        locks = re.findall(r'^resource "aws_backup_vault_lock_configuration" "[^"]+"\s*\{\n(.*?)^\}', ALL_TF, re.M | re.S)
        for lock in locks:
            self.assertNotIn("aws_backup_vault.processing", lock)
            self.assertNotIn("aws_backup_vault.ejc3_backup_dr_cmk", lock)

    def test_cutover_routes_both_existing_selections_through_one_gate(self):
        # true is an intentional later rollout, not a permanent test failure.
        self.assertRegex(SECURITY, r"backup_recovery_cutover_enabled\s*=\s*(?:false|true)\b")
        for source, name, key, legacy in (
            (DEV, "dev_servers", "dev", "aws_backup_plan.dev_servers.id"),
            (JUMPBOX, "jumpbox", "jumpbox", "aws_backup_plan.jumpbox[0].id"),
        ):
            selection = resource("aws_backup_selection", name, source)
            expected = ('plan_id = local.backup_recovery_cutover_enabled ? '
                        'aws_backup_plan.processing["' + key + '"]' + '.id : ' + legacy)
            self.assertIn(re.sub(r"\s+", " ", expected), re.sub(r"\s+", " ", selection))
            self.assertNotIn('resources = ["*"]', re.sub(r"\s+", " ", selection))
            self.assertIn("!local.backup_recovery_cutover_enabled || local.backup_recovery_cleanup_enabled", selection)
            self.assertRegex(selection, r"create_before_destroy\s*=\s*true")

    def test_controller_and_selections_protect_the_same_five_volumes(self):
        protected = compact_list(SECURITY, "backup_protected_volume_arns")
        dev = compact_list(resource("aws_backup_selection", "dev_servers", DEV), "resources")
        jumpbox = compact_list(resource("aws_backup_selection", "jumpbox", JUMPBOX), "resources")
        for name in ("arm_persistent_volume_arn", "x86_persistent_volume_arn", "nextjs_root_volume_arn"):
            self.assertIn("local." + name, protected)
            self.assertIn("local." + name, dev)
        for fragment in ("var.enable_jumpbox ? aws_ebs_volume.jumpbox_home[0].arn",
                         "var.enable_jumpbox_2 ?", "aws_instance.jumpbox_2[0].root_block_device[0].volume_id"):
            self.assertIn(fragment, protected)
            self.assertIn(fragment, jumpbox)

    def test_only_the_aws_ebs_encrypted_roots_get_a_cmk_hop(self):
        # Membership here tracks which roots are actually encrypted with the account's
        # default alias/aws/ebs key (unshareable cross-account), not a fixed headcount.
        # fcvm-metal-arm joined this list on 2026-09-19: the 2026-09-13 AZ move rebuilt its
        # root from an aws_ami_from_instance image, which landed on aws/ebs instead of
        # whatever key the volume had before.
        hop = compact_list(SECURITY, "backup_cmk_hop_volume_arns")
        for wanted in ("local.nextjs_root_volume_arn",
                       "aws_instance.jumpbox_2[0].root_block_device[0].volume_id",
                       "local.arm_persistent_volume_arn"):
            self.assertIn(wanted, hop)
        for unwanted in ("x86_persistent_volume_arn", "jumpbox_home"):
            self.assertNotIn(unwanted, hop)
        checkpoint = resource("aws_backup_vault", "ejc3_backup_dr_cmk")
        self.assertRegex(checkpoint, r"provider\s*=\s*aws\.dr\b")
        self.assertRegex(checkpoint, r"kms_key_arn\s*=\s*aws_kms_key\.backup_dr\.arn")
        self.assertRegex(resource("aws_kms_key", "backup_dr"), r"prevent_destroy\s*=\s*true")

    def test_cleanup_authority_is_gated_and_scoped_to_two_vault_policies(self):
        for policy_name, vault in (("processing_cleanup", "processing"),
                                   ("checkpoint_cleanup", "ejc3_backup_dr_cmk")):
            policy = resource("aws_backup_vault_policy", policy_name)
            self.assertRegex(policy, r"count\s*=\s*local\.backup_recovery_cleanup_enabled\s*\?\s*1\s*:\s*0")
            self.assertRegex(policy, r"backup_vault_name\s*=\s*aws_backup_vault\." + vault + r"\.name")
            self.assertIn("AWS = aws_iam_role.backup_recovery.arn", policy)
            self.assertIn('Action = "backup:DeleteRecoveryPoint"', re.sub(r"\s+", " ", policy))
            self.assertNotIn('Action = "backup:*"', re.sub(r"\s+", " ", policy))
        processing = resource("aws_backup_vault_policy", "processing_cleanup")
        self.assertIn('"aws:ResourceTag/BackupPipeline" = "fleet-processing-v2"', processing)
        final = resource("aws_backup_vault_policy", "staging_recovery_dr")
        self.assertNotIn("DeleteRecoveryPoint", final)

    def test_controller_has_no_backup_identity_deletion_or_retention_bypass(self):
        policy = resource("aws_iam_role_policy", "backup_recovery")
        self.assertNotIn("backup:DeleteRecoveryPoint", policy)
        denied = statement(policy, "NoAlternativeDeletionOrRetentionBypass")
        self.assertRegex(denied, r'Effect\s*=\s*"Deny"')
        self.assertRegex(denied, r'Resource\s*=\s*"\*"')
        self.assertNotIn("Condition", denied)
        for action in ("backup:UpdateRecoveryPointLifecycle", "backup:DisassociateRecoveryPoint",
                       "backup:DisassociateRecoveryPointFromParent", "backup:TagResource", "backup:UntagResource",
                       "backup:PutBackupVaultAccessPolicy", "backup:DeleteBackupVaultAccessPolicy",
                       "backup:PutBackupVaultLockConfiguration", "backup:DeleteBackupVaultLockConfiguration"):
            self.assertIn('"' + action + '"', denied)
        self.assertNotIn("ec2:DeleteSnapshot", denied)
        self.assertIn('"iam:PassedToService" = "backup.amazonaws.com"', policy)
        self.assertIn("Resource = local.backup_service_role_arn", re.sub(r"\s+", " ", policy))

    def test_direct_and_missing_context_snapshot_deletion_remain_explicitly_denied(self):
        policy = resource("aws_iam_role_policy", "backup_recovery")
        denied = statement(policy, "NoSnapshotDeletionOutsideBackup")
        self.assertRegex(denied, r'Effect\s*=\s*"Deny"')
        self.assertRegex(denied, r'Action\s*=\s*"ec2:DeleteSnapshot"')
        self.assertRegex(denied, r'Resource\s*=\s*"\*"')
        self.assertIn('"ForAllValues:StringNotEquals" = { "aws:CalledVia" = "backup.amazonaws.com" }', denied)
        # Missing/empty CalledVia must match the deny. Null:false would exempt
        # missing context; ForAnyValue on this deny would do the same.
        self.assertNotRegex(denied, r"\bNull\s*=")
        self.assertNotIn("ForAnyValue", denied)

    def test_forwarded_pipeline_snapshot_deletion_is_gated_and_tag_scoped(self):
        policy = resource("aws_iam_role_policy", "backup_recovery")
        grant = statement(policy, "DeleteProcessingSnapshotsOnlyThroughBackup")
        self.assertRegex(policy, r'local\.backup_recovery_cleanup_enabled\s*\?\s*\[\s*\{\s*'
                         r'(?:#[^\n]*\n\s*)*Sid\s*=\s*"DeleteProcessingSnapshotsOnlyThroughBackup"')
        self.assertRegex(grant, r'Effect\s*=\s*"Allow"')
        self.assertRegex(grant, r'Action\s*=\s*"ec2:DeleteSnapshot"')
        self.assertRegex(grant, r'Resource\s*=\s*local\.backup_snapshot_arns\b')
        self.assertIn('"ForAnyValue:StringEquals" = { "aws:CalledVia" = "backup.amazonaws.com" }', grant)
        self.assertRegex(grant, r'"ec2:Owner"\s*=\s*data\.aws_caller_identity\.current\.account_id')
        self.assertRegex(grant, r'"aws:ResourceTag/BackupPipeline"\s*=\s*"fleet-processing-v2"')
        self.assertIn('Null = { "aws:ResourceTag/aws:backup:source-resource" = "false" }', grant)
        snapshot_arns = re.search(r'backup_snapshot_arns\s*=\s*\[(.*?)\]', SECURITY, re.S).group(1)
        self.assertEqual(re.findall(r'"([^"]+)"', snapshot_arns), [
            "arn:aws:ec2:${var.aws_region}::snapshot/*", "arn:aws:ec2:us-east-1::snapshot/*"])

    def test_untagged_initial_checkpoint_exception_is_exact_and_retires_at_cutover(self):
        policy = resource("aws_iam_role_policy", "backup_recovery")
        grant = statement(policy, "DeleteExactSupersededBootstrapCheckpointsThroughBackup")
        self.assertIn("local.backup_recovery_cleanup_enabled && !local.backup_recovery_cutover_enabled ? [", policy)
        self.assertRegex(grant, r'Effect\s*=\s*"Allow"')
        self.assertRegex(grant, r'Action\s*=\s*"ec2:DeleteSnapshot"')
        snapshots = re.search(r'\bResource\s*=\s*\[(.*?)\]', grant, re.S).group(1)
        self.assertEqual(set(re.findall(r'"([^"]+)"', snapshots)), {
            "arn:aws:ec2:us-east-1::snapshot/snap-0a7f6a0dde2e248b6",
            "arn:aws:ec2:us-east-1::snapshot/snap-017274f57046154b9"})
        self.assertNotIn("*", snapshots)
        self.assertIn('"ForAnyValue:StringEquals" = { "aws:CalledVia" = "backup.amazonaws.com" }', grant)
        self.assertRegex(grant, r'"ec2:Owner"\s*=\s*data\.aws_caller_identity\.current\.account_id')
        self.assertRegex(grant, r'Null\s*=\s*\{ "aws:ResourceTag/aws:backup:source-resource" = "false" \}')
        # One unconditional direct-call deny and only these two conditional allows.
        self.assertEqual(policy.count('"ec2:DeleteSnapshot"'), 3)

    def test_retry_provenance_read_does_not_grant_retagging(self):
        policy = resource("aws_iam_role_policy", "backup_recovery")
        grant = statement(policy, "ReadProcessingRetryProvenance")
        self.assertRegex(grant, r'Effect\s*=\s*"Allow"')
        self.assertRegex(grant, r'Action\s*=\s*"backup:ListTags"')
        self.assertRegex(grant, r'Resource\s*=\s*"arn:aws:ec2:\$\{var\.aws_region\}::snapshot/\*"')
        for forbidden in ("ec2:CreateTags", "ec2:DeleteTags", "iam:CreateServiceLinkedRole"):
            self.assertNotIn(forbidden, policy)

    def test_native_ebs_tag_read_requires_backup_forwarding_and_source_region(self):
        policy = resource("aws_iam_role_policy", "backup_recovery")
        grant = statement(policy, "ReadProcessingTagsOnlyThroughBackup")
        self.assertRegex(grant, r'Effect\s*=\s*"Allow"')
        self.assertRegex(grant, r'Action\s*=\s*"ec2:DescribeTags"')
        self.assertRegex(grant, r'Resource\s*=\s*"\*"')
        self.assertIn('"ForAnyValue:StringEquals" = { "aws:CalledVia" = "backup.amazonaws.com" }', grant)
        self.assertRegex(grant, r'StringEquals\s*=\s*\{ "aws:RequestedRegion" = var\.aws_region \}')
        self.assertEqual(policy.count('"ec2:DescribeTags"'), 1)
        self.assertNotIn('"ec2:DescribeSnapshots"', policy)

    def test_controller_uses_new_final_vault_and_optional_canary(self):
        function = resource("aws_lambda_function", "backup_recovery")
        for field, value in {
            "primary_region": "var.aws_region", "stage_region": "local.backup_recovery_region",
            "cleanup_enabled": "local.backup_recovery_cleanup_enabled",
            "cmk_hop_volumes": "local.backup_cmk_hop_volume_arns",
            "processing_vault": "aws_backup_vault.processing.name",
            "stage_vault_arn": "aws_backup_logically_air_gapped_vault.staging_recovery_dr.arn",
            "restore_plan_arn": "aws_backup_restore_testing_plan.fleet_dr.arn",
            "canary_start_at": "local.backup_initial_restore_at",
            "canary_accepted": "local.backup_recovery_cutover_enabled",
            "volumes": "local.backup_protected_volume_arns",
        }.items():
            self.assertRegex(function, r"\b" + field + r"\s*=\s*" + re.escape(value))
        self.assertIn("try(aws_backup_restore_testing_plan.initial[0].arn, null)", function)
        self.assertIn("try(aws_backup_restore_testing_plan.initial[0].name, null)", function)
        self.assertNotRegex(function, r"aws_backup_logically_air_gapped_vault\.staging_recovery\.")

    def test_restore_resources_and_metadata_are_in_recovery_region(self):
        for source, plan_name, selection_name in ((SECURITY, "fleet_dr", "fleet_ebs_dr"),
                                                 (CANARY, "initial", "initial_ebs")):
            plan = resource("aws_backup_restore_testing_plan", plan_name, source)
            selection = resource("aws_backup_restore_testing_selection", selection_name, source)
            self.assertRegex(plan, r"provider\s*=\s*aws\.staging_dr\b")
            self.assertRegex(selection, r"provider\s*=\s*aws\.staging_dr\b")
            self.assertIn("aws_backup_logically_air_gapped_vault.staging_recovery_dr.arn", plan)
            self.assertRegex(selection, r'protected_resource_type\s*=\s*"EBS"')
            self.assertRegex(selection, r'protected_resource_arns\s*=\s*\["\*"\]')
            self.assertRegex(selection, r"validation_window_hours\s*=\s*2\b")
            self.assertIn("data.aws_availability_zones.backup_restore_staging_dr.names[0]", selection)
            self.assertRegex(selection, r'volumeType\s*=\s*"gp3"')
            self.assertNotRegex(selection, re.compile(r"^\s*encrypted\s*=", re.M))

    def test_restore_selections_override_inherited_source_encryption_key(self):
        # LAG copies can retain an original aws/ebs source key in restore metadata.
        # Both paths must select the recovery account/region's key explicitly.
        destination_key = ('"arn:aws:kms:${local.backup_recovery_region}:'
                           '${data.aws_caller_identity.staging.account_id}:alias/aws/ebs"')
        for source, selection_name in ((SECURITY, "fleet_ebs_dr"), (CANARY, "initial_ebs")):
            with self.subTest(selection=selection_name):
                selection = resource("aws_backup_restore_testing_selection", selection_name, source)
                metadata = re.search(r"restore_metadata_overrides\s*=\s*\{(.*?)\n  \}", selection, re.S)
                self.assertIsNotNone(metadata)
                self.assertRegex(metadata.group(1), r"\bkmsKeyId\s*=\s*" + re.escape(destination_key))

    def test_restore_role_cannot_boot_attach_or_use_source_region(self):
        role = resource("aws_iam_role_policy", "backup_restore_test")
        observer = resource("aws_iam_role_policy", "backup_recovery_observer")
        self.assertRegex(role, r'Effect\s*=\s*"Deny"\s+Action\s*=\s*\["ec2:RunInstances",\s*"ec2:AttachVolume",\s*"iam:PassRole"\]')
        self.assertIn('"ec2:Encrypted" = "true"', role)
        self.assertIn('"ec2:VolumeType" = "gp3"', role)
        for policy in (role, observer):
            self.assertNotIn("var.aws_region", policy)
            self.assertIn('"aws:RequestedRegion" = local.backup_recovery_region', policy)
            self.assertIn('"aws:ResourceTag/awsbackup-restore-test"', policy)
        self.assertIn("aws_backup_restore_testing_plan.initial[*].arn", observer)
        for forbidden in ("backup:StartRestoreJob", "backup:StartCopyJob", "ec2:AttachVolume"):
            self.assertNotIn(forbidden, observer)

    def test_restore_reencrypts_only_from_the_exact_managed_lag_key(self):
        # Provider 5.100's standard-vault data source explicitly rejects LAGs.
        self.assertNotIn('data "aws_backup_vault" "staging_recovery_dr"', SECURITY)
        source_key = "arn:aws:kms:us-east-1:792761027311:key/74da7bec-f50b-45aa-9602-2666a665a785"
        self.assertRegex(SECURITY, r'backup_recovery_source_key_arn\s*=\s*"' + re.escape(source_key) + '"')
        vault = resource("aws_backup_logically_air_gapped_vault", "staging_recovery_dr")
        self.assertRegex(vault, r"prevent_destroy\s*=\s*true")
        role = resource("aws_iam_role_policy", "backup_restore_test")
        grant = re.search(r'Sid\s*=\s*"ReEncryptFromExactRecoveryVaultKey"(.*?)\n      \},', role, re.S)
        self.assertIsNotNone(grant)
        statement = grant.group(1)
        self.assertRegex(statement, r'Effect\s*=\s*"Allow"')
        self.assertRegex(statement, r'Action\s*=\s*"kms:ReEncryptFrom"\s*\n')
        self.assertRegex(statement, r"Resource\s*=\s*local\.backup_recovery_source_key_arn\s*\n")
        self.assertIn('StringEquals = { "kms:ViaService" = "ec2.${local.backup_recovery_region}.amazonaws.com" }', statement)
        self.assertNotIn("*", statement)
        self.assertEqual(role.count("local.backup_recovery_source_key_arn"), 1)

    def test_canary_is_explicit_minute_date_year_and_null_gated(self):
        self.assertRegex(CANARY, r'backup_initial_restore_at\s*=\s*(?:null|"[^"]+")')
        self.assertIn('formatdate("m h D M ? YYYY", local.backup_initial_restore_at)', CANARY)
        for kind, name in (("aws_backup_restore_testing_plan", "initial"),
                           ("aws_backup_restore_testing_selection", "initial_ebs")):
            self.assertRegex(resource(kind, name, CANARY), r"count\s*=\s*local\.backup_initial_restore_at\s*==\s*null\s*\?\s*0\s*:\s*1")
        plan = resource("aws_backup_restore_testing_plan", "initial", CANARY)
        self.assertIn("precondition", plan)
        self.assertIn(":00Z$", plan)
        self.assertNotRegex(plan, r"\b(?:timestamp|plantimestamp)\(")
        self.assertRegex(plan, r'schedule_expression_timezone\s*=\s*"Etc/UTC"')

    def test_oneoff_capture_exercises_processing_without_moving_legacy_selections(self):
        self.assertRegex(CANARY, r'backup_initial_capture_at\s*=\s*(?:null|"[^"]+")')
        self.assertIn('formatdate("m h D M ? YYYY", local.backup_initial_capture_at)', CANARY)
        plan = resource("aws_backup_plan", "initial_capture", CANARY)
        selection = resource("aws_backup_selection", "initial_capture", CANARY)
        for config in (plan, selection):
            self.assertRegex(config, r"count\s*=\s*local\.backup_initial_capture_at\s*==\s*null\s*\?\s*0\s*:\s*1")
        self.assertIn("aws_backup_vault.processing.name", plan)
        self.assertIn('BackupPipeline = "fleet-processing-v2"', plan)
        self.assertNotRegex(plan, r"\b(?:delete_after|cold_storage_after|copy_action)\b")
        self.assertRegex(plan, r"start_window\s*=\s*60\b")
        self.assertRegex(plan, r"completion_window\s*=\s*300\b")
        self.assertRegex(selection, r"resources\s*=\s*local\.backup_protected_volume_arns")
        self.assertRegex(selection, r"iam_role_arn\s*=\s*local\.backup_service_role_arn")
        self.assertNotIn("backup_recovery_cutover_enabled", selection)
        self.assertNotRegex(plan, r"\b(?:timestamp|plantimestamp)\(")

    def test_periodic_scan_and_lambda_are_bounded(self):
        rule = resource("aws_cloudwatch_event_rule", "backup_reconcile")
        function = resource("aws_lambda_function", "backup_recovery")
        self.assertIn('schedule_expression = "rate(1 hour)"', re.sub(r"\s+", " ", rule))
        self.assertRegex(function, r"reserved_concurrent_executions\s*=\s*1\b")
        self.assertRegex(function, r"timeout\s*=\s*180\b")
        self.assertRegex(function, r"memory_size\s*=\s*256\b")
        self.assertIn("aws_organizations_organization.security", function)
        self.assertIn("aws_backup_global_settings.main", function)

    def test_recovery_events_use_east1_and_exact_forwarder_trust(self):
        for kind in ("aws_cloudwatch_event_rule", "aws_cloudwatch_event_target"):
            self.assertRegex(resource(kind, "backup_events_staging_dr"), r"provider\s*=\s*aws\.staging_dr\b")
        policy = resource("aws_cloudwatch_event_bus_policy", "backup_recovery")
        self.assertIn('"aws:PrincipalArn" = aws_iam_role.backup_event_forward_staging.arn', policy)
        self.assertIn('Action = "events:PutEvents"', re.sub(r"\s+", " ", policy))
        self.assertIn('state = ["COMPLETED", "FAILED", "EXPIRED", "ABORTED", "PARTIAL"]', SECURITY)
        self.assertIn('status = ["COMPLETED", "FAILED", "ABORTED"]', SECURITY)

    def test_ebs_copy_evidence_is_local_bounded_and_keeps_full_events(self):
        for kind in ("aws_cloudwatch_log_group", "aws_cloudwatch_event_rule",
                     "aws_cloudwatch_log_resource_policy", "aws_cloudwatch_event_target"):
            self.assertRegex(resource(kind, "backup_copy_evidence"), r"provider\s*=\s*aws\.dr\b")
        group = resource("aws_cloudwatch_log_group", "backup_copy_evidence")
        self.assertRegex(group, r'retention_in_days\s*=\s*7\b')
        self.assertIn('"/aws/events/fleet-backup-copy-snapshots"', group)
        rule = resource("aws_cloudwatch_event_rule", "backup_copy_evidence")
        for field, expected in (("account", "data.aws_caller_identity.current.account_id"),
                                ("region", '"us-east-1"'), ("source", '"aws.ec2"'),
                                ("detail-type", '"EBS Snapshot Notification"')):
            self.assertRegex(rule, r"\b" + field + r"\s*=\s*\[" + re.escape(expected) + r"\]")
        self.assertIn('event = ["copySnapshot"]', rule)
        self.assertNotRegex(rule, r"\b(?:incremental|result|resources)\s*=")
        target = resource("aws_cloudwatch_event_target", "backup_copy_evidence")
        self.assertRegex(target, r"arn\s*=\s*aws_cloudwatch_log_group\.backup_copy_evidence\.arn")
        self.assertNotRegex(target, r"\b(?:role_arn|input|input_path|input_transformer)\s*[={]")
        self.assertIn("aws_cloudwatch_log_resource_policy.backup_copy_evidence", target)
        self.assertRegex(target, r"maximum_event_age_in_seconds\s*=\s*86400\b")
        self.assertRegex(target, r"maximum_retry_attempts\s*=\s*185\b")

    def test_ebs_log_policy_separates_stream_creation_from_rule_bound_writes(self):
        policy = resource("aws_cloudwatch_log_resource_policy", "backup_copy_evidence")
        creation = re.search(r'Sid\s*=\s*"AllowEventBridgeStreamCreation"(.*?)\n      \},', policy, re.S).group(1)
        writes = re.search(r'Sid\s*=\s*"AllowOnlyThisRuleToWriteEvents"(.*?)\n      \},', policy, re.S).group(1)
        self.assertRegex(creation, r'Action\s*=\s*"logs:CreateLogStream"')
        self.assertIn('"${aws_cloudwatch_log_group.backup_copy_evidence.arn}:*"', creation)
        self.assertNotIn("Condition", creation)
        self.assertRegex(writes, r'Action\s*=\s*"logs:PutLogEvents"')
        self.assertIn('"${aws_cloudwatch_log_group.backup_copy_evidence.arn}:*:*"', writes)
        self.assertIn('ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.backup_copy_evidence.arn }', writes)
        for statement in (creation, writes):
            self.assertIn('Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"]', statement)
            self.assertNotRegex(statement, r'\bResource\s*=\s*"\*"')

    def test_packaged_controller_and_test_workflow_are_credential_free(self):
        archive = block(SECURITY, "data", "archive_file", "backup_recovery")
        self.assertIn('file("${path.module}/scripts/backup-recovery.py")', archive)
        self.assertIn("source_code_hash", resource("aws_lambda_function", "backup_recovery"))
        self.assertIn("python3 -S -B scripts/test-backup-topology.py", WORKFLOW)
        self.assertIn("persist-credentials: false", WORKFLOW)
        self.assertNotIn("id-token: write", WORKFLOW)
        self.assertNotIn("configure-aws-credentials", WORKFLOW)


if __name__ == "__main__":
    unittest.main(verbosity=2)

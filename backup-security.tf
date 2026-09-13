# One long-term history in the recovery account, outside the workload region.
# Bootstrap is additive: keep the old schedules until copies AND restores are proven.
# Unencrypted EBS snapshots copy directly. Only the two aws/ebs-encrypted roots need
# a same-account cross-region re-encryption step; never replace live disks for this.
# AWS documents this two-step pattern for EBS as well as RDS:
# https://aws.amazon.com/blogs/storage/protecting-amazon-rds-db-instances-encrypted-using-kms-aws-managed-key-with-cross-account-and-cross-region-backups/

locals {
  # Permanent rollout gates, not operator options. Verify five final copies and
  # restores, then enable cleanup against disposable processing captures while the
  # legacy selections remain active. Switch selections only after cleanup succeeds.
  backup_recovery_cleanup_enabled = true
  backup_recovery_cutover_enabled = true
  backup_recovery_region          = "us-east-1"
  backup_service_role_arn         = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/AWSBackupDefaultServiceRole"
  backup_protected_volume_arns = compact([
    local.arm_persistent_volume_arn,
    local.x86_persistent_volume_arn,
    local.nextjs_root_volume_arn,
    var.enable_jumpbox ? aws_ebs_volume.jumpbox_home[0].arn : "",
    var.enable_jumpbox_2 ? "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:volume/${aws_instance.jumpbox_2[0].root_block_device[0].volume_id}" : "",
  ])
  backup_cmk_hop_volume_arns = compact([
    local.nextjs_root_volume_arn,
    var.enable_jumpbox_2 ? "arn:aws:ec2:${var.aws_region}:${data.aws_caller_identity.current.account_id}:volume/${aws_instance.jumpbox_2[0].root_block_device[0].volume_id}" : "",
  ])
  backup_snapshot_arns = [
    "arn:aws:ec2:${var.aws_region}::snapshot/*",
    "arn:aws:ec2:us-east-1::snapshot/*",
  ]

  # DescribeBackupVault readback, 2026-09-07: exact AWS-owned key of the east1
  # staging_recovery_dr vault protected by prevent_destroy below. Provider 5.100
  # exports no LAG key attribute and its aws_backup_vault data source rejects LAG
  # vaults. Keep this public pin; a deliberate vault replacement must re-verify it.
  backup_recovery_source_key_arn = "arn:aws:kms:us-east-1:792761027311:key/74da7bec-f50b-45aa-9602-2666a665a785"
}

provider "aws" {
  alias  = "staging_dr"
  region = local.backup_recovery_region
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dev_staging[0].id}:role/OrganizationAccountAccessRole"
  }
}

# Only new-generation pending points enter this vault after cutover. No automatic
# expiration: a stalled copy must accumulate storage/raise an alarm, not lose data.
# Unlike the final LAG, this temporary workspace deliberately has no Vault Lock.
resource "aws_backup_vault" "processing" {
  name = "ejc3-backup-processing"
  tags = { Managed = "terraform", Purpose = "pending-cross-account-backup-copies" }
  lifecycle { prevent_destroy = true }
}

# These plans have no selections until the cutover gate is proven. They make one
# source point per day; the controller retains day-1 points for 365d, other Sundays
# for 30d, and ordinary days for 7d in the final vault. Source retention never decides
# the destination tier, and failed copies have no source expiration deadline.
resource "aws_backup_plan" "processing" {
  for_each = { dev = "cron(0 6 * * ? *)", jumpbox = "cron(0 5 * * ? *)" }
  name     = "fleet-processing-${each.key}"
  rule {
    rule_name           = "daily_processing"
    target_vault_name   = aws_backup_vault.processing.name
    schedule            = each.value
    start_window        = 60
    completion_window   = 300
    recovery_point_tags = { BackupPipeline = "fleet-processing-v2" }
  }
  tags = { Managed = "terraform", Purpose = "pending-cross-account-backup-copies" }
}

resource "aws_kms_key" "backup_dr" {
  provider                = aws.dr
  description             = "Re-encrypt fleet backups for cross-account recovery"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "DestinationAccountCopyUse"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.staging.account_id}:root" }
        Action    = ["kms:Decrypt", "kms:ReEncryptFrom", "kms:DescribeKey"]
        Resource  = "*"
      },
      {
        Sid       = "DestinationAWSResourceGrants"
        Effect    = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.staging.account_id}:root" }
        Action    = "kms:CreateGrant"
        Resource  = "*"
        Condition = { Bool = { "kms:GrantIsForAWSResource" = "true" } }
      },
    ]
  })
  lifecycle { prevent_destroy = true }
}

resource "aws_kms_alias" "backup_dr" {
  provider      = aws.dr
  name          = "alias/fleet-backup-dr"
  target_key_id = aws_kms_key.backup_dr.key_id
}

resource "aws_backup_vault" "ejc3_backup_dr_cmk" {
  provider    = aws.dr
  name        = "ejc3-backup-dr-cmk"
  kms_key_arn = aws_kms_key.backup_dr.arn
  tags        = { Managed = "terraform", Purpose = "rolling-checkpoints-for-two-encrypted-roots" }
  lifecycle { prevent_destroy = true }
}

# Compliance retention and AWS-owned encryption prevent either recovery-point deletion
# or customer KMS key sabotage. The operator explicitly approved non-removable backups.
# aws/ebs source snapshots reach this vault only after the intermediate CMK copy.
resource "aws_backup_logically_air_gapped_vault" "staging_recovery" {
  provider           = aws.staging
  name               = "ejc3-backup-recovery"
  min_retention_days = 7
  max_retention_days = 366
  tags               = { Managed = "terraform", Purpose = "cross-account-immutable-recovery" }
  lifecycle { prevent_destroy = true }
}

# Keep the already-created empty west1 LAG above untouched. The sole long-term
# destination belongs in a different region from every protected source volume.
resource "aws_backup_logically_air_gapped_vault" "staging_recovery_dr" {
  provider           = aws.staging_dr
  name               = "ejc3-backup-recovery"
  min_retention_days = 7
  max_retention_days = 366
  tags               = { Managed = "terraform", Purpose = "sole-long-term-cross-account-recovery" }
  lifecycle { prevent_destroy = true }
}

resource "aws_backup_vault_policy" "staging_recovery_dr" {
  provider          = aws.staging_dr
  backup_vault_name = aws_backup_logically_air_gapped_vault.staging_recovery_dr.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AcceptCopiesFromMainBackupRole"
      Effect    = "Allow"
      Principal = { AWS = local.backup_service_role_arn }
      Action    = "backup:CopyIntoBackupVault"
      Resource  = aws_backup_logically_air_gapped_vault.staging_recovery_dr.arn
    }]
  })
}

resource "aws_backup_vault_policy" "staging_recovery" {
  provider          = aws.staging
  backup_vault_name = aws_backup_logically_air_gapped_vault.staging_recovery.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "AcceptCopiesFromMainBackupRole"
      Effect    = "Allow"
      Principal = { AWS = local.backup_service_role_arn }
      Action    = "backup:CopyIntoBackupVault"
      Resource  = aws_backup_logically_air_gapped_vault.staging_recovery.arn
    }]
  })
}

resource "aws_iam_role_policy" "backup_copy_keys" {
  name = "fleet-backup-copy-keys"
  role = "AWSBackupDefaultServiceRole"
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource = aws_kms_key.backup_dr.arn
      },
      {
        Effect    = "Allow"
        Action    = "kms:CreateGrant"
        Resource  = aws_kms_key.backup_dr.arn
        Condition = { Bool = { "kms:GrantIsForAWSResource" = "true" } }
      },
      {
        Effect   = "Allow"
        Action   = "backup:CopyIntoBackupVault"
        Resource = [aws_backup_vault.ejc3_backup_dr_cmk.arn, aws_backup_logically_air_gapped_vault.staging_recovery_dr.arn]
      },
    ]
  })
}

resource "aws_iam_role" "backup_recovery" {
  name = "fleet-backup-recovery"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}

# DeleteRecoveryPoint authorizes snapshot ARNs, not vault ARNs. Grant it ONLY through
# these two vault policies, never the controller identity policy. That keeps legacy
# history and the final immutable vault outside the deletion authority. The policies
# themselves remain absent until the copy/restore gate has passed.
resource "aws_backup_vault_policy" "processing_cleanup" {
  count             = local.backup_recovery_cleanup_enabled ? 1 : 0
  backup_vault_name = aws_backup_vault.processing.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CleanupVerifiedNewGenerationPoints"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.backup_recovery.arn }
      Action    = "backup:DeleteRecoveryPoint"
      Resource  = "arn:aws:ec2:${var.aws_region}::snapshot/*"
      Condition = { StringEquals = { "aws:ResourceTag/BackupPipeline" = "fleet-processing-v2" } }
    }]
  })
}

resource "aws_backup_vault_policy" "checkpoint_cleanup" {
  count             = local.backup_recovery_cleanup_enabled ? 1 : 0
  provider          = aws.dr
  backup_vault_name = aws_backup_vault.ejc3_backup_dr_cmk.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "CleanupVerifiedSupersededCheckpoints"
      Effect    = "Allow"
      Principal = { AWS = aws_iam_role.backup_recovery.arn }
      Action    = "backup:DeleteRecoveryPoint"
      Resource  = "arn:aws:ec2:${local.backup_recovery_region}::snapshot/*"
    }]
  })
}

# The observer cannot restore, copy, attach, or read backup contents. Its only resource
# deletion is a tagged detached restore-test volume after the dedicated job's TTL.
resource "aws_iam_role" "backup_recovery_observer" {
  provider = aws.staging
  name     = "fleet-backup-recovery-observer"
  assume_role_policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { AWS = aws_iam_role.backup_recovery.arn }, Action = "sts:AssumeRole" }]
  })
}

resource "aws_iam_role_policy" "backup_recovery_observer" {
  provider = aws.staging
  name     = "recovery-metadata-and-validation"
  role     = aws_iam_role.backup_recovery_observer.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "backup:ListRecoveryPointsByBackupVault"
        Resource = aws_backup_logically_air_gapped_vault.staging_recovery_dr.arn
      },
      {
        Effect   = "Allow"
        Action   = "backup:GetRestoreTestingPlan"
        Resource = concat([aws_backup_restore_testing_plan.fleet_dr.arn], aws_backup_restore_testing_plan.initial[*].arn)
      },
      {
        # Job IDs and EC2 DescribeVolumes do not support IAM resource scoping.
        Effect    = "Allow"
        Action    = ["backup:ListRestoreJobs", "backup:ListRestoreJobsByProtectedResource", "backup:DescribeRestoreJob", "backup:PutRestoreValidationResult", "ec2:DescribeVolumes"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.backup_recovery_region } }
      },
      {
        Effect    = "Allow"
        Action    = "ec2:DeleteVolume"
        Resource  = "arn:aws:ec2:${local.backup_recovery_region}:${data.aws_caller_identity.staging.account_id}:volume/*"
        Condition = { Null = { "aws:ResourceTag/awsbackup-restore-test" = "false" } }
      },
    ]
  })
}

resource "aws_cloudwatch_log_group" "backup_recovery" {
  name              = "/aws/lambda/fleet-backup-recovery"
  retention_in_days = 90
}

resource "aws_iam_role_policy" "backup_recovery" {
  name = "fleet-backup-recovery"
  role = aws_iam_role.backup_recovery.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      {
        Sid      = "NoAlternativeDeletionOrRetentionBypass"
        Effect   = "Deny"
        Action   = ["backup:UpdateRecoveryPointLifecycle", "backup:DisassociateRecoveryPoint", "backup:DisassociateRecoveryPointFromParent", "backup:TagResource", "backup:UntagResource", "backup:PutBackupVaultAccessPolicy", "backup:DeleteBackupVaultAccessPolicy", "backup:PutBackupVaultLockConfiguration", "backup:DeleteBackupVaultLockConfiguration"]
        Resource = "*"
      },
      {
        # DeleteRecoveryPoint forwards EC2 authorization as the caller, not the
        # stored backup role. Missing CalledVia also matches this explicit deny.
        # AWSBackupFullAccess uses the same Backup-only FAS condition for its allow:
        # https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AWSBackupFullAccess.html
        Sid       = "NoSnapshotDeletionOutsideBackup"
        Effect    = "Deny"
        Action    = "ec2:DeleteSnapshot"
        Resource  = "*"
        Condition = { "ForAllValues:StringNotEquals" = { "aws:CalledVia" = "backup.amazonaws.com" } }
      },
      {
        Sid      = "ReadProcessingRetryProvenance"
        Effect   = "Allow"
        Action   = "backup:ListTags"
        Resource = "arn:aws:ec2:${var.aws_region}::snapshot/*"
      },
      {
        # Live CloudTrail 2026-09-08: ListTags forwards DescribeTags using this
        # role. EC2 tag reads have no resource-level scope, so require Backup FAS
        # and the source region; the entry-point ListTags ARN scope stays above.
        Sid      = "ReadProcessingTagsOnlyThroughBackup"
        Effect   = "Allow"
        Action   = "ec2:DescribeTags"
        Resource = "*"
        Condition = {
          "ForAnyValue:StringEquals" = { "aws:CalledVia" = "backup.amazonaws.com" }
          StringEquals               = { "aws:RequestedRegion" = var.aws_region }
        }
      },
      {
        # Creation times let the daily-capture and monthly-test checks give a volume
        # created after their deadline a grace period instead of reporting it missing
        # (moving a dev box gives it a new root volume). EC2 Describe* has no
        # resource-level scope, so the region is the only narrowing available: the
        # checks read the protected volumes through a primary-region client.
        Sid       = "ReadVolumeCreationTimes"
        Effect    = "Allow"
        Action    = "ec2:DescribeVolumes"
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.backup_recovery.arn}:*"
      },
      {
        Effect   = "Allow"
        Action   = "backup:ListRecoveryPointsByBackupVault"
        Resource = [aws_backup_vault.ejc3_backup.arn, aws_backup_vault.processing.arn, aws_backup_vault.ejc3_backup_dr_cmk.arn]
      },
      {
        Effect    = "Allow"
        Action    = "backup:ListCopyJobs"
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = [var.aws_region, "us-east-1"] } }
      },
      {
        Effect   = "Allow"
        Action   = "backup:StartCopyJob"
        Resource = local.backup_snapshot_arns
      },
      {
        Effect   = "Allow"
        Action   = "backup:CopyFromBackupVault"
        Resource = local.backup_snapshot_arns
        Condition = {
          "ForAllValues:ArnEquals" = { "backup:CopyTargets" = [aws_backup_vault.ejc3_backup_dr_cmk.arn, aws_backup_logically_air_gapped_vault.staging_recovery_dr.arn] }
        }
      },
      {
        Effect   = "Allow"
        Action   = "backup:CopyIntoBackupVault"
        Resource = [aws_backup_vault.ejc3_backup_dr_cmk.arn, aws_backup_logically_air_gapped_vault.staging_recovery_dr.arn]
      },
      {
        Effect    = "Allow"
        Action    = "iam:PassRole"
        Resource  = local.backup_service_role_arn
        Condition = { StringEquals = { "iam:PassedToService" = "backup.amazonaws.com" } }
      },
      {
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = aws_iam_role.backup_recovery_observer.arn
      },
      {
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.cost_alerts.arn
      },
      {
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "FleetBackup" } }
      },
      ], local.backup_recovery_cleanup_enabled ? [
      {
        # This prerequisite cannot bypass the two vault policies: direct EC2
        # calls are explicitly denied above, and Backup deletion has no identity
        # allow. Require our owner, new pipeline tag, and AWS's reserved backup tag.
        Sid      = "DeleteProcessingSnapshotsOnlyThroughBackup"
        Effect   = "Allow"
        Action   = "ec2:DeleteSnapshot"
        Resource = local.backup_snapshot_arns
        Condition = {
          "ForAnyValue:StringEquals" = { "aws:CalledVia" = "backup.amazonaws.com" }
          StringEquals = {
            "ec2:Owner"                      = data.aws_caller_identity.current.account_id
            "aws:ResourceTag/BackupPipeline" = "fleet-processing-v2"
          }
          Null = { "aws:ResourceTag/aws:backup:source-resource" = "false" }
        }
      },
      ] : [], local.backup_recovery_cleanup_enabled && !local.backup_recovery_cutover_enabled ? [
      {
        # The two initial CMK seeds predate the pipeline tag. Exact snapshot pins
        # from the accepted 2026-09-07 copy lineage avoid retagging or granting
        # deletion over legacy snapshots. Retire this exception at final cutover.
        Sid    = "DeleteExactSupersededBootstrapCheckpointsThroughBackup"
        Effect = "Allow"
        Action = "ec2:DeleteSnapshot"
        Resource = [
          "arn:aws:ec2:us-east-1::snapshot/snap-0a7f6a0dde2e248b6",
          "arn:aws:ec2:us-east-1::snapshot/snap-017274f57046154b9",
        ]
        Condition = {
          "ForAnyValue:StringEquals" = { "aws:CalledVia" = "backup.amazonaws.com" }
          StringEquals               = { "ec2:Owner" = data.aws_caller_identity.current.account_id }
          Null                       = { "aws:ResourceTag/aws:backup:source-resource" = "false" }
        }
      },
    ] : [])
  })
}

data "archive_file" "backup_recovery" {
  type        = "zip"
  output_path = "${path.module}/.terraform/backup-recovery.zip"
  source {
    filename = "index.py"
    content  = file("${path.module}/scripts/backup-recovery.py")
  }
}

resource "aws_lambda_function" "backup_recovery" {
  function_name                  = "fleet-backup-recovery"
  role                           = aws_iam_role.backup_recovery.arn
  runtime                        = "python3.13"
  handler                        = "index.handler"
  architectures                  = ["arm64"]
  memory_size                    = 256
  timeout                        = 180
  reserved_concurrent_executions = 1
  filename                       = data.archive_file.backup_recovery.output_path
  source_code_hash               = data.archive_file.backup_recovery.output_base64sha256
  environment {
    variables = {
      CONFIG = jsonencode({
        primary_region       = var.aws_region
        primary_vault        = aws_backup_vault.ejc3_backup.name
        primary_vault_arn    = aws_backup_vault.ejc3_backup.arn
        processing_vault     = aws_backup_vault.processing.name
        processing_vault_arn = aws_backup_vault.processing.arn
        cleanup_enabled      = local.backup_recovery_cleanup_enabled
        cmk_hop_volumes      = local.backup_cmk_hop_volume_arns
        dr_vault             = aws_backup_vault.ejc3_backup_dr_cmk.name
        dr_vault_arn         = aws_backup_vault.ejc3_backup_dr_cmk.arn
        dr_key               = aws_kms_key.backup_dr.arn
        stage_region         = local.backup_recovery_region
        stage_vault          = aws_backup_logically_air_gapped_vault.staging_recovery_dr.name
        stage_vault_arn      = aws_backup_logically_air_gapped_vault.staging_recovery_dr.arn
        stage_account        = data.aws_caller_identity.staging.account_id
        copy_role            = local.backup_service_role_arn
        observer_role        = aws_iam_role.backup_recovery_observer.arn
        restore_role         = aws_iam_role.backup_restore_test.arn
        restore_plan         = aws_backup_restore_testing_plan.fleet_dr.name
        restore_plan_arn     = aws_backup_restore_testing_plan.fleet_dr.arn
        canary_plan          = try(aws_backup_restore_testing_plan.initial[0].name, null)
        canary_plan_arn      = try(aws_backup_restore_testing_plan.initial[0].arn, null)
        canary_start_at      = local.backup_initial_restore_at
        # Verified cutover retires one-off health checks before AWS job history
        # ages out. Monthly recovery tests and old test-volume cleanup continue.
        canary_accepted = local.backup_recovery_cutover_enabled
        volumes         = local.backup_protected_volume_arns
        topic           = aws_sns_topic.cost_alerts.arn
      })
    }
  }
  depends_on = [aws_iam_role_policy.backup_recovery, aws_iam_role_policy.backup_copy_keys,
    aws_iam_role_policy.backup_recovery_observer, aws_backup_vault_policy.staging_recovery_dr,
  aws_organizations_organization.security, aws_backup_global_settings.main]
}

resource "aws_cloudwatch_event_rule" "backup_reconcile" {
  name                = "backup-recovery-hourly"
  schedule_expression = "rate(1 hour)"
}

resource "aws_cloudwatch_event_target" "backup_reconcile" {
  rule = aws_cloudwatch_event_rule.backup_reconcile.name
  arn  = aws_lambda_function.backup_recovery.arn
}

resource "aws_lambda_permission" "backup_reconcile" {
  statement_id  = "HourlyRecoveryCheck"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backup_recovery.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.backup_reconcile.arn
}

resource "aws_cloudwatch_metric_alarm" "backup_controller_errors" {
  alarm_name          = "fleet-backup-controller-errors"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.backup_recovery.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "backup_copies_missing" {
  alarm_name          = "fleet-backup-copies-missing"
  namespace           = "FleetBackup"
  metric_name         = "MissingRecoveryCopies"
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 3
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "breaching"
  alarm_description   = "A fleet volume has no captured or final recovery point within 48h, or the hourly checker stopped reporting."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "backup_restore_tests_unhealthy" {
  alarm_name          = "fleet-backup-restore-tests-unhealthy"
  namespace           = "FleetBackup"
  metric_name         = "UnhealthyRestoreTests"
  statistic           = "Maximum"
  period              = 3600
  evaluation_periods  = 2
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "breaching"
  alarm_description   = "A configured volume's monthly detached restore test is missing, failed, overdue, or unvalidated; also detects stopped health reporting."
  alarm_actions       = [aws_sns_topic.cost_alerts.arn]
  ok_actions          = [aws_sns_topic.cost_alerts.arn]
}

# EBS emits copySnapshot completion events in the destination region but does not
# save them. Keep the full event, including detail.incremental, for operator evidence
# about the two main-account east1 CMK checkpoints. Snapshot IDs change every run:
# correlate detail.snapshot_id/source with completed Backup copy-job lineage rather
# than filtering this rule to IDs or to incremental=true (which would hide full copies).
# This observes main/east1 copies only, not the service-owned final LAG storage leg.
# https://docs.aws.amazon.com/ebs/latest/userguide/ebs-cloud-watch-events.html#copy-snapshot-complete
resource "aws_cloudwatch_log_group" "backup_copy_evidence" {
  provider          = aws.dr
  name              = "/aws/events/fleet-backup-copy-snapshots"
  retention_in_days = 7
  tags              = { Managed = "terraform", Purpose = "ebs-incremental-copy-evidence" }
}

resource "aws_cloudwatch_event_rule" "backup_copy_evidence" {
  provider       = aws.dr
  name           = "fleet-backup-copy-snapshot-evidence"
  description    = "Retain EBS copy completion evidence, including full/incremental status"
  event_bus_name = "default"
  event_pattern = jsonencode({
    account     = [data.aws_caller_identity.current.account_id]
    region      = ["us-east-1"]
    source      = ["aws.ec2"]
    detail-type = ["EBS Snapshot Notification"]
    detail      = { event = ["copySnapshot"] }
  })
  tags = { Managed = "terraform", Purpose = "ebs-incremental-copy-evidence" }
}

resource "aws_cloudwatch_log_resource_policy" "backup_copy_evidence" {
  provider    = aws.dr
  policy_name = "fleet-backup-copy-snapshot-evidence"
  # EventBridge's CreateLogStream call does not supply the rule SourceArn. Keep
  # that grant limited to this one group, and bind PutLogEvents to the exact rule.
  # This split follows the pinned provider's documented CloudWatch Logs example:
  # https://github.com/hashicorp/terraform-provider-aws/blob/v5.100.0/website/docs/r/cloudwatch_event_target.html.markdown#cloudwatch-log-group-usage
  policy_document = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowEventBridgeStreamCreation"
        Effect    = "Allow"
        Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
        Action    = "logs:CreateLogStream"
        Resource  = "${aws_cloudwatch_log_group.backup_copy_evidence.arn}:*"
      },
      {
        Sid       = "AllowOnlyThisRuleToWriteEvents"
        Effect    = "Allow"
        Principal = { Service = ["events.amazonaws.com", "delivery.logs.amazonaws.com"] }
        Action    = "logs:PutLogEvents"
        Resource  = "${aws_cloudwatch_log_group.backup_copy_evidence.arn}:*:*"
        Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.backup_copy_evidence.arn } }
      },
    ]
  })
}

resource "aws_cloudwatch_event_target" "backup_copy_evidence" {
  provider       = aws.dr
  rule           = aws_cloudwatch_event_rule.backup_copy_evidence.name
  event_bus_name = "default"
  target_id      = "copy-snapshot-evidence"
  arn            = aws_cloudwatch_log_group.backup_copy_evidence.arn
  # No RoleArn or input transform: Logs uses its resource policy and retains the
  # original event, preserving strings/booleans and unexpected diagnostic fields.
  retry_policy {
    maximum_event_age_in_seconds = 86400
    maximum_retry_attempts       = 185
  }
  depends_on = [aws_cloudwatch_log_resource_policy.backup_copy_evidence]
}

# Job-state events accelerate reconciliation/alerts; the hourly scan remains the
# source of truth because AWS Backup events are delivered on a best-effort basis.
resource "aws_cloudwatch_event_bus" "backup_recovery" {
  name = "backup-recovery"
}

locals {
  backup_job_event_pattern = jsonencode({
    source = ["aws.backup"]
    # Restore events use status; backup/copy events use state. Event delivery is
    # best effort, so the hourly controller separately verifies expected test jobs.
    "$or" = [
      {
        detail-type = ["Backup Job State Change", "Copy Job State Change"]
        detail      = { state = ["COMPLETED", "FAILED", "EXPIRED", "ABORTED", "PARTIAL"] }
      },
      {
        detail-type = ["Restore Job State Change"]
        detail      = { status = ["COMPLETED", "FAILED", "ABORTED"] }
      },
    ]
  })
}

resource "aws_iam_role" "backup_event_forward" {
  name = "backup-recovery-event-forward"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "backup_event_forward" {
  name = "forward-backup-events"
  role = aws_iam_role.backup_event_forward.name
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "events:PutEvents", Resource = aws_cloudwatch_event_bus.backup_recovery.arn }]
  })
}

resource "aws_cloudwatch_event_rule" "backup_events_primary" {
  name          = "backup-recovery-job-events"
  event_pattern = local.backup_job_event_pattern
}

resource "aws_cloudwatch_event_target" "backup_events_primary" {
  rule     = aws_cloudwatch_event_rule.backup_events_primary.name
  arn      = aws_cloudwatch_event_bus.backup_recovery.arn
  role_arn = aws_iam_role.backup_event_forward.arn
}

resource "aws_cloudwatch_event_rule" "backup_events_dr" {
  provider      = aws.dr
  name          = "backup-recovery-job-events"
  event_pattern = local.backup_job_event_pattern
}

resource "aws_cloudwatch_event_target" "backup_events_dr" {
  provider = aws.dr
  rule     = aws_cloudwatch_event_rule.backup_events_dr.name
  arn      = aws_cloudwatch_event_bus.backup_recovery.arn
  role_arn = aws_iam_role.backup_event_forward.arn
}

resource "aws_iam_role" "backup_event_forward_staging" {
  provider = aws.staging
  name     = "backup-recovery-event-forward"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Principal = { Service = "events.amazonaws.com" }, Action = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.staging.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "backup_event_forward_staging" {
  provider = aws.staging
  name     = "forward-backup-events"
  role     = aws_iam_role.backup_event_forward_staging.name
  policy = jsonencode({
    Version   = "2012-10-17"
    Statement = [{ Effect = "Allow", Action = "events:PutEvents", Resource = aws_cloudwatch_event_bus.backup_recovery.arn }]
  })
}

resource "aws_cloudwatch_event_bus_policy" "backup_recovery" {
  event_bus_name = aws_cloudwatch_event_bus.backup_recovery.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "StagingBackupEvents", Effect = "Allow"
      Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.staging.account_id}:root" }
      Action    = "events:PutEvents", Resource = aws_cloudwatch_event_bus.backup_recovery.arn
      Condition = { ArnEquals = { "aws:PrincipalArn" = aws_iam_role.backup_event_forward_staging.arn } }
    }]
  })
}

resource "aws_cloudwatch_event_rule" "backup_events_staging" {
  provider      = aws.staging
  name          = "backup-recovery-job-events"
  event_pattern = local.backup_job_event_pattern
}

resource "aws_cloudwatch_event_target" "backup_events_staging" {
  provider = aws.staging
  rule     = aws_cloudwatch_event_rule.backup_events_staging.name
  arn      = aws_cloudwatch_event_bus.backup_recovery.arn
  role_arn = aws_iam_role.backup_event_forward_staging.arn
}

resource "aws_cloudwatch_event_rule" "backup_events_staging_dr" {
  provider      = aws.staging_dr
  name          = "backup-recovery-job-events"
  event_pattern = local.backup_job_event_pattern
}

resource "aws_cloudwatch_event_target" "backup_events_staging_dr" {
  provider = aws.staging_dr
  rule     = aws_cloudwatch_event_rule.backup_events_staging_dr.name
  arn      = aws_cloudwatch_event_bus.backup_recovery.arn
  role_arn = aws_iam_role.backup_event_forward_staging.arn
}

resource "aws_cloudwatch_event_rule" "backup_dispatch" {
  name           = "backup-recovery-dispatch"
  event_bus_name = aws_cloudwatch_event_bus.backup_recovery.name
  event_pattern  = local.backup_job_event_pattern
}

resource "aws_cloudwatch_event_target" "backup_dispatch" {
  rule           = aws_cloudwatch_event_rule.backup_dispatch.name
  event_bus_name = aws_cloudwatch_event_bus.backup_recovery.name
  arn            = aws_lambda_function.backup_recovery.arn
}

resource "aws_lambda_permission" "backup_dispatch" {
  statement_id  = "BackupJobEvents"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backup_recovery.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.backup_dispatch.arn
}

# Monthly verification restores only encrypted, detached EBS volumes. It cannot boot
# copied user data, attach a volume to a host, or pass an administrator instance role.
# AWS Backup's restore-testing service-linked role cleans tagged test volumes after
# the two-hour validation window, including failures; the controller backs up cleanup
# after four hours only for known jobs' detached, tagged test volumes. A successful
# metadata check is not a guest filesystem/boot integrity test.
resource "aws_iam_role" "backup_restore_test" {
  provider = aws.staging
  name     = "fleet-backup-detached-restore-test"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow", Principal = { Service = "backup.amazonaws.com" }, Action = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.staging.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "backup_restore_test" {
  provider = aws.staging
  name     = "detached-encrypted-ebs-only"
  role     = aws_iam_role.backup_restore_test.name
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Deny"
        Action   = ["ec2:RunInstances", "ec2:AttachVolume", "iam:PassRole"]
        Resource = "*"
      },
      {
        Effect    = "Allow"
        Action    = ["ec2:DescribeSnapshots", "ec2:DescribeVolumes", "ec2:DescribeAvailabilityZones"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:RequestedRegion" = local.backup_recovery_region } }
      },
      {
        Effect   = "Allow", Action = "ec2:CreateVolume"
        Resource = "arn:aws:ec2:${local.backup_recovery_region}::snapshot/*"
      },
      {
        Effect   = "Allow", Action = "ec2:CreateVolume"
        Resource = "arn:aws:ec2:${local.backup_recovery_region}:${data.aws_caller_identity.staging.account_id}:volume/*"
        Condition = {
          Bool         = { "ec2:Encrypted" = "true" }
          StringEquals = { "ec2:VolumeType" = "gp3" }
        }
      },
      {
        Effect    = "Allow", Action = "ec2:CreateTags"
        Resource  = "arn:aws:ec2:${local.backup_recovery_region}:${data.aws_caller_identity.staging.account_id}:volume/*"
        Condition = { StringEquals = { "ec2:CreateAction" = "CreateVolume" } }
      },
      {
        Effect    = "Allow"
        Action    = "ec2:DeleteVolume"
        Resource  = "arn:aws:ec2:${local.backup_recovery_region}:${data.aws_caller_identity.staging.account_id}:volume/*"
        Condition = { Null = { "aws:ResourceTag/awsbackup-restore-test" = "false" } }
      },
      {
        # EBS reconstruction from Backup storage uses the direct snapshot permissions
        # in AWSBackupServiceRolePolicyForRestores, restricted to this region.
        Effect   = "Allow"
        Action   = ["ebs:CompleteSnapshot", "ebs:StartSnapshot", "ebs:PutSnapshotBlock"]
        Resource = "arn:aws:ec2:${local.backup_recovery_region}::snapshot/*"
      },
      {
        Effect   = "Allow"
        Action   = "kms:DescribeKey"
        Resource = "arn:aws:kms:${local.backup_recovery_region}:${data.aws_caller_identity.staging.account_id}:key/*"
      },
      {
        Effect    = "Allow"
        Action    = ["kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey*", "kms:ReEncrypt*", "kms:DescribeKey"]
        Resource  = "arn:aws:kms:${local.backup_recovery_region}:${data.aws_caller_identity.staging.account_id}:key/*"
        Condition = { StringEquals = { "kms:ViaService" = "ec2.${local.backup_recovery_region}.amazonaws.com" } }
      },
      {
        # The 2026-09-07 22:02:25 UTC restore canary hit a CloudTrail-confirmed
        # ReEncryptFrom denial after CreateVolume accepted the destination key.
        # Permit only that operation on this vault's exact service-owned source
        # key, through regional EBS; do not grant arbitrary cross-account keys.
        Sid       = "ReEncryptFromExactRecoveryVaultKey"
        Effect    = "Allow"
        Action    = "kms:ReEncryptFrom"
        Resource  = local.backup_recovery_source_key_arn
        Condition = { StringEquals = { "kms:ViaService" = "ec2.${local.backup_recovery_region}.amazonaws.com" } }
      },
      {
        Effect    = "Allow", Action = "kms:CreateGrant"
        Resource  = "arn:aws:kms:${local.backup_recovery_region}:${data.aws_caller_identity.staging.account_id}:key/*"
        Condition = { Bool = { "kms:GrantIsForAWSResource" = "true" } }
      },
    ]
  })
}

resource "aws_backup_restore_testing_plan" "fleet_dr" {
  provider                     = aws.staging_dr
  name                         = "fleet_monthly_detached_ebs"
  schedule_expression          = "cron(0 12 8 * ? *)"
  schedule_expression_timezone = "Etc/UTC"
  start_window_hours           = 1
  recovery_point_selection {
    algorithm             = "LATEST_WITHIN_WINDOW"
    include_vaults        = [aws_backup_logically_air_gapped_vault.staging_recovery_dr.arn]
    recovery_point_types  = ["SNAPSHOT"]
    selection_window_days = 35
  }
}

data "aws_availability_zones" "backup_restore_staging_dr" {
  provider = aws.staging_dr
  state    = "available"
}

resource "aws_backup_restore_testing_selection" "fleet_ebs_dr" {
  provider                  = aws.staging_dr
  name                      = "fleet_ebs"
  restore_testing_plan_name = aws_backup_restore_testing_plan.fleet_dr.name
  protected_resource_type   = "EBS"
  # The dedicated vault receives only the five configured fleet volume sources.
  protected_resource_arns = ["*"]
  iam_role_arn            = aws_iam_role.backup_restore_test.arn
  # More than one hourly reconciliation interval leaves room to validate when the
  # best-effort completion event is lost; successful validation triggers cleanup.
  validation_window_hours = 2
  restore_metadata_overrides = {
    availabilityZone = data.aws_availability_zones.backup_restore_staging_dr.names[0]
    volumeType       = "gp3"
    # Copied metadata can retain the original source-account/region KMS key. Use
    # this recovery account's EBS key, not a key needed only by the old live disk.
    kmsKeyId = "arn:aws:kms:${local.backup_recovery_region}:${data.aws_caller_identity.staging.account_id}:alias/aws/ebs"
  }
  depends_on = [aws_iam_role_policy.backup_restore_test]
}

output "backup_recovery_vault_arn" {
  description = "Immutable AWS-owned-key cross-account recovery destination (copies must be verified live)"
  value       = aws_backup_logically_air_gapped_vault.staging_recovery_dr.arn
}

output "backup_recovery_controller" {
  description = "Hourly/event-driven backup copy reconciliation and isolated restore-test metadata validation"
  value       = aws_lambda_function.backup_recovery.function_name
}

# Free external-access findings only: one account-zone analyzer per enabled region.
# Reuse security-regions.tf routing. No paid internal/unused analyzer, custom policy
# checks, workload permission change, or notification pipeline. One archive rule, below.
# Explicitly own the two required service roles before regional analyzers start.

resource "aws_iam_service_linked_role" "access_analyzer_main" {
  aws_service_name = "access-analyzer.amazonaws.com"
}

resource "aws_iam_service_linked_role" "access_analyzer_staging" {
  provider         = aws.staging
  aws_service_name = "access-analyzer.amazonaws.com"
}

resource "aws_accessanalyzer_analyzer" "external_main_ap_northeast_1" {
  provider      = aws.security_main_ap_northeast_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_ap_northeast_2" {
  provider      = aws.security_main_ap_northeast_2
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_ap_northeast_3" {
  provider      = aws.security_main_ap_northeast_3
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_ap_south_1" {
  provider      = aws.security_main_ap_south_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_ap_southeast_1" {
  provider      = aws.security_main_ap_southeast_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_ap_southeast_2" {
  provider      = aws.security_main_ap_southeast_2
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_ca_central_1" {
  provider      = aws.security_main_ca_central_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_eu_central_1" {
  provider      = aws.security_main_eu_central_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_eu_north_1" {
  provider      = aws.security_main_eu_north_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_eu_west_1" {
  provider      = aws.security_main_eu_west_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_eu_west_2" {
  provider      = aws.security_main_eu_west_2
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_eu_west_3" {
  provider      = aws.security_main_eu_west_3
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_sa_east_1" {
  provider      = aws.security_main_sa_east_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_us_east_1" {
  provider      = aws.dr
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_us_east_2" {
  provider      = aws.security_main_us_east_2
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_us_west_1" {
  provider      = aws
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_main_us_west_2" {
  provider      = aws.west2
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_main]
}

resource "aws_accessanalyzer_analyzer" "external_staging_ap_northeast_1" {
  provider      = aws.security_staging_ap_northeast_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_ap_northeast_2" {
  provider      = aws.security_staging_ap_northeast_2
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_ap_northeast_3" {
  provider      = aws.security_staging_ap_northeast_3
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_ap_south_1" {
  provider      = aws.security_staging_ap_south_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_ap_southeast_1" {
  provider      = aws.security_staging_ap_southeast_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_ap_southeast_2" {
  provider      = aws.security_staging_ap_southeast_2
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_ca_central_1" {
  provider      = aws.security_staging_ca_central_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_eu_central_1" {
  provider      = aws.security_staging_eu_central_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_eu_north_1" {
  provider      = aws.security_staging_eu_north_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_eu_west_1" {
  provider      = aws.security_staging_eu_west_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_eu_west_2" {
  provider      = aws.security_staging_eu_west_2
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_eu_west_3" {
  provider      = aws.security_staging_eu_west_3
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_sa_east_1" {
  provider      = aws.security_staging_sa_east_1
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_us_east_1" {
  provider      = aws.staging_dr
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_us_east_2" {
  provider      = aws.security_staging_us_east_2
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_us_west_1" {
  provider      = aws.staging
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

resource "aws_accessanalyzer_analyzer" "external_staging_us_west_2" {
  provider      = aws.security_staging_us_west_2
  analyzer_name = "security-external-access"
  type          = "ACCOUNT"
  tags          = { Managed = "terraform" }
  depends_on    = [aws_iam_service_linked_role.access_analyzer_staging]
}

# The one archived finding shape (owner, 2026-10-21, #577): AWS Backup's own temporary share of an EBS snapshot with its
# service account while it copies a recovery point into the logically air-gapped vault (backup-security.tf). CloudTrail shows
# backup.amazonaws.com adding the share, copying and removing it within minutes, every night, in the two regions the copies
# run in. Archived only for EBS snapshots AND those principals; any other resource type, account or region still alerts.
# AWS Backup uses a second service account for the cross-account copy into ejc3-backup-recovery (the dev-staging account,
# us-east-1): on 2026-10-22 CloudTrail showed backup.amazonaws.com (AWSBackupDefaultServiceRole) share the jumpbox-home
# recovery snapshot with 975050365828, an account outside the organization, 13 minutes after creating it; the copy job
# completed and the temporary snapshot, with its share, was gone minutes later.
locals {
  access_analyzer_backup_lag_principals = ["950509372133", "975050365828"]
}

resource "aws_accessanalyzer_archive_rule" "backup_lag_copy_us_west_1" {
  provider      = aws
  analyzer_name = aws_accessanalyzer_analyzer.external_main_us_west_1.analyzer_name
  rule_name     = "aws-backup-air-gapped-copy"
  filter {
    criteria = "resourceType"
    eq       = ["AWS::EC2::Snapshot"]
  }
  filter {
    criteria = "principal.AWS"
    eq       = local.access_analyzer_backup_lag_principals
  }
}

resource "aws_accessanalyzer_archive_rule" "backup_lag_copy_us_east_1" {
  provider      = aws.dr
  analyzer_name = aws_accessanalyzer_analyzer.external_main_us_east_1.analyzer_name
  rule_name     = "aws-backup-air-gapped-copy"
  filter {
    criteria = "resourceType"
    eq       = ["AWS::EC2::Snapshot"]
  }
  filter {
    criteria = "principal.AWS"
    eq       = local.access_analyzer_backup_lag_principals
  }
}

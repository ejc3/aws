terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Future volumes and snapshot copies only; no existing data is re-encrypted and
# no KMS key is created or changed. Existing volume snapshots inherit that volume.
resource "aws_ebs_encryption_by_default" "security" {
  enabled = true
}

# Also blocks public access to already-public owned snapshots. Private sharing
# (including cross-account backups) is unaffected. EBS-backed AMIs are separate.
resource "aws_ebs_snapshot_block_public_access" "security" {
  state = "block-all-sharing"
}

# Future launch default, not hard enforcement: explicit launch options can
# override it. Existing hosts are unchanged. AWS provider 5.100 also writes
# no-preference for the other regional metadata options; preflight verified those
# are unset in all 34 regions/accounts. Review any non-default before applying.
resource "aws_ec2_instance_metadata_defaults" "security" {
  http_tokens = "required"
}

# Block new public document sharing, not private sharing or use of AWS documents.
# Existing public documents would need a separate reviewed permission change.
# Use the full ARN: AWS provider 5.100 normalizes setting_id to ARN on refresh.
resource "aws_ssm_service_setting" "block_public_document_sharing" {
  setting_id    = "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:servicesetting/ssm/documents/console/public-sharing-permission"
  setting_value = "Disable"
  # Deleting this resource resets the setting and would permit public sharing.
  lifecycle { prevent_destroy = true }
}

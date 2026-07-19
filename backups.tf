# backups.tf
#
# Terraform-managed backup vault "ejc3-backup" (replaces the hand-created, un-managed
# "fcvm-backups" vault — that one stays until its existing recovery points age out).
#
# Protection so disks aren't lost even if things go haywire:
#   - governance-mode Vault Lock: recovery points are immutable to normal operations,
#     `terraform destroy`, and bad actors; a designated break-glass role can still
#     override in a true emergency, and the lock itself is reversible.
#   - cross-region DR copy (weekly + monthly) to us-east-1, so a us-west-1 event
#     can't lose everything.
#   - a monthly rule kept 365d in cold storage for long-tail / late-discovery recovery.
# Cross-ACCOUNT copy to dev-staging is added once its provider is wired (task #4).

# us-east-1 provider for the cross-region DR vault
provider "aws" {
  alias  = "dr"
  region = "us-east-1"
}

resource "aws_backup_vault" "ejc3_backup" {
  name = "ejc3-backup"
  tags = { Name = "ejc3-backup", Managed = "terraform" }
}

resource "aws_backup_vault" "ejc3_backup_dr" {
  provider = aws.dr
  name     = "ejc3-backup-dr"
  tags     = { Name = "ejc3-backup-dr", Managed = "terraform" }
}

# Governance mode (no changeable_for_days => governance, not the irreversible compliance).
resource "aws_backup_vault_lock_configuration" "ejc3_backup" {
  backup_vault_name  = aws_backup_vault.ejc3_backup.name
  min_retention_days = 1
  max_retention_days = 366
}

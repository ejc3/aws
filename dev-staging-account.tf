# dev-staging-account.tf
#
# Isolated AWS member account used to pre-validate terraform + user_data changes
# before they reach the real dev servers. A bad apply here cannot touch prod.
# Follow-on plumbing (backend, cross-account provider, AMI share, secrets) lives
# in dev-staging-bootstrap.tf once this account exists.

variable "enable_dev_staging" {
  description = "Create the isolated dev-staging member account"
  type        = bool
  default     = true
}

variable "dev_staging_email" {
  description = "Unique root email for the dev-staging member account (gmail +alias is fine)"
  type        = string
  default     = "ej.campbell+dev-staging@gmail.com"
}

# Existing organization (management account is the only member today).
data "aws_organizations_organization" "current" {}

# Dedicated OU so staging accounts are grouped (and SCP guardrails can attach here later)
# rather than sitting loose at the org root.
resource "aws_organizations_organizational_unit" "sandbox" {
  count     = var.enable_dev_staging ? 1 : 0
  name      = "Sandbox"
  parent_id = data.aws_organizations_organization.current.roots[0].id
}

resource "aws_organizations_account" "dev_staging" {
  count     = var.enable_dev_staging ? 1 : 0
  name      = "dev-staging"
  email     = var.dev_staging_email
  parent_id = aws_organizations_organizational_unit.sandbox[0].id
  role_name = "OrganizationAccountAccessRole" # role in the member account this (mgmt) account can assume

  # Let terraform close the account on destroy (still enters a 90-day suspension window).
  close_on_deletion = true

  lifecycle {
    # Email/name changes force a brand-new account; guard against accidental replace/destroy.
    prevent_destroy = true
    ignore_changes  = [role_name]
  }

  tags = {
    Purpose = "dev-staging"
    Managed = "terraform"
  }
}

output "dev_staging_account_id" {
  description = "Account ID of the dev-staging member account (empty until created)"
  value       = var.enable_dev_staging ? aws_organizations_account.dev_staging[0].id : null
}

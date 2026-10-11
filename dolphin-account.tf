# dolphin-account.tf
#
# A member account of its own for Dolphin (the dolphin-labs-hq sites), so Dolphin's resources can move out of this
# account one consumer at a time. It sits in its own OU at the org root. Its Terraform lives in the private repository
# dolphin-labs-hq/dolphin-aws and is applied from nextjs-dev only (dolphin-bootstrap.tf builds the state bucket and the
# apply role that repository needs). See AGENTS.md, "The Dolphin account".
#
# Nothing in this account becomes reachable from the Dolphin account: the trust runs one way, from here into it.

resource "aws_organizations_organizational_unit" "dolphin" {
  name      = "Dolphin"
  parent_id = data.aws_organizations_organization.current.roots[0].id
  tags      = { Purpose = "dolphin", Managed = "terraform" }
}

resource "aws_organizations_account" "dolphin" {
  name      = "dolphin"
  email     = local.dolphin_account_email
  parent_id = aws_organizations_organizational_unit.dolphin.id
  role_name = "OrganizationAccountAccessRole" # the role in the member account this (management) account assumes

  # Unlike dev-staging, never close it from Terraform: this account will hold production data. A destroy would only try
  # to remove it from the organization, and prevent_destroy stops even that.
  close_on_deletion = false

  lifecycle {
    # A change of name or email replaces the account; guard against an accidental replace or destroy.
    prevent_destroy = true
    ignore_changes  = [role_name]
  }

  tags = { Purpose = "dolphin", Managed = "terraform" }
}

output "dolphin_account_id" {
  description = "Account ID of the Dolphin member account"
  value       = aws_organizations_account.dolphin.id
}

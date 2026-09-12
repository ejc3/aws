# Owner-approved SECURITY contacts. Read each account's existing primary name/phone
# and reuse the established alert email rather than committing personal contact data.
# The native primary-contact data source includes address fields in protected state;
# keep plans/state private even though resource attributes below are redacted.
data "aws_account_primary_contact" "main" {}

data "aws_account_primary_contact" "staging" {
  provider = aws.staging
}

# Omit account_id on both data and resources: each provider acts on its own account.
# Supplying the management account's ID instead invokes the Organizations API path.
resource "aws_account_alternate_contact" "security_main" {
  alternate_contact_type = "SECURITY"
  email_address          = sensitive(data.aws_ssm_parameter.alert_email.value)
  name                   = sensitive(data.aws_account_primary_contact.main.full_name)
  phone_number           = sensitive(data.aws_account_primary_contact.main.phone_number)
  title                  = "Owner"

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = data.aws_caller_identity.current.account_id == "928413605543"
      error_message = "The main security contact belongs only to the main fleet account."
    }
  }
}

resource "aws_account_alternate_contact" "security_staging" {
  provider = aws.staging

  alternate_contact_type = "SECURITY"
  email_address          = sensitive(data.aws_ssm_parameter.alert_email.value)
  name                   = sensitive(data.aws_account_primary_contact.staging.full_name)
  phone_number           = sensitive(data.aws_account_primary_contact.staging.phone_number)
  title                  = "Owner"

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = data.aws_caller_identity.staging.account_id == "249042068453"
      error_message = "The recovery security contact belongs only to the recovery account."
    }
  }
}

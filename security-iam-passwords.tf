# Global IAM-user console-password guardrails, not root or SSO authentication.
# September 12, 2026 inventory: main has an unmanaged weak custom policy; recovery
# has no custom policy. Neither account has an IAM user console password enabled.
# AWS provider 5.100 uses UpdateAccountPasswordPolicy for both create and update;
# import main's singleton so the reviewed plan exposes its existing weak settings.
import {
  to = aws_iam_account_password_policy.main
  id = "iam-account-password-policy"
}

resource "aws_iam_account_password_policy" "main" {
  minimum_password_length        = 14
  require_uppercase_characters   = true
  require_lowercase_characters   = true
  require_numbers                = true
  require_symbols                = true
  password_reuse_prevention      = 24
  max_password_age               = 0
  hard_expiry                    = false
  allow_users_to_change_password = false

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = data.aws_caller_identity.current.account_id == "928413605543"
      error_message = "The main IAM password policy belongs only to the main fleet account."
    }
  }
}

resource "aws_iam_account_password_policy" "staging" {
  provider = aws.staging

  minimum_password_length        = 14
  require_uppercase_characters   = true
  require_lowercase_characters   = true
  require_numbers                = true
  require_symbols                = true
  password_reuse_prevention      = 24
  max_password_age               = 0
  hard_expiry                    = false
  allow_users_to_change_password = false

  lifecycle {
    prevent_destroy = true
    precondition {
      condition     = data.aws_caller_identity.staging.account_id == "249042068453"
      error_message = "The recovery IAM password policy belongs only to the recovery account."
    }
  }
}

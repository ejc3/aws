# dolphin-security.tf
#
# Security monitoring for the Dolphin member account (dolphin-account.tf), built from here like its state bucket
# (dolphin-bootstrap.tf) and never from dolphin-aws. Trust runs one way: this account oversees the Dolphin account and
# nothing in the Dolphin account may write into this one. So, unlike dev-staging, the Dolphin account is NOT in
# local.security_account_ids and forwards nothing to the central bus:
#
# - GuardDuty runs in all 17 regions there (modules/security-region with forward_findings = false), and this account is
#   its GuardDuty ADMINISTRATOR by invitation, region by region. An administrator's EventBridge receives its members'
#   findings, so the existing security-forward-findings rule in each region of this account forwards Dolphin's findings
#   with no new path.
# - Posture (Config, Security Hub with the foundational standard, Inspector) runs in us-west-1, where Dolphin's
#   resources live. Config delivers to a bucket IN the Dolphin account. This account is its Security Hub administrator
#   (us-west-1), so control failures arrive the same way; Inspector findings, which the shared pattern leaves to
#   Inspector's own events, get one rule here (dolphin_security_inspector).
# - A Config aggregator here reads the Dolphin account's configuration (Config console, "dolphin" aggregator).
# - The account defaults (modules/security-defaults) apply in every region, as in the other accounts.
#
# What it does not cover: the Dolphin account's CloudTrail control-plane alerts (root sign-in, StopLogging, IAM
# changes) that security-forward-audit raises for the other two accounts; they need an event path into this account.
# The organization trail still records every call there.

locals {
  # Workload region of the Dolphin account: posture, Security Hub administration and the Config aggregator.
  dolphin_security_region = "us-west-1"
}

provider "aws" {
  alias  = "dolphin_ap_south_1"
  region = "ap-south-1"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_ap_northeast_1"
  region = "ap-northeast-1"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_ap_northeast_2"
  region = "ap-northeast-2"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_ap_northeast_3"
  region = "ap-northeast-3"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_ap_southeast_1"
  region = "ap-southeast-1"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_ap_southeast_2"
  region = "ap-southeast-2"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_ca_central_1"
  region = "ca-central-1"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_eu_central_1"
  region = "eu-central-1"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_eu_north_1"
  region = "eu-north-1"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_eu_west_1"
  region = "eu-west-1"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_eu_west_2"
  region = "eu-west-2"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_eu_west_3"
  region = "eu-west-3"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_sa_east_1"
  region = "sa-east-1"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_us_east_1"
  region = "us-east-1"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_us_east_2"
  region = "us-east-2"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

provider "aws" {
  alias  = "dolphin_us_west_2"
  region = "us-west-2"
  assume_role {
    role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"
  }
}

# ------------------------------------------------------------------------------------------------ Config bucket
resource "aws_s3_bucket" "dolphin_security_config" {
  provider = aws.dolphin
  bucket   = "dolphin-security-records-${aws_organizations_account.dolphin.id}"
  tags     = { Name = "dolphin-security-records", Purpose = "security-baseline", Managed = "terraform" }
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_public_access_block" "dolphin_security_config" {
  provider                = aws.dolphin
  bucket                  = aws_s3_bucket.dolphin_security_config.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "dolphin_security_config" {
  provider = aws.dolphin
  bucket   = aws_s3_bucket.dolphin_security_config.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "dolphin_security_config" {
  provider = aws.dolphin
  bucket   = aws_s3_bucket.dolphin_security_config.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_versioning" "dolphin_security_config" {
  provider = aws.dolphin
  bucket   = aws_s3_bucket.dolphin_security_config.id
  versioning_configuration { status = "Enabled" }
  lifecycle { prevent_destroy = true }
}

resource "aws_s3_bucket_lifecycle_configuration" "dolphin_security_config" {
  provider = aws.dolphin
  bucket   = aws_s3_bucket.dolphin_security_config.id
  rule {
    id     = "one-year-security-records"
    status = "Enabled"
    filter {}
    expiration { days = 365 }
    noncurrent_version_expiration { noncurrent_days = 365 }
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
  depends_on = [aws_s3_bucket_versioning.dolphin_security_config]
}

# The recorder writes with its own role (security-config-<region>, modules/security-region); Config itself checks the
# bucket as the service. Both are pinned to the Dolphin account.
resource "aws_s3_bucket_policy" "dolphin_security_config" {
  provider = aws.dolphin
  bucket   = aws_s3_bucket.dolphin_security_config.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "RequireTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.dolphin_security_config.arn, "${aws_s3_bucket.dolphin_security_config.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false", "aws:PrincipalIsAWSService" = "false" } }
      },
      {
        Sid       = "ConfigBucketCheck"
        Effect    = "Allow"
        Principal = { Service = "config.amazonaws.com" }
        Action    = ["s3:GetBucketAcl", "s3:ListBucket"]
        Resource  = aws_s3_bucket.dolphin_security_config.arn
        Condition = { StringEquals = { "aws:SourceAccount" = aws_organizations_account.dolphin.id } }
      },
      {
        Sid       = "ConfigServiceDelivery"
        Effect    = "Allow"
        Principal = { Service = "config.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.dolphin_security_config.arn}/config/AWSLogs/${aws_organizations_account.dolphin.id}/Config/*"
        Condition = { StringEquals = {
          "aws:SourceAccount" = aws_organizations_account.dolphin.id
          "s3:x-amz-acl"      = "bucket-owner-full-control"
        } }
      }
    ]
  })
  depends_on = [
    aws_s3_bucket_public_access_block.dolphin_security_config,
    aws_s3_bucket_ownership_controls.dolphin_security_config,
    aws_s3_bucket_server_side_encryption_configuration.dolphin_security_config,
    aws_s3_bucket_versioning.dolphin_security_config,
  ]
}

# ------------------------------------------------------------------------------------------------ per region

module "dolphin_security_ap_south_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_ap_south_1 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_ap_south_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_ap_south_1 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in ap-south-1.
resource "aws_guardduty_member" "dolphin_ap_south_1" {
  provider                   = aws.security_main_ap_south_1
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_ap_south_1.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_ap_south_1" {
  provider          = aws.dolphin_ap_south_1
  detector_id       = module.dolphin_security_ap_south_1.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_ap_south_1]
}

module "dolphin_security_ap_northeast_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_ap_northeast_1 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_ap_northeast_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_ap_northeast_1 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in ap-northeast-1.
resource "aws_guardduty_member" "dolphin_ap_northeast_1" {
  provider                   = aws.security_main_ap_northeast_1
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_ap_northeast_1.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_ap_northeast_1" {
  provider          = aws.dolphin_ap_northeast_1
  detector_id       = module.dolphin_security_ap_northeast_1.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_ap_northeast_1]
}

module "dolphin_security_ap_northeast_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_ap_northeast_2 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_ap_northeast_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_ap_northeast_2 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in ap-northeast-2.
resource "aws_guardduty_member" "dolphin_ap_northeast_2" {
  provider                   = aws.security_main_ap_northeast_2
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_ap_northeast_2.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_ap_northeast_2" {
  provider          = aws.dolphin_ap_northeast_2
  detector_id       = module.dolphin_security_ap_northeast_2.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_ap_northeast_2]
}

module "dolphin_security_ap_northeast_3" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_ap_northeast_3 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_ap_northeast_3" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_ap_northeast_3 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in ap-northeast-3.
resource "aws_guardduty_member" "dolphin_ap_northeast_3" {
  provider                   = aws.security_main_ap_northeast_3
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_ap_northeast_3.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_ap_northeast_3" {
  provider          = aws.dolphin_ap_northeast_3
  detector_id       = module.dolphin_security_ap_northeast_3.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_ap_northeast_3]
}

module "dolphin_security_ap_southeast_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_ap_southeast_1 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_ap_southeast_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_ap_southeast_1 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in ap-southeast-1.
resource "aws_guardduty_member" "dolphin_ap_southeast_1" {
  provider                   = aws.security_main_ap_southeast_1
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_ap_southeast_1.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_ap_southeast_1" {
  provider          = aws.dolphin_ap_southeast_1
  detector_id       = module.dolphin_security_ap_southeast_1.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_ap_southeast_1]
}

module "dolphin_security_ap_southeast_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_ap_southeast_2 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_ap_southeast_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_ap_southeast_2 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in ap-southeast-2.
resource "aws_guardduty_member" "dolphin_ap_southeast_2" {
  provider                   = aws.security_main_ap_southeast_2
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_ap_southeast_2.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_ap_southeast_2" {
  provider          = aws.dolphin_ap_southeast_2
  detector_id       = module.dolphin_security_ap_southeast_2.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_ap_southeast_2]
}

module "dolphin_security_ca_central_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_ca_central_1 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_ca_central_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_ca_central_1 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in ca-central-1.
resource "aws_guardduty_member" "dolphin_ca_central_1" {
  provider                   = aws.security_main_ca_central_1
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_ca_central_1.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_ca_central_1" {
  provider          = aws.dolphin_ca_central_1
  detector_id       = module.dolphin_security_ca_central_1.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_ca_central_1]
}

module "dolphin_security_eu_central_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_eu_central_1 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_eu_central_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_eu_central_1 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in eu-central-1.
resource "aws_guardduty_member" "dolphin_eu_central_1" {
  provider                   = aws.security_main_eu_central_1
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_eu_central_1.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_eu_central_1" {
  provider          = aws.dolphin_eu_central_1
  detector_id       = module.dolphin_security_eu_central_1.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_eu_central_1]
}

module "dolphin_security_eu_north_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_eu_north_1 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_eu_north_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_eu_north_1 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in eu-north-1.
resource "aws_guardduty_member" "dolphin_eu_north_1" {
  provider                   = aws.security_main_eu_north_1
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_eu_north_1.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_eu_north_1" {
  provider          = aws.dolphin_eu_north_1
  detector_id       = module.dolphin_security_eu_north_1.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_eu_north_1]
}

module "dolphin_security_eu_west_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_eu_west_1 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_eu_west_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_eu_west_1 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in eu-west-1.
resource "aws_guardduty_member" "dolphin_eu_west_1" {
  provider                   = aws.security_main_eu_west_1
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_eu_west_1.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_eu_west_1" {
  provider          = aws.dolphin_eu_west_1
  detector_id       = module.dolphin_security_eu_west_1.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_eu_west_1]
}

module "dolphin_security_eu_west_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_eu_west_2 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_eu_west_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_eu_west_2 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in eu-west-2.
resource "aws_guardduty_member" "dolphin_eu_west_2" {
  provider                   = aws.security_main_eu_west_2
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_eu_west_2.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_eu_west_2" {
  provider          = aws.dolphin_eu_west_2
  detector_id       = module.dolphin_security_eu_west_2.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_eu_west_2]
}

module "dolphin_security_eu_west_3" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_eu_west_3 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_eu_west_3" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_eu_west_3 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in eu-west-3.
resource "aws_guardduty_member" "dolphin_eu_west_3" {
  provider                   = aws.security_main_eu_west_3
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_eu_west_3.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_eu_west_3" {
  provider          = aws.dolphin_eu_west_3
  detector_id       = module.dolphin_security_eu_west_3.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_eu_west_3]
}

module "dolphin_security_sa_east_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_sa_east_1 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_sa_east_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_sa_east_1 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in sa-east-1.
resource "aws_guardduty_member" "dolphin_sa_east_1" {
  provider                   = aws.security_main_sa_east_1
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_sa_east_1.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_sa_east_1" {
  provider          = aws.dolphin_sa_east_1
  detector_id       = module.dolphin_security_sa_east_1.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_sa_east_1]
}

module "dolphin_security_us_east_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_us_east_1 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_us_east_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_us_east_1 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in us-east-1.
resource "aws_guardduty_member" "dolphin_us_east_1" {
  provider                   = aws.dr
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_us_east_1.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_us_east_1" {
  provider          = aws.dolphin_us_east_1
  detector_id       = module.dolphin_security_us_east_1.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_us_east_1]
}

module "dolphin_security_us_east_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_us_east_2 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_us_east_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_us_east_2 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in us-east-2.
resource "aws_guardduty_member" "dolphin_us_east_2" {
  provider                   = aws.security_main_us_east_2
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_us_east_2.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_us_east_2" {
  provider          = aws.dolphin_us_east_2
  detector_id       = module.dolphin_security_us_east_2.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_us_east_2]
}

module "dolphin_security_us_west_1" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = local.security_posture_enabled
  record_global_iam     = true
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_us_west_1" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in us-west-1.
resource "aws_guardduty_member" "dolphin_us_west_1" {
  provider                   = aws
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_us_west_1.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_us_west_1" {
  provider          = aws.dolphin
  detector_id       = module.dolphin_security_us_west_1.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_us_west_1]
}

module "dolphin_security_us_west_2" {
  source                = "./modules/security-region"
  providers             = { aws = aws.dolphin_us_west_2 }
  forward_findings      = false
  central_event_bus_arn = ""
  forwarding_role_arn   = ""
  audit_bucket_name     = aws_s3_bucket.dolphin_security_config.id
  audit_bucket_arn      = aws_s3_bucket.dolphin_security_config.arn
  posture_enabled       = false
  record_global_iam     = false
  depends_on            = [aws_s3_bucket_policy.dolphin_security_config]
}

module "security_defaults_dolphin_us_west_2" {
  source    = "./modules/security-defaults"
  providers = { aws = aws.dolphin_us_west_2 }
}

# This account invites, the Dolphin account accepts: afterwards GuardDuty sends this account's EventBridge the Dolphin
# account's findings in us-west-2.
resource "aws_guardduty_member" "dolphin_us_west_2" {
  provider                   = aws.west2
  account_id                 = aws_organizations_account.dolphin.id
  detector_id                = module.security_main_us_west_2.guardduty_detector_id
  email                      = local.dolphin_account_email
  invite                     = true
  disable_email_notification = true
}

resource "aws_guardduty_invite_accepter" "dolphin_us_west_2" {
  provider          = aws.dolphin_us_west_2
  detector_id       = module.dolphin_security_us_west_2.guardduty_detector_id
  master_account_id = data.aws_caller_identity.current.account_id
  depends_on        = [aws_guardduty_member.dolphin_us_west_2]
}

# ------------------------------------------------------------------------------------------------ Security Hub and Config
resource "aws_securityhub_member" "dolphin" {
  account_id = aws_organizations_account.dolphin.id
  invite     = true
  depends_on = [module.security_main_us_west_1]
}

resource "aws_securityhub_invite_accepter" "dolphin" {
  provider   = aws.dolphin
  master_id  = data.aws_caller_identity.current.account_id
  depends_on = [aws_securityhub_member.dolphin, module.dolphin_security_us_west_1]
}

resource "aws_config_aggregate_authorization" "dolphin" {
  provider              = aws.dolphin
  account_id            = data.aws_caller_identity.current.account_id
  authorized_aws_region = local.dolphin_security_region
}

resource "aws_config_configuration_aggregator" "dolphin" {
  name = "dolphin"
  account_aggregation_source {
    account_ids = [aws_organizations_account.dolphin.id]
    regions     = [local.dolphin_security_region]
  }
  depends_on = [aws_config_aggregate_authorization.dolphin]
}

# Inspector findings reach Security Hub, and so this account, but the shared findings pattern skips Inspector's Security
# Hub copies (an account's own Inspector events notify directly; the Dolphin account's cannot reach here). This rule
# forwards exactly those copies for the Dolphin account to the central bus.
resource "aws_cloudwatch_event_rule" "dolphin_security_inspector" {
  name        = "dolphin-security-inspector"
  description = "Forward the Dolphin account's HIGH/CRITICAL Inspector findings (through Security Hub) to the central security bus"
  event_pattern = jsonencode({
    source        = ["aws.securityhub"]
    "detail-type" = ["Security Hub Findings - Imported"]
    detail = { findings = {
      AwsAccountId = [aws_organizations_account.dolphin.id]
      ProductArn   = [{ suffix = ":product/aws/inspector" }]
      Severity     = { Label = ["HIGH", "CRITICAL"] }
      RecordState  = ["ACTIVE"]
      Workflow     = { Status = ["NEW"] }
    } }
  })
}

resource "aws_cloudwatch_event_target" "dolphin_security_inspector" {
  rule      = aws_cloudwatch_event_rule.dolphin_security_inspector.name
  target_id = "central-security-alerts"
  arn       = aws_cloudwatch_event_bus.security_alerts.arn
  role_arn  = aws_iam_role.security_forward.arn
  dead_letter_config { arn = aws_sqs_queue.security_delivery_failures.arn }
}

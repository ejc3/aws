# security-hub-automation.tf
#
# Security Hub automation rules, in each region where this account runs Security Hub (us-west-1, us-west-2, us-east-1). A rule records WHY a finding is not an alert here: it sets the finding's workflow to
# SUPPRESSED with a note before Security Hub sends it on, and the alert pattern (modules/security-region,
# security-forward-findings) forwards only Workflow NEW, so a suppressed finding never reaches the alert topic.
#
# Add a rule only for a finding that is true by design, with the reason in its note; a real exposure is fixed, not
# suppressed. The PR watch's alert step (scripts/pr-watch/SKILL.md) adds them.

locals {
  # EC2.9 (instance has a public IPv4) is true by design in THIS account only: there is no NAT, so every box here (the
  # app runners, the dev boxes, wbox, the jumpboxes) egresses on its own address, and what may connect in is the security
  # groups' and Cloudflare Access's job, checked by their own controls (EC2.13/14/18/19), which stay unsuppressed. The
  # Dolphin and staging accounts are not covered: an instance with a public address there still alerts. Account, not tags:
  # most app-runner findings are recorded before the runner is tagged.
  public_ipv4_rule = {
    name        = "ec2-public-ipv4-by-design"
    description = "EC2.9 in the main account is the fleet's design: no NAT, so runners and dev boxes egress on their own address; inbound is the security groups' and Cloudflare Access's job, checked by their own controls. Other accounts still alert."
    note        = "Public IPv4 by design in the main account (no NAT); inbound is governed by security groups and Cloudflare Access. security-hub-automation.tf"
  }
}

resource "aws_securityhub_automation_rule" "public_ipv4_by_design" {
  count       = local.security_posture_enabled ? 1 : 0
  rule_name   = local.public_ipv4_rule.name
  description = local.public_ipv4_rule.description
  rule_order  = 1
  rule_status = "ENABLED"

  criteria {
    aws_account_id {
      comparison = "EQUALS"
      value      = data.aws_caller_identity.current.account_id
    }
    compliance_security_control_id {
      comparison = "EQUALS"
      value      = "EC2.9"
    }
    record_state {
      comparison = "EQUALS"
      value      = "ACTIVE"
    }
  }

  actions {
    type = "FINDING_FIELDS_UPDATE"
    finding_fields_update {
      workflow {
        status = "SUPPRESSED"
      }
      note {
        text       = local.public_ipv4_rule.note
        updated_by = "terraform"
      }
    }
  }

  depends_on = [module.security_main_us_west_1]
}

resource "aws_securityhub_automation_rule" "public_ipv4_by_design_west2" {
  provider    = aws.west2
  count       = local.security_posture_enabled ? 1 : 0
  rule_name   = local.public_ipv4_rule.name
  description = local.public_ipv4_rule.description
  rule_order  = 1
  rule_status = "ENABLED"

  criteria {
    aws_account_id {
      comparison = "EQUALS"
      value      = data.aws_caller_identity.current.account_id
    }
    compliance_security_control_id {
      comparison = "EQUALS"
      value      = "EC2.9"
    }
    record_state {
      comparison = "EQUALS"
      value      = "ACTIVE"
    }
  }

  actions {
    type = "FINDING_FIELDS_UPDATE"
    finding_fields_update {
      workflow {
        status = "SUPPRESSED"
      }
      note {
        text       = local.public_ipv4_rule.note
        updated_by = "terraform"
      }
    }
  }

  depends_on = [module.security_main_us_west_2]
}

resource "aws_securityhub_automation_rule" "public_ipv4_by_design_dr" {
  provider    = aws.dr
  count       = local.security_posture_enabled ? 1 : 0
  rule_name   = local.public_ipv4_rule.name
  description = local.public_ipv4_rule.description
  rule_order  = 1
  rule_status = "ENABLED"

  criteria {
    aws_account_id {
      comparison = "EQUALS"
      value      = data.aws_caller_identity.current.account_id
    }
    compliance_security_control_id {
      comparison = "EQUALS"
      value      = "EC2.9"
    }
    record_state {
      comparison = "EQUALS"
      value      = "ACTIVE"
    }
  }

  actions {
    type = "FINDING_FIELDS_UPDATE"
    finding_fields_update {
      workflow {
        status = "SUPPRESSED"
      }
      note {
        text       = local.public_ipv4_rule.note
        updated_by = "terraform"
      }
    }
  }

  depends_on = [module.security_main_us_east_1]
}

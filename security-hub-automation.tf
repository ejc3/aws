# security-hub-automation.tf
#
# Security Hub automation rules (us-west-1, where Security Hub runs; as the Dolphin account's administrator these also
# apply to its findings). A rule records WHY a finding is not an alert here: it sets the finding's workflow to
# SUPPRESSED with a note before Security Hub sends it on, and the alert pattern (modules/security-region,
# security-forward-findings) forwards only Workflow NEW, so a suppressed finding never reaches the alert topic.
#
# Add a rule only for a finding that is true by design, with the reason in its note; a real exposure is fixed, not
# suppressed. The PR watch's alert step (scripts/pr-watch/SKILL.md) adds them.

resource "aws_securityhub_automation_rule" "public_ipv4_by_design" {
  count       = local.security_posture_enabled ? 1 : 0
  rule_name   = "ec2-public-ipv4-by-design"
  description = "EC2.9 (instance has a public IPv4) is the fleet's design: there is no NAT, so runners and dev boxes egress on their own address; what may connect in is the security groups' and Cloudflare Access's job, checked by their own controls."
  rule_order  = 1
  rule_status = "ENABLED"

  criteria {
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
        text       = "Public IPv4 by design (no NAT); inbound is governed by security groups and Cloudflare Access. security-hub-automation.tf"
        updated_by = "terraform"
      }
    }
  }

  depends_on = [module.security_main_us_west_1]
}

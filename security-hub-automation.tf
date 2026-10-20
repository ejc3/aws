# security-hub-automation.tf
#
# Security Hub automation rules, in each region where this account runs Security Hub (us-west-1, us-west-2, us-east-1). A rule records WHY a finding is not an alert here: it sets the finding's workflow to
# SUPPRESSED with a note before Security Hub sends it on, and the alert pattern (modules/security-region,
# security-forward-findings) forwards only Workflow NEW, so a suppressed finding never reaches the alert topic.
#
# Add a rule only for a finding that is true by design, with the reason in its note; a real exposure is fixed, not
# suppressed. The PR watch's alert step (scripts/pr-watch/SKILL.md) adds them.
#
# Every rule, whatever its list entry says, also requires (in the resource blocks below):
#   - one account exactly: this account for the main rules; the staging account for the rules created there (staging
#     runs its own Security Hub, with no administrator, in us-west-1 and us-east-1); the Dolphin account for the rules
#     created here in us-west-1, where this account is its Security Hub administrator and its findings arrive. Each
#     account's rules list only the controls shown to be by design THERE;
#   - product "Security Hub": only control findings are ever suppressed. GuardDuty and Inspector findings are threats
#     and vulnerabilities, never "by design", and no rule can match them;
#   - an ACTIVE record.
# A rule scoped to resources names them in resource_ids (EQUALS: exact ARNs; PREFIX only for IAM.1's session policies).
# Security Hub limits: at most 20 values per criteria field, rule_order 1..1000 and unique in a region.
# scripts/test-security-hub-automation.py pins all of this.

locals {
  # Rules for every Security Hub region. resource_comparison "" with no resource_ids = account-wide for that control.
  security_hub_rules = [
    {
      # EC2.9 (instance has a public IPv4) is true by design in THIS account only: there is no NAT, so every box here (the
      # app runners, the dev boxes, wbox, the jumpboxes) egresses on its own address, and what may connect in is the
      # security groups' and Cloudflare Access's job, checked by their own controls (EC2.13/14/18/19). Account, not
      # tags: most app-runner findings are recorded before the runner is tagged.
      name                = "ec2-public-ipv4-by-design"
      order               = 1
      description         = "EC2.9 in the main account is the fleet's design: no NAT, so runners and dev boxes egress on their own address; inbound is the security groups' and Cloudflare Access's job, checked by their own controls. Other accounts still alert."
      note                = "Public IPv4 by design in the main account (no NAT); inbound is governed by security groups and Cloudflare Access. security-hub-automation.tf"
      controls            = ["EC2.9"]
      resource_comparison = ""
      resource_ids        = []
    },
    {
      # GuardDuty.5-11: EKS audit logs, Lambda, EKS runtime, EC2 malware scanning, RDS, S3 and runtime monitoring. The
      # detector runs (threat findings still alert); the separately priced protection plans are off for cost
      # (modules/security-region, the detector and its disabled-feature preconditions).
      name                = "guardduty-protection-plans-off-by-design"
      order               = 2
      description         = "GuardDuty.5-11 in the main account: the separately priced GuardDuty protection plans are deliberately off for cost (modules/security-region). The detector runs and its threat findings still alert."
      note                = "GuardDuty protection plans deliberately off for cost; the detector runs and its findings alert. security-hub-automation.tf"
      controls            = ["GuardDuty.5", "GuardDuty.6", "GuardDuty.7", "GuardDuty.8", "GuardDuty.9", "GuardDuty.10", "GuardDuty.11"]
      resource_comparison = ""
      resource_ids        = []
    },
    {
      # Inspector.2 (ECR scanning) and Inspector.3 (Lambda code scanning): the enabler scans EC2 and Lambda dependencies
      # only (modules/security-region, aws_inspector2_enabler), on purpose. Inspector's own CVE findings still alert.
      name                = "inspector-ecr-lambda-code-off-by-design"
      order               = 3
      description         = "Inspector.2 and Inspector.3 in the main account: ECR and Lambda code scanning are deliberately off; Inspector scans EC2 and Lambda dependencies (modules/security-region). Inspector's CVE findings still alert."
      note                = "Inspector ECR and Lambda code scanning deliberately off; EC2 and Lambda dependency scans run and alert. security-hub-automation.tf"
      controls            = ["Inspector.2", "Inspector.3"]
      resource_comparison = ""
      resource_ids        = []
    },
    {
      # Config.1 fails with CONFIG_RECORDER_CUSTOM_ROLE: the recorder writes with our own role (security-config-<region>,
      # modules/security-region) and records three resource types, on purpose. An automation rule cannot match a
      # finding's status reason, so this covers Config.1 in this account; a recorder stopped or deleted still alerts
      # through security-forward-audit (StopConfigurationRecorder, DeleteConfigurationRecorder).
      name                = "config-custom-recorder-role-by-design"
      order               = 4
      description         = "Config.1 in the main account: the recorder uses our own role and records a reviewed set of types (modules/security-region). Stopping or deleting the recorder alerts through the CloudTrail rule instead."
      note                = "Config recorder uses a custom role by design; recorder stop/delete alerts via security-forward-audit. security-hub-automation.tf"
      controls            = ["Config.1"]
      resource_comparison = ""
      resource_ids        = []
    },
    {
      # EC2.25 (launch template assigns a public IP) and ECS.2 (ECS service assigns a public IP): the EC2.9 reason, no
      # NAT. The launch templates are the runners', wbox's and the boxes'; the services are games/mp-router and
      # imagine/imagine. Inbound is their security groups' job.
      name                = "public-ip-assignment-by-design"
      order               = 5
      description         = "EC2.25 and ECS.2 in the main account: launch templates and ECS services assign public IPs because there is no NAT (the EC2.9 reason); inbound is the security groups' job, checked by their own controls."
      note                = "Public IP assignment by design in the main account (no NAT); inbound is governed by security groups. security-hub-automation.tf"
      controls            = ["EC2.25", "ECS.2"]
      resource_comparison = ""
      resource_ids        = []
    },
    {
      # IAM.1 (a policy allows *:*) on nextjs-ct-member-<person> only: STS session policies nextjs-ct-imds passes as
      # PolicyArns (nextjs-ct-user-data.tf, aws_iam_policy.nextjs_ct_member). Each Allows * then Denies, which only
      # narrows nextjs-dev-role, and none is attached to any principal. Any other *:* policy still alerts.
      name                = "nextjs-ct-session-policies-by-design"
      order               = 6
      description         = "IAM.1 on the nextjs-ct-member-* policies only: STS session policies passed as PolicyArns, which can only narrow nextjs-dev-role and are attached to no principal. Any other admin policy still alerts."
      note                = "nextjs-ct-member-* are STS session policies (narrow nextjs-dev-role only; attached to nothing). security-hub-automation.tf"
      controls            = ["IAM.1"]
      resource_comparison = "PREFIX"
      resource_ids        = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:policy/nextjs-ct-member-"]
    },
  ]

  # EC2.18 / EC2.19 (open to the internet) on exactly the groups that open SSH (22) and Eternal Terminal (2022) to the
  # world, by design TODAY: key-only SSH is one of the fleet's two ways in. A new or other group opened to the internet
  # still alerts. The suppression is per group, not per port, so the test pins these groups' public ports to 22 and 2022.
  security_hub_open_ssh_rule = {
    name                = "public-ssh-by-design"
    order               = 7
    description         = "EC2.18 and EC2.19 on the named security groups only: key-only SSH and Eternal Terminal (tcp 22, 2022) are open to the internet by design until the planned Cloudflare VPN replaces public SSH. Any other group still alerts."
    note                = "Key-only SSH/ET open by design until the Cloudflare VPN replaces public SSH; then remove this rule. security-hub-automation.tf"
    controls            = ["EC2.18", "EC2.19"]
    resource_comparison = "EQUALS"
  }
  security_hub_open_ssh_groups = {
    "us-west-1" = concat(
      [aws_security_group.nextjs_dev.arn],
      aws_security_group.firecracker_dev[*].arn,
      aws_security_group.x86_dev[*].arn,
      aws_security_group.jumpbox_2[*].arn,
      [module.pbox_fallback_us_west_1.security_group_arn],
    )
    "us-west-2" = [module.pbox_fallback_us_west_2.security_group_arn]
    "us-east-1" = [module.pbox_fallback_us_east_1.security_group_arn]
  }

  # The staging account runs the same modules/security-region, so the same controls are by design there for the same
  # reasons (the main rules' comments): the GuardDuty protection plans, Inspector's ECR and Lambda code scanning and the
  # custom Config recorder role. Its default groups are emptied instead (default-security-groups.tf, EC2.2).
  security_hub_staging_rules = [for r in local.security_hub_rules : merge(r, {
    description = replace(r.description, "in the main account", "in the staging account")
    note        = replace(r.note, "security-hub-automation.tf", "staging account. security-hub-automation.tf")
  }) if contains(["guardduty-protection-plans-off-by-design", "inspector-ecr-lambda-code-off-by-design", "config-custom-recorder-role-by-design"], r.name)]

  # The Dolphin account (dolphin-security.tf, the same modules/security-region): Inspector's ECR and Lambda code scanning,
  # plus ECS.2 on exactly its Redis service, which takes a public IP only to egress (its group allows no inbound and the
  # account has no NAT; dolphin-aws redis.tf), and Config.1, which fails by design on the recorder's own role. A rule cannot
  # match the finding's status reason, so that one would also hide a stopped or deleted recorder: Dolphin has no
  # security-forward-audit path, so the alarm in its own account (dolphin_config_recorder, dolphin-security.tf) raises
  # that instead. Orders follow this region's main rules.
  security_hub_dolphin_rules = [
    {
      name                = "dolphin-inspector-ecr-lambda-code-off-by-design"
      order               = 8
      description         = "Inspector.2 and Inspector.3 in the Dolphin account: ECR and Lambda code scanning are deliberately off (dolphin-security.tf, modules/security-region). Inspector's CVE findings still alert."
      note                = "Dolphin account: Inspector ECR and Lambda code scanning deliberately off; CVE findings alert. security-hub-automation.tf"
      controls            = ["Inspector.2", "Inspector.3"]
      resource_comparison = ""
      resource_ids        = []
    },
    {
      name                = "dolphin-redis-public-ip-by-design"
      order               = 9
      description         = "ECS.2 on the Dolphin Redis service only: it takes a public IP to egress (no NAT in the account) and its security group allows no inbound (dolphin-aws redis.tf). Any other service still alerts."
      note                = "Dolphin Redis: public IP for egress only, no inbound rule (dolphin-aws redis.tf). security-hub-automation.tf"
      controls            = ["ECS.2"]
      resource_comparison = "EQUALS"
      resource_ids        = ["arn:aws:ecs:us-west-1:${data.aws_caller_identity.dolphin.account_id}:service/dolphin/redis"]
    },
    {
      name                = "dolphin-config-custom-recorder-role-by-design"
      order               = 10
      description         = "Config.1 in the Dolphin account: the recorder uses its own role and records a reviewed set of types (dolphin-security.tf, modules/security-region). Stopping, deleting or changing the recorder alerts dolphin-alerts instead."
      note                = "Dolphin account: Config recorder uses a custom role by design; recorder stop/delete/change alerts dolphin-alerts (dolphin-security.tf). security-hub-automation.tf"
      controls            = ["Config.1"]
      resource_comparison = ""
      resource_ids        = []
    },
  ]

  security_hub_rules_by_region = {
    for region, arns in local.security_hub_open_ssh_groups : region => {
      for rule in concat(local.security_hub_rules, [merge(local.security_hub_open_ssh_rule, { resource_ids = arns })]) :
      rule.name => rule
    }
  }
}

resource "aws_securityhub_automation_rule" "by_design" {
  for_each    = { for name, rule in local.security_hub_rules_by_region["us-west-1"] : name => rule if local.security_posture_enabled }
  rule_name   = each.value.name
  description = each.value.description
  rule_order  = each.value.order
  rule_status = "ENABLED"

  criteria {
    aws_account_id {
      comparison = "EQUALS"
      value      = data.aws_caller_identity.current.account_id
    }
    product_name {
      comparison = "EQUALS"
      value      = "Security Hub"
    }
    dynamic "compliance_security_control_id" {
      for_each = each.value.controls
      content {
        comparison = "EQUALS"
        value      = compliance_security_control_id.value
      }
    }
    dynamic "resource_id" {
      for_each = each.value.resource_ids
      content {
        comparison = each.value.resource_comparison
        value      = resource_id.value
      }
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
        text       = each.value.note
        updated_by = "terraform"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = length(each.value.controls) > 0 && (each.value.resource_comparison == "") == (length(each.value.resource_ids) == 0)
      error_message = "A Security Hub rule names at least one control, and a resource comparison exactly when it names resources: a scoped rule with no resources would suppress the control account-wide."
    }
  }

  depends_on = [module.security_main_us_west_1]
}

resource "aws_securityhub_automation_rule" "by_design_west2" {
  provider    = aws.west2
  for_each    = { for name, rule in local.security_hub_rules_by_region["us-west-2"] : name => rule if local.security_posture_enabled }
  rule_name   = each.value.name
  description = each.value.description
  rule_order  = each.value.order
  rule_status = "ENABLED"

  criteria {
    aws_account_id {
      comparison = "EQUALS"
      value      = data.aws_caller_identity.current.account_id
    }
    product_name {
      comparison = "EQUALS"
      value      = "Security Hub"
    }
    dynamic "compliance_security_control_id" {
      for_each = each.value.controls
      content {
        comparison = "EQUALS"
        value      = compliance_security_control_id.value
      }
    }
    dynamic "resource_id" {
      for_each = each.value.resource_ids
      content {
        comparison = each.value.resource_comparison
        value      = resource_id.value
      }
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
        text       = each.value.note
        updated_by = "terraform"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = length(each.value.controls) > 0 && (each.value.resource_comparison == "") == (length(each.value.resource_ids) == 0)
      error_message = "A Security Hub rule names at least one control, and a resource comparison exactly when it names resources: a scoped rule with no resources would suppress the control account-wide."
    }
  }

  depends_on = [module.security_main_us_west_2]
}

resource "aws_securityhub_automation_rule" "by_design_dr" {
  provider    = aws.dr
  for_each    = { for name, rule in local.security_hub_rules_by_region["us-east-1"] : name => rule if local.security_posture_enabled }
  rule_name   = each.value.name
  description = each.value.description
  rule_order  = each.value.order
  rule_status = "ENABLED"

  criteria {
    aws_account_id {
      comparison = "EQUALS"
      value      = data.aws_caller_identity.current.account_id
    }
    product_name {
      comparison = "EQUALS"
      value      = "Security Hub"
    }
    dynamic "compliance_security_control_id" {
      for_each = each.value.controls
      content {
        comparison = "EQUALS"
        value      = compliance_security_control_id.value
      }
    }
    dynamic "resource_id" {
      for_each = each.value.resource_ids
      content {
        comparison = each.value.resource_comparison
        value      = resource_id.value
      }
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
        text       = each.value.note
        updated_by = "terraform"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = length(each.value.controls) > 0 && (each.value.resource_comparison == "") == (length(each.value.resource_ids) == 0)
      error_message = "A Security Hub rule names at least one control, and a resource comparison exactly when it names resources: a scoped rule with no resources would suppress the control account-wide."
    }
  }

  depends_on = [module.security_main_us_east_1]
}

# ---- the staging account: rules created in its own Security Hub (it has no administrator), us-west-1 and us-east-1.
# data.aws_caller_identity.staging (aws.staging) is dev-staging-bootstrap.tf's.

resource "aws_securityhub_automation_rule" "by_design_staging" {
  provider    = aws.staging
  for_each    = { for rule in local.security_hub_staging_rules : rule.name => rule if local.security_posture_enabled }
  rule_name   = each.value.name
  description = each.value.description
  rule_order  = each.value.order
  rule_status = "ENABLED"

  criteria {
    aws_account_id {
      comparison = "EQUALS"
      value      = data.aws_caller_identity.staging.account_id
    }
    product_name {
      comparison = "EQUALS"
      value      = "Security Hub"
    }
    dynamic "compliance_security_control_id" {
      for_each = each.value.controls
      content {
        comparison = "EQUALS"
        value      = compliance_security_control_id.value
      }
    }
    dynamic "resource_id" {
      for_each = each.value.resource_ids
      content {
        comparison = each.value.resource_comparison
        value      = resource_id.value
      }
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
        text       = each.value.note
        updated_by = "terraform"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = length(each.value.controls) > 0 && (each.value.resource_comparison == "") == (length(each.value.resource_ids) == 0)
      error_message = "A Security Hub rule names at least one control, and a resource comparison exactly when it names resources: a scoped rule with no resources would suppress the control account-wide."
    }
  }

  depends_on = [module.security_staging_us_west_1]
}

resource "aws_securityhub_automation_rule" "by_design_staging_dr" {
  provider    = aws.staging_dr
  for_each    = { for rule in local.security_hub_staging_rules : rule.name => rule if local.security_posture_enabled }
  rule_name   = each.value.name
  description = each.value.description
  rule_order  = each.value.order
  rule_status = "ENABLED"

  criteria {
    aws_account_id {
      comparison = "EQUALS"
      value      = data.aws_caller_identity.staging.account_id
    }
    product_name {
      comparison = "EQUALS"
      value      = "Security Hub"
    }
    dynamic "compliance_security_control_id" {
      for_each = each.value.controls
      content {
        comparison = "EQUALS"
        value      = compliance_security_control_id.value
      }
    }
    dynamic "resource_id" {
      for_each = each.value.resource_ids
      content {
        comparison = each.value.resource_comparison
        value      = resource_id.value
      }
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
        text       = each.value.note
        updated_by = "terraform"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = length(each.value.controls) > 0 && (each.value.resource_comparison == "") == (length(each.value.resource_ids) == 0)
      error_message = "A Security Hub rule names at least one control, and a resource comparison exactly when it names resources: a scoped rule with no resources would suppress the control account-wide."
    }
  }

  depends_on = [module.security_staging_us_east_1]
}

# ---- the Dolphin account: rules created here, its Security Hub administrator in us-west-1, applied to its findings.
data "aws_caller_identity" "dolphin" {
  provider = aws.dolphin
}

resource "aws_securityhub_automation_rule" "by_design_dolphin" {
  for_each    = { for rule in local.security_hub_dolphin_rules : rule.name => rule if local.security_posture_enabled }
  rule_name   = each.value.name
  description = each.value.description
  rule_order  = each.value.order
  rule_status = "ENABLED"

  criteria {
    aws_account_id {
      comparison = "EQUALS"
      value      = data.aws_caller_identity.dolphin.account_id
    }
    product_name {
      comparison = "EQUALS"
      value      = "Security Hub"
    }
    dynamic "compliance_security_control_id" {
      for_each = each.value.controls
      content {
        comparison = "EQUALS"
        value      = compliance_security_control_id.value
      }
    }
    dynamic "resource_id" {
      for_each = each.value.resource_ids
      content {
        comparison = each.value.resource_comparison
        value      = resource_id.value
      }
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
        text       = each.value.note
        updated_by = "terraform"
      }
    }
  }

  lifecycle {
    precondition {
      condition     = length(each.value.controls) > 0 && (each.value.resource_comparison == "") == (length(each.value.resource_ids) == 0)
      error_message = "A Security Hub rule names at least one control, and a resource comparison exactly when it names resources: a scoped rule with no resources would suppress the control account-wide."
    }
  }

  # The Config.1 suppression goes live only with the alarm that stands in for it, and the alarm on its delivery.
  depends_on = [
    module.dolphin_security_us_west_1,
    aws_cloudwatch_event_target.dolphin_config_recorder,
    aws_cloudwatch_metric_alarm.dolphin_config_recorder_delivery,
  ]
}

# #515 created the EC2.9 rule as one counted resource per region; it is now the list's first entry.
moved {
  from = aws_securityhub_automation_rule.public_ipv4_by_design[0]
  to   = aws_securityhub_automation_rule.by_design["ec2-public-ipv4-by-design"]
}

moved {
  from = aws_securityhub_automation_rule.public_ipv4_by_design_west2[0]
  to   = aws_securityhub_automation_rule.by_design_west2["ec2-public-ipv4-by-design"]
}

moved {
  from = aws_securityhub_automation_rule.public_ipv4_by_design_dr[0]
  to   = aws_securityhub_automation_rule.by_design_dr["ec2-public-ipv4-by-design"]
}

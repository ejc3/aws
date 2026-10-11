# Shared prerequisite for the staged backup and security-monitoring deployments.
# Adopt the existing organization; preserve SSO while enabling Backup and CloudTrail.
# Keep this resource at one address/file when the staged branches are integrated.
import {
  to = aws_organizations_organization.security
  id = "o-1d3o5eerdv"
}

resource "aws_organizations_organization" "security" {
  feature_set                   = "ALL"
  aws_service_access_principals = ["sso.amazonaws.com", "cloudtrail.amazonaws.com", "backup.amazonaws.com"]
  enabled_policy_types          = ["AISERVICES_OPT_OUT_POLICY"]
  lifecycle { prevent_destroy = true }
}

# Opt every account out of AWS AI services storing and using what they are sent to improve themselves. Without it
# Amazon may keep and use, for example, the pictures colton-games sends Rekognition (colton-games-moderation.tf), and
# those are kids' photos. Attached at the root, so it covers every account, the staging one included.
resource "aws_organizations_policy" "ai_services_opt_out" {
  name        = "ai-services-opt-out"
  description = "Opt all accounts out of AI services content storage and use for service improvement"
  type        = "AISERVICES_OPT_OUT_POLICY"
  content = jsonencode({
    services = { default = { opt_out_policy = { "@@assign" = "optOut" } } }
  })
}

resource "aws_organizations_policy_attachment" "ai_services_opt_out" {
  policy_id = aws_organizations_policy.ai_services_opt_out.id
  target_id = aws_organizations_organization.security.roots[0].id
}

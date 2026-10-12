# dolphin-region-scp.tf
#
# Regions outside the Dolphin account's monitored three are unusable there, by policy (issue #403). dolphin-security.tf
# runs GuardDuty, Config, Security Hub and the account defaults only in local.dolphin_security_regions (us-west-1,
# us-west-2, us-east-1): all 17 regions meant 16 more provider aliases, and a plan on the 8 GB jumpbox ran out of
# memory. A service control policy on the Dolphin OU now denies every regional action anywhere else, so a credential
# that leaks from the Dolphin account (a dolphin-aws apply on nextjs-dev, a Lambda role, a person's session) cannot
# start an instance, a function or a bucket in a region nobody watches.
#
# - Attached to the Dolphin OU only. The organization root, dev-staging and this (management) account are untouched:
#   SCPs never apply to the management account, and enabling the policy type attaches AWS's FullAWSAccess to the root,
#   every OU and every account, so nothing else changes. That enabling is the one organization-wide step here.
# - Global services are exempt (NotAction): their calls carry us-east-1 or no region, and sign-in, billing and
#   Identity Center must keep working wherever the console sends them.
# - No principal is exempt, OrganizationAccountAccessRole included: an exemption by role name would be a bypass, since
#   an administrator inside the Dolphin account (dolphin-terraform) can rewrite that role's trust policy and assume it.
#   This account's Terraform touches the Dolphin account only in the three regions. Break-glass for anything found
#   elsewhere is detaching this policy from the management account, which SCPs never restrict. Service-linked roles are
#   never subject to SCPs.
# - The region list is local.dolphin_security_regions itself, so monitoring and the policy cannot drift apart.

locals {
  # Services whose actions are global (no region, or us-east-1 whatever the caller's region) and must keep working from
  # any console region. Regional services are deliberately absent: config, kms, acm, ec2 and the rest are denied
  # outside the three regions.
  dolphin_region_scp_global_actions = [
    "account:*",
    "aws-portal:*",
    "billing:*",
    "budgets:*",
    "ce:*",
    "cloudfront:*",
    "consolidatedbilling:*",
    "cur:*",
    "ec2:DescribeRegions",
    "freetier:*",
    "globalaccelerator:*",
    "health:*",
    "iam:*",
    "identitystore:*",
    "identitystore-auth:*",
    "invoicing:*",
    "notifications:*",
    "organizations:*",
    "payments:*",
    "pricing:*",
    "route53:*",
    "route53domains:*",
    "s3:GetAccountPublicAccessBlock",
    "s3:ListAllMyBuckets",
    "shield:*",
    "signin:*",
    "sso:*",
    "sso-directory:*",
    "sso-oauth:*",
    "sts:*",
    "support:*",
    "tax:*",
    "trustedadvisor:*",
    "waf:*",
  ]
  dolphin_region_scp = {
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyOutsideMonitoredRegions"
        Effect    = "Deny"
        NotAction = local.dolphin_region_scp_global_actions
        Resource  = "*"
        Condition = {
          StringNotEquals = { "aws:RequestedRegion" = local.dolphin_security_regions }
        }
      },
      {
        # Actions called in an allowed region that put copies in another one: aws:RequestedRegion sees only the source,
        # and the copy is often made by a service-linked role, which SCPs never cover. The Dolphin account uses none of
        # them; allowing one means adding its destination check here first.
        Sid    = "DenyCrossRegionCopies"
        Effect = "Deny"
        Action = [
          "backup:StartCopyJob",
          "cloudformation:CreateStackInstances",
          "dynamodb:CreateTableReplica",
          "ecr:PutReplicationConfiguration",
          "kms:ReplicateKey",
          "s3:PutReplicationConfiguration",
          "secretsmanager:ReplicateSecretToRegions",
        ]
        Resource = "*"
      },
      {
        # aws:RequestedRegion is the endpoint called, not where a bucket goes: CreateBucket through an allowed endpoint
        # can name another region in LocationConstraint. Denied only when a constraint is given (Null false) and is not
        # one of the three; no constraint means us-east-1, which is allowed.
        Sid      = "DenyBucketsOutsideMonitoredRegions"
        Effect   = "Deny"
        Action   = "s3:CreateBucket"
        Resource = "*"
        Condition = {
          StringNotEquals = { "s3:LocationConstraint" = local.dolphin_security_regions }
          Null            = { "s3:LocationConstraint" = "false" }
        }
      },
    ]
  }
}

resource "aws_organizations_policy" "dolphin_region_deny" {
  name        = "dolphin-region-deny"
  description = "Deny regional actions outside the Dolphin account's monitored regions (dolphin-security.tf)"
  type        = "SERVICE_CONTROL_POLICY"
  content     = jsonencode(local.dolphin_region_scp)
  # The policy type must be enabled on the root first (security-organization.tf).
  depends_on = [aws_organizations_organization.security]
}

resource "aws_organizations_policy_attachment" "dolphin_region_deny" {
  policy_id = aws_organizations_policy.dolphin_region_deny.id
  target_id = aws_organizations_organizational_unit.dolphin.id
}

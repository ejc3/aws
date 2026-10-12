#!/usr/bin/env python3
"""dolphin-region-scp.tf: the Dolphin OU, and only it, cannot use regions dolphin-security.tf does not monitor."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCP = (ROOT / "dolphin-region-scp.tf").read_text()
ORG = (ROOT / "security-organization.tf").read_text()
SEC = (ROOT / "dolphin-security.tf").read_text()
ALL = "\n".join(p.read_text() for p in ROOT.glob("*.tf"))


def block(kind, name, text=SCP):
    m = re.search(r'^resource "%s" "%s" \{\n(.*?)^\}' % (kind, name), text, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class DolphinRegionScpTests(unittest.TestCase):
    def test_the_scp_policy_type_is_enabled_alongside_the_ai_opt_out(self):
        types = re.search(r"enabled_policy_types\s*=\s*\[(.*?)\]", ORG).group(1)
        self.assertEqual(re.findall(r'"([A-Z_]+)"', types), ["AISERVICES_OPT_OUT_POLICY", "SERVICE_CONTROL_POLICY"])

    def test_attached_to_the_dolphin_ou_and_nowhere_else(self):
        att = block("aws_organizations_policy_attachment", "dolphin_region_deny")
        self.assertIn("target_id = aws_organizations_organizational_unit.dolphin.id", att)
        scp_attachments = re.findall(r'policy_id\s*=\s*aws_organizations_policy\.dolphin_region_deny\.id', ALL)
        self.assertEqual(len(scp_attachments), 1)
        self.assertNotIn("roots[0]", att)
        self.assertNotIn("dev_staging", att)

    def test_regions_are_exactly_the_monitored_ones(self):
        self.assertIn('StringNotEquals = { "aws:RequestedRegion" = local.dolphin_security_regions }', SCP)
        regions = re.findall(r'"([a-z]{2}-[a-z]+-\d)"', re.search(r"dolphin_security_regions = \[(.*?)\]", SEC).group(1))
        self.assertEqual(regions, ["us-west-1", "us-west-2", "us-east-1"])

    def test_deny_with_global_services_exempt_and_regional_ones_not(self):
        policy = block("aws_organizations_policy", "dolphin_region_deny")
        self.assertIn('type        = "SERVICE_CONTROL_POLICY"', policy)
        self.assertIn('Effect    = "Deny"', SCP)
        self.assertIn("NotAction = local.dolphin_region_scp_global_actions", SCP)
        actions = re.findall(r'^\s+"([a-z0-9-]+:[A-Za-z*]+)",$', SCP, re.M)
        for needed in ("iam:*", "sts:*", "organizations:*", "sso:*", "identitystore:*", "sso-oauth:*", "signin:*",
                       "cloudfront:*", "route53:*", "support:*", "health:*", "billing:*"):
            self.assertIn(needed, actions)
        for regional in ("ec2:*", "s3:*", "lambda:*", "kms:*", "config:*", "guardduty:*", "acm:*", "bedrock:*"):
            self.assertNotIn(regional, actions)

    def test_no_principal_is_exempt(self):
        # A role-name exemption is a bypass: an administrator in the Dolphin account can rewrite that role's trust.
        self.assertNotIn("aws:PrincipalARN", SCP)
        self.assertNotIn("aws:PrincipalArn", SCP)
        self.assertNotRegex(SCP, r"ArnNotLike|ArnNotEquals|StringNotLike")


if __name__ == "__main__":
    unittest.main(verbosity=2)

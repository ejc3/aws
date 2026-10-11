#!/usr/bin/env python3
"""claw-master-dashboard-site.tf: Access on the dashboard Worker admits the owner's named people first, then exactly
one service token for metal-box agents (#374), whose secret only administration and dev-server-role can read."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "claw-master-dashboard-site.tf").read_text()


def block(kind, name):
    return re.search(r'%s "%s" \{.*?\n\}\n' % (re.escape(kind), re.escape(name)), TF, re.S).group()


class AccessTests(unittest.TestCase):
    def test_people_first_then_the_agents_token(self):
        app = block('resource "cloudflare_zero_trust_access_application"', "claw_master_dashboard")
        owner = app.index("claw_master_dashboard_owner.id")
        agents = app.index("claw_master_dashboard_agents.id")
        self.assertLess(owner, agents)
        self.assertRegex(app[owner:agents], r"precedence = 1")
        self.assertRegex(app[agents:], r"precedence = 2")
        self.assertEqual(app.count("policy."), 2)

    def test_the_agents_policy_admits_only_its_own_token(self):
        pol = block('resource "cloudflare_zero_trust_access_policy"', "claw_master_dashboard_agents")
        self.assertIn('decision         = "non_identity"', pol)
        self.assertEqual(pol.count("service_token = {"), 1)
        self.assertIn("cloudflare_zero_trust_access_service_token.claw_master_dashboard_agents.id", pol)
        self.assertNotIn("email", pol)
        self.assertNotIn("everyone", pol)

    def test_only_administration_and_the_metal_boxes_read_the_token(self):
        pol = block('resource "aws_secretsmanager_secret_policy"', "claw_master_dashboard_agent_access")
        self.assertIn('Effect    = "Deny"', pol)
        self.assertIn("concat(local.games_mp_admin_principals, [aws_iam_role.dev_server.arn])", pol)
        self.assertNotIn("nextjs_dev", TF)
        grant = block('resource "aws_iam_role_policy"', "dev_server_claw_master_dashboard_access")
        self.assertIn("Resource = aws_secretsmanager_secret.claw_master_dashboard_agent_access.arn", grant)

    def test_the_worker_learns_the_client_id_not_the_secret(self):
        out = block("output", "claw_master_dashboard")
        self.assertIn("agent_service_token_client_id = cloudflare_zero_trust_access_service_token.claw_master_dashboard_agents.client_id", out)
        self.assertNotIn("client_secret", out)


class ReaderKeyTests(unittest.TestCase):
    def test_the_worker_key_reads_metrics_and_alarms_in_one_region_only(self):
        pol = block('resource "aws_iam_user_policy"', "claw_master_dashboard")
        self.assertIn('Action    = ["cloudwatch:GetMetricData", "cloudwatch:ListMetrics"]', pol)
        # Alarm reads: this account's alarms in this region (AWS authorizes DescribeAlarms against alarm:*, so a name
        # prefix cannot narrow it).
        self.assertIn('Action   = "cloudwatch:DescribeAlarms"', pol)
        self.assertIn(':alarm:*"', pol)
        self.assertIn('Condition = { StringEquals = { "aws:RequestedRegion" = var.aws_region } }', pol)
        self.assertNotRegex(pol, r"cloudwatch:(Put|Delete|Set|Enable|Disable)")

if __name__ == "__main__":
    unittest.main()

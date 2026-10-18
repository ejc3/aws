#!/usr/bin/env python3
"""claw-master-dashboard-site.tf: Access on the dashboard Worker admits the owner's named people first, then exactly
one service token for metal-box agents (#374), whose secret only administration and dev-server-role can read; the
Worker's own allowlist is written by Terraform from the same list."""
import hashlib
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


class AllowlistTests(unittest.TestCase):
    """One list of people: the Access policy and the Worker's own check both come from people/addresses, through one
    local, and Terraform is the only writer of the Worker's copy (a KV value; v5 has no Worker-secret resource)."""

    LOCAL = "local.claw_master_dashboard_people"

    def test_one_local_reads_people_and_both_consumers_read_it(self):
        # Exactly one expression in this file reads people/addresses: the list's definition.
        self.assertEqual(len(re.findall(r"local\.people\.", TF)), 2, "only the owner and the named list")
        self.assertRegex(
            TF,
            r"claw_master_dashboard_people = concat\(\[nonsensitive\(local\.people\.owner\)\], "
            r"try\(nonsensitive\(local\.people\.claude_master_dashboard\), \[\]\)\)",
        )
        pol = block('resource "cloudflare_zero_trust_access_policy"', "claw_master_dashboard_owner")
        self.assertIn("include = [for e in %s : { email = { email = e } }]" % self.LOCAL, pol)
        kv = block('resource "cloudflare_workers_kv"', "claw_master_dashboard_allowed_emails")
        self.assertIn('value = sensitive(join(",", %s))' % self.LOCAL, kv)

    def test_the_kv_value_is_the_key_the_worker_reads_in_its_own_namespace(self):
        kv = block('resource "cloudflare_workers_kv"', "claw_master_dashboard_allowed_emails")
        self.assertIn('key_name     = "allowed-emails"', kv)
        self.assertIn("namespace_id = cloudflare_workers_kv_namespace.claw_master_dashboard_config.id", kv)
        self.assertNotIn("metadata", kv)

    def test_the_namespace_survives_and_its_id_is_an_output_but_the_list_is_not(self):
        ns = block('resource "cloudflare_workers_kv_namespace"', "claw_master_dashboard_config")
        self.assertIn("prevent_destroy = true", ns)
        out = block("output", "claw_master_dashboard")
        self.assertIn("allowlist_kv_namespace_id = cloudflare_workers_kv_namespace.claw_master_dashboard_config.id", out)
        self.assertNotIn("claw_master_dashboard_people", out)
        self.assertNotIn("cloudflare_workers_kv.", out)
        self.assertNotIn("local.people", out)

    def test_no_second_list_is_described_as_set_by_hand(self):
        self.assertNotIn("must name the same people", TF)
        self.assertNotRegex(TF, r"against DASHBOARD_ALLOWED_EMAILS")
        self.assertIn("Terraform writes that key from the same local as the Access policy", TF)

    def test_the_cloudflare_provider_is_the_default_one(self):
        # The KV writes go through the provider token (Workers KV Storage Write added for this), never the token
        # minter or the Workers Builds alias.
        for kind, name in (
            ('resource "cloudflare_workers_kv_namespace"', "claw_master_dashboard_config"),
            ('resource "cloudflare_workers_kv"', "claw_master_dashboard_allowed_emails"),
        ):
            self.assertNotRegex(block(kind, name), r"(?m)^\s*provider\s*=")


class AccountAliasTests(unittest.TestCase):
    """The Worker's account aliases are derived from claude-master/account-labels with claw-master's own rules
    (ejc3/CLIProxyAPI internal/clawmaster/metrics.go AccountKeyFor and sanitizeDimension, cmd/claw-master/main.go
    loadAccountLabels), so that secret is the one place to name an account."""

    # Made-up account ids and the codes `claw-master account-key` (release claw-master-39add35) printed for them.
    CLAW_MASTER_SAYS = {
        "12345678-90ab-cdef-1234-567890abcdef": "acct-ed043f31",
        "0F0F0F0F-AAAA-BBBB-CCCC-DDDDEEEEFFFF": "acct-7eb455f9",
        "aaaaaaaa-0000-0000-0000-000000000000": "acct-80e75d69",
    }

    @staticmethod
    def account_key(account_id):
        # What the Terraform expression computes: "acct-${substr(sha256(lower(id)), 0, 8)}" (sha256() is hex).
        return "acct-" + hashlib.sha256(account_id.strip().lower().encode()).hexdigest()[:8]

    def test_the_algorithm_reproduces_claw_masters_codes(self):
        for account_id, code in self.CLAW_MASTER_SAYS.items():
            self.assertEqual(self.account_key(account_id), code, account_id)

    def test_terraform_hashes_the_lowercased_id_and_keeps_eight_hex_digits(self):
        self.assertIn("lower(p[0]) => p[1]...", TF)
        self.assertIn('"acct-${substr(sha256(id), 0, 8)}=', TF)

    def test_names_are_sanitized_as_the_server_reports_them(self):
        # sanitizeDimension: runs of other characters become "-", at most 48 characters.
        self.assertIn('substr(replace(names[length(names) - 1], "/[^A-Za-z0-9._-]+/", "-"), 0, 48)', TF)

    def test_lines_are_read_by_load_account_labels_rules(self):
        self.assertIn('startswith(l, "#") ? []', TF)
        self.assertIn('regexall("^([0-9A-Fa-f-]{8,64})\\\\s*=\\\\s*(.*\\\\S)$", l)', TF)
        self.assertIn('regexall("^([0-9A-Fa-f-]{8,64})\\\\s+(.*\\\\S)$", l)', TF)
        self.assertIn('replace(try(data.aws_secretsmanager_secret_version.claw_master_account_labels[0].secret_string, ""), "\\r", "")', TF)

    def test_written_sensitive_to_its_own_key_and_never_output(self):
        kv = block('resource "cloudflare_workers_kv"', "claw_master_dashboard_account_aliases")
        self.assertIn('key_name     = "account-aliases"', kv)
        self.assertIn("namespace_id = cloudflare_workers_kv_namespace.claw_master_dashboard_config.id", kv)
        self.assertIn("value = local.claw_master_dashboard_account_aliases", kv)
        self.assertIn("claw_master_dashboard_account_aliases = sensitive(join(", TF)
        out = block("output", "claw_master_dashboard")
        self.assertNotIn("account_label", out)
        self.assertNotIn("aliases", out)

    def test_the_secret_is_read_only_where_the_server_exists(self):
        data = block('data "aws_secretsmanager_secret_version"', "claw_master_account_labels")
        self.assertIn("count     = var.enable_claw_master_server ? 1 : 0", data)
        self.assertIn("secret_id = aws_secretsmanager_secret.claw_master_account_labels[0].id", data)


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

#!/usr/bin/env python3
"""The Dolphin member account (dolphin-account.tf): its own OU at the org root, a root address read from the people/addresses
secret and never written in git, and guarded against being replaced, destroyed or closed from Terraform. Its bootstrap
(dolphin-bootstrap.tf): a protected state bucket and an apply role only nextjs-dev may assume, with nothing in this account
trusting the Dolphin account. Its sign-in (dolphin-identity-center.tf): the owner as administrator, Steve read-only with no
secret values, and Steve's user only once his address is in the people secret."""
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = (ROOT / "dolphin-account.tf").read_text()
PEOPLE = (ROOT / "people.tf").read_text()
BOOT = (ROOT / "dolphin-bootstrap.tf").read_text()
IDC = (ROOT / "dolphin-identity-center.tf").read_text()


def block(text, header):
    m = re.search(re.escape(header) + r" \{\n.*?\n\}\n", text, re.S)
    assert m, header
    return m.group(0)


class DolphinAccountTests(unittest.TestCase):
    def test_its_own_ou_at_the_org_root(self):
        ou = block(ACCOUNT, 'resource "aws_organizations_organizational_unit" "dolphin"')
        self.assertIn('name      = "Dolphin"', ou)
        self.assertIn("parent_id = data.aws_organizations_organization.current.roots[0].id", ou)

    def test_the_account_is_guarded_and_never_closed_by_terraform(self):
        acct = block(ACCOUNT, 'resource "aws_organizations_account" "dolphin"')
        self.assertIn('name      = "dolphin"', acct)
        self.assertIn("parent_id = aws_organizations_organizational_unit.dolphin.id", acct)
        self.assertIn('role_name = "OrganizationAccountAccessRole"', acct)
        self.assertIn("close_on_deletion = false", acct)
        self.assertIn("prevent_destroy = true", acct)

    def test_the_root_address_comes_from_the_people_secret(self):
        acct = block(ACCOUNT, 'resource "aws_organizations_account" "dolphin"')
        self.assertIn("email     = local.dolphin_account_email", acct)
        self.assertRegex(PEOPLE, r"  dolphin_account_email\s+= nonsensitive\(local\.people\.dolphin_account\)")
        shape = block(PEOPLE, 'resource "terraform_data" "people_shape"')
        self.assertIn("can(local.people.dolphin_account)", shape)  # a missing key fails the plan, not the account
        self.assertIn("local.people.dolphin_account))", shape)  # and it must be one address
        self.assertNotIn("@", ACCOUNT)

    def test_the_org_trail_can_encrypt_its_events_without_the_account_being_trusted(self):
        mon = (ROOT / "security-monitoring.tf").read_text()
        ids = re.search(r"  security_account_ids = \[\n(.*?)\n  \]", mon, re.S).group(1)
        self.assertNotIn("dolphin", ids)  # that list admits an account's principals to the Config bucket and key
        self.assertIn('concat(local.security_account_ids, [aws_organizations_account.dolphin.id]) : "arn:aws:cloudtrail:*:${account}:trail/*"', mon)
        # The other use: security-alerts-notify matches the Dolphin id in events this account's own rules forward
        # (an administrator's copy of a member finding). A pattern, not a grant.
        self.assertIn("event_pattern = jsonencode({ account = concat(local.security_account_ids, [aws_organizations_account.dolphin.id]) })", mon)
        self.assertEqual(mon.count("aws_organizations_account.dolphin"), 2)


class DolphinBootstrapTests(unittest.TestCase):
    def test_everything_in_the_dolphin_account_goes_through_the_org_access_role(self):
        prov = block(BOOT, 'provider "aws"')
        self.assertIn('alias  = "dolphin"', prov)
        self.assertIn('role_arn = "arn:aws:iam::${aws_organizations_account.dolphin.id}:role/OrganizationAccountAccessRole"', prov)
        for m in re.finditer(r'resource "(aws_[a-z0-9_]+)" "([a-z0-9_]+)" \{\n(.*?)\n\}\n', BOOT, re.S):
            kind, name, body = m.groups()
            if (kind, name) == ("aws_iam_role_policy", "nextjs_dev_dolphin_terraform"):
                self.assertNotIn("provider", body)  # the one management-account resource
            else:
                self.assertIn("provider = aws.dolphin", re.sub(r"\s+", " ", body), name)

    def test_the_state_bucket_is_protected_versioned_encrypted_and_private(self):
        self.assertIn('dolphin_state_bucket       = "dolphin-terraform-state-${aws_organizations_account.dolphin.id}"', BOOT)
        self.assertIn("prevent_destroy = true", block(BOOT, 'resource "aws_s3_bucket" "dolphin_terraform_state"'))
        self.assertIn('status = "Enabled"', block(BOOT, 'resource "aws_s3_bucket_versioning" "dolphin_terraform_state"'))
        self.assertIn('sse_algorithm = "AES256"', BOOT)
        pab = block(BOOT, 'resource "aws_s3_bucket_public_access_block" "dolphin_terraform_state"')
        for flag in ("block_public_acls", "ignore_public_acls", "block_public_policy", "restrict_public_buckets"):
            self.assertRegex(pab, flag + r"\s+= true")
        self.assertNotIn("aws_dynamodb_table", BOOT)  # S3 native locking

    def test_state_objects_are_for_the_appliers_only(self):
        pol = block(BOOT, 'resource "aws_s3_bucket_policy" "dolphin_terraform_state"')
        self.assertIn('"aws:SecureTransport" = "false"', pol)
        self.assertIn('Sid       = "StateObjectsForAppliersOnly"', pol)
        self.assertIn('"s3:GetObject"', pol)
        self.assertIn("ArnNotLike", pol)
        self.assertIn("local.dolphin_terraform_role_arn", pol)
        self.assertIn("local.dolphin_sso_admin_role_arns", pol)
        self.assertNotIn("ReadOnly", pol)

    def test_the_apply_role_trusts_exactly_nextjs_dev_and_lasts_an_hour(self):
        role = block(BOOT, 'resource "aws_iam_role" "dolphin_terraform"')
        self.assertIn('name                 = "dolphin-terraform"', role)
        self.assertIn("max_session_duration = 3600", role)
        self.assertEqual(role.count("Principal"), 1)
        self.assertIn("Principal = { AWS = aws_iam_role.nextjs_dev.arn }", role)
        self.assertIn('Action    = "sts:AssumeRole"', role)
        self.assertNotIn("Federated", role)  # no CI identity: GitHub never applies
        self.assertIn('policy_arn = "arn:aws:iam::aws:policy/AdministratorAccess"',
                      block(BOOT, 'resource "aws_iam_role_policy_attachment" "dolphin_terraform_admin"'))

    def test_nextjs_dev_may_assume_that_role_and_nothing_else(self):
        pol = block(BOOT, 'resource "aws_iam_role_policy" "nextjs_dev_dolphin_terraform"')
        self.assertIn("role = aws_iam_role.nextjs_dev.id", pol)
        self.assertEqual(pol.count("Effect"), 1)
        self.assertIn('Action   = "sts:AssumeRole"', pol)
        self.assertIn("Resource = aws_iam_role.dolphin_terraform.arn", pol)
        self.assertNotIn("*", pol)

    def test_nothing_in_the_management_account_trusts_the_dolphin_account(self):
        for path in ROOT.glob("*.tf"):
            if path.name in ("dolphin-bootstrap.tf", "dolphin-account.tf", "dolphin-identity-center.tf", "dolphin-security.tf"):
                continue
            if path.name == "security-monitoring.tf":  # the trail key's encryption context only, pinned above
                continue
            if path.name == "dolphin-region-scp.tf":
                # The SCP restricts the Dolphin account; its id appears only in the one inference-profile ARN its deny
                # exempts, which grants the Dolphin account nothing here.
                text = path.read_text()
                self.assertEqual(text.count("aws_organizations_account.dolphin"), 1, path.name)
                self.assertIn('dolphin_haiku_profile_arn = "arn:aws:bedrock:us-west-2:${aws_organizations_account.dolphin.id}:inference-profile/', text)
                self.assertNotIn("local.dolphin_account_id", text, path.name)
                self.assertNotRegex(text, r'Principal|sts:AssumeRole|"Allow"', path.name)
                continue
            text = path.read_text()
            self.assertNotIn("aws_organizations_account.dolphin", text, path.name)
            self.assertNotIn("local.dolphin_account_id", text, path.name)


class DolphinSecurityTests(unittest.TestCase):
    def test_monitoring_grants_the_dolphin_account_nothing_here(self):
        text = (ROOT / "dolphin-security.tf").read_text()
        # The resource policies are in the Dolphin account, for its own services: the Config bucket's, and dolphin-alerts'
        # (its default same-account statement plus that account's own Config recorder rule, #564); the one role is this
        # account's own, trusted only by this account's EventBridge rule.
        self.assertEqual(text.count("Principal = "), 6)
        self.assertEqual(text.count('resource "aws_iam_role" '), 1)
        role = block(text, 'resource "aws_iam_role" "dolphin_security_inspector"')
        self.assertNotIn("provider", role)
        self.assertIn('Principal = { Service = "events.amazonaws.com" }', role)
        self.assertIn('"aws:SourceAccount" = data.aws_caller_identity.current.account_id', role)
        self.assertEqual(text.count('resource "aws_s3_bucket_policy"'), 1)
        policy = block(text, 'resource "aws_s3_bucket_policy" "dolphin_security_config"')
        self.assertIn("provider = aws.dolphin\n", policy)
        self.assertEqual(policy.count("Principal = "), 3)
        self.assertEqual(text.count('resource "aws_sns_topic_policy"'), 1)
        topic = block(text, 'resource "aws_sns_topic_policy" "dolphin_alerts"')
        self.assertIn("provider = aws.dolphin\n", topic)
        self.assertEqual(topic.count("Principal = "), 2)
        self.assertIn('"AWS:SourceOwner" = data.aws_caller_identity.dolphin.account_id', topic)
        self.assertIn('"aws:SourceArn" = aws_cloudwatch_event_rule.dolphin_config_recorder.arn', topic)
        self.assertNotIn("current", topic)
        self.assertIn("provider    = aws.dolphin\n", block(text, 'resource "aws_cloudwatch_event_rule" "dolphin_config_recorder"'))
        for forbidden in ("aws_cloudwatch_event_bus_policy",
                          "aws_sqs_queue_policy", "aws_kms_key", "aws_lambda_permission"):
            self.assertNotIn('resource "' + forbidden, text)


class DolphinIdentityCenterTests(unittest.TestCase):
    def test_the_owner_is_administrator_through_the_existing_permission_set(self):
        self.assertIn('data "aws_ssoadmin_permission_set" "administrator"', IDC)
        self.assertNotIn('resource "aws_ssoadmin_permission_set" "administrator"', IDC)  # read, never managed
        self.assertIn('attribute_value = "ejc3"', IDC)
        a = block(IDC, 'resource "aws_ssoadmin_account_assignment" "dolphin_owner_admin"')
        self.assertIn("permission_set_arn = data.aws_ssoadmin_permission_set.administrator.arn", a)
        self.assertIn("target_id          = aws_organizations_account.dolphin.id", a)

    def test_steve_is_read_only_for_eight_hours_and_reads_no_secret_value(self):
        ps = block(IDC, 'resource "aws_ssoadmin_permission_set" "dolphin_read_only"')
        self.assertIn('name             = "DolphinReadOnly"', ps)
        self.assertIn('session_duration = "PT8H"', ps)
        self.assertIn('managed_policy_arn = "arn:aws:iam::aws:policy/ReadOnlyAccess"', IDC)
        self.assertEqual(IDC.count("aws_ssoadmin_managed_policy_attachment"), 1)
        inline = block(IDC, 'resource "aws_ssoadmin_permission_set_inline_policy" "dolphin_read_only"')
        self.assertNotIn('"Allow"', inline)
        for action in ("secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue", "ssm:GetParameter", "kms:Decrypt", "s3:GetObject",
                       "lambda:GetFunction", "lambda:GetFunctionConfiguration", "ecs:DescribeTaskDefinition",
                       "ec2:DescribeInstanceAttribute", "ec2:GetLaunchTemplateData"):
            self.assertIn('"%s"' % action, inline)
        self.assertIn("${local.dolphin_state_bucket}/*", inline)

    def test_steves_user_and_assignment_wait_for_his_address(self):
        self.assertIn('dolphin_steve_email = nonsensitive(try(tostring(local.people.dolphin_steve), ""))', IDC)
        self.assertIn("user_name         = local.dolphin_steve_email", block(IDC, 'resource "aws_identitystore_user" "dolphin_steve"'))
        for header in ('resource "aws_identitystore_user" "dolphin_steve"',
                       'resource "aws_ssoadmin_account_assignment" "dolphin_steve_read_only"'):
            self.assertIn('count             = local.dolphin_steve_email == "" ? 0 : 1'.replace("             ", " " * (13 if "user" in header else 14)), block(IDC, header))
        a = block(IDC, 'resource "aws_ssoadmin_account_assignment" "dolphin_steve_read_only"')
        self.assertIn("permission_set_arn = aws_ssoadmin_permission_set.dolphin_read_only.arn", a)
        self.assertIn("target_id          = aws_organizations_account.dolphin.id", a)
        self.assertNotIn("administrator", a)
        self.assertNotIn("@", re.sub(r'regex\("[^"]*"', "", IDC))  # no address, only the shape check

    def test_steve_is_assigned_to_nothing_but_the_dolphin_account(self):
        targets = re.findall(r"target_id\s+= (.+)", IDC)
        self.assertEqual(set(targets), {"aws_organizations_account.dolphin.id"})


class StarterCheckoutTests(unittest.TestCase):
    """nextjs-dev clones dolphin-aws for skevh and ejc3, and the rendered setup script stays valid bash."""

    SETUP = (ROOT / "nextjs-user-data.tf").read_text()

    def test_both_accounts_get_the_dolphin_aws_checkout(self):
        repos = re.search(r"  nextjs_user_repos = \{\n(.*?)\n  \}\n", self.SETUP, re.S).group(1)
        for user in ("skevh", "ejc3"):
            self.assertRegex(repos, r'%s\s+= \[[^\]]*"dolphin-labs-hq/dolphin-aws"' % user)

    def test_the_clone_loop_is_bash_not_a_nested_template_loop(self):
        # A template for-loop nested in the per-user case arm welds one repository's `fi` onto the next one's `if`
        # (the ~ markers eat the newline): `fi        if` is a syntax error, and setup-sync refuses the whole script.
        start = self.SETUP.index('    case "$u" in\n%{~for ru, rl in local.nextjs_user_repos~}')
        end = self.SETUP.index("    esac\n", start)
        arm = self.SETUP[start:end]
        self.assertEqual(arm.count("%{~for"), 1, "only the per-user loop may be a template loop")
        self.assertIn('${ru}) starter_repos="${join(" ", rl)}" ;;', arm)
        loop = self.SETUP[end:self.SETUP.index("    done\n", end)]
        self.assertIn("for repo in $starter_repos; do", loop)
        self.assertIn('gh repo clone "$repo" "/home/$u/$${repo##*/}"', loop)
        self.assertIn('    starter_repos=""\n    case "$u" in', self.SETUP)  # set -u: defined for every user


if __name__ == "__main__":
    unittest.main(buffer=True)

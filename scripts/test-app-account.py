#!/usr/bin/env python3
"""Offline checks for modules/app-account (one member account per app, ejc3/constellation docs/APP-ACCOUNTS.md) and its
first caller, family-account.tf: as test-astryx-account.py pins Astryx. The account is never closed or replaced by
Terraform; its OU is fenced to one region; one exact apply role trusts fcvm-arm (pinned) and the owner's container only,
and member containers cannot read its pointer; state is for the appliers; on Cloudflare only the sign-in path is behind
Access (checked again at the connector), the API gets exactly its own paths, zero-cache the rest, and the API's
/family/zero/* (zero-cache's calls on the task's localhost) is never routed."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
MOD = ROOT / "modules" / "app-account"
ACCOUNT = (MOD / "account.tf").read_text()
STAGE = (MOD / "stage.tf").read_text()
EMAIL = (MOD / "email.tf").read_text()
VARS = (MOD / "variables.tf").read_text()
OUT = (MOD / "outputs.tf").read_text()
FAMILY = (ROOT / "family-account.tf").read_text()
CONST = (ROOT / "constellation-account.tf").read_text()
PARITY = (ROOT / "nextjs-dev-fcvm-parity.tf").read_text()
CT = (ROOT / "nextjs-ct-user-data.tf").read_text()
PEOPLE = (ROOT / "people.tf").read_text()


def block(header, text):
    start = text.index(header)
    depth = 0
    for i in range(text.index("{", start), len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    raise AssertionError(header)


def family_setting(name):
    m = re.search(rf'^\s*{name}\s*=\s*"([^"]+)"', FAMILY, re.M)
    if not m:
        raise AssertionError(name)
    return m.group(1)


class AccountTests(unittest.TestCase):
    def test_the_account_is_never_closed_or_replaced_by_terraform(self):
        account = block('resource "aws_organizations_account" "this"', ACCOUNT)
        self.assertIn("close_on_deletion          = false", account)
        self.assertIn("prevent_destroy = true", account)
        self.assertIn('iam_user_access_to_billing = "DENY"', account)
        self.assertIn("parent_id                  = aws_organizations_organizational_unit.this.id", account)

    def test_its_root_address_comes_from_people_addresses_and_is_never_written(self):
        self.assertIn("family_account_email        = nonsensitive(local.people.family_account)", PEOPLE)
        self.assertIn("can(local.people.family_account)", PEOPLE)
        self.assertRegex(FAMILY, r"\n  email\s+= local\.family_account_email\n")
        self.assertIn("sensitive   = true", block('variable "email"', VARS))
        for text in (ACCOUNT, STAGE, VARS, OUT, FAMILY):
            self.assertNotRegex(text, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")

    def test_the_provider_goes_through_the_access_role_of_that_account(self):
        prov = block('provider "aws" {\n  alias  = "family"', FAMILY)
        self.assertIn('role_arn = "arn:aws:iam::${module.family_account.account_id}:role/OrganizationAccountAccessRole"', prov)
        self.assertIn("aws.app = aws.family", FAMILY)

    def test_the_guardrail_scp_follows_the_organization_resource(self):
        # The root id from the resource, not the data source: the SCP follows the policy types being enabled.
        self.assertIn("organization_root_id = aws_organizations_organization.security.roots[0].id", FAMILY)


class GuardrailTests(unittest.TestCase):
    def test_region_leave_and_iam_users_are_denied_on_the_apps_ou_only(self):
        policy = block('resource "aws_organizations_policy" "guardrails"', ACCOUNT)
        for sid in ("DenyOutsideTheAppsRegion", "DenyLeavingTheOrganization", "DenyIamUsersAndKeys", "DenyBucketsOutsideTheAppsRegion"):
            self.assertIn(f'Sid       = "{sid}"' if sid == "DenyOutsideTheAppsRegion" else f'Sid      = "{sid}"', policy)
        self.assertIn("NotAction = var.global_actions", policy)
        self.assertIn('Condition = { StringNotEquals = { "aws:RequestedRegion" = [var.region] } }', policy)
        self.assertIn('Action   = ["iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile", "iam:UpdateLoginProfile"]', policy)
        attach = block('resource "aws_organizations_policy_attachment" "guardrails"', ACCOUNT)
        self.assertIn("target_id = aws_organizations_organizational_unit.this.id", attach)
        self.assertIn("global_actions       = local.dolphin_region_scp_global_actions", FAMILY)


class ApplyRoleTests(unittest.TestCase):
    def test_the_apply_role_trusts_only_fcvm_arm_pinned_and_the_owners_container(self):
        role = block('resource "aws_iam_role" "terraform"', ACCOUNT)
        self.assertEqual(role.count("sts:AssumeRole"), 2)
        self.assertIn("Principal = { AWS = var.appliers.dev_server_role_arn }", role)
        self.assertIn('Condition = { StringEquals = { "ec2:SourceInstanceARN" = var.appliers.applier_instance_arn } }', role)
        self.assertIn("Principal = { AWS = var.appliers.nextjs_dev_role_arn }", role)
        self.assertIn("max_session_duration = 3600", role)
        self.assertIn("provider             = aws.app", role)
        self.assertIn("dev_server_role_arn  = aws_iam_role.dev_server.arn", FAMILY)
        self.assertIn("applier_instance_arn = local.constellation_applier_instance_arn", FAMILY)
        self.assertIn("nextjs_dev_role_arn  = aws_iam_role.nextjs_dev.arn", FAMILY)

    def test_fcvm_arms_grants_name_exactly_this_role_and_pointer_with_the_pin(self):
        for sid, resource in (("AssumeTheFamilyApplyRoleOnly", "module.family_account.terraform_role_arn"),
                              ("ReadTheFamilyPointerOnly", "module.family_account.pointer_arn")):
            rest = CONST.split(f'Sid       = "{sid}"', 1)[1].split("}, {", 1)[0]
            self.assertIn(f"Resource  = {resource}", rest)
            self.assertIn('Condition = { StringEquals = { "ec2:SourceInstanceARN" = local.constellation_applier_instance_arn } }', rest)

    def test_the_owners_container_gets_the_role_and_pointer_and_members_do_not(self):
        self.assertIn("Resource = module.family_account.terraform_role_arn", PARITY.split('Sid      = "FamilyApply"', 1)[1].split("}", 1)[0])
        self.assertIn("Resource = module.family_account.pointer_arn", PARITY.split('Sid      = "FamilyPointer"', 1)[1].split("}", 1)[0])
        deny = CT.split('Sid    = "OwnerOnlyApplyPointers"', 1)[1].split("},", 1)[0]
        self.assertIn('Effect = "Deny"', deny)
        self.assertIn("module.family_account.pointer_name", deny)

    def test_nothing_gives_a_dev_box_the_organization_or_the_access_role(self):
        for text in (FAMILY, ACCOUNT, STAGE):
            self.assertNotIn("organizations:*", text)
        self.assertNotRegex(PARITY + CONST, r"OrganizationAccountAccessRole[^\n]*family")


class StateAndPointerTests(unittest.TestCase):
    def test_state_objects_are_for_the_appliers_only(self):
        policy = block('resource "aws_s3_bucket_policy" "state"', ACCOUNT)
        self.assertIn('Condition = { ArnNotLike = { "aws:PrincipalArn" = [local.terraform_role_arn, local.access_role_arn] } }', policy)
        self.assertIn('Condition = { Bool = { "aws:SecureTransport" = "false" } }', policy)
        self.assertIn("prevent_destroy = true", block('resource "aws_s3_bucket" "state"', ACCOUNT))

    def test_the_pointer_holds_no_credential(self):
        pointer = block("pointer = {", ACCOUNT)
        keys = set(re.findall(r"^\s+(\w+)\s+=", pointer, re.M))
        self.assertEqual(keys, {"account_id", "region", "state_bucket", "terraform_role", "stage", "email"})
        stage = block("stage_pointer = local.stage_on ? {", STAGE)
        self.assertEqual(set(re.findall(r"^\s+(\w+)\s+=", stage, re.M)), {"hostname", "access_team_domain", "access_aud", "tunnel_secret_name"})
        email = block("email_pointer = local.email_on ? {", EMAIL)
        self.assertEqual(set(re.findall(r"^\s+(\w+)\s+=", email, re.M)), {"identity_arn", "from"})


class StageTests(unittest.TestCase):
    def test_access_covers_only_the_login_path_and_admits_the_family_allowlist(self):
        login = block('resource "cloudflare_zero_trust_access_application" "login"', STAGE)
        self.assertIn("domain           = local.login_path", login)
        self.assertIn('destinations     = [{ type = "public", uri = local.login_path }]', login)
        self.assertIn("policies         = [for i, id in var.stage.login_policy_ids : { id = id, precedence = i + 1 }]", login)
        self.assertIn("login_policy_ids = [cloudflare_zero_trust_access_policy.cc_games_allowed.id]", FAMILY)

    def test_the_rest_of_the_host_bypasses_for_the_apis_own_token(self):
        public = block('resource "cloudflare_zero_trust_access_application" "public"', STAGE)
        self.assertIn("domain       = var.stage.hostname", public)
        self.assertIn('decision   = "bypass"', block('resource "cloudflare_zero_trust_access_policy" "public"', STAGE))

    def test_the_connector_requires_access_on_the_login_path_then_the_api_then_zero_cache_then_404(self):
        config = block('resource "cloudflare_zero_trust_tunnel_cloudflared_config" "stage"', STAGE)
        login_rule = config[config.index("# The sign-in"):config.index("path     = var.stage.api_path_regex")]
        self.assertIn("required  = true", login_rule)
        self.assertIn("aud_tag   = [cloudflare_zero_trust_access_application.login[0].aud]", login_rule)
        order = [config.index(s) for s in ('path     = "^${replace(var.stage.login_path', "path     = var.stage.api_path_regex",
                                             'service  = "http://localhost:${var.stage.zero_port}"', '[{ service = "http_status:404" }]')]
        self.assertEqual(order, sorted(order))
        self.assertIn('api_port         = 8081', FAMILY)
        self.assertIn('zero_port        = 4848', FAMILY)

    def test_the_login_path_is_a_plain_path(self):
        self.assertIn('can(regex("^/[A-Za-z0-9/_-]+$", var.stage.login_path))', VARS)
        self.assertRegex(family_setting("login_path"), r"^/[A-Za-z0-9/_-]+$")

    def test_familys_regexes_route_exactly_its_api_paths_and_never_zero_caches_calls(self):
        login = re.compile("^" + re.escape(family_setting("login_path")))  # what the module writes, escaped
        api = re.compile(family_setting("api_path_regex"))
        for path in ("/family/native/login", "/family/native/login?challenge=x"):
            self.assertTrue(login.search(path), path)
        for path in ("/family/v1/native/token", "/healthz", "/", "/x/family/native/login"):
            self.assertFalse(login.search(path), path)
        # The API's routes the app calls (ejc3/constellation apps/api/src/family/http.ts, FamilySession.swift).
        for path in ("/family/v1/native/token", "/family/v1/refresh", "/family/v1/sign-out", "/family/native/login", "/healthz",
                     # Family's own sign-in (Sign in with Apple, email codes, adding a way, Apple's notifications).
                     "/family/v1/native/apple", "/family/v1/native/email/start", "/family/v1/native/email/verify",
                     "/family/v1/native/methods", "/family/v1/native/methods/email/verify",
                     "/family/v1/native/apple/notifications"):
            self.assertTrue(api.search(path), path)
        # zero-cache's own calls to the API, the lobby's routes, and anything else, never reach the API from outside.
        for path in ("/family/zero/query", "/family/zero/mutate", "/zero/query", "/me", "/family", "/healthzx", "/",
                     "/sync/v49/connect", "/x/family/v1/refresh"):
            self.assertFalse(api.search(path), path)

    def test_the_hostname_appears_only_after_access_and_the_rules(self):
        dns = block('resource "cloudflare_dns_record" "stage"', STAGE)
        for dep in ("cloudflare_zero_trust_access_application.login,", "cloudflare_zero_trust_access_application.public,",
                    "cloudflare_zero_trust_tunnel_cloudflared_config.stage,"):
            self.assertIn(dep, dns)
        self.assertIn("proxied = true", dns)

    def test_the_connector_token_goes_to_the_apps_account_only(self):
        secret = block('resource "aws_secretsmanager_secret" "tunnel"', STAGE)
        version = block('resource "aws_secretsmanager_secret_version" "tunnel"', STAGE)
        self.assertIn("provider                = aws.app", secret)
        self.assertIn("provider      = aws.app", version)
        self.assertEqual(STAGE.count("cloudflared_token.stage[0].token"), 1)
        self.assertNotIn("token", OUT)


class EmailTests(unittest.TestCase):
    def test_the_identity_is_in_the_apps_account_with_dkim_and_its_own_mail_from(self):
        identity = block('resource "aws_sesv2_email_identity" "sender"', EMAIL)
        self.assertIn("provider       = aws.app", identity)
        self.assertIn("email_identity = var.email_sender.domain", identity)
        mail_from = block('resource "aws_sesv2_email_identity_mail_from_attributes" "sender"', EMAIL)
        self.assertIn("provider               = aws.app", mail_from)
        self.assertIn('local.email_on ? "bounce.${var.email_sender.domain}" : null', EMAIL)
        dkim = block('resource "cloudflare_dns_record" "sender_dkim"', EMAIL)
        self.assertIn("count   = local.email_on ? 3 : 0", dkim)
        self.assertIn("proxied = false", dkim)
        self.assertIn('content  = "feedback-smtp.${var.region}.amazonses.com"', block('resource "cloudflare_dns_record" "sender_mail_from_mx"', EMAIL))
        self.assertIn('include:amazonses.com', block('resource "cloudflare_dns_record" "sender_mail_from_spf"', EMAIL))

    def test_nothing_here_can_send_or_holds_a_credential(self):
        # Sending is granted in the app's repository, to its task role; no user, key or SMTP password here.
        code = "\n".join(line for line in EMAIL.splitlines() if not line.lstrip().startswith("#"))
        for word in ("aws_iam_user", "aws_iam_access_key", "aws_iam_role", "ses:Send", "smtp_password", "secret"):
            self.assertNotIn(word, code)

    def test_family_sends_from_its_own_subdomain_never_the_apex(self):
        sender = block("email_sender = {", FAMILY)
        domain = re.search(r'domain\s+= "([^"]+)"', sender).group(1)
        self.assertTrue(domain.endswith(".dolphin-labs.dev"), domain)
        # The apex is the work address's SES identity in the Dolphin account (dolphin-labs-email-send.tf).
        self.assertNotEqual(domain, "dolphin-labs.dev")
        self.assertIn("zone_id         = var.dolphin_zone_id", sender)
        self.assertRegex(sender, r'from_local_part = "[a-z0-9.+-]+"')


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

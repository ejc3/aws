#!/usr/bin/env python3
"""Offline checks for Astryx sync's member account (astryx-account.tf) and its staging hostname (astryx-stage.tf): a dev
box applies inside that one account only, through one exact role, the OU is fenced to us-west-1, and on Cloudflare only
/native/login is behind Access, open to anyone who signs in, with the connector requiring Access there too."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "astryx-account.tf").read_text()
STAGE = (ROOT / "astryx-stage.tf").read_text()
CONST = (ROOT / "constellation-account.tf").read_text()
PARITY = (ROOT / "nextjs-dev-fcvm-parity.tf").read_text()
CT = (ROOT / "nextjs-ct-user-data.tf").read_text()
PEOPLE = (ROOT / "people.tf").read_text()


def block(header, text=None):
    text = TF if text is None else text
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


def statement(policy, sid):
    """The statement with this Sid, from its Sid to the end of its Condition (or of the statement)."""
    rest = policy.split(f'Sid       = "{sid}"', 1)[1]
    return rest.split("}, {", 1)[0].split("}]", 1)[0]


class AccountTests(unittest.TestCase):
    def test_the_account_is_never_closed_or_replaced_by_terraform(self):
        account = block('resource "aws_organizations_account" "astryx"')
        self.assertIn("close_on_deletion          = false", account)
        self.assertIn("prevent_destroy = true", account)
        self.assertIn("email                      = local.astryx_account_email", account)
        self.assertIn("parent_id                  = aws_organizations_organizational_unit.astryx.id", account)
        self.assertIn('name      = "Astryx"', block('resource "aws_organizations_organizational_unit" "astryx"'))

    def test_its_root_address_comes_from_people_addresses(self):
        self.assertIn("astryx_account_email        = nonsensitive(local.people.astryx_account)", PEOPLE)
        self.assertIn("can(local.people.astryx_account)", PEOPLE)
        self.assertIn('local.people.astryx_account))', PEOPLE.split("precondition {", 3)[2])  # the one-address check
        for text in (TF, STAGE):
            self.assertNotRegex(text, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[a-z]{2,}")

    def test_the_provider_goes_through_the_access_role_in_the_home_region(self):
        prov = re.search(r'provider "aws" \{\n  alias  = "astryx"\n  region = var.aws_region\n  assume_role \{\n    role_arn = "([^"]+)"', TF)
        self.assertIsNotNone(prov)
        self.assertTrue(prov.group(1).endswith(":role/OrganizationAccountAccessRole"))


class GuardrailTests(unittest.TestCase):
    def test_region_leave_and_iam_users_are_denied_on_the_ou_only(self):
        scp = block("astryx_scp = {")
        self.assertIn("astryx_regions            = [var.aws_region]", TF)
        self.assertIn('Condition = { StringNotEquals = { "aws:RequestedRegion" = local.astryx_regions } }', scp)
        self.assertIn("NotAction = local.dolphin_region_scp_global_actions", scp)
        self.assertIn('Action   = "organizations:LeaveOrganization"', scp)
        self.assertIn('"iam:CreateUser", "iam:CreateAccessKey", "iam:CreateLoginProfile", "iam:UpdateLoginProfile"', scp)
        self.assertIn('StringNotEquals = { "s3:LocationConstraint" = local.astryx_regions }', scp)
        self.assertNotIn('"Allow"', scp)
        att = block('resource "aws_organizations_policy_attachment" "astryx_guardrails"')
        self.assertIn("target_id = aws_organizations_organizational_unit.astryx.id", att)
        self.assertIn('type        = "SERVICE_CONTROL_POLICY"', block('resource "aws_organizations_policy" "astryx_guardrails"'))

    def test_nothing_else_names_the_astryx_account(self):
        # Trust runs one way, into the account: no other file names it, so nothing here can come to trust it by the way.
        allowed = {"astryx-account.tf", "astryx-stage.tf"}
        for f in ROOT.glob("*.tf"):
            if f.name in allowed:
                continue
            text = f.read_text()
            for ref in ("aws_organizations_account.astryx", "local.astryx_account_id", "aws.astryx"):
                self.assertNotIn(ref, text, f"{f.name}: {ref}")


class ApplyRoleTests(unittest.TestCase):
    def test_the_apply_role_trusts_only_fcvm_arm_and_the_owners_container(self):
        role = block('resource "aws_iam_role" "astryx_terraform"')
        self.assertEqual(role.count("Principal ="), 2)
        self.assertIn("Principal = { AWS = aws_iam_role.dev_server.arn }", role)
        self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', statement(role, "FcvmArmOnly"))
        self.assertIn("Principal = { AWS = aws_iam_role.nextjs_dev.arn }", role)
        self.assertIn("max_session_duration = 3600", role)
        admin = block('resource "aws_iam_role_policy_attachment" "astryx_terraform_admin"')
        self.assertIn("provider   = aws.astryx", admin)

    def test_fcvm_arms_grant_names_exactly_that_role_and_the_pointer_with_the_pin(self):
        grant = block('resource "aws_iam_policy" "dev_server_constellation_terraform"', CONST)
        for sid, action, res in (("AssumeTheAstryxApplyRoleOnly", "sts:AssumeRole", "aws_iam_role.astryx_terraform.arn"),
                                 ("ReadTheAstryxPointerOnly", "ssm:GetParameter", "aws_ssm_parameter.astryx.arn")):
            stmt = statement(grant, sid)
            self.assertIn(f'Action    = "{action}"', stmt, sid)
            self.assertIn(f"Resource  = {res}", stmt, sid)
            self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', stmt, sid)
        # Statements in the existing policy, not another policy on dev-server-role (it has no room).
        self.assertNotIn('resource "aws_iam_policy"', TF)
        self.assertNotIn("aws_iam_role.dev_server.name", TF)

    def test_the_owners_container_gets_the_role_and_pointer_and_members_do_not(self):
        self.assertIn("Resource = aws_iam_role.astryx_terraform.arn", PARITY.split('Sid      = "AstryxApply"', 1)[1].split("},", 1)[0])
        self.assertIn("Resource = aws_ssm_parameter.astryx.arn", PARITY.split('Sid      = "AstryxPointer"', 1)[1].split("},", 1)[0])
        member = CT.split("nextjs_ct_deny_member = [", 1)[1].split("\n  ]\n", 1)[0]
        self.assertIn("aws_ssm_parameter.astryx.arn", member)
        owner_only_roles = member.split('Sid      = "OwnerOnlyRoles"', 1)[1].split("}", 1)[0]
        self.assertIn('Action   = "sts:AssumeRole"', owner_only_roles)
        self.assertIn('Resource = "*"', owner_only_roles)

    def test_nothing_gives_a_dev_box_the_organization_or_other_accounts(self):
        policies = [block(m.group(0)[:-2]) for m in re.finditer(r'resource "aws_iam_(?:role_policy|policy)" "\w+" \{', TF)]
        for body in policies + [statement(block('resource "aws_iam_policy" "dev_server_constellation_terraform"', CONST), s)
                                for s in ("AssumeTheAstryxApplyRoleOnly", "ReadTheAstryxPointerOnly")]:
            self.assertNotIn("organizations:", body)
            self.assertNotIn("OrganizationAccountAccessRole", body)
            self.assertNotIn("arn:aws:iam::*", body)
        self.assertNotIn("organizations:CreateAccount", TF)

    def test_github_holds_no_aws_credential_for_this_account(self):
        for forbidden in ("aws_iam_openid_connect_provider", "AssumeRoleWithWebIdentity", "token.actions.githubusercontent.com",
                          "github_actions_variable", "ReadOnlyAccess"):
            self.assertNotIn(forbidden, TF)
            self.assertNotIn(forbidden, STAGE)


class StateAndPointerTests(unittest.TestCase):
    def test_state_objects_are_for_the_appliers_only(self):
        pol = block('resource "aws_s3_bucket_policy" "astryx_terraform_state"')
        self.assertIn("StateObjectsForAppliersOnly", pol)
        self.assertIn("DenyInsecureTransport", pol)
        self.assertIn('"s3:GetObject"', pol)
        self.assertIn("[local.astryx_terraform_role_arn, local.astryx_access_role_arn]", pol)
        self.assertIn('status = "Enabled"', block('resource "aws_s3_bucket_versioning" "astryx_terraform_state"'))
        self.assertIn("prevent_destroy = true", block('resource "aws_s3_bucket" "astryx_terraform_state"'))
        self.assertIn('bucket   = "astryx-terraform-state-${local.astryx_account_id}"', TF)
        self.assertIn('sse_algorithm = "AES256"', block('resource "aws_s3_bucket_server_side_encryption_configuration" "astryx_terraform_state"'))
        self.assertIn('object_ownership = "BucketOwnerEnforced"', block('resource "aws_s3_bucket_ownership_controls" "astryx_terraform_state"'))
        self.assertIn("restrict_public_buckets = true", block('resource "aws_s3_bucket_public_access_block" "astryx_terraform_state"'))

    def test_the_pointer_holds_no_credential(self):
        param = block('resource "aws_ssm_parameter" "astryx"')
        self.assertIn('name        = "/infra/astryx"', param)
        self.assertIn('type        = "String"', param)
        self.assertIn("value       = jsonencode(local.astryx_pointer)", param)
        pointer = block("astryx_stage_pointer = {", STAGE)
        self.assertEqual(re.findall(r"^\s+(\w+)\s+=", pointer, re.M),
                         ["hostname", "access_team_domain", "access_aud", "tunnel_secret_name"])
        for leak in ("token", "secret_string", "secret_version", "client_secret", "password"):
            self.assertNotIn(leak, pointer.replace("tunnel_secret_name", "").replace("astryx_stage_tunnel.name", ""))
        self.assertIn("stage          = local.astryx_stage_pointer", TF)


class StageTests(unittest.TestCase):
    def test_access_covers_only_the_login_path_and_admits_anyone_who_signs_in(self):
        self.assertEqual(STAGE.count('resource "cloudflare_zero_trust_access_application"'), 1)
        app = block('resource "cloudflare_zero_trust_access_application" "astryx_stage_login"', STAGE)
        self.assertIn('astryx_stage_login_path = "${local.astryx_stage_hostname}/native/login"', STAGE)
        self.assertIn("domain           = local.astryx_stage_login_path", app)
        self.assertIn('destinations     = [{ type = "public", uri = local.astryx_stage_login_path }]', app)
        self.assertEqual(re.findall(r"id\s*=\s*(cloudflare_zero_trust_access_policy\.\w+)\.id", app),
                         ["cloudflare_zero_trust_access_policy.astryx_stage_anyone_signed_in"])
        for idp in ("identity_provider.github[*].id", "identity_provider.google[*].id", "identity_provider.onetimepin.id"):
            self.assertIn(idp, app)
        policy = block('resource "cloudflare_zero_trust_access_policy" "astryx_stage_anyone_signed_in"', STAGE)
        self.assertIn('decision         = "allow"', policy)
        self.assertIn("include          = [{ everyone = {} }]", policy)
        # Nothing on this host is bypassed or opened by a policy: the rest has no application, and the API checks tokens.
        self.assertNotIn('"bypass"', STAGE)
        self.assertNotIn("non_identity", STAGE)
        self.assertEqual(STAGE.count("everyone = {}"), 1)

    def test_the_connector_requires_access_on_the_login_path_and_routes_the_rest(self):
        config = block('resource "cloudflare_zero_trust_tunnel_cloudflared_config" "astryx_stage"', STAGE)
        rules = config[config.index("ingress = ["):].split("      {\n        hostname")[1:]
        self.assertEqual(len(rules), 3)
        login, api, zero = rules
        self.assertIn("path     = local.astryx_stage_login_regex", login)
        self.assertIn('service  = "http://localhost:${local.astryx_stage_api_port}"', login)
        self.assertIn("required  = true", login)
        self.assertIn("aud_tag   = [cloudflare_zero_trust_access_application.astryx_stage_login.aud]", login)
        self.assertIn("path     = local.astryx_stage_api_regex", api)
        self.assertIn('service  = "http://localhost:${local.astryx_stage_api_port}"', api)
        self.assertNotIn("path", zero)
        self.assertIn('service  = "http://localhost:${local.astryx_stage_zero_port}"', zero)
        self.assertIn('{ service = "http_status:404" }', config)
        self.assertEqual(set(re.findall(r'service  = "([^"]+)"', config)),
                         {"http://localhost:${local.astryx_stage_api_port}", "http://localhost:${local.astryx_stage_zero_port}"})
        self.assertIn("astryx_stage_api_port   = 8080", STAGE)
        self.assertIn("astryx_stage_zero_port  = 4848", STAGE)

    def test_the_regexes_route_exactly_the_api_paths_and_cover_the_access_path(self):
        login = re.compile(re.search(r'astryx_stage_login_regex = "([^"]+)"', STAGE).group(1))
        api = re.compile(re.search(r'astryx_stage_api_regex   = "([^"]+)"', STAGE).group(1))
        for path in ("/native/login", "/native/login/callback", "/native/loginx"):
            self.assertTrue(login.search(path), path)  # a superset of what Access covers: fails closed
        for path in ("/native/logout", "/api/zero/query", "/healthz", "/", "/sync/v49/connect", "/x/native/login"):
            self.assertFalse(login.search(path), path)
        for path in ("/api/zero/query", "/api/zero/mutate", "/native/login", "/native/token", "/healthz"):
            self.assertTrue(api.search(path), path)
        for path in ("/", "/sync/v49/connect", "/keepalive", "/healthzx", "/api", "/native", "/x/api/zero/query"):
            self.assertFalse(api.search(path), path)

    def test_the_hostname_appears_only_after_access_and_the_rules(self):
        dns = block('resource "cloudflare_dns_record" "astryx_stage"', STAGE)
        self.assertIn('astryx_stage_hostname   = "astryx-stage.dolphin-labs.dev"', STAGE)
        self.assertIn("cloudflare_zero_trust_access_application.astryx_stage_login,", dns)
        self.assertIn("cloudflare_zero_trust_tunnel_cloudflared_config.astryx_stage,", dns)
        self.assertIn("proxied = true", dns)
        self.assertIn('name       = "astryx-stage"', block('resource "cloudflare_zero_trust_tunnel_cloudflared" "astryx_stage"', STAGE))

    def test_the_connector_token_goes_to_the_astryx_account_only(self):
        secret = block('resource "aws_secretsmanager_secret" "astryx_stage_tunnel"', STAGE)
        version = block('resource "aws_secretsmanager_secret_version" "astryx_stage_tunnel"', STAGE)
        self.assertIn("provider                = aws.astryx", secret)
        self.assertIn('name                    = "astryx/nonprod/tunnel"', secret)
        self.assertIn("provider      = aws.astryx", version)
        self.assertIn("secret_id     = aws_secretsmanager_secret.astryx_stage_tunnel.id", version)
        self.assertIn("secret_string = jsonencode({ TUNNEL_TOKEN = data.cloudflare_zero_trust_tunnel_cloudflared_token.astryx_stage.token })", version)
        self.assertEqual(STAGE.count("cloudflared_token.astryx_stage.token"), 1)
        self.assertNotIn("aws_iam_", STAGE)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

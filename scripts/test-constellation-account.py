#!/usr/bin/env python3
"""Offline checks for constellation's member account (constellation-account.tf): a dev box applies inside that one account
only, through one exact role pinned to fcvm-arm's instance, and never gains a path to the organization or another member
account."""

from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "constellation-account.tf").read_text()
LATTICE = (ROOT / "constellation-lattice-stage.tf").read_text()
MCP = (ROOT / "constellation-mcp-stage.tf").read_text()
DEPLOY = (ROOT / "workers-deploy.tf").read_text()


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


class ConstellationAccountTests(unittest.TestCase):
    def test_the_account_is_never_closed_or_replaced_by_terraform(self):
        account = block('resource "aws_organizations_account" "constellation"')
        self.assertIn("close_on_deletion          = false", account)
        self.assertIn("prevent_destroy = true", account)
        self.assertIn("local.constellation_account_email", account)

    def test_the_apply_role_trusts_only_fcvm_arm_and_nextjs(self):
        role = block('resource "aws_iam_role" "constellation_terraform"')
        self.assertIn("Principal = { AWS = aws_iam_role.dev_server.arn }", role)
        self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', role)
        # The owner's nextjs-ct container: unpinned (a chained session has no instance ARN); members deny AssumeRole.
        self.assertIn("Principal = { AWS = aws_iam_role.nextjs_dev.arn }", role)
        self.assertEqual(role.count("Principal ="), 2)

    def test_the_pin_is_fcvm_arm_or_an_arn_no_instance_has(self):
        self.assertIn("try(aws_instance.firecracker_dev[0].arn, ", TF)
        self.assertIn(":instance/fcvm-arm-not-running", TF)

    def test_the_dev_box_grant_names_exactly_that_role(self):
        grant = block('resource "aws_iam_policy" "dev_server_constellation_terraform"')
        attach = block('resource "aws_iam_role_policy_attachment" "dev_server_constellation_terraform"')
        self.assertIn("role       = aws_iam_role.dev_server.name", attach)
        self.assertIn('Action    = "sts:AssumeRole"', grant)
        self.assertIn("Resource  = aws_iam_role.constellation_terraform.arn", grant)
        self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', grant)

    def test_nothing_gives_a_dev_box_the_organization_or_other_accounts(self):
        # The options refused in constellation's own notes: organizations:CreateAccount for a dev box, and AssumeRole on
        # OrganizationAccountAccessRole in any account (admin of every member account). Every IAM policy in this file,
        # inline or managed, whoever it is attached to, is screened: a managed policy's body does not name its role.
        policies = [block(m.group(0)[:-2]) for m in re.finditer(r'resource "aws_iam_(?:role_policy|policy)" "\w+" \{', TF)]
        self.assertTrue(any("aws_iam_role.constellation_terraform.arn" in b for b in policies))
        for body in policies:
            self.assertNotIn("organizations:", body)
            self.assertNotIn("OrganizationAccountAccessRole", body)
            self.assertNotIn("arn:aws:iam::*", body)
        self.assertNotIn("organizations:CreateAccount", TF)

    def test_github_holds_no_aws_credential_for_this_account(self):
        # State and configuration reads can carry secret values; a same-repository pull request could read them.
        for forbidden in ("aws_iam_openid_connect_provider", "AssumeRoleWithWebIdentity", "token.actions.githubusercontent.com",
                          "github_actions_variable", "ReadOnlyAccess"):
            self.assertNotIn(forbidden, TF)

    def test_state_objects_are_for_the_appliers_only(self):
        policy = block('resource "aws_s3_bucket_policy" "constellation_terraform_state"')
        self.assertIn("StateObjectsForAppliersOnly", policy)
        self.assertIn('"s3:GetObject"', policy)
        self.assertIn("local.constellation_terraform_role_arn", policy)

    def test_the_pointer_is_readable_by_fcvm_arm_only_and_holds_no_credential(self):
        grant = block('resource "aws_iam_policy" "dev_server_constellation_terraform"')
        read = grant[grant.index("ReadTheConstellationPointerOnly"):]
        self.assertIn('Action    = "ssm:GetParameter"', read)
        self.assertIn("Resource  = aws_ssm_parameter.constellation.arn", read)
        self.assertIn('"ec2:SourceInstanceARN" = local.constellation_applier_instance_arn', read)
        param = block('resource "aws_ssm_parameter" "constellation"')
        self.assertIn('name        = "/infra/constellation"', param)
        self.assertIn('type        = "String"', param)
        self.assertIn("value       = jsonencode(local.constellation_pointer)", param)
        pointer = block("lattice_stage_pointer = {", LATTICE)
        for leak in ("token", "secret_string", "secret_version", "client_secret"):
            self.assertNotIn(leak, pointer.replace("tunnel_secret_name", "").replace("tunnel_secret_key", "").replace('"TUNNEL_TOKEN"', ""))


class LatticeStageTests(unittest.TestCase):
    def test_the_host_is_owner_only(self):
        app = block('resource "cloudflare_zero_trust_access_application" "lattice_stage"', LATTICE)
        self.assertIn("domain           = local.lattice_stage_hostname", app)
        self.assertEqual(re.findall(r"id\s*=\s*(cloudflare_zero_trust_access_policy\.\w+)\.id", app),
                         ["cloudflare_zero_trust_access_policy.lattice_stage_owner"])
        owner = block('resource "cloudflare_zero_trust_access_policy" "lattice_stage_owner"', LATTICE)
        self.assertIn('decision         = "allow"', owner)
        self.assertIn("include          = [{ email = { email = local.browser_manager_owner } }]", owner)
        self.assertNotIn("everyone", app)

    def test_only_the_two_bearer_token_paths_skip_access(self):
        paths = re.search(r"lattice_stage_bypass_paths = \[(.*?)\n  \]", LATTICE, re.S).group(1)
        self.assertEqual(re.findall(r'"\$\{local\.lattice_stage_hostname\}(/[^"]*)"', paths), ["/lattice/live", "/lattice/v1/*"])
        self.assertNotIn("native", paths)
        bypass = block('resource "cloudflare_zero_trust_access_application" "lattice_stage_bearer_paths"', LATTICE)
        self.assertIn("for uri in local.lattice_stage_bypass_paths", bypass)
        self.assertIn("domain       = local.lattice_stage_bypass_paths[0]", bypass)
        self.assertIn("cloudflare_zero_trust_access_policy.lattice_stage_bearer_paths.id", bypass)
        # The only bypass-everyone policy in the file is the one attached to those paths.
        self.assertEqual(LATTICE.count('decision   = "bypass"'), 1)
        self.assertEqual(LATTICE.count("everyone = {}"), 1)

    def test_the_hostname_appears_only_after_access_covers_it(self):
        dns = block('resource "cloudflare_dns_record" "lattice_stage"', LATTICE)
        self.assertIn("cloudflare_zero_trust_access_application.lattice_stage,", dns)
        self.assertIn("cloudflare_zero_trust_access_application.lattice_stage_bearer_paths,", dns)
        self.assertIn("proxied = true", dns)

    def test_the_tunnel_reaches_only_the_task_local_api(self):
        config = block('resource "cloudflare_zero_trust_tunnel_cloudflared_config" "lattice_stage"', LATTICE)
        self.assertEqual(set(re.findall(r'service  = "([^"]+)"', config)), {"http://localhost:${local.lattice_stage_api_port}"})
        self.assertIn('{ service = "http_status:404" }', config)

    def test_the_connector_requires_access_except_on_the_bypassed_paths(self):
        config = block('resource "cloudflare_zero_trust_tunnel_cloudflared_config" "lattice_stage"', LATTICE)
        rules = config[config.index("ingress = ["):]
        first, second = rules.split("      {\n        hostname")[1:3]
        self.assertIn("path     = local.lattice_stage_bypass_regex", first)
        self.assertNotIn("origin_request", first)
        self.assertNotIn("path", second)
        self.assertIn("required  = true", second)
        self.assertIn("aud_tag   = [cloudflare_zero_trust_access_application.lattice_stage.aud]", second)
        # The connector's regex lets through exactly what Access bypasses, and no more.
        rx = re.compile(re.search(r'lattice_stage_bypass_regex = "([^"]+)"', LATTICE).group(1))
        for path in ("/lattice/live", "/lattice/v1/accounts", "/lattice/v1/a/b"):
            self.assertTrue(rx.search(path), path)
        for path in ("/lattice/native/login", "/lattice/live/x", "/lattice/livex", "/lattice/v1", "/lattice/v1x", "/", "/x/lattice/v1/a"):
            self.assertFalse(rx.search(path), path)

    def test_the_connector_token_goes_to_the_constellation_account_only(self):
        secret = block('resource "aws_secretsmanager_secret" "lattice_stage_tunnel"', LATTICE)
        version = block('resource "aws_secretsmanager_secret_version" "lattice_stage_tunnel"', LATTICE)
        self.assertIn("provider                = aws.constellation", secret)
        self.assertIn('name                    = "constellation/nonprod/tunnel"', secret)
        self.assertIn("provider      = aws.constellation", version)
        self.assertIn("secret_id     = aws_secretsmanager_secret.lattice_stage_tunnel.id", version)
        self.assertIn("secret_string = jsonencode({ TUNNEL_TOKEN = data.cloudflare_zero_trust_tunnel_cloudflared_token.lattice_stage.token })", version)
        self.assertEqual(LATTICE.count("cloudflared_token.lattice_stage.token"), 1)
        self.assertNotIn("aws_iam_", LATTICE)


class McpStageTests(unittest.TestCase):
    def test_the_sign_in_is_owner_only(self):
        app = block('resource "cloudflare_zero_trust_access_application" "constellation_mcp_stage_oidc"', MCP)
        self.assertIn('type       = "saas"', app)
        self.assertEqual(re.findall(r"id\s*=\s*(cloudflare_zero_trust_access_policy\.\w+)\.id", app),
                         ["cloudflare_zero_trust_access_policy.constellation_mcp_stage_owner"])
        owner = block('resource "cloudflare_zero_trust_access_policy" "constellation_mcp_stage_owner"', MCP)
        self.assertIn('decision         = "allow"', owner)
        self.assertIn("include          = [{ email = { email = local.browser_manager_owner } }]", owner)
        self.assertNotIn("everyone", MCP)
        self.assertNotIn("bypass", MCP.split("locals {", 1)[1])

    def test_codes_go_only_to_the_workers_callback(self):
        app = block('resource "cloudflare_zero_trust_access_application" "constellation_mcp_stage_oidc"', MCP)
        uris = re.search(r"redirect_uris = \[(.*?)\]", app).group(1)
        self.assertEqual(uris, '"https://${local.constellation_mcp_stage_hostname}/callback"')
        self.assertIn('grant_types   = ["authorization_code_with_pkce"]', app)

    def test_the_hostname_waits_for_the_worker_and_its_sign_in(self):
        domain = block('resource "cloudflare_workers_custom_domain" "constellation_mcp_stage"', MCP)
        self.assertIn("count      = local.constellation_mcp_stage_deployed ? 1 : 0", domain)
        self.assertIn("service    = local.constellation_mcp_stage_worker", domain)
        self.assertIn("cloudflare_zero_trust_access_application.constellation_mcp_stage_oidc,", domain)
        self.assertIn("constellation_mcp_stage_deployed = false", MCP)

    def test_the_service_secret_goes_to_the_worker_container_and_the_apis_is_written_once(self):
        uses = re.findall(r"random_password\.constellation_lattice_service\.result", MCP)
        self.assertEqual(len(uses), 1)
        # The API's copy cannot be a managed version: its policy denies reads to everyone but constellation's roles, and
        # a version resource reads its value back on every refresh (every plan failed after the first apply).
        self.assertNotIn('resource "aws_secretsmanager_secret_version" "constellation_lattice_service"', MCP)
        removed = MCP.split("removed {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn("from = aws_secretsmanager_secret_version.constellation_lattice_service", removed)
        self.assertIn("destroy = false", removed)
        worker = block('resource "aws_secretsmanager_secret_version" "constellation_mcp_stage"', MCP)
        self.assertIn("random_password.constellation_lattice_service.result", worker)
        self.assertNotIn("provider", worker)
        self.assertNotIn("aws_iam_", MCP)

    def test_the_worker_container_is_for_administration_only(self):
        policy = block('resource "aws_secretsmanager_secret_policy" "constellation_mcp_stage"', MCP)
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('Action    = "secretsmanager:GetSecretValue"', policy)
        self.assertIn('Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }', policy)

    def test_constellation_deploys_from_main_only(self):
        envs = re.search(r"workers_deploy_environments_ejc3 = toset\(\[(.*?)\]\)", DEPLOY).group(1)
        self.assertIn('"constellation"', envs)
        rule = block('resource "github_repository_environment_deployment_policy" "workers_deploy_ejc3_main"', DEPLOY)
        self.assertIn('branch_pattern = "main"', rule)
        variables = block('resource "github_actions_environment_variable" "constellation_mcp_stage"', MCP)
        self.assertIn('github_repository_environment.workers_deploy_ejc3["constellation"].environment', variables)
        self.assertNotIn("secret", variables.replace("github_actions_environment_variable", ""))

    def test_the_pointer_holds_no_credential(self):
        pointer = block("constellation_mcp_stage_pointer = {", MCP)
        for leak in ("client_secret", "secret_string", "random_password", "token"):
            self.assertNotIn(leak, pointer)
        self.assertIn("mcp_stage      = local.constellation_mcp_stage_pointer", TF)


if __name__ == "__main__":
    unittest.main(buffer=True)

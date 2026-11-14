#!/usr/bin/env python3
"""The Cloudflare Workers deploy token (workers-deploy.tf and the script that installs it): a narrow typed token resource,
an administrators-only container, and no secret ever on a command line."""
import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "workers-deploy.tf").read_text()
INSTALL = (ROOT / "scripts" / "workers-deploy-secret.sh").read_text()
ALL_TF = "\n".join(p.read_text() for p in ROOT.glob("*.tf"))

WORKERS_SCRIPTS_WRITE = "e086da7e2179491d91ee5f35b3ca210a"
ACCOUNT_SETTINGS_READ = "c1fde68c7bcc44588cbb6ddbc16d6480"


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


class ContainerTests(unittest.TestCase):
    def test_the_container_exists_and_only_administration_may_read_it(self):
        self.assertIn('name                    = "cloudflare-workers-deploy-token"', TF)
        self.assertIn('Effect    = "Deny"', TF)
        self.assertIn('Action    = "secretsmanager:GetSecretValue"', TF)
        self.assertIn('"aws:PrincipalArn" = local.games_mp_admin_principals', TF)

    def test_no_dev_box_role_is_granted_the_token(self):
        self.assertNotIn("dev_server", code(TF))
        self.assertNotIn("nextjs_dev", code(TF))

    def test_nothing_reads_the_stored_value_back_into_terraform(self):
        self.assertNotRegex(ALL_TF, r'data\s+"aws_secretsmanager_secret_version"\s+"[^"]*workers_deploy')
        self.assertNotRegex(ALL_TF, r'ephemeral\s+"aws_secretsmanager_secret_version"\s+"[^"]*workers_deploy')


class TokenResourceTests(unittest.TestCase):
    """The deploy token is a typed Terraform resource (issue #16: no curl or local-exec for Cloudflare resources)."""

    def resource(self, kind, name):
        start = code(TF).index('%s "%s" "%s" {' % ("resource", kind, name))
        body = code(TF)[start:]
        depth, seen = 0, False
        for i, ch in enumerate(body):
            depth += ch == "{"
            depth -= ch == "}"
            seen = seen or depth > 0
            if seen and depth == 0:
                return body[:i + 1]

    def test_there_is_no_mint_script_and_nothing_shells_out_or_calls_the_api(self):
        self.assertFalse((ROOT / "scripts" / "workers-deploy-token.sh").exists())
        self.assertNotRegex(code(TF), r"local-exec|provisioner|curl|api\.cloudflare\.com")

    def test_the_account_token_is_read_ephemerally_and_trimmed_for_the_minting_provider(self):
        self.assertIn('ephemeral "aws_secretsmanager_secret_version" "cloudflare_account_token"', TF)
        self.assertIn('secret_id = "cloudflare-account-token"', TF)
        self.assertIn("api_token = trimspace(ephemeral.aws_secretsmanager_secret_version.cloudflare_account_token.secret_string)", TF)
        self.assertIn('alias = "token_minter"', TF)

    def test_the_token_has_exactly_two_permissions_on_one_account(self):
        self.assertIn('workers_scripts_write = "%s"' % WORKERS_SCRIPTS_WRITE, TF)
        self.assertIn('account_settings_read = "%s"' % ACCOUNT_SETTINGS_READ, TF)
        self.assertIn("permission_groups = [{ id = local.workers_scripts_write }, { id = local.account_settings_read }]", TF)
        self.assertEqual(code(TF).count("permission_groups"), 1)
        self.assertIn('resources         = jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })', TF)

    def test_the_new_token_uses_the_minting_provider_and_is_not_ip_pinned_for_github_runners(self):
        r = self.resource("cloudflare_account_token", "workers_deploy")
        self.assertIn("provider   = cloudflare.token_minter", r)
        self.assertIn("policies   = local.workers_deploy_policies", r)
        self.assertNotIn("condition", r)

    def test_terraform_writes_the_token_into_the_container(self):
        r = self.resource("aws_secretsmanager_secret_version", "cloudflare_workers_deploy_token")
        self.assertIn("secret_string = cloudflare_account_token.workers_deploy.value", r)
        self.assertIn("aws_secretsmanager_secret.cloudflare_workers_deploy_token.id", r)

    def test_no_trace_of_the_curl_minted_token_remains(self):
        self.assertNotIn("workers_deploy_legacy", code(TF))


class InstallScriptTests(unittest.TestCase):
    def test_arguments_are_validated_before_use(self):
        self.assertIn('[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]', INSTALL)
        self.assertIn('[[ -z "$AS" || "$AS" =~ ^[a-z][a-z0-9_-]*$ ]]', INSTALL)

    def test_refuses_dolphin_labs_before_reading_the_token(self):
        # dolphin-labs' copy is a main-only environment secret Terraform writes; a repository secret would undo that.
        env = dict(os.environ, PATH="/nonexistent")
        # GitHub resolves repository names case-insensitively, so every spelling is refused.
        for repo in ("dolphin-labs-hq/dolphin-labs", "Dolphin-Labs-HQ/Dolphin-Labs", "dolphin-labs-hq/dolphin-films",
                     "dolphin-labs-hq/dolphin-maps", "ejc3/imagine", "EJC3/Remote-Claw", "ejc3/FCVM", "ejc3/Constellation"):
            r = subprocess.run(["/bin/bash", str(ROOT / "scripts" / "workers-deploy-secret.sh"), repo],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 3, (repo, r.stderr))
            self.assertIn("main-only environments", r.stderr)
        self.assertLess(INSTALL.index("dolphin-labs-hq/dolphin-labs|dolphin-labs-hq/dolphin-films|"), INSTALL.index("secretsmanager get-secret-value"))

    def test_every_other_site_repo_gets_a_main_only_environment(self):
        # ejc3/aws#466: imagine, remote-claw and dolphin-maps take the token from a `cloudflare-deploy` environment
        # admitted to main only, each with the deploy token as its CLOUDFLARE_API_TOKEN.
        src = code(TF)
        self.assertIn('workers_deploy_environments_ejc3 = toset(["imagine", "remote-claw", "fcvm", "constellation"])', src)
        for env, policy, secret, provider in (
                ("workers_deploy_ejc3", "workers_deploy_ejc3_main", "workers_deploy_ejc3", "github.agents_md"),
                ("workers_deploy_dolphin_maps", "workers_deploy_dolphin_maps_main", "workers_deploy_dolphin_maps",
                 "github.dolphin_labs_ci")):
            e = re.search(r'resource "github_repository_environment" "' + env + r'" \{.*?\n\}', src, re.S).group()
            self.assertIn('environment       = "cloudflare-deploy"', e)
            self.assertIn("custom_branch_policies = true", e)
            self.assertIn("protected_branches     = false", e)
            self.assertIn("provider          = " + provider, e)
            p = re.search(r'resource "github_repository_environment_deployment_policy" "' + policy + r'" \{.*?\n\}', src, re.S).group()
            self.assertIn('branch_pattern = "main"', p)
            s = re.search(r'resource "github_actions_environment_secret" "' + secret + r'" \{.*?\n\}', src, re.S).group()
            self.assertIn('secret_name     = "CLOUDFLARE_API_TOKEN"', s)
            self.assertIn("plaintext_value = cloudflare_account_token.workers_deploy.value", s)
            # Every repo here is refused by the install script, so nobody puts the repository secret back.
        for repo in ("ejc3/imagine", "ejc3/remote-claw", "ejc3/fcvm", "ejc3/constellation", "dolphin-labs-hq/dolphin-maps"):
            self.assertIn(repo, INSTALL.split("case", 1)[1].split("esac", 1)[0])

    def test_codercolton_sites_get_their_environment_secret_not_a_repository_secret(self):
        # CoderColton is outside Terraform's GitHub owners: its main-only `cloudflare-deploy` environment is set by
        # this script, and only there (ejc3/aws#466).
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            calls = os.path.join(d, "calls")
            for name, body in (("aws", 'printf "%040d" 0'), ("gh", f'printf "%s\\n" "$*" >> {calls}; cat >/dev/null')):
                with open(os.path.join(d, name), "w") as f:
                    f.write("#!/bin/bash\n" + body + "\n")
                os.chmod(os.path.join(d, name), 0o755)
            env = dict(os.environ, PATH=d + ":/usr/bin:/bin")
            for repo in ("CoderColton/colton-games", "CoderColton/nest-step", "ejc3/ts-api"):
                r = subprocess.run(["/bin/bash", str(ROOT / "scripts" / "workers-deploy-secret.sh"), repo],
                                   capture_output=True, text=True, env=env)
                self.assertEqual(r.returncode, 0, r.stderr)
            lines = open(calls).read().splitlines()
        self.assertEqual(lines, ["secret set CLOUDFLARE_API_TOKEN --repo CoderColton/colton-games --env cloudflare-deploy",
                                 "secret set CLOUDFLARE_API_TOKEN --repo CoderColton/nest-step --env cloudflare-deploy",
                                 "secret set CLOUDFLARE_API_TOKEN --repo ejc3/ts-api"])

    def test_refuses_an_empty_stored_token(self):
        self.assertIn('"${#TOKEN}" -ge 30', INSTALL)

    def test_as_another_account_it_uses_that_accounts_own_login_and_copies_nothing(self):
        self.assertIn("sudo -n -u $AS gh secret set CLOUDFLARE_API_TOKEN", INSTALL)
        self.assertNotRegex(code(INSTALL), r'hosts\.yml|\.config/gh|GH_TOKEN|gh auth token')


if __name__ == "__main__":
    unittest.main(buffer=True)

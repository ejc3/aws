#!/usr/bin/env python3
"""The Cloudflare Workers deploy token (workers-deploy.tf and the two scripts that mint and install it): a narrow token,
an administrators-only container Terraform never reads, and no secret ever on a command line."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "workers-deploy.tf").read_text()
MINT = (ROOT / "scripts" / "workers-deploy-token.sh").read_text()
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

    def test_terraform_never_reads_the_value_so_it_is_not_in_state(self):
        self.assertNotRegex(ALL_TF, r'data\s+"aws_secretsmanager_secret_version"\s+"[^"]*workers_deploy')
        self.assertNotRegex(ALL_TF, r'ephemeral\s+"aws_secretsmanager_secret_version"\s+"[^"]*workers_deploy')
        self.assertNotIn("secret_string", code(TF))


class MintScriptTests(unittest.TestCase):
    def test_both_scripts_are_valid_bash(self):
        for name in ("workers-deploy-token.sh", "workers-deploy-secret.sh"):
            r = subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_the_token_has_exactly_two_permissions_on_one_account(self):
        groups = re.findall(r'^([A-Z_]+)=([0-9a-f]{32})\s*$', MINT, re.M)
        perms = {name: value for name, value in groups if name != "ACCOUNT_ID"}
        self.assertEqual(perms, {"WORKERS_SCRIPTS_WRITE": WORKERS_SCRIPTS_WRITE, "ACCOUNT_SETTINGS_READ": ACCOUNT_SETTINGS_READ})
        # and the request names exactly those two, nothing more
        self.assertEqual(MINT.count('{"id":"%s"}'), 2)
        self.assertIn('"com.cloudflare.api.account.%s":"*"', MINT)
        self.assertNotRegex(code(MINT), r'com\.cloudflare\.api\.(user|zone|account\.[^%])')

    def test_no_secret_is_ever_on_a_command_line(self):
        body = code(MINT) + code(INSTALL)
        # The bearer header is built only by printf into curl's stdin (-H @-), never as -H "Authorization: ...".
        self.assertNotRegex(body, r'-H\s+["\']Authorization')
        self.assertNotRegex(body, r'--secret-string\s+["\']?\$')
        self.assertNotRegex(body, r'--secret-string\s+["\']?[A-Za-z0-9]{20,}')
        self.assertIn("-H @-", MINT)
        self.assertIn("--secret-string file:///dev/stdin", MINT)
        self.assertNotRegex(code(INSTALL), r'gh secret set[^\n]*(--body|-b\s)')
        self.assertIn("printf '%s' \"$TOKEN\" | gh secret set CLOUDFLARE_API_TOKEN", INSTALL)

    def test_curl_uses_ipv4_because_the_account_token_is_pinned(self):
        self.assertIn("curl -4", MINT)

    def test_the_old_token_is_revoked_only_after_the_new_one_is_stored_and_verified(self):
        c = code(MINT)
        stored, verified, revoked = c.index("put-secret-value"), c.index("/tokens/verify"), c.index("DELETE")
        self.assertLess(stored, verified)
        self.assertLess(verified, revoked)
        self.assertIn('t["id"] != new', c)  # never revokes the token it just minted

    def test_the_token_is_not_ip_pinned_for_github_runners(self):
        self.assertNotIn('"condition"', MINT)
        self.assertNotIn("request.ip", MINT)


class InstallScriptTests(unittest.TestCase):
    def test_arguments_are_validated_before_use(self):
        self.assertIn('[[ "$REPO" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]', INSTALL)
        self.assertIn('[[ -z "$AS" || "$AS" =~ ^[a-z][a-z0-9_-]*$ ]]', INSTALL)

    def test_refuses_an_empty_stored_token(self):
        self.assertIn('"${#TOKEN}" -ge 30', INSTALL)

    def test_as_another_account_it_uses_that_accounts_own_login_and_copies_nothing(self):
        self.assertIn("sudo -n -u $AS gh secret set CLOUDFLARE_API_TOKEN", INSTALL)
        self.assertNotRegex(code(INSTALL), r'hosts\.yml|\.config/gh|GH_TOKEN|gh auth token')


if __name__ == "__main__":
    unittest.main()

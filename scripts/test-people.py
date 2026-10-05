#!/usr/bin/env python3
"""people.tf: addresses live in one Secrets Manager secret read only by administration, not in git. The container is created empty
(Terraform holds no value) and no tracked file holds a personal address."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "people.tf").read_text()
ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
# A mailbox at a public provider is a person; the rest are placeholders, hosts, or unix user@unit names.
PERSONAL = re.compile(r"@(gmail|googlemail|yahoo|icloud|me|hotmail|outlook|proton|protonmail|aol)\.[a-z.]+$", re.I)


def block(header):
    m = re.search(re.escape(header) + r" \{\n.*?\n\}\n", TF, re.S)
    assert m, header
    return m.group(0)


class PeopleTests(unittest.TestCase):
    def test_it_is_a_container_with_no_value_in_terraform(self):
        secret = block('resource "aws_secretsmanager_secret" "people"')
        self.assertIn('name                    = "people/addresses"', secret)
        # Terraform READS a version (a data source) but never WRITES one: no resource sets a value.
        self.assertNotIn('resource "aws_secretsmanager_secret_version"', TF)
        self.assertNotRegex(TF, r"(?m)^\s*secret_string\s*=")

    def test_only_administration_may_read_it(self):
        policy = block('resource "aws_secretsmanager_secret_policy" "people"')
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('Action    = "secretsmanager:GetSecretValue"', policy)
        self.assertIn("local.games_mp_admin_principals", policy)
        for role in ("dev_server", "nextjs_dev"):
            self.assertNotIn(role, policy)


    def test_it_is_read_into_the_names_the_configuration_uses_and_the_plan_fails_on_a_bad_value(self):
        self.assertIn('data "aws_secretsmanager_secret_version" "people"', TF)
        for name, key in (("dev_allowed_emails", "family"), ("browser_manager_owner", "owner"), ("dev_staging_email", "staging_account")):
            # nonsensitive(): the old literals were not sensitive, and a sensitive mark alone plans an update on every consumer
            self.assertRegex(TF, r"  %s\s+= nonsensitive\(local\.people\.%s\)" % (name, key))
        shape = block('resource "terraform_data" "people_shape"')
        self.assertEqual(shape.count("precondition {"), 3)
        self.assertIn("length(local.people.family) > 0", shape)  # an empty Access allowlist never reaches an apply
        self.assertNotRegex(shape, r"error_message\s*=\s*\"[^\"]*\$\{")  # an error names keys, never interpolates a value

    def test_the_old_variables_and_literals_are_gone_from_their_call_sites(self):
        cf = (ROOT / "cloudflare.tf").read_text()
        self.assertNotIn('variable "dev_allowed_emails"', cf)
        self.assertNotIn("var.dev_allowed_emails", cf)
        self.assertEqual(cf.count("local.dev_allowed_emails"), 4)
        self.assertNotIn('variable "dev_staging_email"', (ROOT / "dev-staging-account.tf").read_text())
        self.assertIn("local.dev_staging_email", (ROOT / "dev-staging-account.tf").read_text())
        self.assertNotIn("browser_manager_owner    =", (ROOT / "browser-manager.tf").read_text())
        auth = (ROOT / "browser-manager" / "lib" / "auth.mjs").read_text()
        self.assertIn("env.BM_OWNER_EMAIL || ''", auth)  # no built-in owner: the Terraform output is required

    def test_no_tracked_file_holds_a_personal_mailbox(self):
        files = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout.split("\n")
        hits = []
        for name in files:
            if not name or name.endswith((".lock", ".png", ".jpg", ".webp", ".woff2", ".ico", ".zip", ".gz", ".pdf", ".ttf", ".otf", ".wav")):
                continue
            try:
                text = (ROOT / name).read_text(errors="strict")
            except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                for m in ADDRESS.finditer(line):
                    if PERSONAL.search(m.group()):
                        hits.append("%s:%d" % (name, n))
        self.assertEqual(hits, [], "personal addresses in tracked files (use the people/addresses secret, see people.tf)")


if __name__ == "__main__":
    unittest.main()

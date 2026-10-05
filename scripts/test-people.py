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

# Every address-shaped string in a tracked file is a failure unless it is a known NON-mailbox form. The allowlist is the point:
# a personal mailbox at any provider, employer or school fails, and so does one at a domain we own (a mailbox there is a person).
NOT_A_MAILBOX = (
    re.compile(r"(^|\.)(example\.(com|net|org)|[a-z0-9-]+\.example|[a-z0-9-]+\.test|[a-z0-9-]+\.invalid|localhost)$"),  # placeholders
    re.compile(r"\.(service|timer|socket|mount|target|slice|path)$"),  # systemd units: agent-session-sync@ubuntu.service
    re.compile(r"^github\.com$"),  # git@github.com (a clone URL), checked with the user below
    re.compile(r"^([a-z0-9-]+\.)+cc-games\.dev$|^ssh\.dolphin-labs\.dev$"),  # ssh login hosts (user@host), never an apex mailbox
    re.compile(r"\.local$"),  # mDNS hostnames in ssh key comments: user@Host.local
    re.compile(r"^pooler\.supabase\.com$|\.pooler\.supabase\.com$"),  # a database URL: user:password@host
)


# systemd template instances: `cloudflared@cc-games.dev` is the unit cloudflared@ instantiated for a hostname, not a mailbox
SYSTEMD_TEMPLATES = {"cloudflared"}


def is_mailbox(address):
    user, domain = address.rsplit("@", 1)
    domain = domain.lower()
    if user in SYSTEMD_TEMPLATES or domain == "users.noreply.github.com":  # unit instances; GitHub's noreply placeholder
        return False
    if domain == "github.com":
        return user.lower() != "git"
    return not any(rule.search(domain) for rule in NOT_A_MAILBOX)


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

    def test_no_tracked_file_holds_a_mailbox_except_known_non_mailbox_forms(self):
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
                    if is_mailbox(m.group()):
                        hits.append("%s:%d" % (name, n))
        self.assertEqual(hits, [], "email addresses in tracked files (use the people/addresses secret, see people.tf)")

    def test_the_scan_flags_any_provider_and_our_own_domains_but_not_the_known_forms(self):
        at = "@"  # the examples are assembled, so this file holds no address literal itself
        for bad in ("a.b" + at + "gmail.com", "x" + at + "acme-corp.com", "y" + at + "school.k12.ca.us", "z" + at + "dolphin-labs.dev",
                    "w" + at + "cc-games.dev", "n" + at + "github.com", "q" + at + "mail.yahoo.co.uk"):
            self.assertTrue(is_mailbox(bad), bad)
        for ok in ("admin" + at + "example.com", "o" + at + "owner.example.test", "agent-session-sync" + at + "ubuntu.service",
                   "git" + at + "github.com", "admin" + at + "skevh-mac-ssh.cc-games.dev", "ejc3" + at + "ssh.dolphin-labs.dev",
                   "u" + at + "Some-Mac.local", "%s" + at + "aws-0-us-east-1.pooler.supabase.com", "cloudflared" + at + "cc-games.dev",
                   "GH_LOGIN" + at + "users.noreply.github.com"):
            self.assertFalse(is_mailbox(ok), ok)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""The Dolphin member account (dolphin-account.tf): its own OU at the org root, a root address read from the people/addresses
secret and never written in git, and guarded against being replaced, destroyed or closed from Terraform."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ACCOUNT = (ROOT / "dolphin-account.tf").read_text()
PEOPLE = (ROOT / "people.tf").read_text()


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
        self.assertEqual(mon.count("aws_organizations_account.dolphin"), 1)


if __name__ == "__main__":
    unittest.main()

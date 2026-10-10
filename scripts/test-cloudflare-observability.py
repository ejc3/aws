#!/usr/bin/env python3
"""The read-only Cloudflare logs and metrics token (cloudflare-observability.tf): only Read permission groups, account-wide
and on every zone, nothing that names people (Access audit logs), readable by administration and the two dev-box roles."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "cloudflare-observability.tf").read_text()

# Permission group ids, as GET /accounts/<id>/tokens/permission_groups named them on 2026-10-10.
ACCOUNT = {
    "b89a480218d04ceb98b4fe57ca29dc1f": "Account Analytics Read",
    "66c1ed49f4ed46098b75696a6d4ee3c9": "Workers Observability Read",
    "05880cd1bdc24d8bae0be2136972816b": "Workers Tail Read",
    "1a71c399035b4950a1bd1466bbe4f420": "Workers Scripts Read",
    "6a315a56f18441e59ed03352369ae956": "Logs Read",
    "f3604047d46144d2a3e9cf4ac99d7f16": "Allow Request Tracer Read",
}
ZONE = {
    "9c88f9c5bce24ce7af9a958ba9c504db": "Analytics Read",
    "c4a30cd58c5d42619c86a3c36c441e2d": "Logs Read",
    "69ca0c60ffc24f5386ccb38885112c44": "Zone Observability Read",
    "fac65912d42144aa86b7dd33281bf79e": "Health Checks Read",
}
ACCESS_AUDIT_LOGS_READ = "b05b28e839c54467a7d6cba5d3abb5a3"
ACCESS_SCIM_LOGS_READ = "e985ca9351db460faebbe8681c48e560"


def block(name):
    m = re.search(r"^  %s = \{\n(.*?)^  \}" % name, TF, re.S | re.M)
    assert m, name
    return set(re.findall(r'"([0-9a-f]{32})"', m.group(1)))


class TokenTests(unittest.TestCase):
    def test_exactly_the_read_groups_at_their_scopes(self):
        self.assertEqual(block("cloudflare_observability_account_groups"), set(ACCOUNT))
        self.assertEqual(block("cloudflare_observability_zone_groups"), set(ZONE))
        for name in [*ACCOUNT.values(), *ZONE.values()]:
            self.assertTrue(name.endswith("Read"), name)

    def test_nothing_that_names_people(self):
        for gid in (ACCESS_AUDIT_LOGS_READ, ACCESS_SCIM_LOGS_READ):
            self.assertNotIn(gid, TF)

    def test_the_account_policy_and_the_every_zone_policy(self):
        self.assertIn('jsonencode({ "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*" })', TF)
        self.assertIn('"com.cloudflare.api.account.zone.*" = "*"', TF)
        self.assertIn("provider   = cloudflare.token_minter", TF)
        self.assertNotIn("expires_on", TF, "long-lived, rotated by -replace")


class ReaderTests(unittest.TestCase):
    def test_administration_and_the_two_dev_box_roles_read_it_and_no_one_else(self):
        self.assertIn("cloudflare_observability_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]", TF)
        self.assertIn("concat(local.games_mp_admin_principals, local.cloudflare_observability_readers)", TF)
        self.assertIn('for_each   = { dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }', TF)
        self.assertIn('actions   = ["secretsmanager:GetSecretValue"]', TF)


if __name__ == "__main__":
    unittest.main()

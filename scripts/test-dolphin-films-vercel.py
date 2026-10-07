#!/usr/bin/env python3
"""dolphin-films-vercel.tf: Terraform writes the dolphin-films Vercel project's environment from the dolphin-films
containers and the people secret, through a provider whose token reaches the dolphin-labs team only. Nothing is read
until the gate names a container, production and preview never share a credential, and no value or address is in
the repository. Offline."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-films-vercel.tf").read_text()
FILMS = (ROOT / "dolphin-films.tf").read_text()
CODE = "\n".join(line for line in TF.splitlines() if not line.lstrip().startswith("#"))


def block(head):
    m = re.search(r"^%s \{\n.*?^\}" % re.escape(head), TF, re.S | re.M)
    assert m, head + " missing"
    return m.group()


class GateTests(unittest.TestCase):
    def test_the_gate_is_a_committed_set_of_the_four_containers(self):
        gate = block('variable "dolphin_films_vercel_ready"')
        self.assertIn("type        = set(string)", gate)
        self.assertRegex(gate, r"default\s+= \[")
        self.assertIn("contains(keys(local.dolphin_films_secrets), name)", gate)
        self.assertIn("never with -var", TF)

    def test_nothing_is_read_unless_the_gate_names_it(self):
        version = block('data "aws_secretsmanager_secret_version" "dolphin_films"')
        self.assertIn("for_each  = var.dolphin_films_vercel_ready", version)
        token = block('ephemeral "aws_secretsmanager_secret_version" "dolphin_films_vercel_api_token"')
        self.assertIn("count     = length(var.dolphin_films_vercel_ready) > 0 ? 1 : 0", token)
        self.assertEqual(len(re.findall(r"aws_secretsmanager_secret_version", CODE)), 4, "two reads and their two uses")


class TokenTests(unittest.TestCase):
    def test_a_token_of_its_own_for_administration_only(self):
        secret = block('resource "aws_secretsmanager_secret" "dolphin_films_vercel_api_token"')
        self.assertIn('name                    = "vercel-api-token-dolphin-labs"', secret)
        self.assertIn("prevent_destroy = true", secret)
        policy = block('resource "aws_secretsmanager_secret_policy" "dolphin_films_vercel_api_token"')
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('Principal = "*"', policy)
        self.assertIn('Condition = { ArnNotLike = { "aws:PrincipalArn" = local.games_mp_admin_principals } }', policy)

    def test_the_provider_is_an_alias_and_reads_the_token_ephemerally(self):
        provider = block('provider "vercel"')
        self.assertIn('alias     = "dolphin_labs"', provider)
        self.assertIn("one(ephemeral.aws_secretsmanager_secret_version.dolphin_films_vercel_api_token[*].secret_string)", provider)
        self.assertNotIn("team ", provider)
        self.assertNotRegex(CODE, r"aws_secretsmanager_secret(?:_version)?\.vercel_api_token\b", "never the colton-games team's token")


class VariableTests(unittest.TestCase):
    def setUp(self):
        self.res = block('resource "vercel_project_environment_variable" "dolphin_films"')

    def test_every_variable_is_sensitive_on_one_target_through_the_alias(self):
        self.assertIn("provider = vercel.dolphin_labs", self.res)
        self.assertIn("sensitive  = true", self.res)
        self.assertIn("target     = [each.value.target]", self.res)
        self.assertIn("team_id    = var.dolphin_films_vercel_team_id", self.res)
        self.assertIn('dolphin_films_vercel_target     = { prod = "production", nonprod = "preview" }', TF)
        self.assertNotIn("development", CODE)

    def test_the_credentials_differ_between_production_and_preview(self):
        keys = re.search(r"dolphin_films_distinct_keys = \[([^\]]*)\]", TF).group(1)
        self.assertEqual(re.findall(r'"([^"]+)"', keys),
                         ["AUTH_SECRET", "AUTH_GOOGLE_ID", "AUTH_GOOGLE_SECRET", "TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"])
        self.assertIn("is the same in production and non-production", self.res)

    def test_values_have_their_shape_and_production_is_never_shut_to_everyone(self):
        for key in ("AUTH_SECRET", "AUTH_GOOGLE_ID", "TURSO_DATABASE_URL", "FILMS_SEED_EMAILS", "FILMS_ADMIN_EMAILS"):
            self.assertRegex(TF, r"\n    %s\s+= \"\^" % key, key)
        self.assertIn('length(local.dolphin_films_auth_extras["prod"]) == 2', self.res)

    def test_the_address_lists_come_from_the_people_secret(self):
        self.assertIn("try(tolist(local.people.dolphin_films[env].seeds), [])", TF)
        self.assertIn("try(tolist(local.people.dolphin_films[env].admins), [])", TF)
        # Static names and a declassified boolean: no sensitive mark reaches a for_each key.
        self.assertIn('for name in ["FILMS_SEED_EMAILS", "FILMS_ADMIN_EMAILS"] : name if nonsensitive(length(local.dolphin_films_people[env][name]) > 0)', TF)
        self.assertNotRegex(TF, r"for \w+, \w+ in local\.dolphin_films_people")

    def test_no_value_or_address_is_in_the_repository(self):
        for text in (TF, FILMS):
            self.assertNotRegex(text, r"[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}")
            self.assertNotRegex(text, r"libsql://[a-z0-9]|[a-z0-9-]+\.turso\.io|GOCSPX-|eyJ[A-Za-z0-9_-]{10,}")
            self.assertNotRegex(text, r"\d+-[a-z0-9]{20,}\.apps\.googleusercontent\.com")

    def test_the_output_carries_names_only(self):
        out = block('output "dolphin_films_vercel_written"')
        self.assertNotIn("value]", out.replace("value =", ""))
        self.assertNotIn("vercel_values", out)
        self.assertNotIn("secret_string", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)

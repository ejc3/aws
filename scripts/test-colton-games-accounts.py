#!/usr/bin/env python3
"""colton-games-accounts.tf: the accounts credentials of the colton-games site, split by
environment. Production's secrets are readable by the administration set only, non-production's
also by the two dev roles. Terraform reads a secret and writes its variables to Vercel only
once the checked-in gate names it; Production is written from prod and Preview from nonprod,
never one variable for both. Offline."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "colton-games-accounts.tf").read_text()
CODE = "\n".join(line for line in TF.splitlines() if not line.lstrip().startswith("#"))

AUTH = ["AUTH_GOOGLE_ID", "AUTH_GOOGLE_SECRET", "SITE_ADMIN_EMAILS"]
PUSH = ["NEXT_PUBLIC_VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"]
NAMES = ["nonprod/auth", "nonprod/push", "prod/auth", "prod/push"]

# Made-up values of the right shape (web-push prints 87 and 43 URL-safe base64 characters).
PUBLIC_KEY = "B" + "A1b2C3d4_-" * 8 + "E5f6G7"
PRIVATE_KEY = "h8I9j0K1_-" * 4 + "L2m"


def block(kind, name):
    m = re.search(r'^(?:resource|data|output|variable) (?:"%s" )?"%s" \{\n.*?^\}' % (kind, name), TF, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


def local(name):
    """The text of one top-level local: up to the next local, blank line, comment or the block's end."""
    m = re.search(r"^  %s\s*=\s*(.*?)(?=^  \w+\s*=|^\n|^  #|^\})" % name, TF, re.S | re.M)
    assert m, name
    return m.group(1)


def strings(text):
    return re.findall(r'"([^"]+)"', text)


def secrets():
    """{"<env>/<kind>": (keys expression, readers expression)} from local.colton_games_accounts_secrets."""
    table = re.search(r"^  colton_games_accounts_secrets = \{\n(.*?)^  \}", TF, re.S | re.M).group(1)
    rows = re.findall(r'^\s+"([^"]+)"\s*=\s*\{ keys = local\.(\w+), readers = (.+?) \}$', table, re.M)
    assert len(rows) == len(table.strip().splitlines()), "every row of the table must parse"
    return {name: (keys, readers) for name, keys, readers in rows}


def shapes():
    """{variable name: compiled pattern} from local.colton_games_accounts_shapes."""
    rows = re.findall(r'^\s+(\w+)\s*=\s*"((?:[^"\\]|\\.)*)"$', local("colton_games_accounts_shapes"), re.M)
    return {name: re.compile(pattern.replace("\\\\", "\\")) for name, pattern in rows}


class SecretTests(unittest.TestCase):
    def test_each_environment_has_its_own_auth_and_push_secret(self):
        self.assertEqual(sorted(secrets()), NAMES)
        for name, (keys, _) in secrets().items():
            self.assertEqual(keys, "colton_games_accounts_%s_keys" % name.split("/")[1], name)
        self.assertEqual(strings(local("colton_games_accounts_auth_keys")), AUTH)
        self.assertEqual(strings(local("colton_games_accounts_push_keys")), PUSH)
        secret = block("aws_secretsmanager_secret", "colton_games_accounts")
        self.assertIn("for_each = local.colton_games_accounts_secrets", secret)
        self.assertIn('name                    = "colton-games/${each.key}"', secret)

    def test_no_value_is_written_in_the_configuration(self):
        # Terraform reads a value (a data source); it never holds one in a .tf file.
        self.assertNotIn('resource "aws_secretsmanager_secret_version"', TF)
        self.assertNotRegex(TF, r"GOCSPX-[A-Za-z0-9_-]{6,}|\d+-[a-z0-9]{20,}\.apps\.googleusercontent\.com")
        self.assertNotRegex(TF, r"[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", "no address, not even an example")
        self.assertNotRegex(TF, r"\bB[A-Za-z0-9_-]{86}\b|mailto:[A-Za-z0-9]")

    def test_production_is_for_the_administration_set_only(self):
        for name, (_, readers) in secrets().items():
            if name.startswith("prod/"):
                self.assertEqual(readers, "[]", name)

    def test_non_production_adds_the_two_dev_roles_and_no_other(self):
        for name, (_, readers) in secrets().items():
            if name.startswith("nonprod/"):
                self.assertEqual(readers, "local.colton_games_accounts_nonprod_readers", name)
        self.assertIn(
            "colton_games_accounts_nonprod_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]", TF)
        self.assertEqual(set(re.findall(r"aws_iam_role\.(\w+)", CODE)), {"dev_server", "nextjs_dev"})

    def test_the_resource_policy_denies_everyone_but_admins_and_that_environments_readers(self):
        policy = block("aws_secretsmanager_secret_policy", "colton_games_accounts")
        self.assertIn("for_each = local.colton_games_accounts_secrets", policy)
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('Principal = "*"', policy)
        self.assertIn('Action    = "secretsmanager:GetSecretValue"', policy)
        self.assertIn("Resource  = aws_secretsmanager_secret.colton_games_accounts[each.key].arn", policy)
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, each.value.readers)', policy)

    def test_the_dev_roles_read_the_non_production_secrets_only(self):
        doc = block("aws_iam_policy_document", "colton_games_accounts_nonprod_read")
        self.assertIn('actions   = ["secretsmanager:GetSecretValue"]', doc)
        self.assertIn(
            "resources = [for key, secret in aws_secretsmanager_secret.colton_games_accounts : secret.arn "
            'if startswith(key, "nonprod/")]', doc)
        self.assertEqual(len(re.findall(r"^\s+statement \{", doc, re.M)), 1)
        # The only identity grant in the file.
        self.assertEqual(re.findall(r'^data "aws_iam_policy_document" "(\w+)"', TF, re.M),
                         ["colton_games_accounts_nonprod_read"])
        self.assertEqual(re.findall(r'^resource "aws_iam_role_policy_attachment" "(\w+)"', TF, re.M),
                         ["colton_games_accounts_nonprod_read"])
        attach = block("aws_iam_role_policy_attachment", "colton_games_accounts_nonprod_read")
        self.assertIn("{ dev_server = aws_iam_role.dev_server.name, nextjs_dev = aws_iam_role.nextjs_dev.name }", attach)
        self.assertIn("policy_arn = aws_iam_policy.colton_games_accounts_nonprod_read.arn", attach)
        self.assertNotRegex(TF, r'resource "aws_iam_role(_policy)?" ', "no new role and no inline policy")


class GateTests(unittest.TestCase):
    """A secret with no value yet must not be read: reading it fails every plan of the repo."""

    def test_the_gate_starts_empty_and_names_only_the_four_secrets(self):
        gate = block("", "colton_games_accounts_ready")
        self.assertIn("type        = set(string)", gate)
        self.assertRegex(gate, r"(?m)^  default     = \[\]$")
        self.assertIn(
            "alltrue([for name in var.colton_games_accounts_ready : contains(keys(local.colton_games_accounts_secrets), name)])",
            gate)

    def test_only_a_ready_secret_is_read(self):
        self.assertEqual(re.findall(r'^(?:data|ephemeral) "aws_secretsmanager_secret_version" "(\w+)"', TF, re.M),
                         ["colton_games_accounts"])
        read = block("aws_secretsmanager_secret_version", "colton_games_accounts")
        self.assertIn("for_each  = var.colton_games_accounts_ready", read)
        self.assertIn("secret_id = aws_secretsmanager_secret.colton_games_accounts[each.key].id", read)

    def test_only_a_ready_secrets_variables_are_written(self):
        env = local("colton_games_accounts_vercel_env")
        # The map's only source of names is the gate; a secret's own keys and, for an auth
        # secret, AUTH_SECRET hang off each name.
        self.assertEqual(re.findall(r"for name in ([\w.]+) :", env), ["var.colton_games_accounts_ready"])
        self.assertEqual(len(re.findall(r"\bfor \w+(?:, \w+)? in ", env)), 3)
        self.assertIn(
            'for key in concat(local.colton_games_accounts_secrets[name].keys, endswith(name, "/auth") ? ["AUTH_SECRET"] : []) :',
            env)
        self.assertIn(']) : "${entry.key}/${entry.target}" => entry', env)
        self.assertEqual(re.findall(r'^resource "vercel_\w+" "(\w+)"', TF, re.M), ["colton_games_accounts"])
        self.assertIn("for_each = local.colton_games_accounts_vercel_env",
                      block("vercel_project_environment_variable", "colton_games_accounts"))


class VercelTests(unittest.TestCase):
    def test_production_is_written_from_prod_and_preview_from_nonprod(self):
        self.assertIn('colton_games_accounts_vercel_target = { prod = "production", nonprod = "preview" }', TF)
        env = local("colton_games_accounts_vercel_env")
        self.assertIn('target = local.colton_games_accounts_vercel_target[split("/", name)[0]] }', env)
        variable = block("vercel_project_environment_variable", "colton_games_accounts")
        self.assertIn("project_id = local.colton_games_vercel_project_id", variable)
        self.assertIn('key        = split("/", each.key)[0]', variable)
        self.assertIn("value      = local.colton_games_accounts_vercel_values[each.key]", variable)
        # One target per variable: no value ever spans both environments, and none reaches Development.
        self.assertIn("target     = [each.value.target]", variable)
        self.assertNotIn("development", CODE)
        values = local("colton_games_accounts_vercel_values")
        self.assertIn("local.colton_games_accounts_payload[entry.secret][entry.key]", values)

    def test_every_variable_is_sensitive(self):
        variable = block("vercel_project_environment_variable", "colton_games_accounts")
        self.assertIn("sensitive  = true", variable)
        self.assertNotRegex(CODE, r"sensitive\s*=\s*false")

    def test_only_the_push_public_key_reaches_the_browser(self):
        self.assertEqual(set(re.findall(r"NEXT_PUBLIC_\w+", CODE)), {"NEXT_PUBLIC_VAPID_PUBLIC_KEY"})

    def test_the_session_secret_is_generated_once_per_environment(self):
        secret = block("random_password", "colton_games_auth_secret")
        self.assertIn('for_each = toset(["production", "preview"])', secret)
        self.assertGreaterEqual(int(re.search(r"length\s*=\s*(\d+)", secret).group(1)), 32)
        self.assertIn("special  = false", secret)
        # It is in no secret's JSON, so nobody types or copies it.
        self.assertNotIn("AUTH_SECRET", AUTH + PUSH)
        self.assertNotIn("AUTH_SECRET", local("colton_games_accounts_auth_keys"))
        self.assertIn("random_password.colton_games_auth_secret[entry.target].result",
                      local("colton_games_accounts_vercel_values"))

    def test_a_missing_or_misplaced_value_fails_the_plan(self):
        variable = block("vercel_project_environment_variable", "colton_games_accounts")
        self.assertIn(
            'condition     = can(regex(lookup(local.colton_games_accounts_shapes, each.value.key, "\\\\S"), '
            "local.colton_games_accounts_vercel_values[each.key]))", variable)
        # A secret that is not a JSON object, and a key it lacks, become "", which no shape accepts.
        self.assertIn('try(jsondecode(version.secret_string), {})', local("colton_games_accounts_payload"))
        self.assertRegex(local("colton_games_accounts_vercel_values"), r'trimspace\(try\(tostring\(.*\), ""\)\)')
        shape = shapes()
        self.assertFalse(re.compile(r"\S").search(""))
        self.assertTrue(shape["NEXT_PUBLIC_VAPID_PUBLIC_KEY"].search(PUBLIC_KEY))
        self.assertTrue(shape["VAPID_PRIVATE_KEY"].search(PRIVATE_KEY))
        # The public key is compiled into the page: the private key must never pass as it.
        self.assertFalse(shape["NEXT_PUBLIC_VAPID_PUBLIC_KEY"].search(PRIVATE_KEY))
        self.assertFalse(shape["VAPID_PRIVATE_KEY"].search(PUBLIC_KEY))
        self.assertTrue(shape["AUTH_GOOGLE_ID"].search("1234-placeholder.apps.googleusercontent.com"))
        self.assertFalse(shape["AUTH_GOOGLE_ID"].search("GOCSPX-placeholder"))
        self.assertTrue(shape["VAPID_SUBJECT"].search("https://example.com"))
        self.assertFalse(shape["VAPID_SUBJECT"].search("example.com"))
        for name in shape:
            self.assertFalse(shape[name].search(""), name)

    def test_the_two_environments_cannot_be_given_the_same_credential(self):
        self.assertEqual(sorted(strings(local("colton_games_accounts_distinct_keys"))),
                         ["AUTH_GOOGLE_ID", "AUTH_GOOGLE_SECRET", "NEXT_PUBLIC_VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY"])
        variable = block("vercel_project_environment_variable", "colton_games_accounts")
        self.assertEqual(len(re.findall(r"^\s+precondition \{", variable, re.M)), 2)
        self.assertIn("!contains(local.colton_games_accounts_distinct_keys, each.value.key)", variable)
        self.assertIn("local.colton_games_accounts_vercel_values[each.key] != lookup(", variable)
        self.assertIn(
            'local.colton_games_accounts_vercel_values, "${each.value.key}/'
            '${local.colton_games_accounts_other_target[each.value.target]}", ""', variable)
        self.assertIn('colton_games_accounts_other_target  = { production = "preview", preview = "production" }', TF)

    def test_outputs_carry_names_only(self):
        names = block("", "colton_games_accounts_secrets")
        self.assertIn(
            "value       = { for key, secret in aws_secretsmanager_secret.colton_games_accounts : key => secret.name }", names)
        env = block("", "colton_games_accounts_vercel_env")
        self.assertIn("entry.key if entry.target == target", env)
        for out in (names, env):
            self.assertNotIn("secret_string", out)
            self.assertNotIn("vercel_values", out)
            self.assertNotIn("random_password", out)
            self.assertNotIn("sensitive", out)
        self.assertEqual(len(re.findall(r"^output ", TF, re.M)), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)

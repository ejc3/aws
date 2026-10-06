#!/usr/bin/env python3
"""dolphin-films.tf: credentials split by environment, sign-in and the Turso database each
environment has. Production's secrets are readable by the administration set only;
non-production's also by dev-server-role, which may read those and nothing else here. No
value is in Terraform, and the builders bring no new AI grant. Offline."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "dolphin-films.tf").read_text()
COMMON = (ROOT / "dev-instance-common.tf").read_text()
AI = (ROOT / "dev-ai-services.tf").read_text()

AUTH = ["AUTH_SECRET", "AUTH_GOOGLE_ID", "AUTH_GOOGLE_SECRET"]
TURSO = ["TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN"]
CODE = "\n".join(line for line in TF.splitlines() if not line.lstrip().startswith("#"))


def block(kind, name):
    m = re.search(r'^(?:resource|data|output) (?:"%s" )?"%s" \{\n.*?^\}' % (kind, name), TF, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


def key_list(name):
    m = re.search(r"^  %s\s*=\s*\[([^\]]*)\]" % name, TF, re.M)
    assert m, name
    return re.findall(r'"([^"]+)"', m.group(1))


def secrets():
    """{"<env>/<kind>": (keys local, readers expression)} from local.dolphin_films_secrets."""
    table = re.search(r"^  dolphin_films_secrets = \{\n(.*?)^  \}", TF, re.S | re.M).group(1)
    rows = re.findall(r'^\s+"([^"]+)"\s*=\s*\{ keys = local\.(\w+), readers = (.+?) \}$', table, re.M)
    assert len(rows) == len(table.strip().splitlines()), "every row of the table must parse"
    return {name: (keys, readers) for name, keys, readers in rows}


class SecretTests(unittest.TestCase):
    def test_each_environment_has_its_own_auth_and_turso_secret(self):
        self.assertEqual(sorted(secrets()), ["nonprod/auth", "nonprod/turso", "prod/auth", "prod/turso"])
        for name, (keys, _) in secrets().items():
            self.assertEqual(keys, "dolphin_films_%s_keys" % name.split("/")[1], name)
        self.assertEqual(key_list("dolphin_films_auth_keys"), AUTH)
        self.assertEqual(key_list("dolphin_films_turso_keys"), TURSO)
        secret = block("aws_secretsmanager_secret", "dolphin_films")
        self.assertIn("for_each = local.dolphin_films_secrets", secret)
        self.assertIn('name                    = "dolphin-films/${each.key}"', secret)

    def test_no_value_is_in_terraform_or_read_by_it(self):
        # Containers only: no version resource, and no data source or ephemeral read that
        # would carry a value into state or a plan.
        self.assertNotIn("aws_secretsmanager_secret_version", TF)
        self.assertNotIn("vercel_project_environment_variable", TF)
        self.assertNotRegex(TF, r"eyJ[A-Za-z0-9_-]{10,}|sb_secret_[A-Za-z0-9_]{6,}|GOCSPX-[A-Za-z0-9_-]{6,}")
        self.assertNotRegex(TF, r"libsql://[a-z0-9]|[a-z0-9-]+\.turso\.io|\d+-[a-z0-9]{20,}\.apps\.googleusercontent\.com")
        self.assertNotRegex(TF, r"[A-Za-z0-9._+-]+@[A-Za-z0-9-]+\.[A-Za-z]{2,}", "no address, the list variables included")
        self.assertNotIn("NEXT_PUBLIC", TF, "the browser never talks to the database")

    def test_the_store_is_turso_and_nothing_of_supabase_is_left(self):
        self.assertNotRegex(TF, r"(?i)supabase")

    def test_the_app_never_gets_the_platform_token(self):
        # turso/api-token (dev-ai-services.tf) creates and deletes databases and mints their
        # tokens. It is not one of the site's variables and this file grants nothing on it.
        self.assertNotRegex(CODE, r"TURSO_API_TOKEN|turso_api_token|turso/api-token")
        self.assertIn("turso/api-token", TF, "the header says how it differs from the per-database values")

    def test_production_is_for_the_administration_set_only(self):
        for name, (_, readers) in secrets().items():
            if name.startswith("prod/"):
                self.assertEqual(readers, "[]", name)

    def test_non_production_adds_the_metal_dev_role_and_no_other(self):
        for name, (_, readers) in secrets().items():
            if name.startswith("nonprod/"):
                self.assertEqual(readers, "local.dolphin_films_nonprod_readers", name)
        self.assertIn("dolphin_films_nonprod_readers = [aws_iam_role.dev_server.arn]", TF)
        # Every account on nextjs-dev has sudo, so a grant to its role would be box-wide.
        self.assertNotIn("aws_iam_role.nextjs_dev", TF)

    def test_the_resource_policy_denies_everyone_but_admins_and_that_environments_readers(self):
        policy = block("aws_secretsmanager_secret_policy", "dolphin_films")
        self.assertIn("for_each = local.dolphin_films_secrets", policy)
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('Principal = "*"', policy)
        self.assertIn('Action    = "secretsmanager:GetSecretValue"', policy)
        self.assertIn("Resource  = aws_secretsmanager_secret.dolphin_films[each.key].arn", policy)
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, each.value.readers)', policy)

    def test_the_dev_role_reads_the_non_production_secrets_only(self):
        doc = block("aws_iam_policy_document", "dolphin_films_nonprod_read")
        self.assertIn('actions   = ["secretsmanager:GetSecretValue"]', doc)
        self.assertIn(
            'resources = [for key, secret in aws_secretsmanager_secret.dolphin_films : secret.arn if startswith(key, "nonprod/")]',
            doc)
        self.assertEqual(len(re.findall(r"^\s+statement \{", doc, re.M)), 1)
        # The only identity grant in the file, and only to dev-server-role.
        self.assertEqual(re.findall(r'^data "aws_iam_policy_document" "(\w+)"', TF, re.M), ["dolphin_films_nonprod_read"])
        self.assertEqual(re.findall(r'^resource "aws_iam_role_policy_attachment" "(\w+)"', TF, re.M),
                         ["dolphin_films_nonprod_read"])
        attach = block("aws_iam_role_policy_attachment", "dolphin_films_nonprod_read")
        self.assertIn("role       = aws_iam_role.dev_server.name", attach)
        self.assertIn("policy_arn = aws_iam_policy.dolphin_films_nonprod_read.arn", attach)
        self.assertNotRegex(TF, r'resource "aws_iam_role(_policy)?" ', "no new role and no inline policy")

    def test_outputs_carry_names_only(self):
        names = block("", "dolphin_films_secrets")
        self.assertIn("value       = { for key, secret in aws_secretsmanager_secret.dolphin_films : key => secret.name }", names)
        env = block("", "dolphin_films_vercel_env")
        self.assertIn('{ production = "prod", preview = "nonprod" }', env)
        self.assertIn('=> secret.keys if startswith(key, "${env}/")', env)
        # The two lists the site reads are set by the owner in the hosting project: named for
        # every target, held nowhere here.
        self.assertEqual(key_list("dolphin_films_owner_env"), ["FILMS_SEED_EMAILS", "FILMS_ADMIN_EMAILS"])
        self.assertIn('{ "set by the owner in the hosting project" = local.dolphin_films_owner_env },', env)
        self.assertEqual(len(re.findall(r"FILMS_(?:SEED|ADMIN)_EMAILS", CODE)), 2, "named once each, never given a value")
        for out in (names, env):
            self.assertNotIn("secret_string", out)
            self.assertNotIn("sensitive", out)


RUNNER_APP = (ROOT / "runner-app.tf").read_text()


class WebhookGateTests(unittest.TestCase):
    """The controller token is minted by the org owner and put by hand. Reading a secret without a value fails EVERY plan of
    the repository, so dolphin-films' token read and webhook sit behind a gate that starts false."""

    def test_the_gate_is_a_committed_default_never_a_flag(self):
        # It started false (no token had a value, and reading an empty secret fails every plan); it is true once the token
        # is stored. Either way it lives in the file: a -var would make a later plan propose deleting the webhook.
        gate = re.search(r'variable "dolphin_films_token_ready" \{.*?\n\}', RUNNER_APP, re.S).group()
        self.assertRegex(gate, r"default\s+= (true|false)")
        self.assertIn("never with -var", RUNNER_APP)

    def test_nothing_about_dolphin_films_is_read_or_planned_unless_the_gate_is_true(self):
        self.assertIn("dolphin_films_webhook = local.runner_app_webhooks && var.dolphin_films_token_ready", RUNNER_APP)
        # the token read skips the repo, the provider reads no token and the webhook has no count
        self.assertIn("if r != \"dolphin-labs-hq/dolphin-films\" || var.dolphin_films_token_ready", RUNNER_APP)
        self.assertIn("for_each  = local.runner_app_webhooks ? toset(local.runner_app_webhook_repos) : toset([])", RUNNER_APP)
        provider = re.search(r'alias = "dolphin_films".*?\n\}', RUNNER_APP, re.S).group()
        self.assertIn("token = local.dolphin_films_webhook ?", provider)
        hook = re.search(r'resource "github_repository_webhook" "runner_app_dolphin_films" \{.*?\n  count\s+= (.*?)\n', RUNNER_APP, re.S)
        self.assertEqual(hook.group(1), "local.dolphin_films_webhook ? 1 : 0")

    def test_the_other_repos_keep_the_global_gate_only(self):
        self.assertIn("count      = local.runner_app_webhooks ? 1 : 0", RUNNER_APP)
        for repo in ("CoderColton/colton-games", "dolphin-labs-hq/dolphin-labs"):
            self.assertIn('runner_repo_pat["%s"]' % repo, RUNNER_APP)


class BuilderTests(unittest.TestCase):
    """The offline builders need nothing new: they run as dev-server-role, which already has these."""

    def test_no_ai_grant_or_llm_key_is_added_here(self):
        code = "\n".join(line for line in TF.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("bedrock", code.lower())
        self.assertNotRegex(code, r"(?i)anthropic|api[-_]key")

    def test_the_metal_role_already_invokes_claude_on_bedrock(self):
        invoke = re.search(r'Sid\s*=\s*"BedrockRuntimeInvoke".*?Resource\s*=\s*\[(.*?)\]', COMMON, re.S)
        self.assertIsNotNone(invoke)
        self.assertIn('"arn:aws:bedrock:*::foundation-model/anthropic.*"', invoke.group(1))
        self.assertRegex(invoke.group(1), r'"arn:aws:bedrock:\*:\d{12}:inference-profile/\*"')
        self.assertIn('"bedrock:Converse"', invoke.group(0))

    def test_the_metal_role_already_reads_browserbase_and_elevenlabs(self):
        for name in ("browserbase_read", "elevenlabs_read"):
            attach = re.search(r'resource "aws_iam_role_policy_attachment" "%s" \{.*?\n\}' % name, AI, re.S).group()
            self.assertIn("dev_server = aws_iam_role.dev_server.name", attach, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)

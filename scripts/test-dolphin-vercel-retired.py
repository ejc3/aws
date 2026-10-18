#!/usr/bin/env python3
"""dolphin-vercel-retired.tf: the dolphin-labs Vercel team is being deleted, so no Terraform here manages it any more.
What vercel-env.tf and the old dolphin-films-vercel.tf managed there is FORGOTTEN (removed blocks, destroy = false: no
Vercel call, so the plan is the same before and after the deletion), the provider alias stays only as a placeholder that
reads no secret, and the AWS containers that feed the Cloudflare production Workers stay."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEAM = "team_4TayirAcbgetrOUgYXghMXJH"
RETIRED = (ROOT / "dolphin-vercel-retired.tf").read_text()


def tf_files():
    for path in sorted(ROOT.rglob("*.tf")):
        if any(part.startswith(".") for part in path.relative_to(ROOT).parts):
            continue  # .terraform, .claude worktrees
        yield path


def code(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


ALL_CODE = {path.relative_to(ROOT).as_posix(): code(path.read_text()) for path in tf_files()}


class NothingManagesTheTeam(unittest.TestCase):
    def test_no_terraform_names_the_team(self):
        self.assertGreater(len(ALL_CODE), 50)
        for name, text in ALL_CODE.items():
            self.assertNotIn(TEAM, text, name)

    def test_no_block_uses_the_dolphin_labs_vercel_provider(self):
        for name, text in ALL_CODE.items():
            self.assertNotRegex(text, r"provider\s*=\s*vercel\.dolphin_labs\b", name)

    def test_the_old_resources_and_their_gate_are_gone(self):
        self.assertFalse((ROOT / "dolphin-films-vercel.tf").exists())
        for name, text in ALL_CODE.items():
            for gone in ('resource "vercel_project_environment_variable" "vercel_env_dolphin_labs"',
                         'resource "vercel_project_environment_variable" "dolphin_films"',
                         'resource "aws_secretsmanager_secret" "dolphin_films_vercel_api_token"',
                         'resource "aws_secretsmanager_secret_policy" "dolphin_films_vercel_api_token"',
                         "dolphin_films_vercel_ready", "dolphin_films_vercel_team_id", "dolphin_films_vercel_api_token\"",
                         'secret_id = aws_secretsmanager_secret.dolphin_films_vercel_api_token'):
                self.assertNotIn(gone, text, name)

    def test_the_scripts_no_longer_reach_the_team(self):
        for script in ("vercel-env-inventory.py", "vercel-env-capture.py"):
            text = (ROOT / "scripts" / script).read_text()
            self.assertNotIn(TEAM, text, script)
            self.assertNotRegex(text, r'"dolphin-(labs|films)": \(', script)


class ForgottenNotDestroyed(unittest.TestCase):
    def removed(self):
        return re.findall(r"^removed \{\n  from = (\S+)\n\n  lifecycle \{\n    destroy = false\n  \}\n\}$", RETIRED, re.M)

    def test_exactly_these_four_are_forgotten_and_none_destroyed(self):
        self.assertEqual(sorted(self.removed()), [
            "aws_secretsmanager_secret.dolphin_films_vercel_api_token",
            "aws_secretsmanager_secret_policy.dolphin_films_vercel_api_token",
            "vercel_project_environment_variable.dolphin_films",
            "vercel_project_environment_variable.vercel_env_dolphin_labs",
        ])
        self.assertEqual(RETIRED.count("removed {"), 4)
        self.assertNotIn("destroy = true", RETIRED)

    def test_the_provider_alias_is_a_placeholder_that_reads_nothing(self):
        # Terraform refuses to plan an object whose provider configuration is gone, a forget included, so the alias stays
        # until the apply; a forget never calls the API, so the token is a constant of the shape Vercel's provider accepts.
        provider = re.search(r'^provider "vercel" \{\n(.*?)^\}', RETIRED, re.M | re.S).group(1)
        self.assertIn('alias     = "dolphin_labs"', provider)
        self.assertRegex(provider, r'api_token = "[a-z]{24}"\n')
        self.assertNotIn("ephemeral", code(RETIRED))
        self.assertNotIn("data ", code(RETIRED))
        self.assertNotRegex(code(RETIRED), r'^resource ', "only the provider and removed blocks")


class TheWorkersSourcesStay(unittest.TestCase):
    def test_the_dolphin_containers_are_still_managed(self):
        secrets = ALL_CODE["vercel-env-secrets.tf"]
        self.assertIn('"dolphin-labs"  = ["development", "preview", "production"]', secrets)
        self.assertIn('"dolphin-films" = ["preview", "production"]', secrets)
        self.assertIn('resource "aws_secretsmanager_secret" "dolphin_films"', ALL_CODE["dolphin-films.tf"])


if __name__ == "__main__":
    unittest.main()

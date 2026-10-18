#!/usr/bin/env python3
"""The dolphin-labs Vercel team is retired (2026-10-18, ejc3/aws #503): Terraform forgot what it managed there and destroyed
the AWS secret that held its token. dolphin-vercel-retired.tf, which did that, is gone now that it has applied: no Terraform
names the team, uses its provider alias or brings its resources back, and the AWS containers that feed the Cloudflare
production Workers stay."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEAM = "team_4TayirAcbgetrOUgYXghMXJH"


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


class TheRetirementFileIsGone(unittest.TestCase):
    def test_the_one_shot_file_and_its_alias_are_gone(self):
        # It forgot the Vercel objects and destroyed the token secret in one apply; nothing is left for it to do.
        self.assertFalse((ROOT / "dolphin-vercel-retired.tf").exists())
        for name, text in ALL_CODE.items():
            # Other providers (the GitHub one) have their own dolphin_labs alias; only the Vercel one is gone.
            self.assertNotRegex(text, r'provider "vercel" \{[^}]*alias\s*=\s*"dolphin_labs"', name)
            self.assertNotIn("vercel_project_environment_variable.vercel_env_dolphin_labs", text, name)


class TheWorkersSourcesStay(unittest.TestCase):
    def test_the_dolphin_containers_are_still_managed(self):
        secrets = ALL_CODE["vercel-env-secrets.tf"]
        self.assertIn('"dolphin-labs"  = ["development", "preview", "production"]', secrets)
        self.assertIn('"dolphin-films" = ["preview", "production"]', secrets)
        self.assertIn('resource "aws_secretsmanager_secret" "dolphin_films"', ALL_CODE["dolphin-films.tf"])


if __name__ == "__main__":
    unittest.main()

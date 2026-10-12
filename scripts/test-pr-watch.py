#!/usr/bin/env python3
"""Tests for the PR and issue watch installer and its SessionStart hook (scripts/pr-watch/)."""
import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
HOOK = ROOT / "scripts/pr-watch/session-start.sh"
INSTALL = ROOT / "scripts/pr-watch-install.sh"


def run(cmd, home, **kw):
    env = dict(os.environ, HOME=str(home))
    return subprocess.run(cmd, env=env, capture_output=True, text=True, **kw)


class HookTests(unittest.TestCase):
    def test_first_session_owns_the_watch_and_a_live_owner_silences_others(self):
        with tempfile.TemporaryDirectory() as home:
            first = run(["bash", str(HOOK)], home, input="{}")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("/pr-watch", json.loads(first.stdout)["hookSpecificOutput"]["additionalContext"])
            # A live process whose command line names claude owns it: another session is told nothing.
            fake = subprocess.Popen(["bash", "-c", "exec -a claude-fake sleep 30"])
            try:
                time.sleep(0.2)
                (pathlib.Path(home) / ".local/state/pr-watch/owner").write_text(f"{fake.pid}\n")
                second = run(["bash", str(HOOK)], home, input="{}")
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(second.stdout, "")
            finally:
                fake.kill()
                fake.wait()
            # Once the owner is gone, the next session takes over.
            third = run(["bash", str(HOOK)], home, input="{}")
            self.assertIn("/pr-watch", third.stdout)

    def test_a_reused_pid_that_is_not_claude_does_not_keep_ownership(self):
        with tempfile.TemporaryDirectory() as home:
            other = subprocess.Popen(["sleep", "30"])
            try:
                state = pathlib.Path(home) / ".local/state/pr-watch"
                state.mkdir(parents=True)
                (state / "owner").write_text(f"{other.pid}\n")
                self.assertIn("/pr-watch", run(["bash", str(HOOK)], home, input="{}").stdout)
            finally:
                other.kill()
                other.wait()


class InstallerTests(unittest.TestCase):
    def test_watcher_install_keeps_other_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as home:
            claude = pathlib.Path(home) / ".claude"
            claude.mkdir()
            existing = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "keep-me"}]}],
                                  "SessionStart": [{"hooks": [{"type": "command", "command": "other-start"}]}]},
                        "model": "example-model"}
            (claude / "settings.json").write_text(json.dumps(existing))
            for _ in range(2):
                result = run(["bash", str(INSTALL), "--watcher"], home)
                self.assertEqual(result.returncode, 0, result.stderr)
            settings = json.loads((claude / "settings.json").read_text())
            self.assertEqual(settings["model"], "example-model")
            self.assertEqual(settings["hooks"]["PreToolUse"], existing["hooks"]["PreToolUse"])
            commands = [h["command"] for group in settings["hooks"]["SessionStart"] for h in group["hooks"]]
            self.assertEqual(commands.count("other-start"), 1)
            self.assertEqual(len([c for c in commands if "pr-watch" in c]), 1)
            self.assertTrue((claude / "skills/pr-watch/SKILL.md").is_file())

    def test_rejects_unknown_usage(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(run(["bash", str(INSTALL)], home).returncode, 2)
            self.assertEqual(run(["bash", str(INSTALL), "--dev-rule"], home).returncode, 2)


class RuleTextTests(unittest.TestCase):
    def test_dev_boxes_propose_only(self):
        rule = (ROOT / "scripts/pr-watch/dev-box-rule.md").read_text()
        for phrase in ("Never merge", "push to a default branch", "apply Terraform", "draft"):
            self.assertIn(phrase, rule)


if __name__ == "__main__":
    unittest.main(verbosity=2)

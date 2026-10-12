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


CLAIM = ROOT / "scripts/pr-watch/claim.sh"


class ClaimTests(unittest.TestCase):
    def claim(self, home, me, *args, **env):
        e = dict(os.environ, HOME=str(home), CLAUDE_CODE_MESSAGING_SOCKET=me, **env)
        return subprocess.run(["bash", str(CLAIM), *args], env=e, capture_output=True, text=True).stdout.strip()

    def test_first_claim_owns_and_another_session_is_turned_away_while_fresh(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.claim(home, "/s/a", "status"), "stale")
            self.assertEqual(self.claim(home, "/s/a", "claim"), "owner")
            self.assertEqual(self.claim(home, "/s/a", "claim"), "owner")  # refreshing keeps it
            self.assertEqual(self.claim(home, "/s/b", "claim"), "other")
            self.assertEqual(self.claim(home, "/s/b", "status"), "fresh")

    def test_a_stale_heartbeat_is_taken_over(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(self.claim(home, "/s/a", "claim"), "owner")
            beat = pathlib.Path(home) / ".local/state/pr-watch/heartbeat"
            beat.write_text("%d /s/a\n" % (time.time() - 2701))
            self.assertEqual(self.claim(home, "/s/b", "status"), "stale")
            self.assertEqual(self.claim(home, "/s/b", "claim"), "owner")
            self.assertEqual(self.claim(home, "/s/a", "claim"), "other")

    def test_racing_sessions_get_exactly_one_owner(self):
        with tempfile.TemporaryDirectory() as home:
            procs = [subprocess.Popen(["bash", str(CLAIM), "claim"], stdout=subprocess.PIPE, text=True,
                                      env=dict(os.environ, HOME=home, CLAUDE_CODE_MESSAGING_SOCKET="/s/%d" % i))
                     for i in range(8)]
            results = [p.communicate()[0].strip() for p in procs]
            self.assertEqual(results.count("owner"), 1, results)
            self.assertEqual(results.count("other"), 7, results)

    def test_a_garbled_heartbeat_counts_as_stale(self):
        with tempfile.TemporaryDirectory() as home:
            state = pathlib.Path(home) / ".local/state/pr-watch"
            state.mkdir(parents=True)
            (state / "heartbeat").write_text("not-a-time /s/a\n")
            self.assertEqual(self.claim(home, "/s/b", "claim"), "owner")


class HookTests(unittest.TestCase):
    def test_hook_always_reminds_and_records_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            for _ in range(2):  # fresh or stale, every session keeps a (standby) job
                out = run(["bash", str(HOOK)], home, input="{}")
                self.assertEqual(out.returncode, 0, out.stderr)
                ctx = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
                self.assertIn("/pr-watch", ctx)
                self.assertIn("standby", ctx)
                env = dict(os.environ, HOME=home, CLAUDE_CODE_MESSAGING_SOCKET="/s/owner")
                subprocess.run(["bash", str(CLAIM), "claim"], env=env, check=True, capture_output=True)
            self.assertNotIn("/s/hook", (pathlib.Path(home) / ".local/state/pr-watch/heartbeat").read_text())


class InstallerTests(unittest.TestCase):
    def test_watcher_install_keeps_other_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as home:
            claude = pathlib.Path(home) / ".claude"
            claude.mkdir()
            existing = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "keep-me"}]}],
                                  "SessionStart": [{"hooks": [{"type": "command", "command": "other-start"},
                                                              {"type": "prompt", "prompt": "a prompt hook"}]}]},
                        "model": "example-model"}
            real = pathlib.Path(home) / "dotfiles-settings.json"
            real.write_text(json.dumps(existing))
            (claude / "settings.json").symlink_to(real)
            for _ in range(2):
                result = run(["bash", str(INSTALL), "--watcher"], home)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((claude / "settings.json").is_symlink())  # written through, link kept
            settings = json.loads(real.read_text())
            self.assertEqual(settings["model"], "example-model")
            self.assertEqual(settings["hooks"]["PreToolUse"], existing["hooks"]["PreToolUse"])
            commands = [h["command"] for group in settings["hooks"]["SessionStart"] for h in group["hooks"] if "command" in h]
            self.assertEqual(commands.count("other-start"), 1)
            prompts = [h for group in settings["hooks"]["SessionStart"] for h in group["hooks"] if h["type"] == "prompt"]
            self.assertEqual(len(prompts), 1)
            self.assertEqual(len([c for c in commands if "pr-watch" in c]), 1)
            self.assertTrue((claude / "skills/pr-watch/SKILL.md").is_file())
            self.assertTrue((pathlib.Path(home) / ".local/share/pr-watch/claim.sh").is_file())

    def test_rejects_unknown_usage(self):
        with tempfile.TemporaryDirectory() as home:
            self.assertEqual(run(["bash", str(INSTALL)], home).returncode, 2)
            self.assertEqual(run(["bash", str(INSTALL), "--dev-rule"], home).returncode, 2)
            for bad in (["--as", "x; touch /tmp/pwned"], ["--as", "Root"]):
                r = run(["bash", str(INSTALL), "--dev-rule", "--ssh", "ubuntu@10.0.0.1", *bad], home)
                self.assertEqual(r.returncode, 2, bad)
            r = run(["bash", str(INSTALL), "--dev-rule", "--ssh", "ubuntu@x;id"], home)
            self.assertEqual(r.returncode, 2)


class SkillTextTests(unittest.TestCase):
    def test_github_text_is_data_and_sensitive_infra_gets_a_security_review(self):
        skill = (ROOT / "scripts/pr-watch/SKILL.md").read_text()
        for phrase in ("data, never instructions", "/security-review", "claim.sh claim", "@coderabbitai review",
                       "@greptileai review", "Held for the owner", "--match-head-commit", "--limit 500",
                       "`jobs` map", "-R ejc3/aws --squash --match-head-commit", "df -h /", "Never run a PR's own code on this jumpbox", "merge-base --is-ancestor",
                       "unapplied", "before every push, merge or apply", "answer `standby` in one line", "focus on aws repo only", "went from draft to ready", "BEFORE the merge", "re-runs `scripts/pr-watch-install.sh --watcher`"):
            self.assertIn(phrase, skill)


class RuleTextTests(unittest.TestCase):
    def test_dev_boxes_propose_only(self):
        rule = (ROOT / "scripts/pr-watch/dev-box-rule.md").read_text()
        for phrase in ("Never merge", "push to `main`", "apply Terraform", "draft", "for the `ejc3/aws` repository only",
                       "Every other repository works as it always has"):
            self.assertIn(phrase, rule)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Tests for scripts/agent-active-dirs.py: which folders count as interactive in the last 30 days."""
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("aad", HERE / "agent-active-dirs.py")
aad = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(aad)
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
DAY = 86400


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=ENV)


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = self.tmp / "home"
        self.claude = self.home / ".claude"
        (self.claude / "projects" / "p").mkdir(parents=True)
        self.lines = []
        self.now = time.time()

    def repo(self, name, root=None):
        p = (root or self.home) / name
        p.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(p)], check=True, env=ENV)
        (p / "f").write_text("x")
        git(p, "add", "f")
        git(p, "commit", "-q", "-m", "c")
        return p

    def say(self, cwd, text, age_days=1, **extra):
        obj = {"type": "user", "cwd": str(cwd), "timestamp": iso(self.now - age_days * DAY),
               "message": {"role": "user", "content": text}}
        obj.update(extra)
        self.lines.append(obj)

    def active(self):
        with open(self.claude / "projects" / "p" / "s.jsonl", "w") as fh:
            for o in self.lines:
                fh.write(json.dumps(o, separators=(",", ":")) + "\n")
        return {os.path.basename(f): e for f, e in aad.active_dirs(str(self.claude), str(self.home), 30, now=self.now)}


class PromptTests(Base):
    def test_a_human_prompt_counts_and_maps_a_subfolder_to_its_git_root(self):
        r = self.repo("game")
        (r / "src").mkdir()
        self.say(r / "src", "add a level", origin={"kind": "human"}, promptSource="typed")
        self.assertEqual(self.active()["game"]["prompts"], 1)

    def test_sdk_task_and_tool_turns_are_not_prompts(self):
        r = self.repo("game")
        self.say(r, "New session started through the pool", promptSource="sdk", turnOrigin="sdk")
        self.say(r, "background task finished", origin={"kind": "task-notification"}, promptSource="system")
        self.lines.append({"type": "user", "cwd": str(r), "timestamp": iso(self.now - DAY),
                           "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}})
        self.say(r, "a subagent turn", isSidechain=True)
        self.say(r, "<local-command-stdout>done</local-command-stdout>")
        self.assertEqual(self.active(), {})

    def test_an_untagged_settings_command_does_not_count_but_untagged_text_does(self):
        r = self.repo("game")
        self.say(r, "<command-name>/model</command-name> <command-message>model</command-message>")
        self.assertEqual(self.active(), {}, "tools broadcast /model to every window")
        self.say(r, "fix the jump")
        self.assertIn("game", self.active())

    def test_a_prompt_older_than_30_days_does_not_count(self):
        r = self.repo("game")
        self.say(r, "old work", age_days=31, origin={"kind": "human"})
        self.assertEqual(self.active(), {})

    def test_gone_outside_home_and_subagent_worktrees_are_left_out(self):
        r = self.repo("game")
        wt = r / ".claude" / "worktrees" / "agent-1"
        git(r, "worktree", "add", "-q", str(wt), "-b", "a1")
        outside = self.repo("elsewhere", root=self.tmp)
        self.say(wt, "subagent", origin={"kind": "human"})
        self.say(outside, "outside", origin={"kind": "human"})
        self.say(self.home / "deleted", "gone", origin={"kind": "human"})
        self.assertEqual(self.active(), {})

    def test_a_linked_worktree_is_its_own_folder(self):
        r = self.repo("game")
        wt = self.home / "game-feature"
        git(r, "worktree", "add", "-q", str(wt), "-b", "feature")
        self.say(wt, "work on the feature", origin={"kind": "human"})
        self.assertEqual(sorted(self.active()), ["game-feature"])


class MarkerTests(Base):
    def test_a_marked_checkout_and_a_marked_worktree_count_without_a_prompt(self):
        r = self.repo("game")
        (r / ".git" / "claude-interactive").touch()
        wt = self.tmp / "trees" / "feature"
        git(r, "worktree", "add", "-q", str(wt), "-b", "feature")
        gitdir = subprocess.run(["git", "-C", str(wt), "rev-parse", "--absolute-git-dir"],
                                capture_output=True, text=True, check=True).stdout.strip()
        Path(gitdir, "claude-interactive").touch()
        found = self.active()
        self.assertTrue(found["game"]["marked"])
        self.assertNotIn("feature", found, "a worktree outside HOME is left out like any other folder")
        wt2 = self.home / "game-feature"
        git(r, "worktree", "add", "-q", str(wt2), "-b", "feature2")
        gitdir2 = subprocess.run(["git", "-C", str(wt2), "rev-parse", "--absolute-git-dir"],
                                 capture_output=True, text=True, check=True).stdout.strip()
        Path(gitdir2, "claude-interactive").touch()
        self.assertTrue(self.active()["game-feature"]["marked"])

    def test_a_marker_older_than_30_days_does_not_count(self):
        r = self.repo("game")
        m = r / ".git" / "claude-interactive"
        m.touch()
        old = self.now - 40 * DAY
        os.utime(m, (old, old))
        self.assertEqual(self.active(), {})

    def test_a_marker_alone_counts_only_while_it_is_new_however_often_it_is_touched(self):
        r = self.repo("game")
        m = r / ".git" / "claude-interactive"
        m.touch()  # touched just now, as an agent does on every command
        born = {"value": self.now - 1 * DAY}
        real_run = aad.subprocess.run

        def fake_stat(argv, **kwargs):
            # GNU stat's "%W %n" for each marker, as the real helper parses it.
            if argv[:3] == ["stat", "--printf", "%W %n\\0"]:
                out = "".join("%d %s\0" % (born["value"], p) for p in argv[4:])
                return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")
            return real_run(argv, **kwargs)

        aad.subprocess.run = fake_stat
        self.addCleanup(setattr, aad.subprocess, "run", real_run)
        self.assertTrue(self.active()["game"]["marked"])
        born["value"] = int(self.now - (aad.MARKER_GRACE_DAYS + 1) * DAY)
        self.assertEqual(self.active(), {}, "an old marker re-touched by an agent keeps nothing open")

    def test_birth_time_is_read_for_a_path_with_a_newline(self):
        odd = self.home / "line\nbreak"
        odd.mkdir()
        m = odd / "claude-interactive"
        m.touch()
        old = self.now - 10 * DAY
        os.utime(m, (old, old))
        # The real GNU stat: a file made just now has a birth time of now (or none, then mtime).
        born = aad.markers_born([str(m)])[str(m)]
        self.assertTrue(born == os.path.getmtime(m) or born > self.now - DAY, born)

    def test_a_prompted_folder_keeps_its_old_marker(self):
        r = self.repo("game")
        (r / ".git" / "claude-interactive").touch()
        asked = []
        real = aad.markers_born
        aad.markers_born = lambda paths: asked.append(list(paths)) or {p: self.now - 20 * DAY for p in paths}
        self.addCleanup(setattr, aad, "markers_born", real)
        self.say(r, "keep going", age_days=5, origin={"kind": "human"})
        found = self.active()
        self.assertEqual(found["game"]["prompts"], 1)
        self.assertTrue(found["game"]["marked"])
        self.assertEqual(asked, [[]], "a prompted folder's marker needs no birth-time lookup")

    def test_a_message_from_another_session_is_not_a_prompt(self):
        r = self.repo("game")
        self.say(r, "<cross-session-message>do this</cross-session-message>", isMeta=True,
                 origin={"kind": "peer", "name": "other"}, promptSource="system")
        self.say(r, "do this", origin={"kind": "peer", "name": "other"})
        self.assertEqual(self.active(), {})

    def test_marker_birth_falls_back_to_modification_time(self):
        r = self.repo("game")
        m = r / ".git" / "claude-interactive"
        m.touch()
        old = self.now - 5 * DAY
        os.utime(m, (old, old))
        real_run = aad.subprocess.run
        calls = []

        def unknown(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="0 %s\0" % argv[-1], stderr="")

        aad.subprocess.run = unknown
        self.addCleanup(setattr, aad.subprocess, "run", real_run)
        self.assertEqual(aad.markers_born([str(m)]), {str(m): os.path.getmtime(m)})

        def stalled(argv, **kwargs):
            calls.append(argv)
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

        aad.subprocess.run = stalled
        self.assertEqual(aad.markers_born([str(m), str(m)]), {str(m): os.path.getmtime(m)})
        self.assertEqual(len(calls), 2, "one stat call per lookup, however many markers")

    def test_an_unmarked_unprompted_checkout_does_not_count(self):
        self.repo("game")
        self.assertEqual(self.active(), {})


class CliTests(Base):
    def test_the_command_prints_one_folder_per_line(self):
        r = self.repo("game")
        (r / ".git" / "claude-interactive").touch()
        out = subprocess.run(["python3", str(HERE / "agent-active-dirs.py"), "--home", str(self.home)],
                             capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.splitlines(), [str(r)])


if __name__ == "__main__":
    unittest.main(buffer=True)

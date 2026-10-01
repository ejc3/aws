#!/usr/bin/env python3
"""scripts/agent-session-sync.py: a new repository is noticed fast, and only a real new one.
Offline: real git repositories in a temp directory and a fake launcher."""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("ass", Path(__file__).resolve().parent / "agent-session-sync.py")
ass = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ass)

ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def git(path, *args):
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, env=ENV)


def make_repo(root, name, origin="https://github.com/ejc3/example", commit=True):
    p = Path(root) / name
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(p)], check=True, env=ENV)
    git(p, "remote", "add", "origin", origin)
    if commit:
        (p / "f").write_text("x")
        git(p, "add", "f")
        git(p, "commit", "-q", "-m", "c")
    return p


class FakeLauncher:
    def __init__(self, claude=True, codex=True):
        self.ok = {"claude": claude, "codex": codex}
        self.calls = []
        self.cheap_retry = {"claude": True}

    def start(self, kind, path):
        self.calls.append((kind, str(path)))
        return self.ok[kind]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = Path(self.tmp) / "home"
        self.home.mkdir()
        self.cfg = {"home": str(self.home), "roots": [str(self.home / "*"), str(self.home / "src" / "*")],
                    "owners": {"ejc3"}, "repos": set(), "state_dir": str(Path(self.tmp) / "state")}
        self.state = ass.State(os.path.join(self.cfg["state_dir"], "known.json"))
        self.launcher = FakeLauncher()

    def scan(self, now=1000.0, state=None):
        return ass.scan_once(self.cfg, state or self.state, self.launcher, now=now)

    def started(self):
        return sorted({p for _, p in self.launcher.calls})


class FirstRunTests(Base):
    def test_the_first_run_records_existing_repos_and_launches_nothing(self):
        make_repo(self.home, "old")
        self.scan()
        self.assertEqual(self.launcher.calls, [], "boot decided about these; an idle clone must stay skipped")
        self.assertIn(str(self.home / "old"), self.state.known)

    def test_a_repo_that_appears_after_the_first_run_is_launched_once_each(self):
        self.scan()
        r = make_repo(self.home, "fresh")
        self.scan(now=1005)
        self.assertEqual(sorted(self.launcher.calls), [("claude", str(r)), ("codex", str(r))])
        self.scan(now=1010)
        self.scan(now=1015)
        self.assertEqual(len(self.launcher.calls), 2, "handled once, not every tick")

    def test_the_state_survives_a_restart_so_old_repos_are_not_new_again(self):
        make_repo(self.home, "old")
        self.scan()
        again = ass.State(self.state.path)
        self.assertFalse(again.first_run)
        self.scan(now=1100, state=again)
        self.assertEqual(self.launcher.calls, [])


class WhatIsNotNewTests(Base):
    def setUp(self):
        super().setUp()
        self.scan()  # past the first run

    def test_a_linked_worktree_is_never_new(self):
        main = make_repo(self.home, "main")
        self.scan(now=1005)
        self.launcher.calls.clear()
        wt = self.home / "main-feature"
        git(main, "worktree", "add", "-q", str(wt), "-b", "feature")
        self.assertTrue((wt / ".git").is_file(), "a worktree's .git is a file")
        self.scan(now=1010)
        self.assertEqual(self.launcher.calls, [], "a random worktree must not start sessions")

    def test_the_rule_itself_not_a_side_effect_excludes_a_worktree(self):
        # The origin check would also reject a worktree (it has no .git/config), so pin the rule.
        main = make_repo(self.home, "main")
        wt = self.home / "main-feature"
        git(main, "worktree", "add", "-q", str(wt), "-b", "feature")
        self.assertTrue(ass.is_main_checkout(str(main)))
        self.assertFalse(ass.is_main_checkout(str(wt)))
        found = [os.path.basename(p) for p in ass.candidates(self.cfg["roots"])]
        self.assertIn("main", found)
        self.assertNotIn("main-feature", found)

    def test_nothing_nested_below_a_root_is_scanned(self):
        make_repo(self.home / "projects", "deep")
        self.scan(now=1005)
        self.assertEqual(self.launcher.calls, [])

    def test_a_clone_still_running_is_not_launched(self):
        r = make_repo(self.home, "cloning")
        (r / ".git" / "index.lock").write_text("")
        self.scan(now=1005)
        self.assertEqual(self.launcher.calls, [], "a lock means a clone, fetch or checkout is running")
        (r / ".git" / "index.lock").unlink()
        self.scan(now=1010)
        self.assertEqual(sorted(self.launcher.calls), [("claude", str(r)), ("codex", str(r))])

    def test_a_repo_with_no_commit_or_no_checkout_yet_is_not_launched(self):
        make_repo(self.home, "empty", commit=False)
        self.scan(now=1005)
        self.assertEqual(self.launcher.calls, [])

    def test_origins_that_are_not_ours_are_ignored(self):
        make_repo(self.home, "other", origin="https://github.com/someone-else/thing")
        make_repo(self.home, "local", origin="/srv/git/local.git")
        make_repo(self.home, "sync", origin="https://github.com/ejc3/claude-code-sync")
        make_repo(self.home, "history", origin="git@github.com:ejc3/claude-code-history.git")
        self.scan(now=1005)
        self.assertEqual(self.launcher.calls, [])

    def test_hidden_directories_are_ignored(self):
        make_repo(self.home, ".cache-repo")
        self.scan(now=1005)
        self.assertEqual(self.launcher.calls, [])


class OwnerTests(Base):
    def setUp(self):
        super().setUp()
        self.scan()

    def test_an_exact_extra_repo_is_allowed_without_allowing_its_owner(self):
        self.cfg["repos"] = {"dolphin-labs-hq/dolphin-labs"}
        make_repo(self.home, "dl", origin="https://github.com/dolphin-labs-hq/dolphin-labs.git")
        make_repo(self.home, "other", origin="https://github.com/dolphin-labs-hq/something-else")
        self.scan(now=1005)
        self.assertEqual(self.started(), [str(self.home / "dl")])

    def test_every_origin_form_is_understood(self):
        for url in ("https://github.com/ejc3/x", "https://github.com/ejc3/x.git", "git@github.com:ejc3/x.git",
                    "ssh://git@github.com/ejc3/x"):
            self.assertEqual(ass.parse_origin(url), ("ejc3", "x"), url)
        for url in ("", "https://gitlab.com/ejc3/x", "file:///x", None):
            self.assertIsNone(ass.parse_origin(url), url)

    def test_the_accounts_own_github_login_is_an_allowed_owner(self):
        gh = self.home / ".config" / "gh"
        gh.mkdir(parents=True)
        (gh / "hosts.yml").write_text("github.com:\n    users:\n        CoderColton:\n    user: CoderColton\n")
        self.assertEqual(ass.github_login(str(self.home)), "CoderColton")
        self.assertIn("codercolton", ass.allowed_owners(str(self.home), []))
        (self.home / ".config" / "agent-session-sync").mkdir()
        (self.home / ".config" / "agent-session-sync" / "owners").write_text("# kids\nconnor-org\n")
        self.assertIn("connor-org", ass.allowed_owners(str(self.home), ["ejc3"]))


class RetryAndRemovalTests(Base):
    def setUp(self):
        super().setUp()
        self.scan()

    def test_codex_not_logged_in_does_not_hold_up_claude_and_is_retried_later(self):
        self.launcher.ok["codex"] = False
        r = make_repo(self.home, "fresh")
        self.scan(now=1005)
        self.assertIn(("claude", str(r)), self.launcher.calls)
        self.launcher.calls.clear()
        self.scan(now=1010)
        self.assertEqual(self.launcher.calls, [], "a failed attempt waits before the next one")
        self.launcher.ok["codex"] = True
        self.scan(now=1070)
        self.assertEqual(self.launcher.calls, [("codex", str(r))], "only the missing half is retried")

    def test_no_tmux_server_yet_is_polled_every_tick_not_every_minute(self):
        self.launcher.ok["claude"] = False
        r = make_repo(self.home, "fresh")
        self.scan(now=1005)
        self.launcher.ok["claude"] = True
        self.launcher.calls.clear()
        self.scan(now=1010)  # 5 s later
        self.assertEqual(self.launcher.calls, [("claude", str(r))])

    def test_deleting_and_recloning_is_new_again(self):
        r = make_repo(self.home, "again")
        self.scan(now=1005)
        shutil.rmtree(r)
        self.scan(now=1010)
        self.assertNotIn(str(r), self.state.known)
        self.launcher.calls.clear()
        make_repo(self.home, "again")
        self.scan(now=1015)
        self.assertEqual(len(self.launcher.calls), 2)


class CostTests(Base):
    def test_a_handled_repo_costs_no_deeper_check(self):
        self.scan()
        make_repo(self.home, "fresh")
        self.scan(now=1005)
        calls = []
        original = ass.is_finished
        ass.is_finished = lambda p: calls.append(p) or original(p)
        self.addCleanup(setattr, ass, "is_finished", original)
        for i in range(5):
            self.scan(now=1010 + i)
        self.assertEqual(calls, [], "already-handled repos must not spawn git on every tick")


ROOT = Path(__file__).resolve().parent.parent


class WiringTests(unittest.TestCase):
    """Every box that keeps remote-control sessions installs the SAME script and unit."""

    def read(self, name):
        return (ROOT / name).read_text()

    def test_metal_nextjs_and_the_jumpbox_script_install_the_same_snippet(self):
        for f in ("claude-remote-control.tf", "nextjs-user-data.tf", "jumpbox2-user-data.tf"):
            self.assertIn("${local.agent_session_sync_install}", self.read(f), f)
        snippet = self.read("agent-session-sync.tf")
        self.assertIn('scripts/agent-session-sync.py', snippet)
        self.assertIn('scripts/agent-session-sync@.service', snippet)

    def test_the_accounts_that_get_one(self):
        tf = self.read("nextjs-user-data.tf")
        self.assertIn('systemctl enable "agent-session-sync@$u.service"', tf)
        self.assertIn('local.nextjs_codex_seed_users', tf[tf.index('agent-session-sync@$u.service') - 400:tf.index('agent-session-sync@$u.service')])
        self.assertIn("agent_session_sync_enable_ubuntu", self.read("claude-remote-control.tf"))
        self.assertIn("agent_session_sync_enable_ubuntu", self.read("jumpbox2-user-data.tf"))

    def test_a_running_jumpbox_converges_through_a_reviewed_step(self):
        tf = self.read("agent-session-sync.tf")
        block = tf[tf.index('resource "terraform_data" "admin_agent_session_sync"'):]
        for trigger in ("local.admin_tmux_boxes", "agent-session-sync.py", "agent-session-sync@.service",
                        "ssm-agent-session-sync.sh", "local.agent_session_sync_ubuntu_args"):
            self.assertIn(trigger, block)

    def test_the_unit_never_takes_a_session_with_it(self):
        unit = self.read("scripts/agent-session-sync@.service")
        self.assertIn("KillMode=process", unit)
        self.assertIn("Restart=always", unit)
        self.assertIn("NoNewPrivileges=yes", unit)
        self.assertNotRegex(unit, r"KillMode=control-group")

    def test_policy_is_the_owners_repos_never_an_org_wildcard(self):
        tf = self.read("agent-session-sync.tf")
        self.assertIn('agent_session_sync_ubuntu_args = "--owner ejc3 --repo dolphin-labs-hq/dolphin-labs"', tf)
        self.assertNotRegex(tf, r"--owner\s+dolphin-labs")

    def test_the_scan_interval_is_inside_the_fifteen_second_goal(self):
        text = (ROOT / "scripts" / "agent-session-sync.py").read_text()
        import re as _re
        interval = float(_re.search(r'"--interval", type=float, default=([0-9.]+)', text).group(1))
        self.assertLessEqual(interval, 5.0, "scan + ~1s launch must stay well under 15s")


if __name__ == "__main__":
    unittest.main(verbosity=2)

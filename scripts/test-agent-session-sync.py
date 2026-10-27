#!/usr/bin/env python3
"""scripts/agent-session-sync.py: a new repository is noticed fast, and only a real new one.
Offline: real git repositories in a temp directory and a fake launcher."""
import importlib.util
import base64
import json
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


def mark(path):
    """What an agent does for a folder the user wants to prompt: touch claude-interactive in its git dir."""
    gitdir = subprocess.run(["git", "-C", str(path), "rev-parse", "--absolute-git-dir"], check=True,
                            capture_output=True, text=True, env=ENV).stdout.strip()
    Path(gitdir, "claude-interactive").touch()


def make_repo(root, name, origin="https://github.com/ejc3/example", commit=True, marked=True):
    p = Path(root) / name
    p.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(p)], check=True, env=ENV)
    git(p, "remote", "add", "origin", origin)
    if commit:
        (p / "f").write_text("x")
        git(p, "add", "f")
        git(p, "commit", "-q", "-m", "c")
    if marked:
        (p / ".git" / "claude-interactive").touch()
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
                    "owners": {"ejc3"}, "repos": set(), "state_dir": str(Path(self.tmp) / "state"),
                    "active_dirs": lambda cfg: list(self.active)}
        self.active = []
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

    def test_an_unmarked_linked_worktree_is_not_new(self):
        main = make_repo(self.home, "main")
        self.scan(now=1005)
        self.launcher.calls.clear()
        wt = self.home / "main-feature"
        git(main, "worktree", "add", "-q", str(wt), "-b", "feature")
        self.assertTrue((wt / ".git").is_file(), "a worktree's .git is a file")
        self.scan(now=1010)
        self.assertEqual(self.launcher.calls, [], "a random worktree must not start sessions")

    def test_an_unmarked_worktree_is_not_a_candidate_and_a_marked_one_is(self):
        main = make_repo(self.home, "main")
        wt = self.home / "main-feature"
        git(main, "worktree", "add", "-q", str(wt), "-b", "feature")
        found = [os.path.basename(p) for p in ass.candidates(self.cfg["roots"])]
        self.assertIn("main", found)
        self.assertNotIn("main-feature", found)
        mark(wt)
        found = [os.path.basename(p) for p in ass.candidates(self.cfg["roots"])]
        self.assertIn("main-feature", found)

    def test_an_unmarked_clone_is_launched(self):
        r = make_repo(self.home, "plain", marked=False)
        self.scan(now=1005)
        self.assertEqual(sorted(self.launcher.calls), [("claude", str(r)), ("codex", str(r))],
                         "every clone gets a session; only worktrees need the marker")

    def test_a_marked_worktree_elsewhere_is_launched_and_takes_the_main_checkout_s_owner(self):
        main = make_repo(self.home, "main", marked=False)
        self.scan(now=1003)                                 # the clone itself gets its session first
        self.launcher.calls.clear()
        wt = Path(self.tmp) / "elsewhere" / "feature"
        git(main, "worktree", "add", "-q", str(wt), "-b", "feature")
        mark(wt)
        self.scan(now=1005)
        self.assertEqual(sorted(self.launcher.calls), [("claude", str(wt)), ("codex", str(wt))])

    def test_a_subagent_worktree_never_counts_even_when_marked(self):
        main = make_repo(self.home, "main", marked=False)
        wt = main / ".claude" / "worktrees" / "agent-1"
        git(main, "worktree", "add", "-q", str(wt), "-b", "agent-1")
        mark(wt)
        self.scan(now=1005)
        self.assertNotIn(str(wt), [p for _, p in self.launcher.calls])
        self.assertEqual(sorted(self.launcher.calls), [("claude", str(main)), ("codex", str(main))],
                         "only the clone itself gets a session")

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

    def test_a_clone_seen_before_its_origin_is_written_is_rechecked_when_finished(self):
        r = self.home / "cloning"
        subprocess.run(["git", "init", "-q", str(r)], check=True, env=ENV)
        self.scan(now=1005)
        self.assertEqual(self.launcher.calls, [])
        git(r, "remote", "add", "origin", "https://github.com/ejc3/example")
        (r / "f").write_text("x")
        git(r, "add", "f")
        git(r, "commit", "-q", "-m", "clone finished")
        self.scan(now=1010)
        self.assertEqual(sorted(self.launcher.calls), [("claude", str(r)), ("codex", str(r))],
                         "a clone without its origin yet must not be ignored permanently")

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


class UpgradeToEveryCloneTests(Base):
    """A state file from the version that needed a marker on every clone: upgrading must not open a session in
    every old clone at once."""

    def old_state(self, known):
        os.makedirs(self.cfg["state_dir"], exist_ok=True)
        path = os.path.join(self.cfg["state_dir"], "known.json")
        with open(path, "w") as fh:
            json.dump({"known": known, "boot_id": "boot-A", "boot": {}}, fh)
        return ass.State(path, boot_id="boot-A")

    def test_the_first_scan_after_the_upgrade_adopts_old_unmarked_clones_without_launching(self):
        old = make_repo(self.home, "old-clone", marked=False)
        state = self.old_state({})
        self.assertFalse(state.clones)
        self.scan(now=1005, state=state)
        self.assertEqual(self.launcher.calls, [], "existing clones are recorded, not launched")
        self.assertTrue(state.known[str(old)].get("seeded"))
        self.assertTrue(ass.State(state.path, boot_id="boot-A").clones, "the adoption is saved")

    def test_a_clone_made_after_the_upgrade_is_launched(self):
        state = self.old_state({})
        self.scan(now=1005, state=state)
        new = make_repo(self.home, "new-clone", marked=False)
        self.scan(now=1010, state=state)
        self.assertEqual(sorted(self.launcher.calls), [("claude", str(new)), ("codex", str(new))])

    def test_a_fresh_state_records_the_new_policy_on_its_first_run(self):
        self.scan(now=1000)
        self.assertTrue(ass.State(self.state.path).clones)


class AdminClonePolicyTests(Base):
    def test_an_admin_clone_waits_for_the_marker_before_both_agents_start(self):
        self.cfg["require_clone_marker"] = True
        self.scan()
        repo = make_repo(self.home, "admin-clone", marked=False)
        self.scan(now=1005)
        self.assertEqual(self.launcher.calls, [])
        mark(repo)
        self.scan(now=1010)
        self.assertEqual(sorted(self.launcher.calls), [("claude", str(repo)), ("codex", str(repo))])

    def test_the_admin_cli_requires_the_marker_for_the_allowed_collaboration_repo(self):
        launcher = ass.Launcher
        ass.Launcher = lambda cfg: self.launcher
        self.addCleanup(setattr, ass, "Launcher", launcher)
        args = ["--home", str(self.home), "--state-dir", self.cfg["state_dir"], "--owner", "ejc3",
                "--repo", "dolphin-labs-hq/dolphin-labs", "--require-clone-marker", "--once"]
        ass.main(args)
        repo = make_repo(self.home, "collaboration", origin="https://github.com/dolphin-labs-hq/dolphin-labs", marked=False)
        ass.main(args)
        self.assertEqual(self.launcher.calls, [], "an allowed origin does not approve startup hooks")
        mark(repo)
        ass.main(args)
        self.assertEqual(sorted(self.launcher.calls), [("claude", str(repo)), ("codex", str(repo))])


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
        self.assertIn("agent_session_sync_enable_admin", self.read("jumpbox2-user-data.tf"))

    def test_a_running_jumpbox_converges_through_a_reviewed_step(self):
        tf = self.read("agent-session-sync.tf")
        block = tf[tf.index('resource "terraform_data" "admin_agent_session_sync"'):]
        for trigger in ("local.admin_tmux_boxes", "agent-session-sync.py", "agent-session-sync@.service",
                        "ssm-agent-session-sync.sh", "local.agent_session_sync_admin_args"):
            self.assertIn(trigger, block)

    def test_both_admin_entry_paths_require_clone_markers_and_dev_boxes_keep_the_default(self):
        tf = self.read("agent-session-sync.tf")
        self.assertIn('agent_session_sync_admin_args = "${local.agent_session_sync_ubuntu_args} --require-clone-marker"', tf)
        admin = tf.split('resource "terraform_data" "admin_agent_session_sync"', 1)[1]
        self.assertIn("for_each = local.admin_tmux_boxes", admin)
        self.assertEqual(admin.count("local.agent_session_sync_admin_args"), 2, "trigger and SSM environment")
        self.assertNotIn("local.agent_session_sync_ubuntu_args", admin)
        enable = tf.split("agent_session_sync_enable_admin = replace(", 1)[1].split("\n  )", 1)[0]
        for value in ("local.agent_session_sync_enable_ubuntu", "local.agent_session_sync_ubuntu_args", "local.agent_session_sync_admin_args"):
            self.assertIn(value, enable)
        boot = self.read("jumpbox2-user-data.tf")
        self.assertEqual(boot.count("content      = local.jumpbox_2_user_data"), 2, "both admin S3 boot objects")
        self.assertIn("${local.agent_session_sync_enable_admin}", boot)
        self.assertNotIn("${local.agent_session_sync_enable_ubuntu}", boot)
        self.assertLess(boot.index("${local.agent_session_sync_prepare_admin}"), boot.index("${local.agent_session_sync_install}"))
        self.assertNotIn("agent_session_sync_enable_admin", self.read("claude-remote-control.tf"))

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


class RebootTests(Base):
    """tmux windows do not survive a reboot but known.json does: what the watcher launched is launched again."""

    def state_for(self, boot):
        return ass.State(os.path.join(self.cfg["state_dir"], "known.json"), boot_id=boot)

    def setUp(self):
        super().setUp()
        self.state = self.state_for("boot-A")
        self.scan()
        self.fresh = make_repo(self.home, "fresh")
        self.scan(now=1005, state=self.state)          # launched by the watcher: claude and codex done
        self.launcher.calls.clear()

    def test_a_restart_of_the_watcher_on_the_same_boot_relaunches_nothing(self):
        state = self.state_for("boot-A")
        self.assertFalse(state.rebooted)
        self.scan(now=2000, state=state)
        self.assertEqual(self.launcher.calls, [])

    def test_after_a_reboot_claude_starts_in_the_active_folders_only_and_codex_is_left_alone(self):
        idle = make_repo(self.home, "idle", marked=False)
        self.scan(now=1010, state=self.state)          # a clone from before the reboot that nobody prompted
        self.launcher.calls.clear()
        self.active = [str(self.fresh)]               # agent-active-dirs: prompted or marked in 30 days
        state = self.state_for("boot-B")
        self.assertTrue(state.rebooted)
        self.scan(now=2000, state=state)
        self.assertEqual(self.launcher.calls, [("claude", str(self.fresh))], "Codex threads live in Codex, not tmux")
        self.assertNotIn(str(idle), [p for _, p in self.launcher.calls])

    def test_a_folder_the_watcher_launched_but_nobody_prompted_stays_closed_after_a_reboot(self):
        self.active = []
        self.scan(now=2000, state=self.state_for("boot-B"))
        self.assertEqual(self.launcher.calls, [], "no session activity in 30 days: not reopened")

    def test_an_active_folder_that_is_not_allowed_is_not_launched(self):
        other = make_repo(self.home, "theirs", origin="https://github.com/someone-else/x", marked=False)
        self.active = [str(other)]
        self.scan(now=2000, state=self.state_for("boot-B"))
        self.assertEqual(self.launcher.calls, [])

    def test_it_is_done_once_a_window_closed_afterwards_stays_closed(self):
        self.active = [str(self.fresh)]
        state = self.state_for("boot-B")
        self.scan(now=2000, state=state)
        self.launcher.calls.clear()
        self.scan(now=2100, state=state)
        self.assertEqual(self.launcher.calls, [])
        self.assertEqual(self.state_for("boot-B").rebooted, False, "the new boot id was saved")

    def test_with_no_tmux_server_yet_the_boot_launch_is_retried_every_tick_and_survives_a_restart(self):
        self.active = [str(self.fresh)]
        self.launcher.ok["claude"] = False            # cheap_retry: no tmux server yet (a jumpbox after a reboot)
        state = self.state_for("boot-B")
        self.scan(now=2000, state=state)
        self.scan(now=2001, state=state)
        self.assertEqual(self.launcher.calls.count(("claude", str(self.fresh))), 2)
        self.launcher.ok["claude"] = True
        self.launcher.calls.clear()
        restarted = self.state_for("boot-B")         # the watcher restarts on the same boot
        self.assertFalse(restarted.rebooted)
        self.scan(now=2002, state=restarted)
        self.assertEqual(self.launcher.calls, [("claude", str(self.fresh))], "the pending launch was kept on disk")

    def test_an_older_state_file_without_a_boot_id_is_not_taken_for_a_reboot(self):
        path = os.path.join(self.cfg["state_dir"], "known.json")
        import json as _json
        data = _json.load(open(path))
        data.pop("boot_id")
        _json.dump(data, open(path, "w"))
        self.assertFalse(self.state_for("boot-Z").rebooted)


class TrustKeeperTests(Base):
    """A running Claude rewrites ~/.claude.json from its cached copy and drops a trust entry added meanwhile."""

    def write(self, projects):
        import json as _json
        (self.home / ".claude.json").write_text(_json.dumps({"projects": projects, "other": {"keep": 1}}))

    def read(self):
        import json as _json
        return _json.loads((self.home / ".claude.json").read_text())

    def test_a_trust_entry_another_session_overwrote_is_put_back(self):
        repo = str(self.home / "fresh")
        self.write({repo: {"hasTrustDialogAccepted": True}})
        state = ass.State(os.path.join(self.cfg["state_dir"], "known.json"))
        ass.keep_trust(str(self.home), [repo], state)                    # first look: all present, nothing written
        self.write({})                                                   # a running Claude drops it
        os.utime(self.home / ".claude.json", ns=(1, 2_000_000_000))      # (and its mtime moves)
        ass.keep_trust(str(self.home), [repo], state)
        self.assertIn(repo, self.read()["projects"], "the dropped entry was not put back")
        self.assertIs(self.read()["projects"][repo]["hasTrustDialogAccepted"], True)
        self.assertEqual(self.read()["other"], {"keep": 1}, "everything else in the file is preserved")

    def test_an_idle_tick_is_one_stat_and_writes_nothing(self):
        repo = str(self.home / "fresh")
        self.write({repo: {"hasTrustDialogAccepted": True}})
        state = ass.State(os.path.join(self.cfg["state_dir"], "known.json"))
        ass.keep_trust(str(self.home), [repo], state)
        before = (self.home / ".claude.json").stat().st_mtime_ns
        for _ in range(5):
            ass.keep_trust(str(self.home), [repo], state)
        self.assertEqual((self.home / ".claude.json").stat().st_mtime_ns, before)
        calls = []
        real = ass.trust_repos
        ass.trust_repos = lambda *a: calls.append(a) or 0
        self.addCleanup(setattr, ass, "trust_repos", real)
        ass.keep_trust(str(self.home), [repo], state)
        self.assertEqual(calls, [], "an unchanged file is not even read")

    def test_the_scan_keeps_trust_for_repos_it_launched_but_not_for_ones_it_never_launched(self):
        old = self.home / "old"
        self.scan()
        fresh = make_repo(self.home, "fresh")
        self.write({})
        self.scan(now=1005)
        self.assertIn(str(fresh), self.read()["projects"])
        self.assertNotIn(str(old), self.read()["projects"])
        self.write({})
        os.utime(self.home / ".claude.json", ns=(1, 3_000_000_000))
        self.scan(now=1010)
        self.assertIs(self.read()["projects"][str(fresh)]["hasTrustDialogAccepted"], True)


class ClaudeLauncherTests(Base):
    """t-claude is found on EVERY attempt: not being installed yet must never read as 'nothing to do'."""

    def setUp(self):
        super().setUp()
        self.cfg.update({"t_claude_arg": "auto", "tmux": "tmux"})
        self.launcher = ass.Launcher(self.cfg)
        self.launcher.tmux_up = lambda: True
        self.launcher.claude_logged_in = lambda: True
        self.looked = []
        real = ass.find_t_claude
        ass.find_t_claude = lambda home, given: self.looked.append(home) or None
        self.addCleanup(setattr, ass, "find_t_claude", real)

    def test_a_missing_t_claude_is_a_retry_not_a_completion(self):
        self.assertFalse(self.launcher.start_claude(str(self.home / "x")))
        self.assertEqual(len(self.looked), 1)

    def test_the_scan_keeps_asking_until_it_is_installed(self):
        self.scan()
        make_repo(self.home, "fresh")
        for tick in range(3):
            self.scan(now=1005 + tick)                    # claude retries every tick while no tmux/launcher
        entry = self.state.known[str(self.home / "fresh")]
        self.assertFalse(entry["claude"], "a missing launcher must not be recorded as done")
        self.assertGreaterEqual(len(self.looked), 1)
        self.assertNotIn("t_claude", self.cfg, "the path is never cached in the config")


class SelfUpdateTests(unittest.TestCase):
    """A box's installer replaces the file; the running watcher must pick it up without anyone restarting it."""

    def test_the_watcher_runs_new_code_in_the_same_process_when_its_file_is_replaced(self):
        import time as _time
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        copy = tmp / "agent-session-sync.py"
        shutil.copy(ROOT / "scripts" / "agent-session-sync.py", copy)
        home = tmp / "home"
        home.mkdir()
        log = tmp / "log"
        with open(log, "w") as fh:
            proc = subprocess.Popen(["python3", str(copy), "--home", str(home), "--interval", "0.2",
                                     "--state-dir", str(tmp / "state"), "--codex-seed", "/nonexistent"], stdout=fh, stderr=fh)
        self.addCleanup(proc.kill)
        _time.sleep(1.0)
        new = copy.read_text() + "\n# a newer version\n"
        tmp_new = tmp / "new"
        tmp_new.write_text(new)
        os.replace(tmp_new, copy)                         # atomic, like the installer
        deadline = _time.time() + 10
        while _time.time() < deadline and "my code changed" not in log.read_text():
            _time.sleep(0.2)
        self.assertIn("my code changed", log.read_text())
        _time.sleep(1.0)
        self.assertIsNone(proc.poll(), "the process is still running: it re-executed, it did not exit")
        self.assertGreaterEqual(log.read_text().count("watching "), 2, "the new code started watching again")


class UpdateWiringTests(unittest.TestCase):
    def read(self, name):
        return (ROOT / name).read_text()

    def test_the_ssm_step_replaces_files_atomically_and_only_when_they_differ(self):
        sh = self.read("scripts/ssm-agent-session-sync.sh")
        self.assertIn('cmp -s "\\$1" "\\$2"', sh)
        self.assertIn('install -m "\\$3" "\\$1" "\\$2.new" && mv -f "\\$2.new" "\\$2"', sh)
        self.assertNotIn("> /usr/local/bin/agent-session-sync", sh, "never write the live script in place")

    def test_the_jumpboxes_get_the_codex_seeder_the_watcher_calls(self):
        sh = self.read("scripts/ssm-agent-session-sync.sh")
        self.assertIn('seeder=$(base64 -w0 "$here/codex-seed-thread.py")', sh)
        self.assertIn('put "\\$t/seeder" /usr/local/bin/codex-seed-thread 755 || true', sh)
        self.assertNotRegex(sh, r'seeder" /usr/local/bin/codex-seed-thread 755 && restart=1')
        self.assertIn("codex-seed-thread.py", self.read("agent-session-sync.tf"), "a changed seeder re-runs the step")
        self.assertIn("${local.codex_seed_thread_install}", self.read("jumpbox2-user-data.tf"))
        self.assertIn("/usr/local/bin/codex-seed-thread", self.read("codex-remote-control.tf"))
        default = (ROOT / "scripts" / "agent-session-sync.py").read_text()
        self.assertIn('"--codex-seed", default="/usr/local/bin/codex-seed-thread"', default, "the path the installers write")

    def test_the_box_installers_replace_the_script_atomically_too(self):
        tf = self.read("agent-session-sync.tf")
        self.assertIn("cat > /usr/local/bin/agent-session-sync.new <<'AGENTSYNC'", tf)
        self.assertIn("mv -f /usr/local/bin/agent-session-sync.new /usr/local/bin/agent-session-sync", tf)
        self.assertNotIn("cat > /usr/local/bin/agent-session-sync <<", tf, "never write the live script in place")

    def test_a_running_watcher_is_restarted_only_when_its_unit_or_policy_changed(self):
        sh = self.read("scripts/ssm-agent-session-sync.sh")
        self.assertIn('put "\\$t/unit" /etc/systemd/system/agent-session-sync@.service 644 && restart=1', sh)
        self.assertIn('systemctl stop agent-session-sync@ubuntu.service', sh)
        self.assertLess(sh.index('systemctl stop agent-session-sync@ubuntu.service'),
                        sh.index('put "\\$t/script" /usr/local/bin/agent-session-sync 755'))
        self.assertNotRegex(sh, r'script"? /usr/local/bin/agent-session-sync 755 && restart=1')
        self.assertIn('[ "\\$restart" = 1 ] && systemctl restart agent-session-sync@ubuntu.service', sh)

    def test_put_replaces_atomically_reports_a_change_and_leaves_an_identical_file_alone(self):
        sh = self.read("scripts/ssm-agent-session-sync.sh")
        line = next(l for l in sh.splitlines() if l.startswith("put() {"))
        func = line.replace("\\$", "$")
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "src").write_text("new")
        run = lambda: subprocess.run(["bash", "-c", func + '; put "%s" "%s" 644' % (tmp / "src", tmp / "dst")]).returncode
        self.assertEqual(run(), 0)                         # installed: success means "changed"
        self.assertEqual((tmp / "dst").read_text(), "new")
        mtime = (tmp / "dst").stat().st_mtime_ns
        self.assertEqual(run(), 1)                         # identical: nothing done
        self.assertEqual((tmp / "dst").stat().st_mtime_ns, mtime)
        self.assertFalse((tmp / "dst.new").exists())

    def test_the_watcher_on_nextjs_does_not_depend_on_a_codex_login(self):
        tf = self.read("nextjs-user-data.tf")
        block = tf[tf.index("cat > /usr/local/bin/agents-enable"):tf.index("AGENTSENABLE\nchmod 755")]
        codex_guard = block.index('if [ -s "/home/$u/.codex/auth.json" ]; then')
        end_of_codex = block.index("\n  fi\n", codex_guard)
        watch = block.index('systemctl enable "agent-session-sync@$u.service"')
        self.assertGreater(watch, end_of_codex, "the watcher is enabled after the Codex block closes, not inside it")
        guard = block[block.rindex("if [", 0, watch):watch]
        self.assertIn('/home/$u/.claude/.credentials.json', guard)
        self.assertIn('/home/$u/.codex/auth.json', guard)
        self.assertIn("||", guard, "either login is enough")


class AdminInstallOrderingTests(unittest.TestCase):
    """Both admin paths fence the old watcher before publishing the new dev-default script."""

    def run_install(self, kind, stop_ok=True, unchanged=False, policy_write_ok=True, enable_write_ok=True):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, d, True)
        bin_, programs, units = d / "bin", d / "programs", d / "units"
        for p in (bin_, programs, units):
            p.mkdir()
        policy = units / "agent-session-sync@ubuntu.service.d" / "policy.conf"
        policy.parent.mkdir()
        args = "--owner ejc3 --repo dolphin-labs-hq/dolphin-labs --require-clone-marker"
        old_args = args if unchanged else "--owner ejc3 --repo dolphin-labs-hq/dolphin-labs"
        policy.write_text('[Service]\nEnvironment="ASS_ARGS=%s"\n' % old_args)
        (programs / "agent-session-sync").write_text("old script\n")
        (units / "agent-session-sync@.service").write_text("unit\n")
        active, log = d / "active", d / "log"
        active.touch()

        def fake(name, body):
            p = bin_ / name
            p.write_text("#!/bin/bash\n" + body)
            p.chmod(0o755)

        fake("systemctl", f'''case "$1" in
show) echo loaded ;;
stop) echo stop >> {log}; {'rm -f ' + str(active) if stop_ok else 'exit 1'} ;;
start|restart) echo "$1" >> {log}; touch {active} ;;
is-active) test -e {active} ;;
*) exit 0 ;;
esac
''')
        fake("mv", 'echo "publish ${@: -1}" >> "$ORDER_LOG"\nexec /usr/bin/mv "$@"\n')
        fake("sleep", "exit 0\n")
        fake("journalctl", "exit 0\n")
        if not enable_write_ok:
            fake("cat", "printf '[Service]\\n'; exit 1\n")
        tf = (ROOT / "agent-session-sync.tf").read_text()
        if kind == "ssm":
            sh = (ROOT / "scripts/ssm-agent-session-sync.sh").read_text()
            body = sh.split("read -r -d '' remote <<REMOTE || true\n", 1)[1].split("\nREMOTE\n", 1)[0].replace("\\$", "$")
            for key, data in (("script", b"new script\n"), ("seeder", b"seeder\n"), ("active", b"active\n"), ("unit", b"unit\n")):
                body = body.replace("$" + key, base64.b64encode(data).decode())
            body = body.replace("$ASS_ARGS", args)
        else:
            prepare = tf.split("agent_session_sync_prepare_admin = <<-PREPARE\n", 1)[1].split("\nPREPARE\n", 1)[0]
            enable = tf.split("agent_session_sync_enable_ubuntu = <<-ENABLE\n", 1)[1].split("\nENABLE\n", 1)[0]
            body = prepare.replace("${local.agent_session_sync_admin_args}", args)
            body += '\nprintf "new script\\n" > /usr/local/bin/agent-session-sync\necho "publish /usr/local/bin/agent-session-sync" >> "$ORDER_LOG"\n'
            body += enable.replace("${local.agent_session_sync_ubuntu_args}", args)
        body = body.replace("/usr/local/bin", str(programs)).replace("/etc/systemd/system", str(units))
        if not policy_write_ok:
            body = "printf() { builtin printf '[Service]\\n'; return 1; }\n" + body
        self.assertNotIn("/usr/local/bin", body)
        self.assertNotIn("/etc/systemd/system", body)
        result = subprocess.run(["bash", "-c", body], capture_output=True, text=True, timeout=10,
                                env={"PATH": str(bin_) + ":/usr/bin:/bin", "ORDER_LOG": str(log)})
        operations = log.read_text().splitlines() if log.exists() else []
        return result, operations, programs, policy

    def test_admin_paths_stop_then_publish_policy_and_script_then_start(self):
        for kind in ("ssm", "bootstrap"):
            with self.subTest(kind=kind):
                result, operations, programs, policy = self.run_install(kind)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertLess(operations.index("stop"), operations.index("publish " + str(policy)))
                self.assertLess(operations.index("publish " + str(policy)), operations.index("publish " + str(programs / "agent-session-sync")))
                self.assertLess(operations.index("publish " + str(programs / "agent-session-sync")), operations.index("start"))

    def test_a_failed_stop_aborts_both_admin_paths_before_publication(self):
        for kind in ("ssm", "bootstrap"):
            with self.subTest(kind=kind):
                result, operations, programs, policy = self.run_install(kind, stop_ok=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(operations, ["stop"])
                self.assertEqual((programs / "agent-session-sync").read_text(), "old script\n")
                self.assertNotIn("--require-clone-marker", policy.read_text())

    def test_an_unchanged_admin_policy_does_not_restart_the_watcher(self):
        for kind in ("ssm", "bootstrap"):
            with self.subTest(kind=kind):
                result, operations, _, _ = self.run_install(kind, unchanged=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(set(operations) & {"stop", "start", "restart"})

    def test_a_partial_policy_write_aborts_before_policy_or_script_publication(self):
        for kind in ("ssm", "bootstrap"):
            with self.subTest(kind=kind):
                result, operations, programs, policy = self.run_install(kind, policy_write_ok=False)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(operations, [])
                self.assertEqual((programs / "agent-session-sync").read_text(), "old script\n")
                self.assertNotIn("--require-clone-marker", policy.read_text())

    def test_a_failed_bootstrap_enable_write_keeps_the_strict_policy_and_watcher_stopped(self):
        result, operations, _, policy = self.run_install("bootstrap", enable_write_ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("start", operations)
        self.assertNotIn("restart", operations)
        self.assertIn("--require-clone-marker", policy.read_text())


class CodexSeedTests(Base):
    """A seed turn takes minutes and its child can fail: starting it is not success, its exit status is."""

    def setUp(self):
        super().setUp()
        bindir = self.home / ".local" / "bin"
        bindir.mkdir(parents=True)
        (bindir / "codex").write_text("#!/bin/sh\nexit 0\n")        # `codex login status`: logged in
        (bindir / "codex").chmod(0o755)
        self.counter = Path(self.tmp) / "attempts"
        self.seed = Path(self.tmp) / "seed"
        self.seed.write_text('#!/bin/sh\necho x >> %s\n[ "$(wc -l < %s)" -ge 2 ]\n' % (self.counter, self.counter))
        self.seed.chmod(0o755)                                            # fails the first time, succeeds the second
        self.cfg.update({"codex_seed": str(self.seed), "state_dir": str(Path(self.tmp) / "state")})
        self.launcher = ass.Launcher(self.cfg)

    def wait_for(self, path, want):
        import time as _time
        deadline = _time.time() + 10
        while _time.time() < deadline:
            got = self.launcher.start_codex(path)
            if got is not None:
                return got
            _time.sleep(0.05)
        self.fail("the seed never finished")

    def test_starting_the_seed_is_not_success_and_a_failed_child_is_retried(self):
        self.assertIsNone(self.launcher.start_codex("/r"), "just started: not done, not failed")
        self.assertFalse(self.wait_for("/r", False), "the child exited nonzero: a failure, to be retried")
        self.assertIsNone(self.launcher.start_codex("/r"), "a retry starts a new child")
        self.assertTrue(self.wait_for("/r", True), "exit 0 is the only success")
        self.assertEqual(len(self.counter.read_text().split()), 2)

    def test_the_scan_marks_codex_done_only_when_the_child_succeeded_and_polls_while_it_runs(self):
        self.scan()
        r = make_repo(self.home, "fresh")
        self.launcher = FakeLauncher()
        results = iter([None, None, False, None, True])
        self.launcher.start = lambda kind, path: True if kind == "claude" else next(results)
        self.scan(now=1005)
        self.assertFalse(self.state.known[str(r)]["codex"], "running: not done")
        self.scan(now=1006)                                                # polled again at once, not after 60 s
        self.scan(now=1007)                                                # the child failed
        self.assertFalse(self.state.known[str(r)]["codex"])
        self.scan(now=1008)                                                # a failure waits RETRY_SECONDS
        self.assertFalse(self.state.known[str(r)]["codex"])
        self.scan(now=1007 + ass.RETRY_SECONDS + 1)                        # retried: started again
        self.scan(now=1007 + ass.RETRY_SECONDS + 2)                        # and succeeded
        self.assertTrue(self.state.known[str(r)]["codex"])


    def test_a_missing_seeder_is_a_retry_never_a_silent_success(self):
        self.cfg["codex_seed"] = str(Path(self.tmp) / "not-installed")
        self.assertFalse(self.launcher.start_codex("/r"))
        self.cfg["codex_seed"] = None
        self.assertFalse(self.launcher.start_codex("/r"))

    def test_the_scan_leaves_codex_unfinished_while_the_seeder_is_missing_and_finishes_it_when_it_appears(self):
        self.cfg["codex_seed"] = str(Path(self.tmp) / "later")
        self.cfg["t_claude_arg"] = "auto"
        self.launcher = ass.Launcher(self.cfg)
        self.launcher.start_claude = lambda path: True
        self.scan()
        r = make_repo(self.home, "fresh")
        self.scan(now=1005)
        self.assertFalse(self.state.known[str(r)]["codex"])
        later = Path(self.tmp) / "later"
        later.write_text("#!/bin/sh\nexit 0\n")
        later.chmod(0o755)
        self.scan(now=1005 + ass.RETRY_SECONDS + 1)
        for tick in range(100):                                            # the child runs, then is polled
            self.scan(now=1100 + tick)
            if self.state.known[str(r)]["codex"]:
                break
            import time as _time
            _time.sleep(0.05)
        self.assertTrue(self.state.known[str(r)]["codex"])


class OwnerRefreshTests(Base):
    """gh login or the owners file changes while the watcher runs."""

    def setUp(self):
        super().setUp()
        self.cfg.update({"owners_extra": [], "owners": ass.allowed_owners(str(self.home), []),
                         "owners_sig": ass.owners_signature(str(self.home))})

    def login(self, user):
        (self.home / ".config" / "gh").mkdir(parents=True, exist_ok=True)
        (self.home / ".config" / "gh" / "hosts.yml").write_text("github.com:\n    user: %s\n" % user)

    def test_logging_in_to_gh_after_the_watcher_started_makes_later_clones_count(self):
        self.scan()
        self.login("acme")
        self.scan(now=1005)                                                # picks the owner up
        r = make_repo(self.home, "later", origin="https://github.com/acme/later")
        self.scan(now=1010)
        self.assertEqual(self.started(), [str(r)])

    def test_an_edit_of_the_owners_file_is_picked_up_too(self):
        self.scan()
        (self.home / ".config" / "agent-session-sync").mkdir(parents=True)
        (self.home / ".config" / "agent-session-sync" / "owners").write_text("acme\n")
        self.scan(now=1005)
        r = make_repo(self.home, "later", origin="https://github.com/acme/later")
        self.scan(now=1010)
        self.assertEqual(self.started(), [str(r)])

    def test_a_clone_from_before_the_owner_was_allowed_is_launched_when_it_is_allowed_if_it_came_after_the_first_run(self):
        self.scan()
        r = make_repo(self.home, "early", origin="https://github.com/acme/early")
        self.scan(now=1005)
        self.assertEqual(self.started(), [], "not this account's yet")
        self.login("acme")
        self.scan(now=1010)
        self.assertEqual(self.started(), [str(r)])

    def test_adding_an_owner_never_launches_the_old_clones_that_were_there_at_the_first_run(self):
        old = [make_repo(self.home, "old%d" % i, origin="https://github.com/acme/old%d" % i) for i in range(3)]
        self.scan()                                                        # the first run: recorded, ignored
        self.login("acme")
        self.scan(now=1005)
        self.scan(now=1010)
        self.assertEqual(self.started(), [], "an old clone the boot launcher skipped stays skipped: %s" % old)

    def test_nothing_is_reread_when_nothing_changed(self):
        self.scan()
        calls = []
        real = ass.allowed_owners
        ass.allowed_owners = lambda *a: calls.append(a) or real(*a)
        self.addCleanup(setattr, ass, "allowed_owners", real)
        for tick in range(5):
            self.scan(now=1005 + tick)
        self.assertEqual(calls, [])


class ReplacementCloneTests(Base):
    """The same pathname is not the same checkout."""

    def setUp(self):
        super().setUp()
        self.scan()
        self.repo = make_repo(self.home, "fresh")
        self.scan(now=1005)
        self.launcher.calls.clear()

    def test_a_checkout_whose_identity_changed_at_the_same_path_is_new(self):
        entry = self.state.known[str(self.repo)]
        self.assertIsNotNone(entry.get("id"), "the identity is recorded when the repo is first seen")
        entry["id"] = [entry["id"][0], entry["id"][1] + 7]                # what a replacement clone would look like
        self.scan(now=1010)
        self.assertEqual(sorted(self.launcher.calls), [("claude", str(self.repo)), ("codex", str(self.repo))])

    def test_ordinary_git_activity_does_not_make_a_checkout_new(self):
        for n in range(3):
            (self.repo / "f").write_text("change %d" % n)
            git(self.repo, "commit", "-q", "-am", "c%d" % n)
            git(self.repo, "checkout", "-q", "-b", "b%d" % n)
            git(self.repo, "config", "user.name", "x%d" % n)
            self.scan(now=1010 + n)
        self.assertEqual(self.launcher.calls, [], "HEAD and config are replaced by git all the time; .git itself is not")

    def test_state_from_an_older_version_gets_an_identity_without_relaunching(self):
        self.state.known[str(self.repo)].pop("id")
        self.scan(now=1010)
        self.assertEqual(self.launcher.calls, [])
        self.assertIsNotNone(self.state.known[str(self.repo)]["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

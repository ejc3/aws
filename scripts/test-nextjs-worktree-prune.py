#!/usr/bin/env python3
"""Exercise the cleanup worker against real isolated Git repositories."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import tarfile
import unittest
from unittest import mock
from urllib.parse import unquote


SPEC = importlib.util.spec_from_file_location(
    "nextjs_worktree_prune", Path(__file__).with_name("nextjs-worktree-prune.py"))
WORKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKER)
GIT = "/usr/bin/git"


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.repo = self.home / "app"
        self.remote = self.base / "origin"
        self.proc = self.base / "proc"
        (self.proc / "self").mkdir(parents=True)
        self.mountinfo = self.proc / "self/mountinfo"
        self.mountinfo.write_text(f"1 1 8:1 / {self.home} rw - ext4 /dev/test rw\n")
        self.ndev = self.base / "ndev"
        self.ndev.mkdir()
        self.uid = os.geteuid()
        self.environment = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                            "GIT_ALLOW_PROTOCOL": "file", "GIT_CONFIG_NOSYSTEM": "1"}
        self.git(self.home, "init", "-q", "-b", "main", str(self.repo))
        self.git(self.home, "init", "-q", "--bare", "-b", "main", str(self.remote))
        self.git(self.repo, "config", "user.name", "Fixture")
        self.git(self.repo, "config", "user.email", "fixture@example.com")
        (self.repo / "src").mkdir()
        (self.repo / "src/code").write_text("base\n")
        (self.repo / ".gitignore").write_text("*.local\n")
        self.git(self.repo, "add", ".")
        self.git(self.repo, "commit", "-qm", "base")
        self.git(self.repo, "remote", "add", "origin", str(self.remote))
        self.push()
        self.github_responses = None
        self.compares = {}
        self.origin_url = b"https://github.com/example/app.git\n"
        self.network = []
        real_run = WORKER.run

        def fixture_run(home, argv, timeout=WORKER.TIMEOUT):
            if argv[0] == GIT and "get-url" in argv:
                return self.origin_url
            if argv[0] == "/usr/bin/gh":
                # The live-default evidence comes from the local bare fixture,
                # with GitHub's read-only response shape and no real network.
                endpoint = argv[-1]
                self.network.append(endpoint)
                if endpoint == "repos/example/app":
                    return b'{"full_name":"example/app","default_branch":"main"}'
                if endpoint.startswith("repos/example/app/git/ref/heads/"):
                    # Any branch's live tip, read from the bare fixture; a missing branch fails like gh.
                    branch = unquote(endpoint.removeprefix("repos/example/app/git/ref/heads/"))
                    found = subprocess.run([GIT, "-C", str(self.remote), "rev-parse", "--verify", "-q",
                                            "refs/heads/" + branch], env=self.environment,
                                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                    if found.returncode:
                        return None
                    return json.dumps({"ref": "refs/heads/" + branch,
                                       "object": {"type": "commit", "sha": found.stdout.decode().strip()}}).encode()
                if endpoint.startswith("repos/example/app/commits/") and endpoint.endswith("/pulls?per_page=30"):
                    return json.dumps(self.github_responses or []).encode()
                if endpoint.startswith("repos/example/app/compare/"):
                    return json.dumps(self.compares.get(endpoint)).encode()
                raise AssertionError(f"unexpected network proof: {endpoint}")
            return real_run(home, argv, timeout)

        for name, value in (("PROC", self.proc), ("PUBLISHED", self.ndev),
                            ("pressure", lambda home: True), ("run", fixture_run)):
            patch = mock.patch.object(WORKER, name, value)
            patch.start()
            self.addCleanup(patch.stop)

    def git(self, repo, *args):
        return subprocess.run([GIT, "-C", str(repo), *args], env=self.environment,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=5).stdout

    def push(self):
        self.git(self.repo, "push", "-q", "origin", "main")

    def worktree(self, name):
        path = self.home / name
        self.git(self.repo, "worktree", "add", "-q", "-b", name, str(path))
        return path

    def admin(self, path):
        return Path(self.git(path, "rev-parse", "--absolute-git-dir").decode().strip())

    def set_age(self, path, ns):
        admin = self.admin(path)
        for file in (path, path / ".git", admin / "HEAD", admin / "logs/HEAD"):
            if file.exists():
                os.utime(file, ns=(ns, ns))

    def commit(self, path):
        (path / "src/code").write_text("feature\n")
        self.git(path, "add", "src/code")
        self.git(path, "commit", "-qm", "feature")
        return self.git(path, "rev-parse", "HEAD").decode().strip()

    def list(self):
        return WORKER.prune(self.home, self.uid)["candidates"]

    def remove(self, token):
        return WORKER.prune(self.home, self.uid, token)


class WorktreePruneTests(Fixture):
    def test_inaccessible_fd_only_allows_a_single_thread_zombie(self):
        tree = self.worktree("eligible")
        proc = self.proc / "2147483647"
        proc.mkdir()
        scandir = os.scandir

        def deny_fds(path):
            if path == proc / "fd":
                raise PermissionError(13, "inaccessible fd")
            return scandir(path)

        with mock.patch.object(os, "scandir", side_effect=deny_fds):
            # A missing status file supplies no proof about the failed PID.
            with self.assertRaises(PermissionError):
                WORKER.prune(self.home, self.uid, inspect_all=True)
            for state, threads in (("S (sleeping)", "1"), ("Z (zombie)", "2"), ("Z (zombie)", "")):
                with self.subTest(state=state, threads=threads):
                    (proc / "status").write_text(f"State:\t{state}\nThreads:\t{threads}\n")
                    with self.assertRaises(PermissionError):
                        WORKER.prune(self.home, self.uid, inspect_all=True)
            (proc / "status").write_text("State:\tZ (zombie)\nThreads:\t1\n")
            [candidate] = WORKER.prune(self.home, self.uid, inspect_all=True)["candidates"]
            self.assertEqual(WORKER.prune(self.home, self.uid, candidate["token"], inspect_all=True),
                             {"status": "removed"})
        self.assertFalse(tree.exists())

    def test_replaced_pid_with_zombie_status_cannot_excuse_inaccessible_fd(self):
        self.worktree("eligible")
        proc = self.proc / "2147483647"
        proc.mkdir()
        scandir = os.scandir

        def replace_pid(path):
            if path == proc / "fd":
                proc.rename(self.base / "old-pid")
                proc.mkdir()
                (proc / "status").write_text("State:\tZ (zombie)\nThreads:\t1\n")
                raise PermissionError(13, "inaccessible fd")
            return scandir(path)

        with mock.patch.object(os, "scandir", side_effect=replace_pid), self.assertRaises(PermissionError):
            WORKER.prune(self.home, self.uid, inspect_all=True)

    def test_oldest_clean_merged_candidate_removed_without_deleting_ref(self):
        newer = self.worktree("newer")
        older = self.worktree("older")
        self.commit(older)
        self.git(self.repo, "merge", "-qm", "merge older", "--no-ff", "older")
        self.push()
        self.set_age(older, 100_000_000_000)
        self.set_age(newer, 200_000_000_000)
        candidates = self.list()
        self.assertEqual([c["age_ns"] for c in candidates], [100_000_000_000, 200_000_000_000])
        self.assertTrue(older.exists(), "listing must be read-only")
        self.assertEqual(self.remove(candidates[0]["token"]), {"status": "removed"})
        self.assertFalse(older.exists())
        self.assertTrue(newer.exists())
        self.assertTrue(self.repo.exists())
        self.assertIn(b"refs/heads/older", self.git(self.repo, "show-ref", "refs/heads/older"))

    def test_host_without_ndev_publishing_still_ranks_and_removes(self):
        # fcvm-metal-arm has no /var/lib/ndev: nothing is published there.
        merged = self.worktree("merged")
        self.commit(merged)
        self.git(self.repo, "merge", "-qm", "merge", "--no-ff", "merged")
        self.push()
        with mock.patch.object(WORKER, "PUBLISHED", self.base / "absent/instances"):
            candidates = self.list()
            self.assertEqual(len(candidates), 1)
            self.assertEqual(self.remove(candidates[0]["token"]), {"status": "removed"})
        self.assertFalse(merged.exists())

    def test_dangling_publishing_link_still_stops_the_listing(self):
        merged = self.worktree("merged")
        self.commit(merged)
        self.git(self.repo, "merge", "-qm", "merge", "--no-ff", "merged")
        self.push()
        link = self.base / "dangling"
        link.symlink_to(self.base / "absent")
        with mock.patch.object(WORKER, "PUBLISHED", link), self.assertRaises(OSError):
            self.list()
        self.assertTrue(merged.exists())

    def test_local_state_locks_and_active_paths_are_protected(self):
        paths = {name: self.worktree(name) for name in
                 ("ignored", "untracked", "dirty", "locked", "interactive",
                  "cwd", "fd", "published", "mount", "unmerged", "assume")}
        (paths["ignored"] / "local-data.local").write_text("local user state")
        (paths["untracked"] / "new-file").write_text("untracked")
        (paths["dirty"] / "src/code").write_text("dirty")
        self.git(self.repo, "worktree", "lock", str(paths["locked"]))
        (self.admin(paths["interactive"]) / "claude-interactive").touch()
        self.commit(paths["unmerged"])
        self.git(paths["assume"], "update-index", "--assume-unchanged", "src/code")
        (paths["assume"] / "src/code").write_text("hidden dirty")

        pid = self.proc / "2147483647"
        (pid / "fd").mkdir(parents=True)
        (pid / "status").write_text(f"Uid:\t{self.uid}\t{self.uid}\t{self.uid}\t{self.uid}\n")
        (pid / "cwd").symlink_to(paths["cwd"] / "src")
        (pid / "fd/3").symlink_to(paths["fd"] / "src/code")
        (self.ndev / "web.env").write_text(f"DIR={paths['published']}/src\n")
        with self.mountinfo.open("a") as stream:
            stream.write(f"2 1 8:1 / {paths['mount']}/src rw - ext4 /dev/test rw\n")

        self.assertEqual(self.list(), [])
        for path in paths.values():
            self.assertTrue(path.exists())

    def test_new_ignored_data_after_listing_blocks_removal(self):
        path = self.worktree("candidate")
        [candidate] = self.list()
        # This doesn't update the worktree root mtime or Git administration files.
        (path / "src/new.local").write_text("created after listing")
        self.assertEqual(self.remove(candidate["token"]), {"status": "skipped"})
        self.assertEqual((path / "src/new.local").read_text(), "created after listing")

    def test_untracked_dependency_root_skips_recursive_status(self):
        dirty = self.worktree("dirty-dependencies")
        cache = dirty / "node_modules"
        cache.mkdir()
        (cache / "cached-package.local").write_text("local dependency")
        clean = self.worktree("clean")

        checks = []
        git = WORKER.git

        def record(home, path, *args, **kwargs):
            if args[0] == "status":
                checks.append(path)
            return git(home, path, *args, **kwargs)

        with mock.patch.object(WORKER, "git", side_effect=record):
            [candidate] = self.list()
        self.assertIn(clean, checks)
        self.assertNotIn(dirty, checks)
        self.assertEqual(self.remove(candidate["token"]), {"status": "removed"})
        self.assertTrue(cache.exists())

    def test_one_unreadable_worktree_preserves_it_without_blocking_another(self):
        unreadable = self.worktree("unreadable")
        eligible = self.worktree("eligible")
        self.set_age(unreadable, 1)
        self.set_age(eligible, 2)
        real_clean = WORKER.clean

        def verify(home, path, head, admin, **options):
            if path == unreadable:
                raise WORKER.Incomplete("command timed out")
            return real_clean(home, path, head, admin, **options)

        with mock.patch.object(WORKER, "clean", side_effect=verify):
            [candidate] = self.list()
            self.assertEqual(candidate["age_ns"], 2)
            self.assertEqual(self.remove(candidate["token"]), {"status": "removed"})
        self.assertTrue(unreadable.exists())

    def test_nested_untracked_files_are_still_rejected(self):
        dirty = self.worktree("dirty-nested")
        extra = dirty / "src" / "local.local"
        extra.write_text("not tracked")
        self.assertEqual(self.list(), [])
        self.assertTrue(extra.exists())

    def test_squash_requires_exact_pr_head_and_reachable_merge(self):
        path = self.worktree("squash")
        head = self.commit(path)
        self.git(self.repo, "merge", "--squash", "squash")
        self.git(self.repo, "commit", "-qm", "squashed")
        tip = self.git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.push()
        pull = {"head": {"sha": "0" * 40}, "merged_at": "2026-11-04T00:00:00Z",
                "base": {"ref": "main", "repo": {"full_name": "example/app"}},
                "merge_commit_sha": tip}
        self.github_responses = [pull]
        self.assertEqual(self.list(), [])
        pull["head"]["sha"] = head
        pull["merge_commit_sha"] = "0" * 40
        self.assertEqual(self.list(), [])
        pull["merge_commit_sha"] = tip
        [candidate] = self.list()
        self.assertEqual(self.remove(candidate["token"]), {"status": "removed"})
        self.assertFalse(path.exists())

    def test_squash_of_a_later_pr_head_covers_an_earlier_worktree_commit(self):
        path = self.worktree("early")
        early = self.commit(path)
        # The PR's branch moved on after this worktree's last commit, then was
        # squash-merged and deleted: its final head descends from this worktree's HEAD.
        later = self.home / "later"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(later), early)
        (later / "src/code").write_text("more\n")
        self.git(later, "commit", "-qam", "more")
        pr_head = self.git(later, "rev-parse", "HEAD").decode().strip()
        self.git(self.repo, "worktree", "remove", "--force", str(later))
        self.git(self.repo, "merge", "--squash", pr_head)
        self.git(self.repo, "commit", "-qm", "squashed")
        tip = self.git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.push()
        pull = {"head": {"sha": pr_head}, "merged_at": "2026-11-04T00:00:00Z",
                "base": {"ref": "main", "repo": {"full_name": "example/app"}},
                "merge_commit_sha": tip}
        self.github_responses = [pull]
        [candidate] = self.list()
        self.assertEqual(self.remove(candidate["token"]), {"status": "removed"})
        self.assertFalse(path.exists())

    def test_squash_of_an_unrelated_pr_head_proves_nothing(self):
        path = self.worktree("early")
        self.commit(path)
        other = self.home / "other"
        self.git(self.repo, "worktree", "add", "-q", "--detach", str(other), "main")
        (other / "src/code").write_text("unrelated\n")
        self.git(other, "commit", "-qam", "unrelated")
        pr_head = self.git(other, "rev-parse", "HEAD").decode().strip()
        self.git(self.repo, "worktree", "remove", "--force", str(other))
        self.git(self.repo, "merge", "--squash", pr_head)
        self.git(self.repo, "commit", "-qm", "squashed")
        tip = self.git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.push()
        self.github_responses = [{"head": {"sha": pr_head}, "merged_at": "2026-11-04T00:00:00Z",
                                  "base": {"ref": "main", "repo": {"full_name": "example/app"}},
                                  "merge_commit_sha": tip}]
        self.assertEqual(self.list(), [])
        self.assertTrue(path.exists())

    def test_healthy_disk_and_unknown_token_do_not_contact_origin(self):
        self.worktree("candidate")
        with mock.patch.object(WORKER, "pressure", return_value=False), \
             mock.patch.object(WORKER, "roots", side_effect=AssertionError("scanned healthy disk")):
            self.assertEqual(self.list(), [])
            self.assertEqual(self.remove("0" * 64), {"status": "healthy"})
        self.assertEqual(self.remove("0" * 64), {"status": "skipped"})
        self.assertEqual(self.network, [])

    def test_non_github_origin_never_makes_a_network_request(self):
        self.worktree("candidate")
        self.origin_url = b"ssh://some-other-host/private/repo\n"
        self.assertEqual(self.list(), [])
        self.assertEqual(self.network, [])

    def test_one_listing_reuses_proof_across_clones_and_remove_checks_again(self):
        first = self.worktree("first")
        clone = self.home / "other-clone"
        self.git(self.home, "clone", "-q", str(self.remote), str(clone))
        second = self.home / "second"
        self.git(clone, "worktree", "add", "-q", "-b", "second", str(second))
        self.set_age(first, 1)
        self.set_age(second, 2)

        merged = WORKER.merged
        with mock.patch.object(WORKER, "merged", side_effect=merged) as proof:
            old, new = self.list()
            self.assertEqual(proof.call_count, 1)
        self.assertEqual([old["age_ns"], new["age_ns"]], [1, 2])
        self.assertEqual(self.network, ["repos/example/app", "repos/example/app/git/ref/heads/main"])

        # An earlier candidate list is not proof at removal time.
        self.network.clear()
        with mock.patch.object(WORKER, "merged", return_value=False) as proof:
            self.assertEqual(self.remove(old["token"]), {"status": "skipped"})
            proof.assert_called_once()
        self.assertEqual(self.network, ["repos/example/app", "repos/example/app/git/ref/heads/main"])
        self.assertTrue(first.exists())

    def test_stale_clone_requires_exact_remote_ancestry_without_fetching(self):
        path = self.worktree("candidate")
        head = self.git(path, "rev-parse", "HEAD").decode().strip()
        upstream = self.base / "upstream"
        self.git(self.home, "clone", "-q", str(self.remote), str(upstream))
        self.git(upstream, "config", "user.name", "Fixture")
        self.git(upstream, "config", "user.email", "fixture@example.com")
        tip = self.commit(upstream)
        self.git(upstream, "push", "-q", "origin", "main")
        self.assertIsNone(WORKER.git(self.home, self.repo, "cat-file", "-e", tip + "^{commit}"))
        endpoint = f"repos/example/app/compare/{head}...{tip}?per_page=1&page=2"
        self.compares[endpoint] = {"status": "ahead", "behind_by": 0,
                                   "base_commit": {"sha": head}, "merge_base_commit": {"sha": tip}}
        self.assertEqual(self.list(), [])
        self.compares[endpoint]["merge_base_commit"]["sha"] = head
        [candidate] = self.list()
        self.assertEqual(self.remove(candidate["token"]), {"status": "removed"})
        self.assertIsNone(WORKER.git(self.home, self.repo, "cat-file", "-e", tip + "^{commit}"))

    def test_privileged_inspection_protects_a_different_uid_cwd(self):
        path = self.worktree("other-user")
        pid = self.proc / "2147483647"
        (pid / "fd").mkdir(parents=True)
        (pid / "status").write_text("Uid:\t0\t0\t0\t0\n")
        (pid / "cwd").symlink_to(path / "src")
        [candidate] = self.list()
        self.assertEqual(WORKER.prune(self.home, self.uid, inspect_all=True), {"candidates": []})
        self.assertEqual(WORKER.prune(self.home, self.uid, candidate["token"], inspect_all=True),
                         {"status": "skipped"})
        self.assertTrue(path.exists())

    def test_root_repository_subprocess_requires_and_drops_to_owner(self):
        with mock.patch.object(WORKER.os, "geteuid", return_value=0), \
             mock.patch.object(WORKER.subprocess, "Popen", side_effect=OSError) as start:
            with self.assertRaises(WORKER.Incomplete):
                WORKER.git(self.home, self.repo, "status")
            start.assert_not_called()
            with mock.patch.object(WORKER, "COMMAND_OWNER", (self.uid, os.getegid())):
                self.assertIsNone(WORKER.git(self.home, self.repo, "status"))
            self.assertEqual(start.call_args.kwargs["user"], self.uid)
            self.assertEqual(start.call_args.kwargs["group"], os.getegid())
            self.assertEqual(start.call_args.kwargs["extra_groups"], [])

    def merged_worktrees(self, count, checkout=True):
        """Worktrees at the default tip (merged), aged 1..count seconds."""
        paths = []
        for index in range(count):
            path = self.home / f"wt{index:04d}"
            self.git(self.repo, "worktree", "add", "-q", *([] if checkout else ["--no-checkout"]),
                     "-b", path.name, str(path))
            self.set_age(path, (index + 1) * 1_000_000_000)
            paths.append(path)
        return paths

    def test_more_eligible_than_the_list_limit_returns_the_oldest_limit(self):
        # 513 real linked worktrees; skip only the per-tree Git status reads.
        paths = self.merged_worktrees(WORKER.LIST_LIMIT + 1, checkout=False)
        with mock.patch.object(WORKER, "clean", return_value=True):
            candidates = self.list()
        self.assertEqual([c["age_ns"] for c in candidates],
                         [(i + 1) * 1_000_000_000 for i in range(WORKER.LIST_LIMIT)])
        self.assertTrue(all(p.exists() for p in paths), "listing must be read-only")

    def test_ranking_budget_returns_only_the_verified_oldest_prefix(self):
        paths = self.merged_worktrees(4)
        now = [0.0]
        real_clean = WORKER.clean
        checked = []

        def clean(home, path, head, directory, **options):
            checked.append(path)
            if len(checked) == 2:
                now[0] = WORKER.RANK_SECONDS + 1  # the budget runs out during the second check
            return real_clean(home, path, head, directory, **options)

        with mock.patch.object(WORKER, "clock", lambda: now[0]), mock.patch.object(WORKER, "clean", clean):
            candidates = self.list()
        self.assertEqual(checked, paths[:2], "no candidate is checked after the budget")
        self.assertEqual([c["age_ns"] for c in candidates], [1_000_000_000, 2_000_000_000])
        for candidate in candidates:
            self.assertEqual(self.remove(candidate["token"]), {"status": "removed"})
        self.assertEqual([p.exists() for p in paths], [False, False, True, True])

    def test_removal_still_refuses_after_its_budget(self):
        path, = self.merged_worktrees(1)
        token, = (c["token"] for c in self.list())
        times = iter([0.0, 0.0, 0.0, WORKER.RANK_SECONDS + 1])
        with mock.patch.object(WORKER, "clock", lambda: next(times, WORKER.RANK_SECONDS + 1)), \
             self.assertRaises(WORKER.Incomplete):
            self.remove(token)
        self.assertTrue(path.exists())

    def test_incomplete_ranking_is_reported_without_a_partial_list(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(WORKER.sys, "argv", ["worker", "--list"]), \
             mock.patch.object(WORKER.pwd, "getpwuid", return_value=mock.Mock(pw_dir="/home/fixture")), \
             mock.patch.object(WORKER, "prune", side_effect=WORKER.Incomplete), \
             mock.patch.object(WORKER.syslog, "syslog"), mock.patch.object(WORKER.syslog, "openlog"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(WORKER.main(), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("unable to rank account completely", err.getvalue())

    def test_failure_reason_is_available_in_the_account_journal_without_user_data(self):
        import syslog
        for error, reason in ((PermissionError(13, "private token", "/private/path"), "PermissionError errno=13"),
                              (WORKER.Incomplete("command timed out"), "command timed out")):
            with self.subTest(reason=reason), \
                 mock.patch.object(WORKER.sys, "argv", ["worker", "--list"]), \
                 mock.patch.object(WORKER.pwd, "getpwuid", return_value=mock.Mock(pw_dir="/home/fixture")), \
                 mock.patch.object(WORKER, "prune", side_effect=error), \
                 mock.patch.object(syslog, "openlog") as ident, mock.patch.object(syslog, "syslog") as journal, \
                 contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(WORKER.main(), 1)
                self.assertEqual(output.getvalue(), "")
                ident.assert_called_once_with("nextjs-worktree-prune-worker")
                level, message = journal.call_args.args
                self.assertEqual(level, syslog.LOG_WARNING)
                self.assertIn(reason, message)
                self.assertRegex(message, r"main:[0-9]+")
                self.assertNotIn("private", message)
                self.assertNotIn("/home", message)

    def test_reader_aborts_when_stdout_exceeds_its_bound(self):
        with mock.patch.object(WORKER, "OUTPUT_LIMIT", 1024):
            with self.assertRaises(WORKER.Incomplete):
                WORKER.run(self.home, ["/usr/bin/python3", "-I", "-c", "print('x' * 1024)"])


class BuildOutputTests(Fixture):
    """The fcvm-only pass over idle worktrees' ignored node_modules, .next and target."""

    def setUp(self):
        super().setUp()
        # Root-anchored .next, so a deeper .next is not ignored; node_modules and target anywhere.
        (self.repo / ".gitignore").write_text("*.local\nnode_modules/\n/.next\ntarget\n")
        self.git(self.repo, "commit", "-qam", "ignore build output")
        self.push()
        # Everything the fixture writes is "now"; judge idleness eight days later.
        self.later = time.time() + 8 * 86400
        patch = mock.patch.object(WORKER, "wall", lambda: self.later)
        patch.start()
        self.addCleanup(patch.stop)

    def output(self, path, *relative):
        for name in relative:
            (path / name).mkdir(parents=True, exist_ok=True)
            (path / name / "artifact.bin").write_bytes(b"x" * 65536)

    def build_worktree(self, name):
        path = self.worktree(name)
        self.output(path, "node_modules/pkg", ".next/cache", "target/debug", "packages/web/node_modules")
        return path

    def builds(self, apply=True, after=None):
        return WORKER.builds(self.home, self.uid, apply, after)

    def kept(self, path):
        return all((path / name).exists() for name in ("node_modules", ".next", "target"))

    def set_reflog_time(self, path, seconds):
        """Rewrite every HEAD reflog entry's time, as if the worktree was last used then."""
        log = self.admin(path) / "logs/HEAD"
        log.write_bytes(re.sub(rb"> [0-9]+ ([+-][0-9]{4})\t", rb"> %d \1\t" % seconds, log.read_bytes()))

    def test_only_ignored_build_directories_are_removed(self):
        path = self.build_worktree("build")
        # Too deep, a .next the anchored rule does not ignore, and other ignored files.
        self.output(path, "a/b/c/node_modules", "packages/web/.next", "dist")
        (path / "notes.local").write_text("ignored, but not build output")
        preview = self.builds(apply=False)
        self.assertEqual((preview["status"], preview["worktrees"], preview["dirs"], preview["failed"]),
                         ("done", 1, 4, 0))
        self.assertGreaterEqual(preview["bytes"], 4 * 65536)
        self.assertTrue(self.kept(path), "a preview removes nothing")
        result = self.builds()
        self.assertEqual({k: result[k] for k in ("status", "worktrees", "dirs", "bytes", "failed")},
                         {k: preview[k] for k in ("status", "worktrees", "dirs", "bytes", "failed")})
        for gone in ("node_modules", ".next", "target", "packages/web/node_modules"):
            self.assertFalse((path / gone).exists(), gone)
        for kept in ("a/b/c/node_modules/artifact.bin", "packages/web/.next/artifact.bin",
                     "dist/artifact.bin", "notes.local", "src/code", ".gitignore", ".git"):
            self.assertTrue((path / kept).exists(), kept)
        self.assertEqual(self.git(path, "status", "--porcelain", "--untracked-files=no"), b"",
                         "no tracked file changed")
        self.assertNotIn(str(self.home), json.dumps(result), "results carry counts, never paths")

    def test_tracked_target_is_kept(self):
        path = self.build_worktree("tracked")
        (path / "target/keep.txt").write_text("tracked despite the ignore rule")
        self.git(path, "add", "-f", "target/keep.txt")
        self.git(path, "-c", "user.name=F", "-c", "user.email=f@example.com", "commit", "-qm", "track target")
        result = self.builds()
        self.assertEqual(result["dirs"], 3)
        self.assertEqual((path / "target/keep.txt").read_text(), "tracked despite the ignore rule")
        self.assertTrue((path / "target/debug/artifact.bin").exists())
        self.assertFalse((path / "node_modules").exists())

    def test_symlinked_build_directory_and_symlink_targets_are_kept(self):
        path = self.worktree("links")
        store, shared = self.home / "store", self.home / "shared"
        self.output(self.home, "store", "shared")
        (path / "node_modules").symlink_to(store)
        (path / ".next").symlink_to(path / "src")
        self.output(path, "target/debug")
        (path / "target/linked").symlink_to(shared)
        result = self.builds()
        self.assertEqual(result["dirs"], 1)
        self.assertFalse((path / "target").exists())
        self.assertTrue((path / "node_modules").is_symlink())
        self.assertTrue((path / ".next").is_symlink())
        for target in (store / "artifact.bin", shared / "artifact.bin", path / "src/code"):
            self.assertTrue(target.exists(), target)

    def test_interactive_locked_recent_and_busy_worktrees_are_untouched(self):
        paths = {name: self.build_worktree(name) for name in
                 ("interactive", "locked", "recent-file", "recent-reflog", "recent-commit", "cwd", "fd", "mount")}
        marker = self.admin(paths["interactive"]) / "claude-interactive"
        marker.touch()
        os.utime(marker, ns=(1, 1))  # a marker of any age keeps the build output
        self.git(self.repo, "worktree", "lock", str(paths["locked"]))
        future = int((self.later - 86400) * 1e9)
        os.utime(paths["recent-file"] / "src/code", ns=(future, future))
        self.set_reflog_time(paths["recent-reflog"], future // 10**9)
        (paths["recent-commit"] / "src/code").write_text("committed later\n")
        os.utime(paths["recent-commit"] / "src/code", ns=(1, 1))
        when = f"@{future // 10**9} +0000"
        subprocess.run([GIT, "-C", str(paths["recent-commit"]), "commit", "-qam", "later"], check=True, timeout=5,
                       env={**self.environment, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when})
        self.set_reflog_time(paths["recent-commit"], int(time.time()))  # only the commit time is recent
        pid = self.proc / "2147483647"
        (pid / "fd").mkdir(parents=True)
        (pid / "status").write_text(f"Uid:\t{self.uid}\t{self.uid}\t{self.uid}\t{self.uid}\n")
        (pid / "cwd").symlink_to(paths["cwd"] / "src")
        (pid / "fd/3").symlink_to(paths["fd"] / "node_modules/pkg/artifact.bin")
        with self.mountinfo.open("a") as stream:
            stream.write(f"2 1 8:1 / {paths['mount']}/target rw - ext4 /dev/test rw\n")
        result = self.builds()
        self.assertEqual((result["worktrees"], result["dirs"]), (0, 0))
        for name, path in paths.items():
            self.assertTrue(self.kept(path), name)

    def test_git_status_rewriting_index_and_head_does_not_count_as_use(self):
        path = self.build_worktree("read-only-use")
        future = int((self.later - 86400) * 1e9)
        admin = self.admin(path)
        for name in ("index", "HEAD"):
            os.utime(admin / name, ns=(future, future))
        self.assertEqual(self.builds()["worktrees"], 1)
        self.assertFalse((path / "node_modules").exists())

    def test_a_recent_ctime_alone_does_not_count_as_use(self):
        # A hard link made or removed anywhere (a pnpm install) changes a file's
        # ctime. Every file here has a ctime of now and an old mtime.
        path = self.build_worktree("old-mtimes")
        for top, _, files in os.walk(path):
            for name in files:
                os.utime(os.path.join(top, name), ns=(1, 1), follow_symlinks=False)
        os.link(path / "src/code", self.base / "hard-link")
        cutoff = int((time.time() - 60) * 1e9)
        device = self.home.stat().st_dev
        found = WORKER.survey(self.home, path, self.uid, device, cutoff, WORKER.clock() + 60)
        self.assertEqual(len(found), 4)
        os.utime(path / "src/code", ns=(cutoff, cutoff))
        with self.assertRaises(WORKER.NotIdle):
            WORKER.survey(self.home, path, self.uid, device, cutoff, WORKER.clock() + 60)

    def test_nested_clone_keeps_the_whole_worktree(self):
        path = self.build_worktree("nested")
        nested = path / "vendor/lib"
        self.git(self.home, "init", "-q", "-b", "main", str(nested))
        (nested / "target").mkdir()
        (nested / "target/tracked.txt").write_text("the nested clone tracks this")
        self.git(nested, "add", "target/tracked.txt")
        self.git(nested, "-c", "user.name=F", "-c", "user.email=f@example.com", "commit", "-qm", "nested")
        # The outer worktree ignores vendor/lib/target and tracks nothing there.
        self.assertTrue(WORKER.build_ignored(self.home, path, "vendor/lib/target"))
        # A repository inside a build directory keeps that directory only.
        other = self.build_worktree("git-dependency")
        self.git(self.home, "init", "-q", str(other / "node_modules/pkg"))
        result = self.builds()
        self.assertEqual((result["worktrees"], result["dirs"]), (1, 3))
        self.assertTrue((nested / "target/tracked.txt").exists())
        self.assertTrue(self.kept(path))
        self.assertTrue((other / "node_modules/pkg/.git").exists())
        self.assertFalse((other / "target").exists())

    def test_running_executable_or_mapped_library_keeps_the_worktree(self):
        running, mapped, idle = (self.build_worktree(n) for n in ("running", "mapped", "idle"))
        (running / "target/debug/app").write_bytes(b"\x7fELF")
        for pid, link in (("2147483646", running / "target/debug/app"), ("2147483645", None)):
            proc = self.proc / pid
            (proc / "fd").mkdir(parents=True)
            (proc / "status").write_text(f"Uid:\t{self.uid}\t{self.uid}\t{self.uid}\t{self.uid}\n")
            (proc / "cwd").symlink_to("/")
            if link:
                (proc / "exe").symlink_to(link)
        library = mapped / "node_modules/pkg/addon.node"
        library.write_bytes(b"\x7fELF")
        (self.proc / "2147483645/maps").write_text(
            f"7f0000000000-7f0000001000 r-xp 00000000 08:01 12345    {library} (deleted)\n"
            "7f0000002000-7f0000003000 rw-p 00000000 00:00 0 \n")
        result = self.builds()
        self.assertEqual(result["worktrees"], 1)
        self.assertTrue(self.kept(running) and self.kept(mapped))
        self.assertFalse((idle / "node_modules").exists())

    def test_directory_swapped_between_survey_and_removal_is_kept(self):
        for replacement in ("symlink", "directory"):
            with self.subTest(replacement=replacement):
                path = self.build_worktree(f"swap-{replacement}")
                victim = self.home / f"victim-{replacement}"
                self.output(self.home, victim.name)
                measure, swapped = WORKER.measure, []

                def swap(directory, device, end):
                    # Measuring the first directory is the last step before
                    # its recheck; swap it out then.
                    size = measure(directory, device, end)
                    if not swapped:
                        swapped.append(directory)
                        directory.rename(path / "moved-away")
                        if replacement == "symlink":
                            directory.symlink_to(victim)
                        else:
                            directory.mkdir()
                            (directory / "artifact.bin").write_text("new")
                    return size

                with mock.patch.object(WORKER, "measure", swap):
                    result = self.builds()
                self.assertEqual(result["dirs"], 0)
                self.assertTrue((victim / "artifact.bin").exists())
                self.assertTrue((path / "moved-away").is_dir())
                self.assertTrue((swapped[0] / "artifact.bin").exists())
                self.assertTrue(self.kept(path), "no removal after a failed recheck")
                self.git(self.repo, "worktree", "remove", "--force", str(path))

    def test_unremovable_file_is_reported_once_and_the_run_goes_on(self):
        stuck, other = self.build_worktree("stuck"), self.build_worktree("other")
        locked = stuck / "target/debug/root-owned"
        locked.mkdir()
        (locked / "file").write_text("rm cannot remove this")
        locked.chmod(0o555)
        self.addCleanup(locked.chmod, 0o755)
        result = self.builds()
        self.assertEqual((result["status"], result["failed"], result["worktrees"], result["dirs"]),
                         ("done", 1, 2, 7))
        self.assertTrue((locked / "file").exists())
        for gone in ("node_modules", ".next", "packages/web/node_modules"):
            self.assertFalse((stuck / gone).exists(), gone)
        self.assertFalse((other / "target").exists())

    def test_main_checkout_is_untouched(self):
        self.output(self.repo, "node_modules/pkg", "target/debug")
        linked = self.build_worktree("linked")
        self.assertEqual(self.builds()["worktrees"], 1)
        self.assertTrue((self.repo / "node_modules/pkg/artifact.bin").exists())
        self.assertTrue((self.repo / "target/debug/artifact.bin").exists())
        self.assertFalse((linked / "node_modules").exists())

    def test_oldest_first_and_stops_when_pressure_clears(self):
        older, newer = self.build_worktree("older"), self.build_worktree("newer")
        base = int(time.time())
        self.set_reflog_time(older, base + 10)
        self.set_reflog_time(newer, base + 20)
        with mock.patch.object(WORKER, "pressure", lambda home: (older / "node_modules").exists()):
            result = self.builds()
        self.assertEqual((result["status"], result["worktrees"]), ("healthy", 1))
        self.assertFalse((older / "node_modules").exists())
        self.assertTrue(self.kept(newer))
        with mock.patch.object(WORKER, "pressure", return_value=False):
            self.assertEqual(self.builds()["status"], "healthy")
        self.assertTrue(self.kept(newer))

    def test_time_budget_returns_a_cursor_that_the_next_pass_continues(self):
        older, newer = self.build_worktree("older"), self.build_worktree("newer")
        base = int(time.time())
        self.set_reflog_time(older, base + 10)
        self.set_reflog_time(newer, base + 20)
        now = [0.0]
        survey = WORKER.survey

        def slow(*args):
            found = survey(*args)
            now[0] = WORKER.BUILD_SECONDS + 1  # the budget runs out during the first worktree
            return found

        with mock.patch.object(WORKER, "clock", lambda: now[0]), mock.patch.object(WORKER, "survey", slow):
            first = self.builds()
        self.assertEqual((first["status"], first["worktrees"]), ("more", 1))
        self.assertTrue(self.kept(newer))
        match = WORKER.CURSOR.fullmatch(first["next"])
        second = self.builds(after=(int(match[1]), match[2]))
        self.assertEqual((second["status"], second["worktrees"]), ("done", 1))
        self.assertFalse((newer / "node_modules").exists())

    def test_build_modes_run_from_the_command_line_and_after_needs_one(self):
        for argv, apply in ((["worker", "--builds"], False), (["worker", "--clean-builds"], True)):
            with mock.patch.object(WORKER.sys, "argv", argv), \
                 mock.patch.object(WORKER.pwd, "getpwuid", return_value=mock.Mock(pw_dir="/home/fixture", pw_uid=1)), \
                 mock.patch.object(WORKER, "builds", return_value={"status": "done"}) as run, \
                 mock.patch.object(WORKER, "prune", side_effect=AssertionError("merged pass ran")), \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(WORKER.main(), 0)
            self.assertEqual(run.call_args.args[2], apply)
            self.assertEqual(json.loads(out.getvalue()), {"status": "done"})
        with mock.patch.object(WORKER.sys, "argv", ["worker", "--list", "--after", "1:" + "a" * 64]), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            WORKER.main()


class RetireTests(Fixture):
    """--retire (fcvm-metal-arm): archive ignored files, then remove; pushed idle worktrees qualify."""

    def setUp(self):
        super().setUp()
        (self.repo / ".git/info/exclude").write_text(".scratch/\nnode_modules/\n")
        # Everything made during the test is a month old for the idle check.
        later = time.time() + 30 * 86400
        patch = mock.patch.object(WORKER, "wall", lambda: later)
        patch.start()
        self.addCleanup(patch.stop)
        self.archives = self.home / ".local/share/worktree-archive/app"

    def retire_list(self):
        return WORKER.prune(self.home, self.uid, retire=True)["candidates"]

    def retire_remove(self, token):
        return WORKER.prune(self.home, self.uid, token, retire=True)

    def unique_commit(self, path):
        # Distinct content, so two worktrees never share one commit object.
        (path / "src/code").write_text(f"feature {path.name}\n")
        self.git(path, "commit", "-qam", path.name)

    def pushed(self, name):
        path = self.worktree(name)
        self.unique_commit(path)
        self.git(path, "push", "-q", "origin", name)
        return path

    def scratch(self, path):
        (path / ".scratch/sub").mkdir(parents=True)
        (path / ".scratch/notes.md").write_text("keep me\n")
        (path / ".scratch/sub/run.log").write_text("log\n")
        os.symlink("notes.md", path / ".scratch/link")
        (path / ".scratch/node_modules").mkdir()
        (path / ".scratch/node_modules/dep.js").write_text("x\n")
        (path / "node_modules").mkdir()
        (path / "node_modules/big.js").write_text("y\n")
        (path / "app.local").write_text("SECRET=1\n")

    def archived(self):
        return sorted(self.archives.iterdir()) if self.archives.exists() else []

    def members(self, archive):
        with tarfile.open(archive, "r:gz") as tar:
            return {m.name: (tar.extractfile(m).read() if m.isfile() else m.type) for m in tar}

    def test_pushed_idle_worktree_is_archived_then_removed_keeping_its_branch(self):
        path = self.pushed("feature")
        self.scratch(path)
        [candidate] = self.retire_list()
        self.assertEqual(self.retire_remove(candidate["token"]), {"status": "removed"})
        self.assertFalse(path.exists())
        self.assertTrue(self.git(self.repo, "rev-parse", "--verify", "refs/heads/feature"))
        [archive] = self.archived()
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        members = self.members(archive)
        self.assertEqual(members["worktree/.scratch/notes.md"], b"keep me\n")
        self.assertEqual(members["worktree/.scratch/sub/run.log"], b"log\n")
        self.assertEqual(members["worktree/.scratch/link"], tarfile.SYMTYPE)
        self.assertEqual(members["worktree/app.local"], b"SECRET=1\n")
        # A build directory Git ignores whole is not kept; one inside kept files is.
        self.assertFalse([m for m in members if m.startswith("worktree/node_modules")])
        self.assertEqual(members["worktree/.scratch/node_modules/dep.js"], b"x\n")
        manifest = json.loads(members["MANIFEST.json"])
        self.assertEqual(manifest["worktree"], str(path))

    def test_merged_worktree_is_archived_before_removal(self):
        path = self.worktree("merged")
        self.scratch(path)
        [candidate] = self.retire_list()
        self.assertEqual(self.retire_remove(candidate["token"]), {"status": "removed"})
        [archive] = self.archived()
        self.assertIn("worktree/.scratch/notes.md", self.members(archive))

    def test_without_retire_ignored_files_still_keep_the_worktree(self):
        path = self.worktree("merged")
        self.scratch(path)
        self.assertEqual(self.list(), [])
        self.assertTrue(path.exists())

    def test_work_not_safe_in_git_or_in_use_is_kept(self):
        unpushed = self.worktree("unpushed")
        self.unique_commit(unpushed)
        deleted = self.pushed("deleted-upstream")
        self.git(self.remote, "branch", "-D", "deleted-upstream")
        untracked = self.pushed("untracked")
        (untracked / "draft.txt").write_text("not ignored\n")
        modified = self.pushed("modified")
        (modified / "src/code").write_text("edited\n")
        marked = self.pushed("marked")
        (self.admin(marked) / "claude-interactive").touch()
        self.assertEqual(self.retire_list(), [])
        for path in (unpushed, deleted, untracked, modified, marked):
            self.assertTrue(path.exists())

    def test_recent_pushed_worktree_is_kept(self):
        path = self.pushed("recent")
        with mock.patch.object(WORKER, "wall", time.time):
            self.assertEqual(self.retire_list(), [])
        self.assertTrue(path.exists())

    def test_a_repository_inside_ignored_files_keeps_the_worktree(self):
        nested = self.pushed("nested")
        self.scratch(nested)
        self.git(self.home, "init", "-q", str(nested / ".scratch/clone"))
        [candidate] = self.retire_list()
        self.assertEqual(self.retire_remove(candidate["token"]), {"status": "skipped"})
        self.assertTrue((nested / ".scratch/clone/.git").exists())
        self.assertEqual(self.archived(), [])  # no partial archive left behind

    def test_ignored_files_over_the_archive_limit_keep_the_worktree(self):
        big = self.pushed("big")
        self.scratch(big)
        (big / ".scratch/huge.bin").write_bytes(b"z" * 4096)
        [candidate] = self.retire_list()
        with mock.patch.object(WORKER, "ARCHIVE_LIMIT", 1024):
            self.assertEqual(self.retire_remove(candidate["token"]), {"status": "skipped"})
        self.assertTrue((big / ".scratch/huge.bin").exists())
        self.assertEqual(self.archived(), [])

    def test_an_archive_that_does_not_read_back_keeps_the_worktree(self):
        path = self.pushed("feature")
        self.scratch(path)
        [candidate] = self.retire_list()
        real_open = tarfile.open
        empty = io.BytesIO()
        with real_open(fileobj=empty, mode="w"):
            pass

        def truncated(*args, mode="r", **kwargs):
            if mode == "r|":  # the read-back sees an archive with no members
                return real_open(fileobj=io.BytesIO(empty.getvalue()), mode=mode)
            return real_open(*args, mode=mode, **kwargs)

        with mock.patch.object(WORKER.tarfile, "open", truncated):
            self.assertEqual(self.retire_remove(candidate["token"]), {"status": "skipped"})
        self.assertTrue((path / ".scratch/notes.md").exists())
        self.assertEqual(self.archived(), [])

    def test_only_this_cleaners_old_archives_expire(self):
        self.archives.mkdir(parents=True)
        old = self.archives / "wt-01234567-0123456789ab-20260101T000000Z.tar.gz"
        fresh = self.archives / "wt-01234567-0123456789ab-20260901T000000Z.tar.gz"
        other = self.archives / "notes.txt"
        for file in (old, fresh, other):
            file.write_text("x")
        ancient = time.time() - 200 * 86400
        for file in (old, other):
            os.utime(file, (ancient, ancient))
        self.retire_list()
        self.retire_remove("0" * 64)
        self.assertTrue(old.exists())  # listing and removal never expire archives
        # Its own mode, which the driver sends on applying runs whether or not anything is eligible.
        self.assertEqual(WORKER.expire_archives(self.home, self.uid, self.home.stat().st_dev), 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists() and other.exists())

    def test_expiry_skips_a_directory_on_another_disk(self):
        mounted = self.archives.parent / "backup"
        mounted.mkdir(parents=True)
        old = mounted / "wt-01234567-0123456789ab-20260101T000000Z.tar.gz"
        old.write_text("x")
        ancient = time.time() - 200 * 86400
        os.utime(old, (ancient, ancient))
        real_fstat = os.fstat
        inode = mounted.stat().st_ino

        def other_disk(fd):
            info = real_fstat(fd)
            if info.st_ino == inode:  # the mounted directory reports another device
                values = list(info)
                values[2] = info.st_dev + 1
                return os.stat_result(values)
            return info

        with mock.patch.object(WORKER.os, "fstat", other_disk):
            self.assertEqual(WORKER.expire_archives(self.home, self.uid, self.home.stat().st_dev), 0)
        self.assertTrue(old.exists())

    def test_a_change_after_archiving_keeps_the_worktree(self):
        path = self.pushed("feature")
        self.scratch(path)
        [candidate] = self.retire_list()
        real = WORKER.fingerprint
        calls = []

        def changing(*args):
            calls.append(1)
            if len(calls) == 2:  # the recheck after the archive is written
                (path / ".scratch/late.txt").write_text("written after archiving\n")
            return real(*args)

        with mock.patch.object(WORKER, "fingerprint", changing):
            self.assertEqual(self.retire_remove(candidate["token"]), {"status": "skipped"})
        self.assertTrue((path / ".scratch/late.txt").exists())
        self.assertEqual(self.archived(), [])

    def test_a_repository_inside_build_output_keeps_the_worktree(self):
        for name, clone in (("top", "node_modules/pkg"), ("inner", ".scratch/target/clone")):
            path = self.pushed(name)
            self.scratch(path)
            (path / clone).mkdir(parents=True, exist_ok=True)
            self.git(self.home, "init", "-q", str(path / clone))
        for candidate in self.retire_list():
            self.assertEqual(self.retire_remove(candidate["token"]), {"status": "skipped"})
        self.assertTrue((self.home / "top/node_modules/pkg/.git").exists())
        self.assertTrue((self.home / "inner/.scratch/target/clone/.git").exists())

    def test_two_worktrees_with_one_name_and_head_keep_two_archives(self):
        first = self.pushed("feature")
        self.scratch(first)
        (self.home / "other").mkdir()
        second = self.home / "other/feature"
        self.git(self.repo, "worktree", "add", "-q", "-b", "feature-copy", str(second), "feature")
        self.scratch(second)
        (second / ".scratch/notes.md").write_text("second copy\n")
        real_gmtime = time.gmtime
        with mock.patch.object(WORKER.time, "gmtime", lambda *_: real_gmtime(0)):  # the same second
            candidates = self.retire_list()
            self.assertEqual(len(candidates), 2)
            for candidate in candidates:
                self.assertEqual(self.retire_remove(candidate["token"]), {"status": "removed"})
        archives = self.archived()
        self.assertEqual(len(archives), 2)
        notes = {self.members(a)["worktree/.scratch/notes.md"] for a in archives}
        self.assertEqual(notes, {b"keep me\n", b"second copy\n"})

    def test_an_ignored_file_named_like_build_output_is_archived(self):
        (self.repo / ".git/info/exclude").write_text(".scratch/\nnode_modules/\ntarget\n")
        path = self.pushed("feature")
        (path / "target").write_text("user data, not a build directory\n")
        [candidate] = self.retire_list()
        self.assertEqual(self.retire_remove(candidate["token"]), {"status": "removed"})
        [archive] = self.archived()
        self.assertEqual(self.members(archive)["worktree/target"], b"user data, not a build directory\n")

    def test_a_damaged_archive_keeps_the_worktree(self):
        path = self.pushed("feature")
        self.scratch(path)
        [candidate] = self.retire_list()
        real_fsync = os.fsync
        done = []

        def damage(fd):
            if not done and stat_is_file(fd):
                done.append(1)
                os.ftruncate(fd, os.fstat(fd).st_size - 4)  # lose the gzip length trailer
            return real_fsync(fd)

        with mock.patch.object(WORKER.os, "fsync", damage):
            self.assertEqual(self.retire_remove(candidate["token"]), {"status": "skipped"})
        self.assertTrue(done)
        self.assertTrue((path / ".scratch/notes.md").exists())
        self.assertEqual(self.archived(), [])


    def test_as_root_the_archive_is_written_as_the_account_with_no_chown(self):
        path = self.pushed("feature")
        self.scratch(path)
        calls = []
        effective = [0]

        def record(name):
            def call(*args):
                calls.append((name, *args))
                if name == "seteuid":
                    effective[0] = args[0]
            return call

        def refuse(*args, **kwargs):
            raise AssertionError("chown as root")

        archive = WORKER.archive_ignored
        def checked(*args):
            result = archive(*args)
            self.assertEqual(effective[0], 0)  # root again once the file work ends
            return result

        with mock.patch.object(WORKER, "COMMAND_OWNER", (1234, 5678)), \
                mock.patch.object(WORKER.os, "geteuid", lambda: effective[0]), \
                mock.patch.object(WORKER.os, "getgroups", lambda: [0]), \
                mock.patch.object(WORKER.os, "setgroups", record("setgroups")), \
                mock.patch.object(WORKER.os, "setegid", record("setegid")), \
                mock.patch.object(WORKER.os, "seteuid", record("seteuid")), \
                mock.patch.object(WORKER.os, "chown", refuse), mock.patch.object(WORKER.os, "fchown", refuse), \
                mock.patch.object(WORKER, "run", lambda *a, **k: self.fail("a command ran during the archive")):
            # The archive step alone, as the root-run worker calls it.
            archived, _ = checked(self.home, self.uid, self.home.stat().st_dev, self.repo, path,
                                  "0" * 40, [".scratch", "app.local"], time.monotonic() + 60)
        self.assertTrue(archived.exists())
        self.assertEqual(calls[:3], [("setgroups", [5678]), ("setegid", 5678), ("seteuid", 1234)])
        self.assertEqual(calls[3:], [("seteuid", 0), ("setegid", 0), ("setgroups", [0])])
        self.assertEqual(len(self.archived()), 1)

    def test_a_write_just_before_removal_keeps_the_worktree(self):
        path = self.pushed("feature")
        self.scratch(path)
        [candidate] = self.retire_list()
        real = WORKER.archive_ignored

        def then_write(*args):
            result = real(*args)
            # Same size and the old mtime put back: only ctime shows the write.
            note = path / ".scratch/notes.md"
            before = note.stat()
            note.write_text("KEEP ME\n")
            os.utime(note, ns=(before.st_atime_ns, before.st_mtime_ns))
            return result

        with mock.patch.object(WORKER, "archive_ignored", then_write):
            self.assertEqual(self.retire_remove(candidate["token"]), {"status": "skipped"})
        self.assertEqual((path / ".scratch/notes.md").read_text(), "KEEP ME\n")

    def test_a_file_that_grows_past_the_limit_while_copying_stops_the_archive(self):
        path = self.pushed("feature")
        self.scratch(path)
        (path / ".scratch/grow.bin").write_bytes(b"a" * 100)
        [candidate] = self.retire_list()
        real = WORKER.fingerprint
        calls = []

        def grow(*args):
            result = real(*args)
            calls.append(1)
            if len(calls) == 1:  # after the size check, before the copy
                (path / ".scratch/grow.bin").write_bytes(b"a" * 5000)
            return result

        copied = []
        real_read = WORKER.Hashing.read

        def count(self_, size=-1):
            data = real_read(self_, size)
            copied.append(len(data))
            return data

        with mock.patch.object(WORKER, "ARCHIVE_LIMIT", 1024), mock.patch.object(WORKER, "fingerprint", grow), \
                mock.patch.object(WORKER.Hashing, "read", count):
            self.assertEqual(self.retire_remove(candidate["token"]), {"status": "skipped"})
        self.assertLessEqual(sum(copied), 1024)
        self.assertTrue(path.exists())

    def test_retire_is_refused_with_a_build_pass(self):
        with mock.patch.object(WORKER.sys, "argv", ["worker", "--builds", "--retire"]), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            WORKER.main()


def stat_is_file(fd):
    import stat as st
    return st.S_ISREG(os.fstat(fd).st_mode)


if __name__ == "__main__":
    unittest.main(buffer=True)

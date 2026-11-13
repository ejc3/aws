#!/usr/bin/env python3
"""Exercise the cleanup worker against real isolated Git repositories."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "nextjs_worktree_prune", Path(__file__).with_name("nextjs-worktree-prune.py"))
WORKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WORKER)
GIT = "/usr/bin/git"


class WorktreePruneTests(unittest.TestCase):
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
                if endpoint == "repos/example/app/git/ref/heads/main":
                    tip = self.git(self.remote, "rev-parse", "refs/heads/main").decode().strip()
                    return json.dumps({"ref": "refs/heads/main",
                                       "object": {"type": "commit", "sha": tip}}).encode()
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

        def verify(home, path, head, admin):
            if path == unreadable:
                raise WORKER.Incomplete("command timed out")
            return real_clean(home, path, head, admin)

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

    def test_incomplete_ranking_never_returns_a_partial_list(self):
        self.worktree("older")
        self.worktree("newer")
        with mock.patch.object(WORKER, "LIST_LIMIT", 1):
            with self.assertRaises(WORKER.Incomplete):
                self.list()

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(WORKER.sys, "argv", ["worker", "--list"]), \
             mock.patch.object(WORKER.pwd, "getpwuid", return_value=mock.Mock(pw_dir="/home/fixture")), \
             mock.patch.object(WORKER, "prune", side_effect=WORKER.Incomplete), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(WORKER.main(), 1)
        self.assertEqual(out.getvalue(), "")
        self.assertIn("unable to rank account completely", err.getvalue())

    def test_reader_aborts_when_stdout_exceeds_its_bound(self):
        with mock.patch.object(WORKER, "OUTPUT_LIMIT", 1024):
            with self.assertRaises(WORKER.Incomplete):
                WORKER.run(self.home, ["/usr/bin/python3", "-I", "-c", "print('x' * 1024)"])


if __name__ == "__main__":
    unittest.main(buffer=True)

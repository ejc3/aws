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
import time
import unittest
from unittest import mock


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


class WorktreePruneTests(Fixture):
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

        def clean(home, path, head, directory):
            checked.append(path)
            if len(checked) == 2:
                now[0] = WORKER.RANK_SECONDS + 1  # the budget runs out during the second check
            return real_clean(home, path, head, directory)

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

    def set_git_age(self, path, ns):
        admin = self.admin(path)
        for name in ("HEAD", "index", "logs/HEAD"):
            os.utime(admin / name, ns=(ns, ns))

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
                 ("interactive", "locked", "recent-file", "recent-head", "cwd", "fd", "mount")}
        marker = self.admin(paths["interactive"]) / "claude-interactive"
        marker.touch()
        os.utime(marker, ns=(1, 1))  # a marker of any age keeps the build output
        self.git(self.repo, "worktree", "lock", str(paths["locked"]))
        future = int((self.later - 86400) * 1e9)
        os.utime(paths["recent-file"] / "src/code", ns=(future, future))
        os.utime(self.admin(paths["recent-head"]) / "HEAD", ns=(future, future))
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

    def test_main_checkout_is_untouched(self):
        self.output(self.repo, "node_modules/pkg", "target/debug")
        linked = self.build_worktree("linked")
        self.assertEqual(self.builds()["worktrees"], 1)
        self.assertTrue((self.repo / "node_modules/pkg/artifact.bin").exists())
        self.assertTrue((self.repo / "target/debug/artifact.bin").exists())
        self.assertFalse((linked / "node_modules").exists())

    def test_oldest_first_and_stops_when_pressure_clears(self):
        older, newer = self.build_worktree("older"), self.build_worktree("newer")
        self.set_git_age(older, 1_000_000_000)
        self.set_git_age(newer, 2_000_000_000)
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
        self.set_git_age(older, 1_000_000_000)
        self.set_git_age(newer, 2_000_000_000)
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


if __name__ == "__main__":
    unittest.main(buffer=True)

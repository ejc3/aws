#!/usr/bin/env python3
"""Offline acceptance tests for the host's account ordering and output boundary."""
import contextlib
import importlib.util
import io
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "nextjs_worktree_prune_host", Path(__file__).with_name("nextjs-worktree-prune-host.py")
)
host = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = host
SPEC.loader.exec_module(host)


class HostCleanup(unittest.TestCase):
    def test_oldest_across_accounts_and_stop_on_relief(self):
        calls = []
        listings = {
            "ubuntu": [{"token": "a" * 64, "age_ns": 100}],
            "colton": [{"token": "b" * 64, "age_ns": 80}],
            "ejc3": [{"token": "c" * 64, "age_ns": 20}],
        }
        pressure = True

        def query(account, *argv):
            nonlocal pressure
            calls.append((account, *argv))
            if argv == ("--list",):
                return {"candidates": listings[account]}
            if account == "colton":
                pressure = False
            return {"status": "removed"}

        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(host.clean(listings, query, lambda: pressure, True, lambda _: set()), 2)
        self.assertEqual(calls, [
            ("ubuntu", "--list"), ("colton", "--list"), ("ejc3", "--list"),
            ("ejc3", "--remove", "c" * 64), ("colton", "--remove", "b" * 64),
        ])

    def test_audit_lists_without_invoking_removal(self):
        def query(account, *argv):
            self.assertEqual((account, *argv), ("ubuntu", "--list"))
            return {"candidates": [{"token": "a" * 64, "age_ns": 100}]}

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(host.clean(["ubuntu"], query, lambda: True, False, lambda _: set()), 0)
        self.assertIn("eligible ubuntu", out.getvalue())

    def test_bad_account_is_preserved_without_blocking_healthy_accounts(self):
        calls = []

        def query(account, *argv):
            calls.append((account, *argv))
            if account == "ejc3":
                return {"candidates": [{"token": "--apply", "age_ns": 1}]}
            if account == "connor":
                raise TimeoutError("untrusted details")
            if argv == ("--list",):
                age = 50 if account == "ubuntu" else 10
                return {"candidates": [{"token": account[0] * 64 if account == "colton" else "a" * 64,
                                         "age_ns": age}]}
            return {"status": "removed"}

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(host.IncompleteAccounts):
            host.clean(["ubuntu", "ejc3", "connor", "colton"], query, lambda: True, True, lambda _: set())
        self.assertEqual(calls, [
            ("ubuntu", "--list"), ("ejc3", "--list"), ("connor", "--list"), ("colton", "--list"),
            ("colton", "--remove", "c" * 64), ("ubuntu", "--remove", "a" * 64),
        ])
        self.assertIn("preserving ejc3; listing failed (ValueError)", out.getvalue())
        self.assertIn("preserving connor; listing failed (TimeoutError)", out.getvalue())
        self.assertIn("2 eligible, 2 removed, 2 accounts not ranked", out.getvalue())
        self.assertNotIn("untrusted details", out.getvalue())

    def test_host_process_preserves_a_container_home_before_list_and_remove(self):
        with tempfile.TemporaryDirectory() as temp:
            proc = Path(temp)

            def process(pid, namespace, cwd, fd=None):
                path = proc / pid
                (path / "ns").mkdir(parents=True)
                (path / "ns/pid").symlink_to(namespace)
                (path / "cwd").symlink_to(cwd)
                (path / "fd").mkdir()
                if fd:
                    (path / "fd/4").symlink_to(fd)

            process("self", "pid:[10]", "/")
            process("1", "pid:[10]", "/", "/home/colton/worktree/held-file")
            process("2", "pid:[20]", "/home/connor/worktree")
            calls = []
            listings = {"ubuntu": ("a", 100), "ejc3": ("b", 20), "connor": ("c", 50)}

            def query(account, *args):
                calls.append((account, *args))
                if args == ("--list",):
                    token, age = listings[account]
                    if account == "connor":
                        process("3", "pid:[10]", "/home/ejc3/worktree/subdir")
                    return {"candidates": [{"token": token * 64, "age_ns": age}]}
                return {"status": "removed"}

            with patch.object(host, "PROC", proc), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(host.clean(
                    ["ubuntu", "colton", "ejc3", "connor"], query, lambda: True, True,
                    host.host_busy_accounts,
                ), 2)
            self.assertEqual(calls, [
                ("ubuntu", "--list"), ("ejc3", "--list"), ("connor", "--list"),
                ("connor", "--remove", "c" * 64), ("ubuntu", "--remove", "a" * 64),
            ])

    def test_untrusted_stdout_is_bounded_before_json_parsing(self):
        with tempfile.TemporaryDirectory() as temp:
            script = Path(temp) / "worker.py"
            script.write_text("print('a' * (65 * 1024))\n", encoding="utf-8")
            with patch.object(host, "WORKER", script), patch.object(
                host, "worker_command", return_value=[sys.executable, "-I", "-"]
            ), self.assertRaisesRegex(ValueError, "output exceeded"):
                host.query_worker("ubuntu", "--list")

    def test_listed_account_timeout_is_not_treated_as_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text('{"users":{"ejc3":{}}}', encoding="utf-8")
            responses = [subprocess.CompletedProcess([], 0, b"ejc3 container systemd-nspawn\n"),
                         subprocess.TimeoutExpired("machinectl show", 10)]
            with patch.object(host, "CONFIG", config), patch.object(
                host.subprocess, "run", side_effect=responses
            ), self.assertRaises(subprocess.TimeoutExpired):
                host.managed_accounts()


if __name__ == "__main__":
    unittest.main(buffer=True)

#!/usr/bin/env python3
"""Offline acceptance tests for the host's account ordering and output boundary."""
import base64
import contextlib
import gzip
import hashlib
import importlib.util
import io
import os
from pathlib import Path
import re
import subprocess
import textwrap
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
ROOT = Path(__file__).resolve().parents[1]
TF = (ROOT / "nextjs-worktree-prune.tf").read_text()


def tf_block(kind, name):
    """One top-level block of the .tf file, through its closing brace at column 0."""
    match = re.search(rf'^{kind} "aws_ssm_association" "{name}" {{\n(.*?)^}}\n', TF, re.S | re.M)
    assert match, name
    return match[1]


def render_installer(job):
    """Render the installer heredoc as Terraform would for one job (gzip bytes aside)."""
    jobs = dict(re.findall(r'^    (\w+) += (\{.*\})$', TF, re.M))
    fields = dict(re.findall(r'(\w+) = "([^"]*)"', jobs[job]))
    text = TF.split("key => <<-SH\n", 1)[1].split("\n    SH\n", 1)[0] + "\n"
    text = textwrap.dedent(text)

    def script(name):
        return ROOT / "scripts" / name

    text = re.sub(r'\$\{base64gzip\(file\("\$\{path\.module\}/scripts/([^"]+)"\)\)\}',
                  lambda m: base64.b64encode(gzip.compress(script(m[1]).read_bytes())).decode(), text)
    text = re.sub(r'\$\{filesha256\("\$\{path\.module\}/scripts/([^"]+)"\)\}',
                  lambda m: hashlib.sha256(script(m[1]).read_bytes()).hexdigest(), text)
    text = re.sub(r"\$\{job\.(\w+)\}", lambda m: fields[m[1]], text)
    assert "${" not in text and "%{" not in text, "unrendered Terraform interpolation"
    return text


class HostCleanup(unittest.TestCase):
    def test_installed_calendar_checks_on_consecutive_hours_across_midnight(self):
        installer = (Path(__file__).resolve().parents[1] / "nextjs-worktree-prune.tf").read_text()
        calendar = next(line.strip().removeprefix("OnCalendar=") for line in installer.splitlines()
                        if line.strip().startswith("OnCalendar="))
        schedule = subprocess.check_output(
            ["systemd-analyze", "calendar", "--base-time=2026-11-22 22:31:00 UTC", "--iterations=3", calendar],
            text=True, env={**os.environ, "TZ": "UTC", "LC_ALL": "C"},
        )
        for when in ("2026-11-22 23:30:00", "2026-11-23 00:30:00", "2026-11-23 01:30:00"):
            self.assertIn(when, schedule)

    def test_each_installer_is_valid_shell_and_pins_the_script_pair(self):
        for job in ("nextjs", "fcvm"):
            text = render_installer(job)
            subprocess.run(["sh", "-n"], input=text, text=True, check=True)
            for name in ("nextjs-worktree-prune-host.py", "nextjs-worktree-prune.py"):
                digest = hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest()
                # Once for the install check, once in the entry wrapper's check.
                self.assertEqual(text.count(digest), 2, (job, name))
            # The installer touches only its own files and timer: no package
            # manager, and no restart of any other unit.
            self.assertIsNone(re.search(r"\b(apt|apt-get|dpkg|snap)\b", text), job)
            self.assertEqual(re.findall(r"systemctl (\S+)", text),
                             ["daemon-reload", "enable", "is-enabled", "is-active", "show"])

    def test_nextjs_job_is_unchanged_and_fcvm_job_is_host_only(self):
        nextjs, fcvm = render_installer("nextjs"), render_installer("fcvm")
        self.assertIn("ExecStart=/usr/local/sbin/nextjs-worktree-prune --apply\n", nextjs)
        self.assertIn("/run/nextjs-worktree-prune.lock", nextjs)
        self.assertNotIn("fcvm", nextjs)
        self.assertIn("ExecStart=/usr/local/sbin/fcvm-worktree-prune --apply --host-only\n", fcvm)
        for unit in ("/etc/systemd/system/fcvm-worktree-prune.service",
                     "/etc/systemd/system/fcvm-worktree-prune.timer", "/run/fcvm-worktree-prune.lock",
                     "Nice=15", "IOSchedulingClass=idle", "NoNewPrivileges=yes", "TimeoutStartSec=45min",
                     "systemctl enable --now fcvm-worktree-prune.timer"):
            self.assertIn(unit, fcvm)
        self.assertNotIn("/etc/systemd/system/nextjs-worktree-prune", fcvm)
        # The driver loads its worker from this one path on both hosts.
        self.assertIn(str(host.WORKER), fcvm)

    def test_fcvm_association_targets_only_fcvm_arm(self):
        block = tf_block("resource", "fcvm_arm_worktree_prune")
        self.assertIn("count = var.enable_firecracker_instance ? 1 : 0", block)
        self.assertEqual(re.findall(r"values += (.*)", block), ["[aws_instance.firecracker_dev[0].id]"])
        self.assertIn('commands         = local.worktree_prune_install["fcvm"]', block)
        nextjs = tf_block("resource", "nextjs_worktree_prune")
        self.assertEqual(re.findall(r"values += (.*)", nextjs), ["[local.nextjs_live_instance.id]"])
        self.assertIn('commands         = local.worktree_prune_install["nextjs"]', nextjs)

    def test_host_only_cleans_ubuntu_without_machinectl(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            host, "CONFIG", Path(temp) / "absent.json"
        ), patch.object(host.subprocess, "run", side_effect=AssertionError("machinectl called")), patch.object(
            host.subprocess, "Popen", side_effect=AssertionError("command run")
        ):
            self.assertEqual(host.managed_accounts(host_only=True), ["ubuntu"])
            # Only ubuntu is cleaned, so no host process scan is needed either.
            with patch.object(host, "PROC", Path(temp) / "no-proc"):
                self.assertEqual(host.host_busy_accounts(["ubuntu"]), set())
            seen = []
            with patch.object(sys, "argv", ["driver", "--apply", "--host-only"]), patch.object(
                host.os, "geteuid", return_value=0
            ), patch.object(host, "pressured", return_value=True), patch.object(
                host, "clean", side_effect=lambda accounts, *rest: seen.append((accounts, rest[2]))
            ):
                host.main()
            self.assertEqual(seen, [(["ubuntu"], True)])

    def test_host_only_refuses_a_container_host(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.write_text('{"users":{"ejc3":{}}}', encoding="utf-8")
            with patch.object(host, "CONFIG", config), patch.object(
                host.subprocess, "run", side_effect=AssertionError("machinectl called")
            ), self.assertRaisesRegex(ValueError, "host-only"):
                host.managed_accounts(host_only=True)

    def test_without_host_only_a_missing_config_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(
            host, "CONFIG", Path(temp) / "absent.json"
        ), patch.object(sys, "argv", ["driver", "--apply"]), patch.object(
            host.os, "geteuid", return_value=0
        ), patch.object(host, "pressured", return_value=True), patch.object(
            host, "clean", side_effect=AssertionError("cleaned without an account list")
        ), patch.object(host.subprocess, "run", side_effect=AssertionError("machinectl called")), \
                contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit) as stop:
            host.main()
        self.assertEqual(stop.exception.code, 1)
        self.assertIn("stopped (FileNotFoundError)", err.getvalue())

    def test_host_only_refuses_a_dangling_config_symlink(self):
        with tempfile.TemporaryDirectory() as temp:
            config = Path(temp) / "config.json"
            config.symlink_to(Path(temp) / "missing.json")
            with patch.object(host, "CONFIG", config), self.assertRaisesRegex(ValueError, "host-only"):
                host.managed_accounts(host_only=True)

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

#!/usr/bin/env python3
"""codex-restart (scripts/codex-restart.sh) against a fake /proc and a fake `codex`. Offline.

Pins: it reports running vs installed; it restarts only what is out of date; it refuses while
commands are running (Codex's own helper does not count); --force overrides; a restart really
replaces the daemon and records it; nothing is restarted unless --restart is given.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "codex-restart.sh"
ME = os.getuid()
TARGET = "x86_64-unknown-linux-musl"

FAKE_CODEX = """#!/bin/sh
# remote-control stop  -> the daemon's /proc entry disappears
# remote-control start -> a new daemon appears, running whatever `current` points at
P="$FAKE_PROC"
case "$2" in
  stop)  rm -rf "$P/$(cat "$FAKE_STATE/pid")" ;;
  start) n=$(( $(cat "$FAKE_STATE/pid") + 100 )); echo $n > "$FAKE_STATE/pid"
         mkdir -p "$P/$n"
         printf 'app-server\\0--remote-control\\0' > "$P/$n/cmdline"
         printf 'Uid:\\t%s\\n' "$FAKE_UID" > "$P/$n/status"
         ln -s "$(readlink -f "$FAKE_HOME/.codex/packages/standalone/current")/bin/codex" "$P/$n/exe" ;;
esac
"""


class Box:
    """A fake home with two installed releases and a daemon running the old one."""

    def __init__(self, running="0.154.0", installed="0.159.2"):
        self.tmp = tempfile.mkdtemp()
        t = Path(self.tmp)
        self.proc, self.home, self.state = t / "proc", t / "home", t / "state"
        for d in (self.proc, self.state, self.home / ".local" / "bin"):
            d.mkdir(parents=True)
        std = self.home / ".codex" / "packages" / "standalone"
        for v in (running, installed):
            (std / "releases" / f"{v}-{TARGET}" / "bin").mkdir(parents=True, exist_ok=True)
        (std / "current").symlink_to(std / "releases" / f"{installed}-{TARGET}")
        fake = self.home / ".local" / "bin" / "codex"
        fake.write_text(FAKE_CODEX)
        fake.chmod(0o755)
        (self.state / "pid").write_text("1000")
        self.daemon(1000, std / "releases" / f"{running}-{TARGET}" / "bin" / "codex")

    def daemon(self, pid, exe, cmdline=b"app-server\0--remote-control\0"):
        d = self.proc / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(cmdline)
        (d / "status").write_text(f"Uid:\t{ME}\nPPid:\t1\n")
        (d / "comm").write_text("codex\n")
        os.symlink(exe, d / "exe")

    def child(self, pid, parent, comm):
        d = self.proc / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(comm.encode() + b"\0")
        (d / "status").write_text(f"Uid:\t{ME}\nPPid:\t{parent}\n")
        (d / "comm").write_text(comm[:15] + "\n")

    def run(self, *args):
        env = {**os.environ, "CODEX_RESTART_PROC": str(self.proc), "CODEX_RESTART_HOME": str(self.home),
               "CODEX_RESTART_WAIT": "5", "CODEX_RESTART_SYSTEMCTL": "no-such-systemctl", "FAKE_PROC": str(self.proc), "FAKE_STATE": str(self.state),
               "FAKE_HOME": str(self.home), "FAKE_UID": str(ME), "XDG_STATE_HOME": str(Path(self.tmp) / "xdg"),
               "PATH": "/usr/bin:/bin"}
        return subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=60)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class CodexRestartTests(unittest.TestCase):
    def setUp(self):
        self.box = Box()
        self.addCleanup(self.box.cleanup)

    def test_status_reports_and_restarts_nothing(self):
        r = self.box.run()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("running 0.154.0, installed 0.159.2 -> RESTART NEEDED", r.stdout)
        self.assertIn("codex-restart --restart", r.stdout)
        self.assertTrue((self.box.proc / "1000").exists(), "status must not touch the daemon")

    def test_restart_replaces_the_daemon_and_records_it(self):
        r = self.box.run("--restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("daemon 1000 -> 1100, now running 0.159.2", r.stdout)
        self.assertFalse((self.box.proc / "1000").exists())
        rec = (Path(self.box.tmp) / "xdg" / "codex-restart" / "restart.txt").read_text()
        self.assertIn("0.154.0 -> 0.159.2", rec)
        self.assertIn("current", self.box.run().stdout)

    def test_a_current_daemon_is_left_alone_unless_all(self):
        box = Box(running="0.159.2", installed="0.159.2")
        self.addCleanup(box.cleanup)
        r = box.run("--restart")
        self.assertIn("-> current", r.stdout)
        self.assertNotIn("restarting", r.stdout)
        self.assertTrue((box.proc / "1000").exists())
        self.assertIn("restarting", box.run("--restart", "--all").stdout)

    def test_running_commands_block_a_restart_but_codexs_own_helper_does_not(self):
        self.box.child(2000, 1000, "codex-code-mode-host")   # comm is cut to 15 chars by the kernel
        self.assertIn("0 command(s) running", self.box.run().stdout)
        self.box.child(2001, 1000, "cargo")
        r = self.box.run("--restart")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("1 command(s) are running under this daemon", r.stderr)
        self.assertTrue((self.box.proc / "1000").exists(), "must not restart over running work")
        self.assertEqual(self.box.run("--restart", "--force").returncode, 0)
        self.assertFalse((self.box.proc / "1000").exists())

    def test_app_clients_are_counted_not_blocking(self):
        self.box.daemon(3000, "/bin/sh", cmdline=b"codex\0app-server\0proxy\0")
        r = self.box.run("--restart")
        self.assertIn("1 app client(s)", r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_no_daemon_is_not_an_error(self):
        shutil.rmtree(self.box.proc / "1000")
        r = self.box.run("--restart")
        self.assertEqual(r.returncode, 0)
        self.assertIn("no Codex remote-control daemon is running", r.stdout)

    def test_it_never_signals_anything_itself(self):
        text = SCRIPT.read_text()
        self.assertNotRegex(text, r"\bkill\s+-(9|KILL|TERM)|pkill|killall")
        self.assertIn("did not exit within", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

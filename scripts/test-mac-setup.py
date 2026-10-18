#!/usr/bin/env python3
"""The MacBook's agent tools (scripts/mac/bin), exercised on any POSIX box with perl and sh."""

from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parent.parent
BIN = ROOT / "scripts" / "mac" / "bin"


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="mac-setup-test-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.state = self.temp / "state"
        self.env = dict(os.environ, MAC_STATE_DIR=str(self.state), HOME=str(self.temp))
        self.env.pop("MAC_BUILD_SLOT", None)
        self.env.pop("MAC_LEASE", None)

    def run_(self, *args, env=None, timeout=20):
        return subprocess.run([str(a) for a in args], capture_output=True, text=True, timeout=timeout,
                              env=env or self.env)


class Shim(Base):
    """Which invocations take a build slot, and that everything else runs the real tool untouched."""

    def setUp(self):
        super().setUp()
        self.real = self.temp / "real"
        self.real.mkdir()
        for tool in ("swift", "xcodebuild", "xcrun"):
            p = self.real / tool
            p.write_text(f'#!/bin/sh\necho "real {tool} slot=${{MAC_BUILD_SLOT:-none}} $*"\n')
            p.chmod(0o755)
        self.shims = self.temp / "bin"
        self.shims.mkdir()
        for tool in ("swift", "xcodebuild", "xcrun"):
            (self.shims / tool).symlink_to(BIN / "mac-build-shim")
        self.env.update(MAC_REAL_BIN_DIR=str(self.real), MAC_BUILD=str(BIN / "mac-build"))

    def out(self, tool, *args):
        r = self.run_(self.shims / tool, *args)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def assert_capped(self, tool, *args):
        self.assertRegex(self.out(tool, *args), r"slot=[12]\b", f"{tool} {args} should take a slot")

    def assert_passed(self, tool, *args):
        self.assertIn("slot=none", self.out(tool, *args), f"{tool} {args} should run directly")

    def test_swift_builds_take_a_slot_other_subcommands_do_not(self):
        for sub in ("build", "test", "run"):
            self.assert_capped("swift", sub, "-c", "release")
        for args in (("--version",), ("package", "resolve"), ()):
            self.assert_passed("swift", *args)

    def test_xcodebuild_builds_unless_it_is_asked_for_information(self):
        self.assert_capped("xcodebuild", "-scheme", "App", "archive")
        self.assert_capped("xcodebuild")
        for flag in ("-version", "-list", "-showsdks", "-showBuildSettings", "-resolvePackageDependencies"):
            self.assert_passed("xcodebuild", "-scheme", "App", flag)

    def test_xcrun_forms_are_seen_through_its_own_options(self):
        self.assert_capped("xcrun", "swift", "build", "--build-tests")
        self.assert_capped("xcrun", "--sdk", "macosx", "swift", "test")
        self.assert_capped("xcrun", "xcodebuild", "-scheme", "App", "build")
        self.assertIn("real xcrun slot=1 --sdk macosx xcodebuild archive", self.out("xcrun", "--sdk", "macosx", "xcodebuild", "archive"),
                      "xcrun's own options are kept")
        for args in (("clang", "-v"), ("--find", "swift"), ("-f", "xcodebuild"), ("xcodebuild", "-version"),
                     ("swift", "package", "resolve"), ("--show-sdk-path",)):
            self.assert_passed("xcrun", *args)

    def test_a_nested_build_runs_in_its_parents_slot(self):
        env = dict(self.env, MAC_BUILD_SLOT="2")
        r = self.run_(self.shims / "swift", "build", env=env)
        self.assertIn("slot=2 build", r.stdout)

    def test_arguments_survive_verbatim(self):
        self.assertIn("real swift slot=1 build -Xswiftc -DX=a b", self.out("swift", "build", "-Xswiftc", "-DX=a b"))


class Build(Base):
    def test_at_most_two_at_once_and_the_third_waits(self):
        sleeper = self.temp / "sleeper"
        sleeper.write_text("#!/bin/sh\necho start $1 $(date +%s.%N); sleep 2; echo end $1\n")
        sleeper.chmod(0o755)
        procs = [subprocess.Popen([str(BIN / "mac-build"), str(sleeper), str(i)], stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, env=self.env) for i in range(3)]
        time.sleep(0.8)
        status = self.run_(BIN / "mac-build", "--status").stdout
        self.assertEqual(len(re.findall(r"^slot \d: pid \d+", status, re.M)), 2, status)
        results = [p.communicate(timeout=30) for p in procs]
        starts = sorted(float(o.split()[2]) for o, _ in results)
        self.assertLess(starts[1] - starts[0], 1.0)
        self.assertGreaterEqual(starts[2] - starts[0], 1.5, "the third waited for a slot")
        self.assertEqual(sum("waiting for one of 2 build slots" in e for _, e in results), 1)
        self.assertIn("slot 1: free", self.run_(BIN / "mac-build", "--status").stdout)

    def test_the_command_holds_the_slot_not_the_wrapper(self):
        # exec keeps the descriptor: while the build runs, the slot reads as taken by it.
        p = subprocess.Popen([str(BIN / "mac-build"), "sleep", "2"], env=self.env)
        time.sleep(0.5)
        self.assertRegex(self.run_(BIN / "mac-build", "--status").stdout, r"slot 1: pid %d " % p.pid)
        p.wait(timeout=10)

    def test_exit_status_is_the_commands(self):
        self.assertEqual(self.run_(BIN / "mac-build", "sh", "-c", "exit 7").returncode, 7)


class Lease(Base):
    def lease(self, *args, **kw):
        return self.run_(BIN / "mac-lease", *args, **kw)

    def test_take_check_release(self):
        self.assertEqual(self.lease("status").stdout, "free\n")
        self.assertEqual(self.lease("take", "astryx").returncode, 0)
        self.assertEqual(self.lease("check", "astryx").returncode, 0)
        other = self.lease("check", "asc-key")
        self.assertEqual(other.returncode, 75)
        self.assertIn("held by astryx", other.stderr)
        self.assertEqual(self.lease("take", "asc-key").returncode, 75)
        self.assertEqual(self.lease("release", "asc-key").returncode, 75, "only the holder releases")
        self.assertEqual(self.lease("release", "astryx").returncode, 0)
        self.assertEqual(self.lease("take", "asc-key").returncode, 0)

    def test_expiry_frees_it(self):
        self.lease("take", "short", "--ttl", "1")
        time.sleep(1.5)
        self.assertEqual(self.lease("status").stdout, "free\n")

    def test_run_holds_it_for_the_command_and_releases_after(self):
        r = self.lease("run", "job", "--", "sh", "-c", 'echo "$MAC_LEASE"; exit 3')
        self.assertEqual((r.returncode, r.stdout), (3, "job\n"))
        self.assertEqual(self.lease("status").stdout, "free\n")

    def test_a_dead_runs_lease_is_free(self):
        p = subprocess.Popen([str(BIN / "mac-lease"), "run", "crashy", "--", "sleep", "30"], env=self.env)
        time.sleep(0.5)
        self.assertIn("crashy", self.lease("status").stdout)
        p.kill(); p.wait()
        self.assertEqual(self.lease("status").stdout, "free\n")

    def test_wait_takes_it_once_released(self):
        self.lease("take", "first", "--ttl", "2")
        start = time.time()
        self.assertEqual(self.lease("take", "second", "--wait", "10").returncode, 0)
        self.assertGreater(time.time() - start, 1)

    def test_names_are_checked(self):
        self.assertEqual(self.lease("take", "bad name").returncode, 2)


class Peekaboo(Base):
    def setUp(self):
        super().setUp()
        cli = self.temp / "cli"
        cli.write_text('#!/bin/sh\necho "cli $*"\n')
        cli.chmod(0o755)
        self.env.update(PEEKABOO_CLI=str(cli), MAC_LEASE_BIN=str(BIN / "mac-lease"), MAC_LEASE_WAIT="2")

    def test_looking_never_waits_and_driving_waits_for_another_sessions_lease(self):
        self.run_(BIN / "mac-lease", "take", "someone")
        self.assertEqual(self.run_(BIN / "peekaboo", "see", "--mode", "screen").stdout, "cli see --mode screen\n")
        r = self.run_(BIN / "peekaboo", "click", "Allow")
        self.assertEqual(r.returncode, 75)
        self.assertIn("held by someone", r.stderr)
        mine = dict(self.env, MAC_LEASE="someone")
        self.assertEqual(self.run_(BIN / "peekaboo", "click", "Allow", env=mine).stdout, "cli click Allow\n")
        self.run_(BIN / "mac-lease", "release", "someone")
        self.assertEqual(self.run_(BIN / "peekaboo", "type", "x").stdout, "cli type x\n")


class Files(unittest.TestCase):
    def test_syntax(self):
        for f in ("mac-build-shim", "peekaboo"):
            subprocess.run(["sh", "-n", str(BIN / f)], check=True)
        for f in ("mac-lease", "mac-build"):
            subprocess.run(["perl", "-c", str(BIN / f)], check=True, capture_output=True)
        if shutil.which("zsh"):
            for f in (BIN / "mac-gui", ROOT / "scripts" / "mac" / "install.zsh"):
                subprocess.run(["zsh", "-n", str(f)], check=True)
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / "mac-setup.sh")], check=True)

    def test_the_setup_script_ships_every_tool(self):
        setup = (ROOT / "scripts" / "mac-setup.sh").read_text()
        install = (ROOT / "scripts" / "mac" / "install.zsh").read_text()
        for f in sorted(p.name for p in BIN.iterdir()):
            self.assertIn(f"bin/{f}", setup)
            self.assertIn(f, install)

    def test_mac_gui_keeps_secrets_off_the_command_line(self):
        gui = (BIN / "mac-gui").read_text()
        self.assertIn("export -p", gui)
        self.assertIn("umask 077", gui)
        self.assertIn("launchctl asuser", gui)


if __name__ == "__main__":
    unittest.main(verbosity=2)

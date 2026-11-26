#!/usr/bin/env python3
"""The MacBook's agent tools (scripts/mac/bin), exercised on any POSIX box with perl and sh."""

from pathlib import Path
import hashlib
import os
import plistlib
import re
import shlex
import shutil
import signal
import subprocess
import tarfile
import tempfile
import time
import unittest
import zipfile

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

    @staticmethod
    def stop_run_group(wrapper):
        try:
            os.killpg(wrapper.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        wrapper.wait(timeout=5)

    def start_leased_command(self, name, ttl="600"):
        pid_file = self.temp / f"{name}.pid"
        wrapper = subprocess.Popen([
            str(BIN / "mac-lease"), "run", name, "--ttl", ttl, "--", "sh", "-c",
            'printf "%s\\n" "$$" > "$1"; exec sleep 30', "leased", str(pid_file),
        ], env=self.env, start_new_session=True)
        self.addCleanup(self.stop_run_group, wrapper)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if pid_file.exists() and (pid := pid_file.read_text().strip()):
                return wrapper, int(pid)
            self.assertIsNone(wrapper.poll(), "the run exited before its command started")
            time.sleep(0.02)
        self.fail("the leased command did not start")

    def test_a_dead_command_releases_its_run_lease_and_preserves_signal_status(self):
        wrapper, pid = self.start_leased_command("crashy")
        self.assertIn(f"while pid {pid} runs", self.lease("status").stdout)
        os.kill(pid, signal.SIGTERM)
        self.assertEqual(wrapper.wait(timeout=5), 128 + signal.SIGTERM)
        self.assertEqual(self.lease("status").stdout, "free\n")

    def test_a_runs_lease_outlives_its_ttl_while_the_command_runs(self):
        # A screen tour can run past the old 600 s ttl without releasing the desktop.
        wrapper, pid = self.start_leased_command("long", ttl="1")
        time.sleep(1.2)
        self.assertIn(f"while pid {pid} runs", self.lease("status").stdout)
        self.assertEqual(self.lease("take", "other").returncode, 75)
        os.kill(pid, signal.SIGTERM)
        self.assertEqual(wrapper.wait(timeout=5), 128 + signal.SIGTERM)
        self.assertEqual(self.lease("status").stdout, "free\n")

    def test_a_commands_lease_survives_the_wrapper_being_killed(self):
        wrapper, pid = self.start_leased_command("orphan")
        wrapper.kill()
        wrapper.wait(timeout=5)
        os.kill(pid, 0)
        self.assertIn(f"while pid {pid} runs", self.lease("status").stdout)
        self.assertEqual(self.lease("run", "other", "--", "true").returncode, 75)

    def test_an_exited_command_frees_the_lease_even_before_its_wrapper_reaps_it(self):
        wrapper, pid = self.start_leased_command("unreaped")
        os.kill(wrapper.pid, signal.SIGSTOP)
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.lease("status").stdout == "free\n":
                break
            time.sleep(0.02)
        self.assertEqual(self.lease("take", "next").returncode, 0)

    def test_a_denied_run_never_starts_its_command(self):
        self.assertEqual(self.lease("take", "holder").returncode, 0)
        marker = self.temp / "ran"
        self.assertEqual(self.lease("run", "other", "--", "touch", marker).returncode, 75)
        self.assertFalse(marker.exists())

    def test_wait_takes_it_once_released(self):
        self.lease("take", "first", "--ttl", "2")
        start = time.time()
        self.assertEqual(self.lease("take", "second", "--wait", "10").returncode, 0)
        self.assertGreater(time.time() - start, 1)

    def test_names_are_checked(self):
        self.assertEqual(self.lease("take", "bad name").returncode, 2)

    def run_bg(self, name, *args):
        return subprocess.Popen([str(BIN / "mac-lease"), "run", name, *args], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_a_second_run_of_the_same_name_waits(self):
        # Two jobs under one name ran at once: the second run "renewed" the first's lease.
        first = self.run_bg("job", "--", "sleep", "3")
        time.sleep(0.6)
        r = self.lease("run", "job", "--", "true")
        self.assertEqual(r.returncode, 75, "a live run's lease is not taken again by name")
        self.assertIn("held by job", r.stderr)
        start = time.time()
        self.assertEqual(self.lease("run", "job", "--wait", "15", "--", "true").returncode, 0)
        self.assertGreater(time.time() - start, 1.5, "it waited for the first run")
        first.wait(timeout=10)

    def test_killing_a_second_run_leaves_the_first_ones_lease(self):
        first, child = self.start_leased_command("job")
        marker = self.temp / "second-ran"
        second = self.run_bg("job", "--wait", "30", "--", "touch", marker)
        time.sleep(0.6)
        second.kill()
        second.communicate(timeout=5)
        self.assertFalse(marker.exists(), "a killed waiter cannot let its command start")
        self.assertIn(f"while pid {child} runs", self.lease("status").stdout)
        r = self.lease("release", "job")
        self.assertEqual(r.returncode, 75, "a command's lease cannot be released by name")
        self.assertIn(f"while pid {child} runs", self.lease("status").stdout)
        os.kill(child, signal.SIGTERM)
        self.assertEqual(first.wait(timeout=5), 128 + signal.SIGTERM)
        self.assertEqual(self.lease("status").stdout, "free\n")

    def test_a_take_lease_is_renewed_and_released_by_name_and_a_run_under_it_leaves_it(self):
        self.assertEqual(self.lease("take", "mine", "--ttl", "2").returncode, 0)
        self.assertEqual(self.lease("take", "mine", "--ttl", "60").returncode, 0, "renewed across calls")
        time.sleep(2.5)
        self.assertIn("mine (", self.lease("status").stdout, "the renewal's ttl")
        record = self.state / "mac-lease" / "lease"
        original = record.read_bytes()
        self.assertEqual(self.lease("run", "mine", "--ttl", "1", "--", "true").returncode, 0, "a run under one's own take")
        self.assertEqual(record.read_bytes(), original, "the run did not shorten or extend the manual lease")
        self.assertIn("mine (", self.lease("status").stdout, "the run left the take's lease held")
        self.assertEqual(self.lease("release", "mine").returncode, 0)
        self.assertEqual(self.lease("status").stdout, "free\n")

    def test_a_recycled_pid_does_not_keep_a_previous_run_held(self):
        self.lease("status")
        now = int(time.time())
        (self.state / "mac-lease" / "lease").write_text(
            f"previous\t{now + 600}\t{os.getpid()}\t{now}\tMon Jan 1 00:00:00 2001\n"
        )
        self.assertEqual(self.lease("status").stdout, "free\n")
        self.assertEqual(self.lease("take", "next").returncode, 0)

    def waiter(self, name, wait="30"):
        return subprocess.Popen([str(BIN / "mac-lease"), "take", name, "--wait", wait, "--ttl", "60"], env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_waiters_are_served_first_come_first_served(self):
        # Polling, whichever waiter looked first after a release won: one that had waited an hour lost to one
        # that came a second ago. Now the oldest live ticket gets a free lease.
        for _ in range(3):
            self.lease("take", "holder")
            early = self.waiter("early")
            time.sleep(0.6)
            late = self.waiter("late")
            time.sleep(0.6)
            self.assertEqual(self.lease("release", "holder").returncode, 0)
            self.assertEqual(early.wait(timeout=10), 0)
            self.assertIn("early", self.lease("status").stdout)
            self.assertIsNone(late.poll(), "the later waiter still waits")
            self.lease("release", "early")
            self.assertEqual(late.wait(timeout=10), 0)
            self.lease("release", "late")

    def test_a_take_that_does_not_wait_gets_no_lease_while_others_are_queued(self):
        self.lease("take", "holder")
        queued = self.waiter("queued")
        time.sleep(0.6)
        r = self.lease("take", "jumper")
        self.assertEqual(r.returncode, 75)
        self.assertIn("1 queued before you", r.stderr)
        self.lease("release", "holder")
        self.assertEqual(queued.wait(timeout=10), 0)
        self.lease("release", "queued")
        self.assertEqual(self.lease("take", "jumper").returncode, 0, "free and nobody queued")

    def test_a_dead_waiters_ticket_does_not_hold_the_queue(self):
        self.lease("take", "holder")
        gone = self.waiter("gone")
        self.addCleanup(lambda: (gone.kill(), gone.communicate(timeout=5)))
        time.sleep(0.6)
        gone.kill()
        # Inspect without poll() or wait(): the waiting process has exited but its PID still exists.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = subprocess.check_output(["/bin/ps", "-p", str(gone.pid), "-o", "stat="], text=True)
            if state.strip().startswith("Z"):
                break
            time.sleep(0.02)
        self.assertTrue(state.strip().startswith("Z"), "the waiter was not reaped")
        self.lease("release", "holder")
        self.assertEqual(self.lease("take", "next").returncode, 0)

    def test_a_waiter_that_gives_up_leaves_the_queue(self):
        self.lease("take", "holder", "--ttl", "60")
        r = self.lease("take", "brief", "--wait", "1")
        self.assertEqual(r.returncode, 75)
        self.lease("release", "holder")
        self.assertEqual(self.lease("take", "next").returncode, 0)
        self.assertEqual(os.listdir(self.state / "mac-lease" / "queue"), [])

    def test_a_stopped_waiters_ticket_expires_at_its_deadline(self):
        self.assertEqual(self.lease("take", "holder").returncode, 0)
        parked = self.waiter("parked", "3")
        self.addCleanup(lambda: (parked.kill(), parked.communicate(timeout=5)))
        queue = self.state / "mac-lease" / "queue"
        until = time.monotonic() + 2
        tickets = []
        while not tickets and time.monotonic() < until:
            tickets = [p for p in queue.glob("*-parked") if p.stat().st_size]
            if not tickets:
                time.sleep(0.02)
        self.assertEqual(len(tickets), 1)
        deadline = int(tickets[0].read_text())
        os.kill(parked.pid, signal.SIGSTOP)
        self.assertEqual(self.lease("release", "holder").returncode, 0)
        time.sleep(max(0, deadline - time.time()) + 0.15)
        self.assertIsNone(parked.poll(), "the stopped waiter is still alive")
        self.assertEqual(self.lease("take", "next").returncode, 0)
        self.assertFalse(tickets[0].exists())


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


@unittest.skipUnless(shutil.which("zsh"), "needs zsh")
class ZshEnv(Base):
    """The managed ~/.zshenv block wins in a non-interactive zsh, whatever the file did before it."""

    def setUp(self):
        super().setUp()
        self.zshenv = self.temp / ".zshenv"
        self.local = self.temp / ".local" / "bin"
        self.local.mkdir(parents=True)
        self.system = self.temp / "usr-bin"
        self.system.mkdir()
        for d in (self.local, self.system):
            for tool in ("swift", "xcodebuild", "xcrun"):
                (d / tool).write_text("#!/bin/sh\n")
                (d / tool).chmod(0o755)

    def set_(self, *args):
        subprocess.run(["sh", str(ROOT / "scripts" / "mac" / "set-zshenv.sh"), str(self.zshenv), *args],
                       env=self.env, check=True, capture_output=True, text=True)

    def selected_developer(self, *zsh_args, **env):
        shell_env = {"HOME": str(self.temp), "ZDOTDIR": str(self.temp), "PATH": "/usr/bin:/bin", **env}
        return subprocess.check_output(["zsh", *zsh_args, "-c", 'print -r -- "${DEVELOPER_DIR-unset}"'],
                                       env=shell_env, text=True).rstrip("\n")

    def test_mini_defaults_to_the_probed_xcode_and_preserves_toolchain_overrides(self):
        contents = self.temp / "Applications/Xcode.app/Contents"
        developer = contents / "Developer"
        tool = developer / "usr/bin/xcodebuild"
        tool.parent.mkdir(parents=True)
        version = contents / "version.plist"
        version.write_text("27B52")
        tool.write_text('''#!/bin/sh
[ "$DEVELOPER_DIR" = "$HOME/Applications/Xcode.app/Contents/Developer" ] || exit 1
echo "$*" >> "$HOME/checks"
if [ "$*" = -checkFirstLaunchStatus ] && [ -f "$HOME/updating" ]; then
    echo "upgrade in progress" > "$DEVELOPER_DIR/../version.plist"
fi
case "$*" in
    "-license check") test -f "$HOME/licensed" ;;
    "-checkFirstLaunchStatus") test -f "$HOME/initialized" ;;
    *) exit 1 ;;
esac
''')
        tool.chmod(0o755)
        for marker in ("initialized", "licensed"):
            (self.temp / marker).touch()
        self.set_()
        self.assertEqual(self.selected_developer(), "unset", "the desktop profile keeps its own selection")
        self.assertFalse((self.temp / "checks").exists())
        self.set_("--build-only")
        for args in ((), ("-i",)):
            self.assertEqual(self.selected_developer(*args), str(developer))
        for override in ("/other/Xcode.app/Contents/Developer", ""):
            self.assertEqual(self.selected_developer(DEVELOPER_DIR=override), override)
        self.assertEqual((self.temp / "checks").read_text().splitlines(), ["-license check", "-checkFirstLaunchStatus"],
                         "no readiness probes run at shell startup")
        version.write_text("new build")
        self.assertEqual(self.selected_developer(), "unset")
        self.set_("--build-only")
        self.assertEqual(self.selected_developer(), str(developer))
        updating = self.temp / "updating"
        updating.touch()
        self.set_("--build-only")
        self.assertEqual(self.selected_developer(), "unset", "a build replaced during the probes was not validated")
        updating.unlink()
        for marker in ("initialized", "licensed"):
            (self.temp / marker).unlink()
            self.set_("--build-only")
            self.assertEqual(self.selected_developer(), "unset", f"fallback when {marker} is missing")
            (self.temp / marker).touch()
        self.set_("--build-only")
        tool.unlink()
        self.assertEqual(self.selected_developer(), "unset")
        self.set_("--build-only")
        self.assertEqual(self.selected_developer(), "unset")

    def resolved(self):
        # As an SSH command runs: non-interactive, non-login zsh, which reads only ~/.zshenv.
        env = {"HOME": str(self.temp), "PATH": f"{self.system}:/usr/bin:/bin", "ZDOTDIR": str(self.temp)}
        r = subprocess.run(["zsh", "-c", "command -v swift xcodebuild xcrun"], capture_output=True, text=True,
                           env=env, check=True)
        return r.stdout.split()

    def test_a_file_that_appends_local_bin_after_usr_bin_still_gets_the_shims_first(self):
        # The case a "does it mention .local/bin" check let through: appended, and mentioned in a comment.
        self.zshenv.write_text('# tools live in ~/.local/bin\nexport FOO=1\npath+=("$HOME/.local/bin")\n')
        self.assertEqual(self.resolved()[0], str(self.system / "swift"), "precondition: /usr/bin first")
        self.set_()
        self.assertEqual(self.resolved(), [str(self.local / t) for t in ("swift", "xcodebuild", "xcrun")])
        text = self.zshenv.read_text()
        self.assertTrue(text.startswith("# tools live in ~/.local/bin\nexport FOO=1\n"), "the rest is kept")
        self.assertTrue(text.rstrip("\n").endswith("# <<< mac-setup <<<"), "the block is last")

    def test_rerun_is_a_no_op_and_an_old_block_is_replaced_in_place_and_moved_last(self):
        self.zshenv.write_text("export A=1\n")
        self.set_()
        once = self.zshenv.read_text()
        self.set_()
        self.assertEqual(self.zshenv.read_text(), once)
        self.assertEqual(once.count("# >>> mac-setup >>>"), 1)
        # A stale block in the middle, with user lines after it that push /usr/bin back in front.
        stale = once.replace("typeset -U path", "# old body\ntypeset -U path")
        self.zshenv.write_text(stale + 'path=("%s" $path)\n' % self.system)
        self.set_()
        text = self.zshenv.read_text()
        self.assertEqual(text.count("# >>> mac-setup >>>"), 1)
        self.assertNotIn("# old body", text)
        self.assertTrue(text.rstrip("\n").endswith("# <<< mac-setup <<<"))
        self.assertEqual(self.resolved()[0], str(self.local / "swift"))

    def test_a_missing_file_is_created(self):
        self.set_()
        self.assertEqual(self.resolved()[0], str(self.local / "swift"))


class PeekabooInstall(unittest.TestCase):
    """scripts/mac/peekaboo-install.sh with the macOS tools stubbed: pinned, verified, idempotent, never swaps a
    running app unless asked, and cli-current follows the installed app."""

    SCRIPT = ROOT / "scripts" / "mac" / "peekaboo-install.sh"

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="peekaboo-install-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.home = self.temp / "home"
        self.home.mkdir()
        self.stubs = self.temp / "stubs"
        self.stubs.mkdir()
        self.releases = self.temp / "releases"
        self.releases.mkdir()
        self.log = self.temp / "calls.log"
        self.running = self.temp / "running"
        self.team = self.temp / "team"
        self.team.write_text("FWJYW4S8P8\n")
        text = self.SCRIPT.read_text()
        self.version = re.search(r"^VERSION=(\S+)$", text, re.M).group(1)
        # The release payloads, built here: the app zip and the CLI tarball, laid out as the real ones are.
        app = self.temp / "build" / "Peekaboo.app" / "Contents"
        (app / "MacOS").mkdir(parents=True)
        (app / "MacOS" / "Peekaboo").write_text("app\n")
        (app / "Info.plist").write_text(self.version + "\n")
        zip_path = self.releases / f"Peekaboo-{self.version}.app.zip"
        with zipfile.ZipFile(zip_path, "w") as z:
            for p in sorted((self.temp / "build").rglob("*")):
                z.write(p, p.relative_to(self.temp / "build"))
        cli = self.temp / "cli" / "peekaboo-macos-arm64"
        cli.mkdir(parents=True)
        (cli / "peekaboo").write_text("#!/bin/sh\necho cli\n")
        (cli / "peekaboo").chmod(0o755)
        (cli / "VERSION").write_text(self.version + "\n")
        tgz = self.releases / "peekaboo-macos-arm64.tar.gz"
        subprocess.run(["tar", "-czf", str(tgz), "-C", str(self.temp / "cli"), "peekaboo-macos-arm64"], check=True)
        # The script, with its pins pointed at those payloads (the real pins are checked in Files below).
        sums = {n: hashlib.sha256((self.releases / n).read_bytes()).hexdigest() for n in (zip_path.name, tgz.name)}
        text = re.sub(r"^APP_ZIP_SHA256=\S+$", "APP_ZIP_SHA256=" + sums[zip_path.name], text, flags=re.M)
        text = re.sub(r"^CLI_TGZ_SHA256=\S+$", "CLI_TGZ_SHA256=" + sums[tgz.name], text, flags=re.M)
        self.script = self.temp / "peekaboo-install.sh"
        self.script.write_text(text)
        log = shlex.quote(str(self.log))
        self.stub("curl", f'echo "curl $*" >> {log}\nurl=""; out=""\nwhile [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift 2;; -*) shift;; *) url=$1; shift;; esac; done\n'
                          f'cp "{self.releases}/$(basename "$url")" "$out"')
        # --verify fails for any path listed in self.broken (a damaged install), passes otherwise.
        self.broken = self.temp / "broken"
        self.stub("codesign", f'case "$1" in -dv) echo "TeamIdentifier=$(cat {shlex.quote(str(self.team))})" >&2;; '
                              f'*) for a; do :; done; grep -qxF "$a" {shlex.quote(str(self.broken))} 2>/dev/null && exit 1; exit 0;; esac')
        self.stub("defaults", '[ -f "$2.plist" ] && head -1 "$2.plist"')
        self.stub("ditto", 'python3 -c "import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$3" "$4"')
        self.stub("pgrep", f'[ -e {shlex.quote(str(self.running))} ]')
        self.stub("pkill", f'echo "pkill $*" >> {log}; rm -f {shlex.quote(str(self.running))}')
        self.stub("launchctl", f'echo "launchctl $*" >> {log}')
        self.stub("open", f'echo "open $*" >> {log}')
        self.support = self.home / "Library" / "Application Support" / "Peekaboo"
        self.app = self.home / "Applications" / "Peekaboo.app"
        self.plist = self.home / "Library" / "LaunchAgents" / "com.ccgames.peekaboo.plist"

    def stub(self, name, body):
        p = self.stubs / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)

    def run_(self, *args):
        env = {"HOME": str(self.home), "PATH": f"{self.stubs}:/usr/bin:/bin"}
        return subprocess.run(["sh", str(self.script), *args], capture_output=True, text=True, timeout=30, env=env)

    def calls(self):
        return self.log.read_text() if self.log.exists() else ""

    def files(self):
        return {str(p.relative_to(self.home)): (p.readlink() if p.is_symlink() else p.read_bytes() if p.is_file() else None)
                for p in sorted(self.home.rglob("*"))}

    def owners_mac(self):
        """The owner's Mac before this: app 4.4.0 running, its CLI, the hand-made LaunchAgent."""
        (self.app / "Contents").mkdir(parents=True)
        (self.app / "Contents" / "Info.plist").write_text("4.4.0\n")
        (self.support / "cli-4.4.0").mkdir(parents=True)
        (self.support / "cli-4.4.0" / "peekaboo").write_text("#!/bin/sh\n")
        (self.support / "cli-4.4.0" / "peekaboo").chmod(0o755)
        old = self.home / "Library" / "LaunchAgents" / "com.ejcampbell.peekaboo.plist"
        old.parent.mkdir(parents=True)
        old.write_text("hand-made\n")
        self.running.write_text("")

    def test_a_fresh_mac_gets_the_pinned_app_cli_and_agent_and_a_rerun_changes_nothing(self):
        r = self.run_()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.app / "Contents" / "Info.plist").read_text().strip(), self.version)
        self.assertTrue((self.support / f"cli-{self.version}" / "peekaboo").is_file())
        self.assertEqual(os.readlink(self.support / "cli-current"), f"cli-{self.version}")
        plist = plistlib.loads(self.plist.read_bytes())
        self.assertEqual(plist["ProgramArguments"], ["/usr/bin/open", "-g", "-a", str(self.app)])
        self.assertEqual((plist["Label"], plist["RunAtLoad"], plist["LimitLoadToSessionType"]), ("com.ccgames.peekaboo", True, "Aqua"))
        self.assertIn("launchctl bootstrap gui/", self.calls())
        self.assertEqual(self.calls().count("curl "), 2)
        before, calls = self.files(), self.calls()
        r = self.run_()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.files(), before, "a rerun changes nothing")
        self.assertEqual(self.calls().count("curl "), 2, "and downloads nothing")
        self.assertNotIn("bootstrap", self.calls()[len(calls):].replace("launchctl print", ""), "nor reloads the agent")

    def test_an_installed_app_or_cli_whose_signature_no_longer_verifies_is_reinstalled(self):
        self.assertEqual(self.run_().returncode, 0)
        self.broken.write_text(f"{self.app}\n{self.support / f'cli-{self.version}' / 'peekaboo'}\n")
        r = self.run_()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"installed CLI {self.version}", r.stdout)
        self.assertIn(f"installed app {self.version}", r.stdout)
        self.assertEqual(self.calls().count("curl "), 4, "both downloaded again")

    def test_a_running_older_app_is_left_alone_and_the_wrapper_keeps_its_matching_cli(self):
        self.owners_mac()
        r = self.run_()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("is running; it stays", r.stderr)
        self.assertEqual((self.app / "Contents" / "Info.plist").read_text(), "4.4.0\n")
        self.assertNotIn("pkill", self.calls())
        self.assertTrue((self.support / f"cli-{self.version}" / "peekaboo").is_file(), "the new CLI is ready")
        self.assertEqual(os.readlink(self.support / "cli-current"), "cli-4.4.0", "but the wrapper keeps the app's own")
        self.assertFalse((self.home / "Library" / "LaunchAgents" / "com.ejcampbell.peekaboo.plist").exists())
        self.assertIn("launchctl bootout gui/", self.calls())
        self.assertTrue(self.plist.exists())

    def test_update_running_quits_swaps_and_reopens(self):
        self.owners_mac()
        r = self.run_("--update-running")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("pkill -f ^" + str(self.app) + "/Contents/MacOS/Peekaboo", self.calls())
        self.assertEqual((self.app / "Contents" / "Info.plist").read_text().strip(), self.version)
        backup = self.home / ".local" / "state" / "mac-setup" / "backups" / "Peekaboo.app" / "Contents" / "Info.plist"
        self.assertEqual(backup.read_text(), "4.4.0\n")
        self.assertEqual(os.readlink(self.support / "cli-current"), f"cli-{self.version}")
        self.assertIn("launchctl kickstart gui/", self.calls())

    def test_an_older_app_quit_before_the_update_is_started_after_it(self):
        self.owners_mac()
        self.running.unlink()  # the owner quit Peekaboo first, as the update path says
        r = self.run_()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.app / "Contents" / "Info.plist").read_text().strip(), self.version)
        self.assertNotIn("pkill", self.calls(), "nothing was running to quit")
        self.assertIn("launchctl kickstart gui/", self.calls(), "the new app is started")

    def test_a_download_that_does_not_match_its_pin_installs_nothing(self):
        with open(self.releases / "peekaboo-macos-arm64.tar.gz", "ab") as f:
            f.write(b"tampered")
        r = self.run_()
        self.assertEqual(r.returncode, 1)
        self.assertIn("did not match its pinned sha256", r.stderr)
        self.assertFalse(self.app.exists())
        self.assertEqual(list(self.support.iterdir()), [])

    def test_a_downloaded_cli_whose_signature_does_not_verify_is_refused(self):
        self.stub("codesign", f'case "$1" in -dv) echo "TeamIdentifier=$(cat {shlex.quote(str(self.team))})" >&2;; '
                              f'*) for a; do :; done; case "$a" in */peekaboo-macos-arm64/peekaboo) exit 1;; esac; exit 0;; esac')
        r = self.run_()
        self.assertEqual(r.returncode, 1)
        self.assertIn("the CLI's signature does not verify", r.stderr)
        self.assertFalse((self.support / f"cli-{self.version}").exists())

    def test_a_launchagent_that_cannot_load_fails_the_run(self):
        self.stub("launchctl", f'echo "launchctl $*" >> {shlex.quote(str(self.log))}; [ "$1" != bootstrap ] || exit 5')
        r = self.run_()
        self.assertEqual(r.returncode, 1)
        self.assertIn("could not load the LaunchAgent com.ccgames.peekaboo", r.stderr)

    def test_another_signer_is_refused(self):
        self.team.write_text("ABCDEFGHIJ\n")
        r = self.run_()
        self.assertEqual(r.returncode, 1)
        self.assertIn("not signed by FWJYW4S8P8", r.stderr)
        self.assertFalse(self.app.exists())


class ScratchCleanerInstall(Base):
    def setUp(self):
        super().setUp()
        self.stage = self.temp / "source"
        (self.stage / "bin").mkdir(parents=True)
        self.build_helper = self.stage / "bin/mac-build"
        self.build_helper.write_bytes((BIN / "mac-build").read_bytes())
        self.payload = self.stage / "bin/mac-artifact-clean"
        self.payload.write_text("import os\ndef volume_id(path): return os.environ['SCRATCH_UUID']\n")
        self.volume = self.temp.resolve() / "Scratch"
        self.volume.mkdir()
        self.volume_id = "173177ab-a223-40c5-a955-21506cc53942"
        self.domain = "gui/" + str(os.getuid())
        self.label = "com.ccgames.mac-artifact-clean"
        self.home = self.temp / "Mac user & work"
        self.plist = self.home / "Library" / "LaunchAgents" / (self.label + ".plist")
        self.pin = self.home / ".config/mac-setup/scratch-volume-uuid"
        self.launches = self.temp / "launches"
        launchctl = self.temp / "launchctl"
        launchctl.write_text(
            '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CALLS"\n'
            'case "$1:$2" in\n'
            '  print:"$DOMAIN"/*) test -e "$LOADED";;\n'
            '  bootout:"$DOMAIN"/*) test -z "${FAIL_BOOTOUT:-}" && rm "$LOADED";;\n'
            '  *) exit 87;;\nesac\n'
        )
        launchctl.chmod(0o755)
        self.env.update(HOME=str(self.home), PATH=str(self.temp) + os.pathsep + self.env["PATH"],
                        CALLS=str(self.launches), DOMAIN=self.domain, LOADED=str(self.temp / "loaded"),
                        SCRATCH_UUID=self.volume_id, MOUNTED="1")

    def install(self):
        driver = """
import os, runpy, sys
from pathlib import Path
from unittest.mock import patch
with patch.object(Path, "is_mount", return_value=os.environ["MOUNTED"] == "1"):
    runpy.run_path(sys.argv[1])["main"](Path(sys.argv[2]), Path(sys.argv[3]))
"""
        return self.run_("python3", "-c", driver, ROOT / "scripts/mac/install-artifact-cleaner.py",
                         self.stage, self.volume)

    def test_updates_the_remote_login_cleaner_and_build_gate_without_a_gui_job(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        cleaner = self.home / ".local/bin/mac-artifact-clean"
        self.assertFalse(self.plist.exists())
        self.assertEqual(cleaner.read_bytes(), self.payload.read_bytes())
        self.assertTrue(os.access(cleaner, os.X_OK))
        self.payload.write_text(self.payload.read_text() + "# new version\n")
        installed_build = self.home / ".local/bin/mac-build"
        with installed_build.open("rb") as previous:
            self.build_helper.write_bytes(self.build_helper.read_bytes() + b"# updated build gate\n")
            result = self.install()
            self.assertNotEqual(os.fstat(previous.fileno()).st_ino, installed_build.stat().st_ino)
        self.assertEqual(installed_build.read_bytes(), self.build_helper.read_bytes())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(cleaner.read_bytes(), self.payload.read_bytes())
        self.assertFalse(self.launches.exists())

    def test_missing_or_symlinked_mount_never_enrolls_or_creates_a_login_agent(self):
        self.env["MOUNTED"] = "0"
        self.assertIn("held", self.install().stdout)
        self.assertFalse(self.pin.exists())
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.launches.exists())
        self.env["MOUNTED"] = "1"
        original = self.volume
        self.volume = self.temp / "fake-mount"
        self.volume.symlink_to(original, target_is_directory=True)
        self.assertIn("held", self.install().stdout)
        self.assertFalse(self.pin.exists())
        self.volume = original
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.pin.read_text(), self.volume_id + "\n")
        self.assertFalse(self.plist.exists())

    def test_existing_pin_accepts_case_and_never_changes_on_replacement(self):
        self.pin.parent.mkdir(parents=True)
        original = self.volume_id.upper() + "\n"
        self.pin.write_text(original)
        self.env["SCRATCH_UUID"] = "3b4a366f-2741-4273-bf8a-b457cf96aa59"
        self.assertIn("does not match", self.install().stdout)
        self.assertEqual(self.pin.read_text(), original)
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.launches.exists())
        self.env["SCRATCH_UUID"] = self.volume_id
        self.assertEqual(self.install().returncode, 0)
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.pin.read_text(), original)

    def test_upgrade_retires_only_the_legacy_job_even_with_the_drive_disconnected(self):
        self.plist.parent.mkdir(parents=True)
        self.plist.write_text("legacy job")
        other = self.plist.with_name("other.plist")
        other.touch()
        Path(self.env["LOADED"]).touch()
        self.env["MOUNTED"] = "0"
        self.env["FAIL_BOOTOUT"] = "1"
        self.assertNotEqual(self.install().returncode, 0)
        self.assertTrue(self.plist.exists())
        del self.env["FAIL_BOOTOUT"]
        self.assertEqual(self.install().returncode, 0)
        self.assertFalse(self.plist.exists())
        self.assertTrue(other.exists())
        self.assertEqual(self.launches.read_text().splitlines()[-1], f"bootout {self.domain}/{self.label}")


class Files(unittest.TestCase):
    def test_syntax(self):
        for f in (BIN / "mac-build-shim", BIN / "peekaboo", ROOT / "scripts" / "mac" / "set-zshenv.sh",
                  ROOT / "scripts" / "mac" / "peekaboo-install.sh"):
            subprocess.run(["sh", "-n", str(f)], check=True)
        for f in ("mac-lease", "mac-build"):
            subprocess.run(["perl", "-c", str(BIN / f)], check=True, capture_output=True)
        if shutil.which("zsh"):
            for f in (BIN / "mac-gui", ROOT / "scripts" / "mac" / "install.zsh"):
                subprocess.run(["zsh", "-n", str(f)], check=True)
        subprocess.run(["bash", "-n", str(ROOT / "scripts" / "mac-setup.sh")], check=True)

    def test_the_setup_script_ships_every_tool(self):
        setup = (ROOT / "scripts" / "mac-setup.sh").read_text()
        install = (ROOT / "scripts" / "mac" / "install.zsh").read_text()
        for f in sorted(p.name for p in BIN.iterdir() if p.is_file()):
            self.assertIn(f"bin/{f}", setup)
            self.assertIn(f, install)
        self.assertIn('/bin/sh "$src/peekaboo-install.sh" --update-running', install)
        self.assertIn('/bin/sh "$src/peekaboo-install.sh"\n', install)
        self.assertIn('"$src/set-zshenv.sh" "$HOME/.zshenv"', install)
        self.assertNotIn("grep -q '\\.local/bin'", install, "no mention-based check decides the PATH")

    def test_setup_selects_the_tools_for_each_host(self):
        setup = ROOT / "scripts" / "mac-setup.sh"
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            ssh = dest / "ssh"
            ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$OUT/args"\ncat > "$OUT/files.tar"\n')
            ssh.chmod(0o755)
            env = dict(os.environ, PATH=tmp + os.pathsep + os.environ["PATH"], OUT=tmp)
            build = {"install.zsh", "set-zshenv.sh", "bin/mac-build", "bin/mac-build-shim"}
            desktop = {"peekaboo-install.sh", "bin/mac-gui", "bin/mac-lease", "bin/peekaboo", "bin/mac-agents-md",
                       "mac-AGENTS.md"}
            member = desktop - {"mac-AGENTS.md"} | {"member-mac-AGENTS.md"}
            cleaner = {"install-artifact-cleaner.py", "bin/mac-artifact-clean"}
            mini = {"ios-toolchain.zsh", "mini-AGENTS.md", "bin/mac-agents-md"}
            for args, names, flag in [
                (["mini"], build | mini, "--build-only"),
                ([], build | desktop | cleaner, ""),
                (["mac", "--artifact-cleaner-only"], cleaner | {"bin/mac-build"}, ""),
                (["colton-mac", "--update-peekaboo"], build | member, "--update-peekaboo"),
                (["colin-mac"], build | member, ""),
            ]:
                subprocess.run([str(setup), *args], env=env, check=True)
                with tarfile.open(dest / "files.tar") as archive:
                    self.assertEqual(set(archive.getnames()), names)
                remote = (dest / "args").read_text().splitlines()
                self.assertEqual(remote[-2], args[0] if args else "mac")
                self.assertIn(f'"$d" {flag};', remote[-1])
                if "--artifact-cleaner-only" in args:
                    self.assertIn('/usr/bin/python3 "$d/install-artifact-cleaner.py"', remote[-1])
                    self.assertNotIn("install.zsh", remote[-1])
            (dest / "args").unlink()
            for args in (["mini", "--update-peekaboo"], ["mini", "--artifact-cleaner-only"],
                         ["colton-mac", "--artifact-cleaner-only"],
                         ["mac", "--update-peekaboo", "--artifact-cleaner-only"]):
                result = subprocess.run([str(setup), *args], env=env, capture_output=True)
                self.assertEqual(result.returncode, 2)
                self.assertFalse((dest / "args").exists())

    def test_peekaboo_is_pinned_to_the_release_and_the_wrapper_follows_cli_current(self):
        text = (ROOT / "scripts" / "mac" / "peekaboo-install.sh").read_text()
        def pin(name):
            return re.search(rf"^{name}=(\S+)", text, re.M).group(1)
        # v4.9.5 of openclaw/Peekaboo: its checksums.txt and SIGNING-MANIFEST.json.
        self.assertEqual(pin("VERSION"), "4.9.5")
        self.assertEqual(pin("TEAM"), "FWJYW4S8P8")
        self.assertEqual(pin("APP_ZIP_SHA256"), "d176301b0ef4b695c11cbdb8c0bdbbc71f47f866f76c734ddad8343ab5f93e38")
        self.assertEqual(pin("CLI_TGZ_SHA256"), "2f435c736459a3d30279835038fc7e67028875be837f467c87521ea14da3a9ee")
        self.assertIn("BASE=https://github.com/openclaw/Peekaboo/releases/download/v$VERSION", text)
        wrapper = (BIN / "peekaboo").read_text()
        self.assertIn('Peekaboo/cli-current/peekaboo"', wrapper)
        self.assertNotRegex(wrapper, r"cli-\d")

    def test_mac_gui_keeps_secrets_off_the_command_line(self):
        gui = (BIN / "mac-gui").read_text()
        self.assertIn("export -p", gui)
        self.assertIn("umask 077", gui)
        self.assertIn("launchctl asuser", gui)



@unittest.skipUnless(shutil.which("zsh"), "needs zsh")
class AgentsMd(Base):
    """The mini's ~/AGENTS.md: the template, then what is on the machine (devices, signing keychains)."""

    def setUp(self):
        super().setUp()
        self.template = self.temp / "mini-AGENTS.md"
        self.template.write_text("# This Mac mini\n\nRules from the template.\n")
        self.out = self.temp / "AGENTS.md"
        fake = self.temp / "devicectl"
        fake.write_text("#!/bin/sh\n"
                        "echo 'Name   Identifier   State   Model   Reality'\n"
                        "echo 'A phone   ID-1 (UDID)   connected   iPhone (iPhone15,3)   physical'\n"
                        "echo 'A sim   ID-2 (UDID)   shutdown   iPhone 17   simulated'\n")
        fake.chmod(0o755)
        self.env.update(MAC_AGENTS_TEMPLATE=str(self.template), MAC_AGENTS_OUT=str(self.out),
                        MAC_AGENTS_DEVICECTL=str(fake), MAC_AGENTS_HOST="mini")

    def write(self):
        return self.run_("zsh", BIN / "mac-agents-md")

    def test_keeps_the_template_and_lists_physical_devices_and_signing_keychains(self):
        kc = self.temp / "agent-work" / "signing"
        kc.mkdir(parents=True)
        (kc / "dev.keychain-db").write_text("")
        (kc / "dev.keychain.pw").write_text("")
        (kc / "lone.keychain-db").write_text("")  # no password file: not usable, not listed
        r = self.write()
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.out.read_text()
        self.assertTrue(text.startswith("# This Mac mini\n\nRules from the template.\n"))
        self.assertIn("A phone", text)
        self.assertNotIn("A sim", text)
        self.assertIn("~/agent-work/signing/dev.keychain-db", text)
        self.assertNotIn("lone.keychain-db", text)
        self.assertIn("Not installed", text)  # no ~/agent-tools/env.zsh in this home
        self.assertEqual(os.readlink(self.temp / "CLAUDE.md"), str(self.out))

    def test_says_so_when_nothing_is_attached_or_signable(self):
        fake = self.temp / "none"
        fake.write_text("#!/bin/sh\necho 'Name   Identifier   State'\n")
        fake.chmod(0o755)
        self.env["MAC_AGENTS_DEVICECTL"] = str(fake)
        self.assertEqual(self.write().returncode, 0)
        text = self.out.read_text()
        self.assertIn("None physical right now.", text)
        self.assertIn("None: device builds cannot be signed here yet.", text)

    def test_on_a_desktop_mac_lists_the_devices_and_no_mini_sections(self):
        self.env["MAC_AGENTS_HOST"] = "mac"
        kc = self.temp / "agent-work" / "signing"
        kc.mkdir(parents=True)
        (kc / "dev.keychain-db").write_text("")
        (kc / "dev.keychain.pw").write_text("")
        self.assertEqual(self.write().returncode, 0)
        text = self.out.read_text()
        self.assertIn("A phone", text)
        self.assertNotIn("iOS toolchain", text)
        self.assertNotIn("signing keychains", text)

    def test_a_members_mac_says_whether_it_is_on_the_pool(self):
        self.env["MAC_AGENTS_HOST"] = "member"
        bindir = self.temp / "fakebin"
        bindir.mkdir()
        launchctl = bindir / "launchctl"
        launchctl.write_text("#!/bin/sh\nexit \"${LAUNCHCTL_RC:-1}\"\n")
        launchctl.chmod(0o755)
        self.env["PATH"] = f"{bindir}:{self.env['PATH']}"
        self.assertEqual(self.write().returncode, 0)
        self.assertIn("Not on the pool", self.out.read_text())
        (self.temp / ".claude").mkdir()
        (self.temp / ".claude" / "settings.json").write_text('{"env": {"HTTPS_PROXY": "http://127.0.0.1:8445"}}')
        self.env["LAUNCHCTL_RC"] = "0"
        self.assertEqual(self.write().returncode, 0)
        text = self.out.read_text()
        self.assertIn("## Found: Claude and the shared pool", text)
        self.assertIn("On the pool:", text)
        self.env["MAC_AGENTS_HOST"] = "mini"
        self.write()
        self.assertNotIn("shared pool", self.out.read_text(), "only a member's Mac gets the pool line")

    def test_refuses_without_its_template_and_leaves_no_file(self):
        self.env["MAC_AGENTS_TEMPLATE"] = str(self.temp / "missing.md")
        r = self.write()
        self.assertEqual(r.returncode, 1)
        self.assertFalse(self.out.exists())

    def test_leaves_a_claude_md_that_is_not_its_link(self):
        own = self.temp / "CLAUDE.md"
        own.write_text("mine\n")
        self.assertEqual(self.write().returncode, 0)
        self.assertEqual(own.read_text(), "mine\n")


class IosToolchain(unittest.TestCase):
    """The mini's host toolchain: every download pinned to a SHA-256, and a mismatch refused."""

    script = (ROOT / "scripts" / "mac" / "ios-toolchain.zsh").read_text()

    def test_pins_each_download(self):
        for name in ("node", "xcodegen", "micromamba"):
            m = re.search(r"^%s_sha256=([0-9a-f]+)$" % name, self.script, re.M)
            self.assertIsNotNone(m, name)
            self.assertEqual(len(m.group(1)), 64, name)
        for name in ("node_version", "pnpm_version", "xcodegen_version", "micromamba_version", "cocoapods_version"):
            self.assertRegex(self.script, r"(?m)^%s=[0-9][0-9.\-]*$" % name)
        self.assertEqual(self.script.count('fetch "https://'), 3)

    def test_refuses_a_download_whose_hash_is_not_the_pinned_one(self):
        fetch = re.search(r"(?ms)^fetch\(\) \{.*?^\}", self.script).group(0)
        self.assertIn('[ "$got" = "$3" ] || {', fetch)
        self.assertIn("exit 1", fetch)

    def test_the_mini_setup_ships_and_runs_it(self):
        setup = (ROOT / "scripts" / "mac-setup.sh").read_text()
        self.assertIn("files+=(ios-toolchain.zsh mini-AGENTS.md bin/mac-agents-md)", setup)
        install = (ROOT / "scripts" / "mac" / "install.zsh").read_text()
        self.assertIn('/bin/zsh "$src/ios-toolchain.zsh"', install)
        self.assertIn('"$bin/mac-agents-md"', install)
        self.assertIn('agents_md mini "$src/mini-AGENTS.md"', install)
        self.assertIn('agents_md mac "$src/mac-AGENTS.md"', install)
        self.assertIn('agents_md member "$src/member-mac-AGENTS.md"', install)

    def test_a_family_members_mac_is_not_told_it_is_the_owners(self):
        member = (ROOT / "scripts" / "mac" / "member-mac-AGENTS.md").read_text()
        self.assertNotIn("owner's own desktop Mac", member)
        for needle in ("environment **Local**", "/login", "--foreground", "peekaboo see --no-elements"):
            self.assertIn(needle, member)

if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

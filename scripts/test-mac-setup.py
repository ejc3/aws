#!/usr/bin/env python3
"""The MacBook's agent tools (scripts/mac/bin), exercised on any POSIX box with perl and sh."""

from pathlib import Path
import hashlib
import os
import plistlib
import re
import shlex
import shutil
import subprocess
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

    def set_(self):
        subprocess.run(["sh", str(ROOT / "scripts" / "mac" / "set-zshenv.sh"), str(self.zshenv)], check=True)

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
        self.stub("shasum", 'sha256sum "$3"')
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
        for f in sorted(p.name for p in BIN.iterdir()):
            self.assertIn(f"bin/{f}", setup)
            self.assertIn(f, install)
        self.assertIn("set-zshenv.sh peekaboo-install.sh bin |", setup, "the helpers travel with the tools")
        self.assertIn('/bin/sh "$src/peekaboo-install.sh" --update-running', install)
        self.assertIn('/bin/sh "$src/peekaboo-install.sh"\n', install)
        self.assertIn('"$src/set-zshenv.sh" "$HOME/.zshenv"', install)
        self.assertNotIn("grep -q '\\.local/bin'", install, "no mention-based check decides the PATH")

    def test_peekaboo_is_pinned_to_the_release_and_the_wrapper_follows_cli_current(self):
        text = (ROOT / "scripts" / "mac" / "peekaboo-install.sh").read_text()
        pin = lambda name: re.search(rf"^{name}=(\S+)", text, re.M).group(1)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

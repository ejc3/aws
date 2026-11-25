#!/usr/bin/env python3
"""Exercise the mini VM's registration loop with fake SSH and GitHub clients."""

import fcntl
import json
import os
from pathlib import Path
import plistlib
import pwd
import select
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("mac-vm-setup.sh")
FAKE = r'''#!/usr/bin/env python3
import json, os, re, sys, tarfile
from pathlib import Path
config = json.loads(os.environ["FAKE_RUNNERS"])
args = sys.argv[1:]
if Path(sys.argv[0]).name == "ssh":
    cmd = args[-1].replace("\\", "")
    if "install-ci-sim-slots.sh" in cmd:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as files:
            assert {item.name for item in files} == {"bin/mac-build", "ci/sim-slot-shim", "ci/ci-disk-clean",
                                                         "ci/ci-coreaudio-check", "install-ci-sim-slots.sh"}
        print("test: installed CI test slots")
    elif "Print :CFBundleShortVersionString" in cmd:
        print("27.0\n27A123")
    elif ".ci-vm-copied 2>" in cmd:
        print("27.0 27A123")
    elif "test -f" in cmd and ".runner" in cmd:
        name = re.search(r"/runners/(mini-[1-4])/", cmd)[1]
        if config.get("ssh_failed") and name == "mini-4":
            sys.exit(255)
        print("no" if config.get("fresh") else "yes")
    elif "cat /Users/admin/runners/" in cmd:
        sys.stdout.buffer.write(b'\xef\xbb\xbf{"agentId": 717}')
    elif "./config.sh --unattended" in cmd:
        print(*re.search(r"--name (mini-[1-4]) --labels ([a-z0-9,-]+)", cmd).groups(),
              file=open(os.environ["CALLS"], "a"))
else:
    endpoint = next(x for x in args if x.startswith("orgs/"))
    if "--paginate" in args:
        assert endpoint == "orgs/dolphin-labs-hq/actions/runners?per_page=100"
        print(config.get("slot", "" if config.get("fresh") else "717\tmini-4\tonline\ttrue\tfalse"))
    elif endpoint.endswith("/registration-token"):
        print("test-registration")
    elif endpoint.endswith("/labels"):
        assert args[args.index("-X") + 1] == "POST"
        assert args[args.index("-f") + 1] == "labels[]=mini-simulator"
        print("add", endpoint, file=open(os.environ["CALLS"], "a"))
    else:
        raise RuntimeError("Unexpected GitHub call")
'''

# Session gui/<uid> exists. The agent is loaded once bootstrapped; after a bootout of a loaded agent it is still
# listed for two more prints (an instance still exiting), and bootstrap fails while it is listed, as launchctl's does.
LAUNCHCTL = r'''#!/usr/bin/env python3
import os, sys
calls = os.environ["FAKE_LAUNCHCTL_CALLS"]
args = sys.argv[1:]
history = open(calls).read().splitlines() if os.path.exists(calls) else []
with open(calls, "a") as f:
    print(*args, file=f)


def job(words):
    # The job a call names, as domain/label: bootstrap names its plist; bootout and print name the job itself
    # (system/LABEL or gui/UID/LABEL). A domain alone (gui/UID) names no job.
    if words[0] == "bootstrap":
        return words[1] + "/" + os.path.basename(words[2]).removesuffix(".plist")
    if words[0] in ("bootout", "print") and (words[1].startswith("system/") or words[1].count("/") == 2):
        return words[1]
    return None


jobs = {}  # job -> (loaded, prints for which a booted-out instance still lingers)
for call in history:
    words = call.split()
    name = job(words)
    if name is None:
        continue
    loaded, lingering = jobs.get(name, (False, 0))
    if words[0] == "bootstrap":
        jobs[name] = (True, 0)
    elif words[0] == "bootout":
        jobs[name] = (False, 2 if loaded else 0)
    elif lingering:
        jobs[name] = (loaded, lingering - 1)
name = job(args)
loaded, lingering = jobs.get(name, (False, 0))
if args[0] == "print" and name is not None:
    sys.exit(0 if loaded or lingering else 1)
if args[0] == "bootstrap" and (loaded or lingering):
    print("Bootstrap failed: 5: Input/output error", file=sys.stderr)
    sys.exit(5)
'''


class MiniRunnerRouting(unittest.TestCase):
    def run_setup(self, mode="--runners", **config):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for tool in ("gh", "ssh"):
                path = base / tool
                path.write_text(FAKE)
                path.chmod(0o755)
            calls = base / "calls"
            calls.touch()
            env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"],
                       FAKE_RUNNERS=json.dumps(config), CALLS=str(calls))
            result = subprocess.run(["bash", SCRIPT, "ci", "--lan", "test", mode],
                                    env=env, capture_output=True, text=True, timeout=30)
            return result, calls.read_text().splitlines()

    def test_fresh_labels_keep_all_four_eligible_for_general_jobs(self):
        result, calls = self.run_setup(fresh=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("test: installed CI test slots", result.stdout)
        self.assertEqual(calls, [
            "mini-1 gww-ios,mini,ios-pool", "mini-2 gww-ios,mini,ios-pool",
            "mini-3 gww-ios,mini,ios-pool", "mini-4 gww-ios,mini,ios-pool,mini-simulator",
        ])

    def test_slot_update_needs_no_registration_or_vm_setup(self):
        result, calls = self.run_setup(mode="--simulator-slots")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "test: installed CI test slots")
        self.assertEqual(calls, [])

    def test_existing_label_is_added_to_the_registered_id_without_replacing_it(self):
        result, calls = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["add orgs/dolphin-labs-hq/actions/runners/717/labels"])
        result, calls = self.run_setup(slot="717\tmini-4\tonline\ttrue\ttrue")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_conflicts_dont_mutate_the_existing_runners(self):
        for case in (dict(slot="81\tother\tonline\tfalse\ttrue"), dict(slot=""),
                     dict(slot="717\tmini-4\tonline\ttrue\ttrue\n81\tother\tonline\tfalse\ttrue"),
                     dict(slot="81\tmini-4\tonline\tfalse\tfalse"), dict(ssh_failed=True)):
            with self.subTest(case=case):
                result, calls = self.run_setup(**case)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_rebuild_reclaims_only_its_own_offline_simulator_registration(self):
        for state, allowed in [("mini-4\toffline\tfalse", True), ("other\toffline\tfalse", False),
                                ("mini-4\tonline\ttrue", False)]:
            with self.subTest(state=state):
                result, calls = self.run_setup(fresh=True, slot=f"717\t{state}\tfalse")
                self.assertEqual(result.returncode == 0, allowed, result.stderr)
                self.assertEqual(any(c.startswith("mini-4 ") for c in calls), allowed)


class SimulatorSlots(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.prefix = self.root / "local"
        self.shim = self.prefix / "bin/xcodebuild"
        self.mac_build = self.prefix / "libexec/mini-simulator/mac-build"
        self.installer = SCRIPT.parent / "mac/install-ci-sim-slots.sh"
        self.real = self.root / "real-xcodebuild"
        self.real.write_text("#!/usr/bin/env python3\n"
                             "import os, sys, json\n"
                             "print(json.dumps([os.getenv('MAC_BUILD_SLOT'), os.getenv('MAC_SIMULATOR_SLOT'), sys.argv[1:]]))\n")
        self.real.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.root), SIM_REAL_XCODEBUILD=str(self.real),
                        MAC_BUILD=str(self.mac_build), CI_SIM_SLOTS="1")
        for key in ("MAC_BUILD_SLOT", "MAC_SIMULATOR_SLOT", "MAC_STATE_DIR", "CI_SIM_STATE_DIR"):
            self.env.pop(key, None)
        # The disk cleaner's LaunchAgent goes to a stand-in directory and a launchctl that records its calls.
        self.agents = self.root / "LaunchAgents"
        self.launchctl_calls = self.root / "launchctl-calls"
        launchctl = self.root / "launchctl"
        launchctl.write_text(LAUNCHCTL)
        launchctl.chmod(0o755)
        self.daemons = self.root / "LaunchDaemons"
        self.install_env = dict(os.environ, CI_LAUNCH_AGENTS=str(self.agents), CI_LAUNCH_DAEMONS=str(self.daemons),
                                LAUNCHCTL=str(launchctl),
                                CI_CLEAN_USER=pwd.getpwuid(os.getuid()).pw_name,
                                FAKE_LAUNCHCTL_CALLS=str(self.launchctl_calls))

    def install(self):
        return subprocess.run(["sh", self.installer, self.prefix], capture_output=True, text=True,
                              env=self.install_env)

    def launchctl(self):
        return self.launchctl_calls.read_text().splitlines() if self.launchctl_calls.exists() else []

    def test_disk_cleaner_agent_is_installed_once_and_reloaded_only_on_change(self):
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Disk cleaner loaded", result.stdout)
        cleaner = self.mac_build.with_name("ci-disk-clean")
        self.assertTrue(os.access(cleaner, os.X_OK))
        plist = self.agents / "com.ejcampbell.ci-disk-clean.plist"
        with plist.open("rb") as f:
            agent = plistlib.load(f)
        self.assertEqual(agent["ProgramArguments"], [str(cleaner)])
        self.assertEqual(agent["StartInterval"], 3600)
        self.assertEqual((agent["Nice"], agent["ProcessType"], agent["LowPriorityIO"]), (20, "Background", True))
        self.assertEqual(agent["LimitLoadToSessionType"], "Aqua")
        uid = os.getuid()
        audio = self.daemons / "com.ejcampbell.ci-coreaudio-check.plist"
        self.assertEqual([c for c in self.launchctl() if not c.startswith("print")],
                         ["bootout system/com.ejcampbell.ci-coreaudio-check", f"bootstrap system {audio}",
                          f"bootout gui/{uid}/com.ejcampbell.ci-disk-clean", f"bootstrap gui/{uid} {plist}"])
        # Unchanged and loaded: nothing is reloaded.
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Disk cleaner unchanged and loaded", result.stdout)
        self.assertIn("coreaudiod check unchanged and loaded", result.stdout)
        self.assertEqual(sum(c.startswith("bootstrap") for c in self.launchctl()), 2)
        # A changed plist is replaced and reloaded once more, after the old instance has finished exiting.
        plist.write_text(plist.read_text().replace("3600", "60"))
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sum(c.startswith("bootstrap gui") for c in self.launchctl()), 2)
        self.assertEqual(sum(c.startswith("bootstrap system") for c in self.launchctl()), 1)
        with plist.open("rb") as f:
            self.assertEqual(plistlib.load(f)["StartInterval"], 3600)

    def test_install_is_idempotent_and_preserves_an_unmanaged_xcodebuild(self):
        self.shim.parent.mkdir(parents=True)
        self.shim.write_text("existing binary")
        self.assertNotEqual(self.install().returncode, 0)
        self.assertEqual(self.shim.read_text(), "existing binary")
        self.assertFalse(self.mac_build.exists())
        self.shim.unlink()
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.shim.resolve(), self.mac_build.with_name("sim-slot-shim"))

    def test_test_actions_take_slots_and_other_actions_pass_through(self):
        self.assertEqual(self.install().returncode, 0)
        for args, needs_slot in [
            (["-scheme", "test", "build-for-testing"], False),
            (["build", "-scheme", "test"], False),
            (["archive", "-archivePath", "test"], False),
            (["-list", "-json"], False),
            (["-disable-concurrent-destination-testing", "test"], True),
            (["-workspace", "Maps.xcworkspace", "-scheme", "Maps", "test-without-building", "-only-testing:MapUITests/Tour"], True),
            (["-quiet", "-destination", "platform=iOS Simulator,name=iPhone", "test"], True),
        ]:
            with self.subTest(args=args):
                data = subprocess.check_output([self.shim, *args], env=self.env, text=True, timeout=5)
                general, simulator, forwarded = json.loads(data)
                self.assertIsNone(general)
                self.assertEqual(simulator, "1" if needs_slot else None)
                self.assertEqual(forwarded, args)

    def test_occupied_test_slots_hold_tests_but_allow_builds(self):
        self.assertEqual(self.install().returncode, 0)
        # Hold both default slots as concurrent test commands would. Builds must still run,
        # and releasing one slot must admit exactly the same waiting invocation.
        env = self.env.copy()
        env.pop("CI_SIM_SLOTS")
        slots = self.root / ".local/state/sim-slots/mac-build"
        slots.mkdir(parents=True)
        with (slots / "slot.1").open("w") as first, (slots / "slot.2").open("w") as second:
            fcntl.flock(first, fcntl.LOCK_EX)
            fcntl.flock(second, fcntl.LOCK_EX)
            with subprocess.Popen([self.shim, "test"], env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True) as waiting:
                try:
                    self.assertTrue(select.select([waiting.stderr], [], [], 5)[0], "test command never reported waiting")
                    self.assertIn("waiting for one of 2", waiting.stderr.readline())
                    self.assertIsNone(waiting.poll())
                    result = subprocess.check_output([self.shim, "build"], env=env, text=True, timeout=5)
                    self.assertEqual(json.loads(result), [None, None, ["build"]])
                    fcntl.flock(second, fcntl.LOCK_UN)
                    output, _ = waiting.communicate(timeout=8)
                    self.assertEqual(waiting.returncode, 0)
                    self.assertEqual(json.loads(output), [None, "2", ["test"]])
                finally:
                    if waiting.poll() is None:
                        waiting.kill()
                        waiting.wait()

    def test_simulator_and_general_build_slots_are_independent(self):
        self.assertEqual(self.install().returncode, 0)
        # The outer general build must still acquire a simulator slot; two nested simulator
        # wrappers must reuse their single slot instead of waiting on themselves.
        for cmd in ([self.mac_build, self.shim, "test"],
                    [self.mac_build, "--simulator", self.mac_build, self.shim, "test"]):
            with self.subTest(cmd=cmd):
                data = subprocess.check_output(cmd, env=self.env, text=True, timeout=5)
                self.assertEqual(json.loads(data), ["1", "1", ["test"]])


if __name__ == "__main__":
    unittest.main(buffer=True)

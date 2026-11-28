#!/usr/bin/env python3
"""Exercise the mini VM's registration loop with fake SSH and GitHub clients."""

import fcntl
import json
import os
from pathlib import Path
import plistlib
import pwd
import select
import signal
import subprocess
import tempfile
import time
import unittest


SCRIPT = Path(__file__).with_name("mac-vm-setup.sh")
FAKE = r'''#!/usr/bin/env python3
import json, os, re, sys, tarfile
from pathlib import Path
config = json.loads(os.environ["FAKE_RUNNERS"])
args = sys.argv[1:]
if Path(sys.argv[0]).name == "ssh":
    cmd = args[-1].replace("\\", "")
    if "rustup toolchain install" in cmd:
        assert "rustup-init" in cmd and "shasum -a 256 -c" in cmd and ".cargo/bin" in cmd
        print("test: installed Rust " + re.search(r"toolchain install '?([0-9.]+)", cmd)[1])
    elif "install-ci-sim-slots.sh" in cmd:
        with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as files:
            assert {item.name for item in files} == {"bin/mac-build", "ci/sim-slot-shim", "ci/ci-disk-clean",
                                                         "ci/ci-coreaudio-check", "ci/swift-test-shim",
                                                         "install-ci-sim-slots.sh"}
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
        name = re.search(r"/runners/(mini-[1-4])/", cmd)[1]
        agent = 717 if name == "mini-4" else 700 + int(name[-1])
        sys.stdout.buffer.write(b'\xef\xbb\xbf' + json.dumps({"agentId": agent}).encode())
    elif "./config.sh remove" in cmd:
        name = re.search(r"runners/(mini-[1-4])", cmd)[1]
        print("retire", name, file=open(os.environ["CALLS"], "a"))
        if name in config.get("remove_fails", []):
            sys.exit(1)
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
    elif endpoint.endswith("/remove-token"):
        print("test-removal")
    elif endpoint.endswith("/labels"):
        assert args[args.index("-X") + 1] == "POST"
        label = args[args.index("-f") + 1]
        print("add", endpoint, *([label] if label != "labels[]=mini-simulator" else []), file=open(os.environ["CALLS"], "a"))
    elif "/labels/" in endpoint:
        assert args[args.index("-X") + 1] == "DELETE"
        print("remove", endpoint, file=open(os.environ["CALLS"], "a"))
    elif re.search(r"/runners/[0-9]+$", endpoint) and "-X" in args:
        assert args[args.index("-X") + 1] == "DELETE"
        print("deregister", endpoint, file=open(os.environ["CALLS"], "a"))
    elif re.search(r"/runners/[0-9]+$", endpoint) and ".busy" in args:
        print(config.get("busy", {}).get(endpoint.rsplit("/", 1)[1], "false"))
    elif re.search(r"/runners/[0-9]+$", endpoint) and ".id" in args:
        # Still registered after config.sh remove only when the test says so.
        runner = endpoint.rsplit("/", 1)[1]
        if runner not in config.get("stuck", []):
            sys.exit(1)
        print(runner)
    elif re.search(r"/runners/[0-9]+$", endpoint):
        # Each runner's labels now: the pool's before this change unless the test says otherwise.
        runner = endpoint.rsplit("/", 1)[1]
        default = "gww-ios,mini,ios-pool" + (",mini-simulator,ios-simulator" if runner == "717" else "")
        print(config.get("labels", {}).get(runner, default))
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

    def test_fresh_registers_two_runners_mini_3_for_queue_batches_and_mini_4_for_the_rest(self):
        result, calls = self.run_setup(fresh=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("test: installed CI test slots", result.stdout)
        self.assertEqual(calls, [
            "mini-3 ios-queue", "mini-4 gww-ios,mini,ios-pool,ios-queue,mini-simulator,ios-simulator",
        ])

    def test_rust_alone_needs_no_registration_slots_or_vm_setup(self):
        result, calls = self.run_setup(mode="--rust")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "test: installed Rust 1.99.1")
        self.assertEqual(calls, [])

    def test_provisioning_the_runners_installs_rust_too(self):
        result, _ = self.run_setup(fresh=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("test: installed Rust 1.99.1", result.stdout)

    def test_one_mode_at_a_time_and_rust_only_for_ci(self):
        for argv in (["ci", "--lan", "test", "--rust", "--runners"], ["ci", "--lan", "test", "--rust", "--simulator-slots"],
                     ["skevh", "--lan", "test", "--rust"]):
            result = subprocess.run(["bash", SCRIPT, *argv], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 2, argv)
            self.assertIn("usage:", result.stderr)

    def test_slot_update_needs_no_registration_or_vm_setup(self):
        result, calls = self.run_setup(mode="--simulator-slots")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "test: installed CI test slots")
        self.assertEqual(calls, [])

    # Every runner already relabelled: what each carries once this script has run.
    DONE = {"703": "ios-queue", "717": "gww-ios,mini,ios-pool,ios-queue,mini-simulator,ios-simulator"}
    RETIRE = ["retire mini-1", "retire mini-2"]

    def test_existing_label_is_added_to_the_registered_id_without_replacing_it(self):
        result, calls = self.run_setup(labels=self.DONE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["add orgs/dolphin-labs-hq/actions/runners/717/labels", *self.RETIRE])
        result, calls = self.run_setup(slot="717\tmini-4\tonline\ttrue\ttrue", labels=self.DONE)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, self.RETIRE)

    def test_registered_runners_are_relabelled_after_every_check(self):
        # Every runner with the pool's old labels: each mini gains ios-queue, and mini-3 keeps it alone (gww-ios, mini
        # and ios-pool removed: no dolphin-labs PR job and no dolphin-maps job can take it).
        result, calls = self.run_setup(slot="717\tmini-4\tonline\ttrue\ttrue")
        self.assertEqual(result.returncode, 0, result.stderr)
        r = "add orgs/dolphin-labs-hq/actions/runners/{}/labels labels[]=ios-queue"
        self.assertEqual(calls, [
            "remove orgs/dolphin-labs-hq/actions/runners/703/labels/gww-ios",
            "remove orgs/dolphin-labs-hq/actions/runners/703/labels/mini",
            "remove orgs/dolphin-labs-hq/actions/runners/703/labels/ios-pool",
            r.format(703), r.format(717), *self.RETIRE,
        ])
        self.assertIn("mini-3: labels ios-queue", result.stdout)
        # mini-3 as #1109 left it (mini and ios-queue): only mini removed; the others already right.
        result, calls = self.run_setup(slot="717\tmini-4\tonline\ttrue\ttrue", labels={**self.DONE, "703": "mini,ios-queue"})
        self.assertEqual(calls, ["remove orgs/dolphin-labs-hq/actions/runners/703/labels/mini", *self.RETIRE])

    def test_retired_runners_are_deregistered_and_a_busy_one_waits_for_a_rerun(self):
        done = dict(slot="717\tmini-4\tonline\ttrue\ttrue", labels=self.DONE)
        # mini-1 is running a job: left registered, mini-2 retired, and the run says to come back (exit 4).
        result, calls = self.run_setup(busy={"701": "true"}, **done)
        self.assertEqual(result.returncode, 4)
        self.assertEqual(calls, ["retire mini-2"])
        self.assertIn("mini-1: running a job; run this again", result.stderr)
        # A registration config.sh could not remove is deleted through the API.
        result, calls = self.run_setup(stuck=["702"], **done)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["retire mini-1", "retire mini-2", "deregister orgs/dolphin-labs-hq/actions/runners/702"])
        # config.sh remove failing in the guest stops nothing: the host deletes that registration and still retires the
        # other runner.
        result, calls = self.run_setup(remove_fails=["mini-1"], stuck=["701"], **done)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["retire mini-1", "deregister orgs/dolphin-labs-hq/actions/runners/701", "retire mini-2"])
        self.assertIn("mini-1: config.sh remove failed in the guest", result.stderr)
        # Already retired (no registration left in the guest): nothing to do.
        result, calls = self.run_setup(fresh=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("retire mini-1", calls)

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
        self.swift_dir = self.root / "Shared/swift-test"
        self.install_env = dict(os.environ, CI_LAUNCH_AGENTS=str(self.agents), CI_LAUNCH_DAEMONS=str(self.daemons),
                                CI_SWIFT_TEST_DIR=str(self.swift_dir),
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
        with audio.open("rb") as f:
            daemon = plistlib.load(f)
        check = self.mac_build.with_name("ci-coreaudio-check")
        self.assertTrue(os.access(check, os.X_OK))
        self.assertEqual(daemon["Label"], "com.ejcampbell.ci-coreaudio-check")
        self.assertEqual(daemon["ProgramArguments"], [str(check)])
        self.assertEqual((daemon["StartInterval"], daemon["RunAtLoad"], daemon["ProcessType"]), (300, True, "Standard"))
        self.assertNotIn("UserName", daemon)  # root: restarting coreaudiod needs it
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
        # A changed daemon plist is replaced and reloaded too, after its old instance has gone.
        audio.write_text(audio.read_text().replace("300", "60"))
        before = len(self.launchctl())
        result = self.install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("coreaudiod check loaded", result.stdout)
        calls = self.launchctl()[before:]
        self.assertEqual([c for c in calls if not c.startswith("print")][:2],
                         ["bootout system/com.ejcampbell.ci-coreaudio-check", f"bootstrap system {audio}"])
        self.assertGreaterEqual(sum(c == "print system/com.ejcampbell.ci-coreaudio-check" for c in calls), 3)
        with audio.open("rb") as f:
            self.assertEqual(plistlib.load(f)["StartInterval"], 300)
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

    def swift_env(self, **extra):
        # The real swift stand-in records its arguments and slot, can run for a while, and exits with FAKE_SWIFT_RC.
        real = self.root / "real-swift"
        if not real.exists():
            real.write_text("#!/usr/bin/env python3\n"
                            "import json, os, sys, time\n"
                            "print(json.dumps([os.getenv('SWIFT_TEST_SLOT'), sys.argv[1:]]), flush=True)\n"
                            "if os.getenv('FAKE_SWIFT_PID'):\n"
                            "    open(os.environ['FAKE_SWIFT_PID'], 'w').write(str(os.getpid()))\n"
                            "if os.getenv('FAKE_SWIFT_LINGER'):\n"
                            "    import subprocess\n"
                            "    leftover = subprocess.Popen(['sleep', '30'], close_fds=False, stdout=subprocess.DEVNULL,\n"
                            "                                stderr=subprocess.DEVNULL)  # every other descriptor it inherits\n"
                            "    open(os.environ['FAKE_SWIFT_LINGER'], 'w').write(str(leftover.pid))\n"
                            "time.sleep(float(os.getenv('FAKE_SWIFT_SLEEP', '0')))\n"
                            "sys.exit(int(os.getenv('FAKE_SWIFT_RC', '0')))\n")
            real.chmod(0o755)
        env = dict(os.environ, HOME=str(self.root), SWIFT_REAL=str(real), SWIFT_TEST_STATE_DIR=str(self.swift_dir))
        env.update(extra)
        for key in ("SWIFT_TEST_SLOT", "SWIFT_TEST_SLOTS"):
            if key not in extra:
                env.pop(key, None)
        return env

    def test_only_swift_test_takes_the_one_slot(self):
        self.assertEqual(self.install().returncode, 0)
        swift = self.prefix / "bin/swift"
        self.assertEqual(swift.resolve(), self.mac_build.with_name("swift-test-shim"))
        for args, slot in ((["build", "-c", "release"], None), (["package", "resolve"], None),
                           (["build", "--product", "test"], None), (["test", "--parallel"], "1"),
                           (["-v", "test"], None), (["-module-name", "test", "x.swift"], None)):
            with self.subTest(args=args):
                out = subprocess.run([swift, *args], env=self.swift_env(), capture_output=True, text=True, timeout=10)
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertEqual(json.loads(out.stdout), [slot, args])

    def test_concurrent_swift_tests_run_one_at_a_time_and_keep_their_exit_codes(self):
        self.assertEqual(self.install().returncode, 0)
        swift = self.prefix / "bin/swift"
        first = subprocess.Popen([swift, "test"], env=self.swift_env(FAKE_SWIFT_SLEEP="3", FAKE_SWIFT_RC="3"),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(1)  # the first holds the slot
        started = time.monotonic()
        second = subprocess.run([swift, "test"], env=self.swift_env(FAKE_SWIFT_RC="5"),
                                capture_output=True, text=True, timeout=30)
        waited = time.monotonic() - started
        self.assertEqual(first.wait(timeout=30), 3)
        self.assertEqual(second.returncode, 5)
        self.assertGreater(waited, 1.5)  # it could not start until the first had finished
        self.assertEqual(second.stderr.count("swift test: waiting for the VM's test slot"), 1)
        # A build runs beside a test that holds the slot.
        holder = subprocess.Popen([swift, "test"], env=self.swift_env(FAKE_SWIFT_SLEEP="3"),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        started = time.monotonic()
        build = subprocess.run([swift, "build"], env=self.swift_env(), capture_output=True, text=True, timeout=10)
        self.assertEqual(build.returncode, 0)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertNotIn("waiting", build.stderr)
        holder.wait(timeout=30)

    def test_a_killed_swift_test_frees_the_slot_and_a_nested_one_does_not_wait(self):
        self.assertEqual(self.install().returncode, 0)
        swift = self.prefix / "bin/swift"
        holder = subprocess.Popen([swift, "test"], env=self.swift_env(FAKE_SWIFT_SLEEP="60"),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        # A swift test started inside the holder's slot (SWIFT_TEST_SLOT set) runs at once.
        nested = subprocess.run([swift, "test"], env=self.swift_env(SWIFT_TEST_SLOT="1"),
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(json.loads(nested.stdout)[0], "1")
        self.assertNotIn("waiting", nested.stderr)
        holder.kill()  # a cancelled job: the lock goes with the process, however it ends
        holder.wait(timeout=10)
        started = time.monotonic()
        after = subprocess.run([swift, "test"], env=self.swift_env(), capture_output=True, text=True, timeout=10)
        self.assertEqual(after.returncode, 0)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertNotIn("waiting", after.stderr)

    def test_a_process_left_behind_by_swift_test_does_not_keep_the_slot(self):
        self.assertEqual(self.install().returncode, 0)
        swift = self.prefix / "bin/swift"
        pid_file = self.root / "leftover.pid"
        done = subprocess.run([swift, "test"], env=self.swift_env(FAKE_SWIFT_LINGER=str(pid_file)),
                              capture_output=True, text=True, timeout=10)
        leftover = int(pid_file.read_text())
        self.addCleanup(lambda: subprocess.run(["kill", str(leftover)], capture_output=True))
        self.assertEqual(done.returncode, 0)
        os.kill(leftover, 0)  # still running, and the slot is free all the same
        started = time.monotonic()
        after = subprocess.run([swift, "test"], env=self.swift_env(), capture_output=True, text=True, timeout=10)
        self.assertLess(time.monotonic() - started, 1.5)
        self.assertNotIn("waiting", after.stderr)

    def test_a_signal_reaches_swift_test_and_the_shim_ends_the_same_way(self):
        self.assertEqual(self.install().returncode, 0)
        swift = self.prefix / "bin/swift"
        pid_file = self.root / "real.pid"
        shim = subprocess.Popen([swift, "test"], env=self.swift_env(FAKE_SWIFT_SLEEP="60", FAKE_SWIFT_PID=str(pid_file)),
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.assertIn('"1"', shim.stdout.readline())  # the real swift is running in the slot
        deadline = time.monotonic() + 5
        while not (pid_file.exists() and pid_file.read_text().strip()) and time.monotonic() < deadline:
            time.sleep(0.05)
        real = int(pid_file.read_text())
        shim.send_signal(signal.SIGTERM)
        self.assertEqual(shim.wait(timeout=10), -signal.SIGTERM)
        with self.assertRaises(ProcessLookupError):  # passed on: the real swift test ended too
            os.kill(real, 0)
        started = time.monotonic()
        after = subprocess.run([swift, "test"], env=self.swift_env(), capture_output=True, text=True, timeout=10)
        self.assertLess(time.monotonic() - started, 1.5)  # the child went with it: nothing holds the slot

    def test_the_slot_is_one_directory_for_the_vm_whatever_home_is(self):
        self.assertEqual(self.install().returncode, 0)
        self.assertTrue(self.swift_dir.is_dir())
        self.assertEqual(oct(self.swift_dir.stat().st_mode & 0o777), "0o700")
        swift = self.prefix / "bin/swift"
        holder = subprocess.Popen([swift, "test"], env=self.swift_env(FAKE_SWIFT_SLEEP="3", HOME=str(self.root / "a")),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        env = self.swift_env()
        env.pop("HOME")  # another HOME, or none at all: the same slot
        waiter = subprocess.run([swift, "test"], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(waiter.returncode, 0, waiter.stderr)
        self.assertIn("waiting for the VM's test slot", waiter.stderr)
        holder.wait(timeout=30)

    def test_a_slot_file_that_cannot_be_opened_runs_the_test_without_a_slot(self):
        # As after a `sudo swift test` left a root-owned slot file: the test still runs, and says why it has no slot.
        self.assertEqual(self.install().returncode, 0)
        self.swift_dir.mkdir(parents=True, exist_ok=True)
        locked = self.swift_dir / "slot.1"
        locked.write_text("")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o600)
        out = subprocess.run([self.prefix / "bin/swift", "test"], env=self.swift_env(FAKE_SWIFT_RC="3"),
                             capture_output=True, text=True, timeout=10)
        self.assertEqual(out.returncode, 3, out.stderr)  # the test ran, and its own exit code came back
        self.assertEqual(json.loads(out.stdout), [None, ["test"]])
        self.assertIn("running without the VM's test slot", out.stderr)

    def test_a_symlink_at_the_shared_slot_directory_is_refused(self):
        # /Users/Shared is writable by everyone: a planted link is not followed by the installer's chown and chmod.
        target = self.root / "elsewhere"
        target.mkdir()
        target.chmod(0o755)
        self.swift_dir.parent.mkdir(parents=True, exist_ok=True)
        self.swift_dir.symlink_to(target)
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing the symlink", result.stderr)
        self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_an_unmanaged_swift_is_refused_and_left_alone(self):
        swift = self.prefix / "bin/swift"
        swift.parent.mkdir(parents=True)
        swift.write_text("someone else's swift")
        result = self.install()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Refusing to replace unmanaged", result.stderr)
        self.assertEqual(swift.read_text(), "someone else's swift")
        swift.unlink()
        self.assertEqual(self.install().returncode, 0)
        self.assertEqual(self.install().returncode, 0)  # idempotent
        self.assertEqual(swift.resolve(), self.mac_build.with_name("swift-test-shim"))

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



class SigningKeychain(unittest.TestCase):
    def test_the_keychain_is_unlocked_before_its_settings_change(self):
        # A locked keychain's settings change waits on a password dialog nobody answers (2026-11-28).
        script = SCRIPT.read_text()
        self.assertIn("security unlock-keychain -p", script)
        unlock = script.index('security unlock-keychain -p \\"\\$(cat \\$HOME/.ci-keychain-password)\\" \\$kc')
        self.assertLess(unlock, script.index("security set-keychain-settings -lut 21600"))

if __name__ == "__main__":
    unittest.main(buffer=True)

#!/usr/bin/env python3
"""The CI VM's disk cleaner (scripts/mac/ci/ci-disk-clean, #1021) against a fake home, simctl and lsof."""

import contextlib
import fcntl
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

SOURCE = Path(__file__).with_name("mac") / "ci" / "ci-disk-clean"
MIB = 1024**2
HOUR = 3600

# xcrun simctl: the device list comes from a JSON file; erase empties data/ and rewrites device.plist, as
# CoreSimulator does, and refuses a booted device. Every call is recorded first, so an attempted erase shows.
# A device with boots_after_listings = N reports Booted from listing N + 1 on.
XCRUN = r'''#!/usr/bin/env python3
import json, os, shutil, sys
state_file, calls = os.environ["FAKE_SIMS"], os.environ["FAKE_CALLS"]
sims = json.load(open(state_file))
args = sys.argv[1:]
assert args[0] == "simctl"
print(*args[1:], file=open(calls, "a"))
listings = sum(line.startswith("list") for line in open(calls))
for d in sims.values():
    if listings > d.get("boots_after_listings", float("inf")):
        d["state"] = "Booted"
json.dump(sims, open(state_file, "w"))
if args[1:] == ["list", "devices", "--json"]:
    print(json.dumps({"devices": {"com.apple.CoreSimulator.SimRuntime.iOS-26-0": [
        {"udid": u, "state": d["state"], "name": d["name"], "isAvailable": True} for u, d in sims.items()]}}))
elif args[1] == "erase":
    udid = args[2]
    if sims[udid]["state"] != "Shutdown":
        print("Unable to erase contents and settings in current state: Booted", file=sys.stderr)
        sys.exit(149)
    device = os.path.join(os.environ["HOME"], "Library/Developer/CoreSimulator/Devices", udid)
    shutil.rmtree(os.path.join(device, "data"))
    if not sims[udid].get("erase_leaves_no_data"):
        os.mkdir(os.path.join(device, "data"))
    open(os.path.join(device, "device.plist"), "w").write("erased")
else:
    sys.exit(2)
'''
LSOF = r'''#!/usr/bin/env python3
import os
if os.environ.get("FAKE_TOUCH"):
    stamp = float(os.environ["FAKE_TOUCH_TIME"])
    os.utime(os.environ["FAKE_TOUCH"], (stamp, stamp))
print("p1")
for path in filter(None, os.environ.get("FAKE_OPEN", "").split(":")):
    print("n" + path)
'''

# ps: the processes are FAKE_PS's lines (FAKE_PS_FAIL makes it fail), so a runner's job is a Runner.Worker line.
PS = r'''#!/usr/bin/env python3
import os, sys
if os.environ.get("FAKE_PS_FAIL"):
    sys.exit(1)
print("/sbin/launchd")
for line in filter(None, os.environ.get("FAKE_PS", "").split(";")):
    print(line)
'''


def load():
    loader = importlib.machinery.SourceFileLoader("ci_disk_clean", str(SOURCE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def udid(n):
    return f"{n:08X}-0000-4000-8000-000000000000"


class Cleaner(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.tmp = self.root / "var-folders" / "T"
        self.tmp.mkdir(parents=True)
        self.derived = self.home / "Library/Developer/Xcode/DerivedData"
        self.derived.mkdir(parents=True)
        self.devices = self.home / "Library/Developer/CoreSimulator/Devices"
        self.devices.mkdir(parents=True)
        self.work = self.home / "runners/mini-1/_work"
        self.blob(self.work / "checkout/big", 2)
        self.sims, self.calls = self.root / "sims.json", self.root / "calls"
        self.sims.write_text("{}")
        self.calls.touch()
        for name, text in (("xcrun", XCRUN), ("lsof", LSOF), ("ps", PS)):
            (self.root / name).write_text(text)
            (self.root / name).chmod(0o755)
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in ("CI_SIM_STATE_DIR", "MAC_STATE_DIR", "FAKE_OPEN", "FAKE_TOUCH", "FAKE_TOUCH_TIME", "FAKE_PS",
                    "FAKE_PS_FAIL"):
            os.environ.pop(key, None)
        os.environ.update(HOME=str(self.home), TMPDIR=str(self.tmp), FAKE_SIMS=str(self.sims),
                          FAKE_CALLS=str(self.calls))
        self.m = load()
        self.m.XCRUN, self.m.LSOF, self.m.PS = str(self.root / "xcrun"), str(self.root / "lsof"), str(self.root / "ps")
        self.m.GETCONF = str(self.root / "no-getconf")  # not on Linux: the cleaner falls back to TMPDIR
        # The volume is the fake home's own bytes against a made-up capacity, so every removal shows up as free space.
        self.capacity = 1000 * MIB
        self.m.usage = lambda volume: (10_000 * MIB, self.free())
        self.m.MAX_USED = 2.0  # the percentage rule has its own test
        self.now = time.time() + 30 * HOUR  # every file is past both ages unless it is given a later mtime

    def used(self):
        return sum(p.lstat().st_blocks * 512 for p in self.root.rglob("*"))

    def blob(self, path, mib, mtime=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(os.urandom(mib * MIB))
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def worker(self, runner):
        """A job on runner mini-N: its Runner.Worker, as ps lists it."""
        line = f"{self.home}/runners/mini-{runner}/bin.2.338.2/Runner.Worker spawnclient 156 161"
        os.environ["FAKE_PS"] = ";".join(filter(None, [os.environ.get("FAKE_PS", ""), line]))

    def sim(self, n, state, mib, plist_age_hours, name=None, **extra):
        device = self.devices / udid(n)
        self.blob(device / "data/Containers/app", mib)
        plist = device / "device.plist"
        plist.write_text("device")
        stamp = self.now - plist_age_hours * HOUR
        os.utime(plist, (stamp, stamp))
        sims = json.loads(self.sims.read_text())
        sims[udid(n)] = {"state": state, "name": name or f"Dolphin Maps CI mini-{n}: iPhone 17", **extra}
        self.sims.write_text(json.dumps(sims))

    def run_clean(self, min_free_mib, dry_run=False):
        self.m.MIN_FREE = min_free_mib * MIB
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = self.m.clean(self.home, apply=not dry_run, now=self.now)
            self.m.report(result, dry_run)
        erased = [line.split()[1] for line in self.calls.read_text().splitlines() if line.startswith("erase")]
        return result, erased, out.getvalue()

    def free(self):
        return self.capacity - self.used()

    def test_pressure_is_under_30_gib_free_or_85_percent_used(self):
        m = load()
        gib = m.GIB
        self.assertFalse(m.pressure(200 * gib, 31 * gib))
        self.assertTrue(m.pressure(200 * gib, 29 * gib))
        self.assertTrue(m.pressure(1000 * gib, 140 * gib))  # 86% used with 140 GiB free
        self.assertFalse(m.pressure(1000 * gib, 160 * gib))

    def test_no_pressure_does_nothing(self):
        self.sim(1, "Shutdown", 3, 12)
        self.blob(self.derived / "Old-abc/Build/x", 1)
        result, erased, out = self.run_clean(min_free_mib=1)
        self.assertEqual(result["status"], "enough room")
        self.assertEqual(self.calls.read_text(), "")
        self.assertTrue((self.derived / "Old-abc").exists())
        self.assertIn("enough room", out)

    def test_erases_stale_shutdown_simulators_largest_first_and_stops_with_enough_room(self):
        self.sim(1, "Booted", 6, 48)       # booted: never
        self.sim(2, "Shutdown", 5, 2)      # shut down, used within 6 hours: kept
        self.sim(3, "Shutdown", 4, 7)      # stale and largest of the eligible: erased first
        self.sim(4, "Shutdown", 2, 30)     # stale, but the first erase already makes enough room
        self.blob(self.derived / "Old-abc/Build/x", 1)
        need = self.free() + 3 * MIB       # short by 3 MiB: one erase of 4 MiB suffices
        result, erased, out = self.run_clean(min_free_mib=need // MIB)
        self.assertEqual(erased, [udid(3)])
        self.assertEqual(result["status"], "enough room")
        self.assertEqual(sorted(p.name for p in self.devices.iterdir()), [udid(n) for n in (1, 2, 3, 4)])
        self.assertEqual(list((self.devices / udid(3) / "data").iterdir()), [])
        self.assertTrue((self.devices / udid(4) / "data/Containers/app").exists())
        self.assertTrue((self.derived / "Old-abc").exists())  # caches wait until simulators are exhausted
        self.assertIn("1 simulators erased (0.0 GiB)", out)

    def test_ages_out_old_derived_data_and_temp_entries_and_never_runner_work(self):
        recent = self.now - 2 * HOUR
        self.sim(1, "Shutdown", 1, 1)                               # too recent to erase
        self.blob(self.derived / "Old-abc/Build/Products/a", 2)
        self.blob(self.derived / "Fresh-def/Build/a", 2)
        self.blob(self.derived / "Mixed-ghi/info.plist", 1)
        self.blob(self.derived / "Mixed-ghi/Index.noindex/new", 1, mtime=recent)  # touched deep inside: kept
        self.blob(self.tmp / "old-file", 1)
        self.blob(self.tmp / "old-dir/nested/x", 1)
        self.blob(self.tmp / "com.apple.service/x", 1)               # a system service's: kept
        self.blob(self.tmp / "open-dir/x", 1)                        # open in a process: kept
        os.environ["FAKE_OPEN"] = str(self.tmp / "open-dir/x")
        (self.tmp / "link-to-work").symlink_to(self.work)           # a link: kept, and never followed
        os.utime(self.derived / "Fresh-def/Build/a", (recent, recent))
        result, erased, out = self.run_clean(min_free_mib=10_000)
        self.assertEqual(erased, [])
        self.assertEqual(result["status"], "nothing more to clean")
        self.assertEqual(sorted(p.name for p in self.derived.iterdir()), ["Fresh-def", "Mixed-ghi"])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["com.apple.service", "link-to-work", "open-dir"])
        self.assertTrue((self.work / "checkout/big").exists())
        self.assertIn("1 DerivedData entries removed", out)
        self.assertIn("2 temp entries removed", out)

    def test_never_erases_a_simulator_that_is_not_shut_down(self):
        # No slot lock is held: only the state check stands between these devices and an erase. Each in-use
        # device is larger than the one stale shut-down device, so it would be chosen first.
        for n, state in ((1, "Booted"), (2, "Booting"), (3, "Creating"), (4, "Shutting Down")):
            self.sim(n, state, 6, 48)
        self.sim(5, "Shutdown", 8, 48, boots_after_listings=1)  # a job boots it after the cleaner's scan
        self.sim(6, "Shutdown", 1, 48)
        # A dry run takes no second look: the scan alone must leave the in-use devices out.
        result, erased, out = self.run_clean(min_free_mib=10_000, dry_run=True)
        self.assertEqual(erased, [])
        self.assertIn("2 simulators erased", out)  # 5 (still shut down at its first listing) and 6
        self.calls.write_text("")  # restart the listing count: 5 is shut down again at the real run's scan
        # The real run: 5 is Shutdown in the scan and Booted when it is checked again under the locks.
        result, erased, out = self.run_clean(min_free_mib=10_000)
        self.assertEqual(erased, [udid(6)])
        for n in range(1, 6):
            self.assertTrue((self.devices / udid(n) / "data/Containers/app").exists())

    def test_caches_touched_minutes_ago_survive_while_older_ones_go(self):
        minutes = self.now - 10 * 60
        self.sim(1, "Shutdown", 1, 1)
        self.blob(self.derived / "Old-abc/Build/a", 1)
        self.blob(self.derived / "Recent-def/Build/a", 1, mtime=minutes)
        self.blob(self.derived / "Written-ghi/Build/a", 1)  # old at the scan, written before its removal
        self.blob(self.tmp / "old-file", 1)
        self.blob(self.tmp / "recent-file", 1, mtime=minutes)
        # A dry run takes no second look: the scan alone must leave the recent entries out.
        result, erased, out = self.run_clean(min_free_mib=10_000, dry_run=True)
        self.assertIn("2 DerivedData entries removed", out)
        self.assertIn("1 temp entries removed", out)
        # The real run: a job writes into Written-ghi after the scan; the check under the locks must see it.
        os.environ.update(FAKE_TOUCH=str(self.derived / "Written-ghi/Build/a"), FAKE_TOUCH_TIME=str(minutes))
        result, erased, out = self.run_clean(min_free_mib=10_000)
        self.assertEqual(sorted(p.name for p in self.derived.iterdir()), ["Recent-def", "Written-ghi"])
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["recent-file"])
        self.assertIn("1 DerivedData entries removed", out)
        self.assertIn("1 temp entries removed", out)

    def test_a_missing_data_folder_after_an_erase_does_not_stop_the_other_erases(self):
        self.sim(1, "Shutdown", 3, 12, erase_leaves_no_data=True)  # largest, so erased first
        self.sim(2, "Shutdown", 2, 12)
        result, erased, out = self.run_clean(min_free_mib=10_000)
        self.assertEqual(erased, [udid(1), udid(2)])
        self.assertIn("2 simulators erased", out)
        self.assertEqual(result["status"], "nothing more to clean")

    def test_entries_vanishing_during_the_scan_are_skipped(self):
        # Whichever vanish-* entry is walked first removes every one of them: it vanishes during its own walk,
        # and the others vanish between the listing and their stat.
        for name in ("vanish-a", "vanish-b", "vanish-c"):
            self.blob(self.tmp / name / "x", 1)
        self.blob(self.tmp / "old-file", 1)
        real_tree = self.m.tree

        def tree(path, device):
            if Path(path).parent == self.tmp and Path(path).name.startswith("vanish-"):
                for doomed in self.tmp.glob("vanish-*"):
                    shutil.rmtree(doomed)
            return real_tree(path, device)

        self.m.tree = tree
        result, erased, out = self.run_clean(min_free_mib=10_000)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), [])
        self.assertIn("1 temp entries removed", out)
        self.assertEqual(result["status"], "nothing more to clean")

    def hold(self, path, mode):
        """Another process holds `path` (a test command's admission lock shared, or its slot exclusively)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            ["python3", "-c", "import fcntl, sys; f = open(sys.argv[1], 'a'); "
             f"fcntl.flock(f, {mode}); print('held', flush=True); sys.stdin.read()", str(path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        return holder

    def test_a_held_mac_build_lock_stops_derived_data_and_unnamed_simulators_only(self):
        # A runner's own devices need only that runner idle, and temp entries only their own checks; DerivedData and
        # a device no runner is named in still wait for no job at all.
        self.sim(1, "Shutdown", 2, 12)                                 # runner mini-1's, which is idle
        self.sim(2, "Shutdown", 3, 12, name="GWW UI 45120901896-1-ui")  # no runner named: waits
        self.blob(self.derived / "Old-abc/Build/x", 1)
        self.blob(self.tmp / "old-file", 1)
        admission = self.home / ".local/state/mac-build/admission.lock"
        slot = self.home / ".local/state/sim-slots/mac-build/slot.2"
        for path, mode in ((admission, fcntl.LOCK_SH), (slot, fcntl.LOCK_EX)):
            with self.subTest(lock=path.name):
                holder = self.hold(path, mode)
                self.calls.write_text("")
                try:
                    result, erased, out = self.run_clean(min_free_mib=10_000)
                finally:
                    holder.communicate("")
                self.assertIn(udid(1), erased)      # an idle runner's device, whatever lock is held
                self.assertNotIn(udid(2), erased)   # a device no runner is named in waits
                self.assertTrue((self.devices / udid(2) / "data/Containers/app").exists())
                self.assertTrue((self.derived / "Old-abc").exists())
                self.assertFalse((self.tmp / "old-file").exists())
                self.assertIn("holds a slot;", result["status"])
                self.assertIn("entries wait", result["status"])
        self.calls.write_text("")
        result, erased, _ = self.run_clean(min_free_mib=10_000)
        self.assertIn(udid(2), erased)  # the unnamed device, once nothing holds a lock
        self.assertFalse((self.derived / "Old-abc").exists())

    def test_a_busy_runners_devices_wait_while_the_others_are_erased(self):
        # #1033: under steady CI the whole VM was never idle, so nothing was ever erased and the volume filled.
        self.sim(1, "Shutdown", 4, 12)   # mini-1 runs a job: its device waits
        self.sim(2, "Shutdown", 3, 12)   # mini-2 is idle: erased while mini-1 works
        self.sim(3, "Shutdown", 2, 12)
        self.worker(1)
        holder = self.hold(self.home / ".local/state/sim-slots/mac-build/slot.1", fcntl.LOCK_EX)  # mini-1's test
        try:
            result, erased, out = self.run_clean(min_free_mib=10_000)
        finally:
            holder.communicate("")
        self.assertEqual(erased, [udid(2), udid(3)])
        self.assertTrue((self.devices / udid(1) / "data/Containers/app").exists())
        self.assertIn("1 simulators of busy runners skipped", out)
        self.assertEqual(result["status"], "nothing more to clean")

    def test_a_runner_whose_jobs_cannot_be_read_counts_as_busy(self):
        self.sim(1, "Shutdown", 2, 12)
        os.environ["FAKE_PS_FAIL"] = "1"
        result, erased, out = self.run_clean(min_free_mib=10_000)
        self.assertEqual(erased, [])
        self.assertIn("1 simulators of busy runners skipped", out)

    def test_another_runners_job_is_not_mistaken_for_this_runners(self):
        # mini-10's worker is not mini-1's: the runner's directory is matched with its trailing slash.
        self.sim(1, "Shutdown", 2, 12)
        self.worker(10)
        result, erased, out = self.run_clean(min_free_mib=10_000)
        self.assertEqual(erased, [udid(1)])

    def test_a_temp_entry_opened_after_the_scan_is_kept(self):
        # Temp entries go without the locks, so their own checks are repeated just before removal.
        self.blob(self.tmp / "old-dir/x", 1)
        real = self.m.open_paths
        calls = []

        def opened():
            calls.append(1)
            return real() | ({str(self.tmp / "old-dir/x")} if len(calls) > 1 else set())

        self.m.open_paths = opened
        result, erased, out = self.run_clean(min_free_mib=10_000)
        self.assertTrue((self.tmp / "old-dir/x").exists())
        self.assertNotIn("temp entries removed", out)

    def test_dry_run_changes_nothing_and_logs_no_names_or_paths(self):
        self.sim(1, "Shutdown", 2, 12)
        self.blob(self.derived / "Old-abc/Build/x", 1)
        self.blob(self.tmp / "old-file", 1)
        result, erased, out = self.run_clean(min_free_mib=10_000, dry_run=True)
        self.assertEqual(erased, [])
        self.assertTrue((self.derived / "Old-abc").exists() and (self.tmp / "old-file").exists())
        self.assertIn("(dry run)", out)
        self.assertIn("1 simulators erased", out)
        result, erased, out = self.run_clean(min_free_mib=10_000)
        self.assertEqual(erased, [udid(1)])
        for secret in (udid(1), "Dolphin", "Old-abc", "old-file", str(self.root), "/"):
            self.assertNotIn(secret, out)

    def test_main_takes_a_run_lock_and_reports_failures_without_paths(self):
        self.m.usage = lambda volume: (_ for _ in ()).throw(FileNotFoundError(2, "No such file", "/private/x"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.m.main([]), 1)
        self.assertEqual(err.getvalue().strip(), "ci-disk-clean: failed: FileNotFoundError 2")


if __name__ == "__main__":
    unittest.main(buffer=True)

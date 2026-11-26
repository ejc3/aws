#!/usr/bin/env python3
"""The pbox work volume cleaner (pbox-work-clean.tf, scripts/pbox-work-clean.py), offline.

The selection logic runs for real on a temporary tree: an in-use directory, a recent one and one holding a .pbox-keep
are kept; idle caches, then idle task directories, are removed; the pressure pass stops once the volume is under 70%;
nothing outside the tree is touched. The installer is rendered as Terraform would and checked as shell, and the safety
rules are pinned so none can be dropped without this file changing too.
"""
import base64
import gzip
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "pbox-work-clean.py"
TF = (ROOT / "pbox-work-clean.tf").read_text()
SPEC = importlib.util.spec_from_file_location("pbox_work_clean", SCRIPT)
clean = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = clean
SPEC.loader.exec_module(clean)

DAY = 86400 * 10**9
HOUR = 3600 * 10**9


def render_installer():
    """The installer heredoc as Terraform renders it."""
    text = TF.split("pbox_work_clean_install = <<-SH\n", 1)[1].split("\n  SH\n", 1)[0] + "\n"
    text = textwrap.dedent(text)
    text = re.sub(r'\$\{base64gzip\(file\("\$\{path\.module\}/scripts/([^"]+)"\)\)\}',
                  lambda m: base64.b64encode(gzip.compress((ROOT / "scripts" / m[1]).read_bytes())).decode(), text)
    text = re.sub(r'\$\{filesha256\("\$\{path\.module\}/scripts/([^"]+)"\)\}',
                  lambda m: hashlib.sha256((ROOT / "scripts" / m[1]).read_bytes()).hexdigest(), text)
    assert "${" not in text and "%{" not in text, "unrendered Terraform interpolation"
    return text


def heredoc(text, tag):
    return text.split(f"<<'{tag}'\n", 1)[1].split(f"\n{tag}\n", 1)[0]


class Tree:
    """A temporary work volume. Times are relative to a `now` a month ahead of the real clock, so every ctime (always
    the real time of creation) is old, and each entry's age is set by its mtime."""

    def __init__(self, test):
        self.base = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.base, True)
        self.root = self.base / "work"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.outside / "precious").write_bytes(b"p" * 4096)
        self.now = time.time_ns() + 30 * DAY
        self.device = self.root.stat().st_dev

    def file(self, rel, data=b"x" * 8192):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def age(self, rel, days):
        """Set every entry under rel, rel itself last, to `days` before now."""
        stamp = self.now - int(days * DAY)
        top = self.root / rel
        for directory, _, files in os.walk(top, topdown=False):
            for name in files:
                os.utime(os.path.join(directory, name), ns=(stamp, stamp), follow_symlinks=False)
            os.utime(directory, ns=(stamp, stamp))

    def touch(self, rel, hours):
        stamp = self.now - int(hours * HOUR)
        os.utime(self.root / rel, ns=(stamp, stamp), follow_symlinks=False)

    def run(self, apply=True, usage=None, scan=None):
        lines = []
        summary = clean.clean(self.root, self.device, self.now, apply, log=lines.append,
                              usage=usage or (lambda root: (0.10, 10**12)), scan=scan or clean.in_use)
        return summary, lines


class Selection(unittest.TestCase):
    def test_old_caches_go_and_the_checkout_stays(self):
        t = Tree(self)
        t.file("repo/src/main.rs")
        t.file("repo/target/CACHEDIR.TAG", clean.CACHEDIR_SIGNATURE + b"\n")
        t.file("repo/target/debug/app")
        t.file("repo/web/node_modules/pkg/index.js")
        t.file("repo/web/.next/cache/x")
        t.file("repo/py/__pycache__/m.pyc")
        t.file("repo/py/.venv/pyvenv.cfg")
        t.file("repo/py/.venv/lib/site.py")
        t.file("repo/notcargo/target/plan.txt")  # a directory named target with no Cargo marker
        t.age("repo", 3)
        before = (t.root / "repo").stat().st_mtime_ns
        summary, lines = t.run()
        for gone in ("target", "web/node_modules", "web/.next", "py/__pycache__", "py/.venv"):
            self.assertFalse((t.root / "repo" / gone).exists(), gone)
        for kept in ("src/main.rs", "notcargo/target/plan.txt"):
            self.assertTrue((t.root / "repo" / kept).exists(), kept)
        self.assertIn("5 caches, 0 idle task directories, 0 under pressure", summary)
        # Removing a cache does not make its task look recently used.
        self.assertEqual((t.root / "repo").stat().st_mtime_ns, before)
        self.assertEqual(len([l for l in lines if " removed cache " in l]), 5)

    def test_caches_younger_than_two_days_stay(self):
        t = Tree(self)
        t.file("repo/node_modules/pkg/index.js")
        t.age("repo", 1.5)
        t.run()
        self.assertTrue((t.root / "repo/node_modules/pkg/index.js").exists())

    def test_task_directories_idle_seven_days_go_whole(self):
        t = Tree(self)
        t.file("old/notes.txt")
        t.file("old/node_modules/a.js")
        t.age("old", 8)
        t.file("week/notes.txt")
        t.age("week", 6)
        summary, lines = t.run()
        self.assertFalse((t.root / "old").exists())
        self.assertTrue((t.root / "week/notes.txt").exists())
        self.assertIn("0 caches, 1 idle task directories", summary)
        self.assertTrue(any(" removed task directory " in l and '/old"' in l for l in lines))
        self.assertFalse((t.root / clean.TRASH).exists())

    def test_a_top_level_cache_goes_after_two_days(self):
        t = Tree(self)
        t.file(".pnpm-store/v3/files/00/abc")
        t.file("cargo-target/CACHEDIR.TAG", clean.CACHEDIR_SIGNATURE)
        t.file("cargo-target/release/app")
        t.age(".pnpm-store", 3)
        t.age("cargo-target", 3)
        t.run()
        self.assertFalse((t.root / ".pnpm-store").exists())
        self.assertFalse((t.root / "cargo-target").exists())

    def test_a_recent_file_anywhere_keeps_the_whole_directory(self):
        t = Tree(self)
        t.file("busy/deep/er/file")
        t.file("busy/node_modules/a.js")
        t.age("busy", 30)
        t.touch("busy/deep/er/file", 2)
        summary, _ = t.run()
        self.assertTrue((t.root / "busy/node_modules/a.js").exists())
        self.assertIn("1 recent", summary)

    def test_a_copied_tree_with_old_mtimes_counts_by_its_ctime(self):
        t = Tree(self)
        t.file("copied/old-file")
        # As rsync -a or cp -a leave it: a month-old mtime on everything, but written (ctime) just now.
        stamp = time.time_ns() - 30 * DAY
        for path in (t.root / "copied/old-file", t.root / "copied"):
            os.utime(path, ns=(stamp, stamp))
        lines = []
        clean.clean(t.root, t.device, time.time_ns() + HOUR, True, log=lines.append,
                    usage=lambda root: (0.99, 10**12))
        self.assertTrue((t.root / "copied/old-file").exists())

    def test_pbox_keep_anywhere_inside_keeps_it(self):
        t = Tree(self)
        t.file("keepme/a/b/.pbox-keep", b"")
        t.file("keepme/node_modules/a.js")
        t.age("keepme", 60)
        summary, _ = t.run(usage=lambda root: (0.99, 10**12))
        self.assertTrue((t.root / "keepme/node_modules/a.js").exists())
        self.assertIn("kept 1 (.pbox-keep)", summary)

    def test_pbox_keep_at_the_top_keeps_the_volume(self):
        t = Tree(self)
        t.file(".pbox-keep", b"")
        t.file("old/x")
        t.age("old", 60)
        summary, _ = t.run(usage=lambda root: (0.99, 10**12))
        self.assertTrue((t.root / "old/x").exists())
        self.assertIn("keeps the whole volume", summary)

    def test_a_directory_a_process_is_in_is_kept(self):
        t = Tree(self)
        t.file("inuse/sub/x")
        t.file("inuse/node_modules/a.js")
        t.file("open/data.bin")
        t.file("open/node_modules/a.js")
        t.age("inuse", 30)
        t.age("open", 30)
        cwd = subprocess.Popen(["sleep", "60"], cwd=t.root / "inuse/sub")
        self.addCleanup(cwd.wait)
        self.addCleanup(cwd.kill)
        # Another process holds the file (the cleaner skips its own pid).
        held = subprocess.Popen([sys.executable, "-c", "import sys, time; f = open(sys.argv[1], 'rb'); print(flush=True); "
                                 "time.sleep(60)", str(t.root / "open/data.bin")], stdout=subprocess.PIPE)
        self.addCleanup(held.wait)
        self.addCleanup(held.kill)
        held.stdout.readline()
        self.addCleanup(held.stdout.close)
        summary, _ = t.run(usage=lambda root: (0.99, 10**12))
        self.assertTrue((t.root / "inuse/node_modules/a.js").exists())
        self.assertTrue((t.root / "open/node_modules/a.js").exists())
        self.assertIn("2 in use", summary)

    def test_a_process_that_starts_after_the_choice_still_keeps_it(self):
        """The second scan, just before removal, is the one that counts."""
        t = Tree(self)
        t.file("late/x")
        t.age("late", 30)
        scans = []

        def scan(device, root):
            scans.append(1)
            return set() if len(scans) == 1 else {(t.root / "late/x").stat().st_ino}

        summary, lines = t.run(scan=scan)
        self.assertTrue((t.root / "late/x").exists())
        self.assertTrue(any("in use or changed since it was chosen" in l for l in lines))

    def test_symlinks_are_never_followed(self):
        t = Tree(self)
        t.file("linker/x")
        os.symlink(t.outside, t.root / "linker/out")
        os.symlink(t.outside / "precious", t.root / "linker/node_modules")
        (t.root / "linkdir").symlink_to(t.outside)
        t.age("linker", 30)
        t.run(usage=lambda root: (0.99, 10**12))
        self.assertFalse((t.root / "linker").exists())
        self.assertTrue((t.outside / "precious").exists())
        self.assertTrue((t.root / "linkdir").is_symlink())  # a top-level symlink is not a task directory

    def test_top_level_files_and_lost_found_stay(self):
        t = Tree(self)
        t.file("big.log")
        t.file("lost+found/x")
        t.age("lost+found", 60)
        os.utime(t.root / "big.log", ns=(t.now - 60 * DAY,) * 2)
        t.run(usage=lambda root: (0.99, 10**12))
        self.assertTrue((t.root / "big.log").exists())
        self.assertTrue((t.root / "lost+found/x").exists())

    def test_pressure_removes_oldest_first_and_stops_under_seventy_percent(self):
        t = Tree(self)
        for name, days in (("a", 6), ("b", 5), ("c", 4), ("d", 3), ("e", 2)):
            t.file(f"{name}/f")
            t.age(name, days)
        t.file("today/f")
        t.age("today", 0.5)

        def usage(root):
            # 45% plus 10% for each idle directory's file still on the volume, the trash included: 95% to start.
            left = [f for _, _, files in os.walk(root) for f in files if f == "f"]
            return 0.35 + 0.10 * len(left), 10**12  # "today/f" is the sixth

        summary, lines = t.run(usage=usage)
        self.assertEqual(sorted(p.name for p in t.root.iterdir()), [".pbox-clean.log", "d", "e", "today"])
        self.assertIn("3 under pressure", summary)
        self.assertIn("use 95% -> 65%", summary)
        order = [re.search(r'/(\w)"', l)[1] for l in lines if "under pressure " in l]
        self.assertEqual(order, ["a", "b", "c"])

    def test_no_pressure_below_eighty_five_percent(self):
        t = Tree(self)
        t.file("a/f")
        t.age("a", 6)
        t.run(usage=lambda root: (0.84, 10**12))
        self.assertTrue((t.root / "a/f").exists())

    def test_pressure_never_takes_a_recent_or_kept_directory(self):
        t = Tree(self)
        t.file("recent/f")
        t.age("recent", 0.5)
        t.file("kept/.pbox-keep", b"")
        t.age("kept", 6)
        summary, _ = t.run(usage=lambda root: (0.99, 10**12))
        self.assertTrue((t.root / "recent/f").exists())
        self.assertTrue((t.root / "kept/.pbox-keep").exists())

    def test_a_deep_recent_file_keeps_a_directory_even_in_a_dry_run_under_pressure(self):
        t = Tree(self)
        t.file("deep/a/b/c/f")
        t.age("deep", 5)
        t.touch("deep/a/b/c/f", 3)
        summary, lines = t.run(apply=False, usage=lambda root: (0.99, 10**12))
        self.assertEqual([l for l in lines if "would remove" in l], [])
        self.assertIn("1 recent", summary)

    def test_a_dry_run_removes_nothing(self):
        t = Tree(self)
        t.file("old/node_modules/a.js")
        t.age("old", 30)
        summary, lines = t.run(apply=False, usage=lambda root: (0.99, 10**12))
        self.assertTrue((t.root / "old/node_modules/a.js").exists())
        self.assertFalse((t.root / clean.LOG).exists())
        self.assertIn("would free", summary)

    def test_the_log_holds_the_last_summary(self):
        t = Tree(self)
        t.file("old/x")
        t.age("old", 30)
        summary, _ = t.run()
        t.run()
        log = (t.root / clean.LOG).read_text()
        self.assertEqual(log.count("\n"), 1)
        self.assertIn("freed 0 bytes", log)
        self.assertRegex(summary, r"freed [1-9][0-9]* bytes")

    def test_an_interrupted_run_s_trash_is_emptied(self):
        t = Tree(self)
        t.file(f"{clean.TRASH}/123-abc/x")
        t.run()
        self.assertFalse((t.root / clean.TRASH).exists())

    def test_a_mount_inside_keeps_the_directory(self):
        t = Tree(self)
        t.file("mnt/x")
        t.age("mnt", 30)
        real_stat = os.stat

        def fake_stat(name, *args, dir_fd=None, follow_symlinks=True):
            info = real_stat(name, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            if name == "x" and dir_fd is not None:
                values = list(info)
                values[stat.ST_DEV] = info.st_dev + 1
                return os.stat_result(values)
            return info

        clean.os.stat = fake_stat
        self.addCleanup(setattr, clean.os, "stat", real_stat)
        summary, _ = t.run(usage=lambda root: (0.99, 10**12))
        self.assertTrue((t.root / "mnt/x").exists())
        self.assertIn("1 unreadable or mounted", summary)


class Volume(unittest.TestCase):
    def test_refuses_anything_but_a_separate_mount(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(clean.work_volume(Path(d) / "absent"))
            self.assertIsNone(clean.work_volume(Path(d)))  # not a mount point: skipped, nothing walked
        with self.assertRaises(clean.Unsafe):
            clean.work_volume(Path("/"))  # a mount point, but the root filesystem

    def test_main_requires_root(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        result = subprocess.run([sys.executable, str(SCRIPT), "--apply"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)

    def test_mountinfo_paths_are_unescaped(self):
        rows = clean.mounts("36 35 259:1 /a\\040b /mnt/work rw,noatime shared:1 - ext4 /dev/nvme1n1 rw\n")
        self.assertEqual(rows, [("259:1", "/a b", "/mnt/work", "ext4")])


class Rules(unittest.TestCase):
    """The safety rules, pinned: weakening one has to change this file too."""

    def test_thresholds(self):
        self.assertEqual(clean.ROOT, Path("/mnt/work"))
        self.assertEqual(clean.RECENT, 24 * HOUR)
        self.assertEqual(clean.CACHE_IDLE, 2 * DAY)
        self.assertEqual(clean.TASK_IDLE, 7 * DAY)
        self.assertEqual((clean.HIGH, clean.LOW), (0.85, 0.70))
        self.assertEqual(clean.KEEP, ".pbox-keep")
        self.assertIn("lost+found", clean.RESERVED_TOP)

    def test_guards_stay_in_the_script(self):
        text = SCRIPT.read_text()
        for guard in ('os.fwalk(path, follow_symlinks=False', 'follow_symlinks=False)', 'entry.st_dev != device',
                      'entry.st_ino in inuse', 'name == KEEP', '"--one-file-system"', 'os.O_NOFOLLOW',
                      'raise Recent', 'info.st_ino != expected_ino', 'os.utime(parent, ns=',
                      'f"{proc}/cwd", f"{proc}/root", f"{proc}/exe"', 'f"{proc}/maps"', '/usr/sbin/losetup',
                      'kinds[-1] != "ext4"', 'is on the root filesystem', 'def recheck('):
            self.assertIn(guard, text)
        # Removal is a rename into the trash after recheck, and rm runs only on trash entries.
        self.assertEqual(text.count('"/usr/bin/rm"'), 1)
        self.assertIn("os.path.join(root, TRASH, name)", text)


class Installer(unittest.TestCase):
    def test_installer_is_valid_shell_and_pins_the_script(self):
        text = render_installer()
        subprocess.run(["sh", "-n"], input=text, text=True, check=True)
        digest = hashlib.sha256(SCRIPT.read_bytes()).hexdigest()
        self.assertEqual(text.count(digest), 2)  # the install check and the entry point's check
        self.assertIsNone(re.search(r"\b(apt|apt-get|dpkg|snap)\b", text))
        self.assertEqual(re.findall(r"systemctl (\S+)", text),
                         ["daemon-reload", "enable", "is-enabled", "is-active", "show"])

    def test_units(self):
        text = render_installer()
        service, timer, entry = heredoc(text, "UNIT"), text.split("cat > \"$d/timer\" <<'UNIT'\n", 1)[1], \
            heredoc(text, "ENTRY")
        for line in ("Type=oneshot", "ExecStart=/usr/local/sbin/pbox-work-clean --apply", "Nice=15",
                     "IOSchedulingClass=idle", "NoNewPrivileges=yes", "TimeoutStartSec=45min",
                     "ConditionPathIsMountPoint=/mnt/work", "ProtectSystem=strict", "ProtectHome=read-only",
                     "PrivateTmp=yes", "ReadWritePaths=-/mnt/work /run/lock"):
            self.assertIn(line + "\n", service + "\n")
        self.assertIn("OnBootSec=10min\n", timer)
        self.assertIn("OnUnitInactiveSec=1h\n", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn("flock --close --nonblock --conflict-exit-code 0 /run/lock/pbox-work-clean.lock", entry)
        self.assertIn("exec 9>/run/lock/pbox-work-clean.lock", text)
        self.assertIn("/usr/bin/python3 -I /usr/local/lib/pbox-work-clean/clean.py", entry)
        # The lock is the only writable path outside the work volume, and the installer creates it there.
        self.assertTrue(text.index("exec 9>/run/lock") < text.index("systemctl enable --now pbox-work-clean.timer"))

    def test_every_pbox_name_in_every_pbox_region(self):
        self.assertIn("concat(local.parallel_box_names, local.member_pbox_names)", TF)
        self.assertIn("chunklist(local.pbox_work_clean_names, 5)", TF)
        blocks = re.findall(r'^resource "aws_ssm_association" "(\w+)" \{\n(.*?)^\}\n', TF, re.S | re.M)
        providers = {name: (re.search(r"provider += (\S+)", body) or [None, "default"])[1] for name, body in blocks}
        self.assertEqual(providers, {"pbox_work_clean_ohio": "aws.ohio", "pbox_work_clean_east1": "aws.dr",
                                     "pbox_work_clean_west1": "default", "pbox_work_clean_west2": "aws.west2"})
        for _, body in blocks:
            self.assertIn("for_each            = local.pbox_work_clean_groups", body)
            self.assertIn('key    = "tag:Name"', body)
            self.assertIn("values = each.value", body)
            self.assertIn("parameters = local.pbox_work_clean_parameters", body)
        # The fallback sites are these regions (pbox-fallback.tf).
        fallback = (ROOT / "pbox-fallback.tf").read_text()
        for region in ("us-east-1", "us-east-2", "us-west-1", "us-west-2"):
            self.assertIn(f'"{region}" = {{ cidr', fallback)
        self.assertIn("length(local.pbox_work_clean_install) < 60000", TF)

    def test_payload_fits_the_ssm_budget(self):
        self.assertLess(len(render_installer()), 60000)

    def test_agents_are_told(self):
        self.assertIn(".pbox-keep", (ROOT / "scripts" / "parallel-box.sh").read_text())
        self.assertIn(".pbox-keep", (ROOT / "scripts" / "nextjs-pbox-policy.md").read_text())
        self.assertIn("pbox-work-clean.tf", (ROOT / "README.md").read_text())
        self.assertIn("pbox-work-clean.tf", (ROOT / "AGENTS.md").read_text())


if __name__ == "__main__":
    unittest.main(buffer=True)

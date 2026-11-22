#!/usr/bin/env python3
"""Offline Scratch cleanup with real directory trees and simulated disk pressure."""

from contextlib import ExitStack
import datetime
import fcntl
import importlib.machinery
import importlib.util
import os
from pathlib import Path
import plistlib
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

loader = importlib.machinery.SourceFileLoader("cleaner", str(Path(__file__).parent / "mac/bin/mac-artifact-clean"))
spec = importlib.util.spec_from_loader(loader.name, loader)
cleaner = importlib.util.module_from_spec(spec)
loader.exec_module(cleaner)


class Cleanup(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.volume = Path(tmp.name) / "Scratch"
        self.state = Path(tmp.name) / "state"
        self.volume.mkdir()
        self.state.mkdir()
        self.pin = self.state / "volume-uuid"
        self.pin.write_text("original")
        pin = patch.object(cleaner, "PIN", self.pin)
        pin.start()
        self.addCleanup(pin.stop)
        self.device = self.volume.stat().st_dev
        self.start = time.time()
        self.old = self.start - 18 * 86400

    def cache(self, name, when=None):
        root = self.volume / name
        leaf = root / "Build" / "Intermediates.noindex"
        leaf.mkdir(parents=True)
        obj = leaf / "nested" / "temp.o"
        obj.parent.mkdir()
        obj.write_text("rebuild me")
        logdir = root / "Logs/Build"
        logdir.mkdir(parents=True)
        when = self.old if when is None else when
        info = {
            "LastAccessedDate": datetime.datetime.fromtimestamp(when, datetime.timezone.utc).replace(tzinfo=None),
            "WorkspacePath": str(self.volume / Path(name).parts[0] / "Example.xcodeproj"),
        }
        (root / "info.plist").write_bytes(plistlib.dumps(info))
        log = {
            "logs": {
                "123": {
                    "timeStartedRecording": when - cleaner.APPLE_EPOCH - 42,
                    "timeStoppedRecording": when - cleaner.APPLE_EPOCH,
                }
            }
        }
        (logdir / "LogStoreManifest.plist").write_bytes(plistlib.dumps(log))
        (root / "Build/Products").mkdir()
        for p in [*root.rglob("*"), root]:
            os.utime(p, (when, when))
        return root, leaf

    def clean(self, apply, free=None, busy=None):
        result = {"paths": []}
        free = free if free is not None else lambda _: 1
        with (
            patch.object(os.path, "ismount", return_value=True),
            patch.object(cleaner, "space", free),
            patch.object(cleaner, "active", busy or (lambda *args: False)),
            patch.object(cleaner, "volume_id", return_value="original"),
        ):
            cleaner.clean(self.volume, self.state, apply, result)
        return result

    def test_oldest_first_until_real_space_returns_products_and_manifests_remain(self):
        new, second = self.cache("dolphin-browser/dd-ui")
        old, first = self.cache("gww/apps/native/build/.derived", self.old - 300)
        self.cache("other/build/ios-dd", self.old + 10)
        result = self.clean(True, lambda _: 1 if first.exists() else cleaner.TARGET)
        self.assertEqual(result["paths"], [str(first)])
        self.assertEqual(result["status"], "target reached")
        for p in [old / "info.plist", old / "Build/Products", old / "Logs", second, new]:
            self.assertTrue(p.exists())
        self.assertFalse((self.state / "pending.json").exists())

    def test_active_recent_or_unverified_is_held_and_preview_does_not_delete(self):
        busy, busy_leaf = self.cache("project/dd-busy")
        _, fresh_leaf = self.cache("project/dd-fresh")
        (fresh_leaf / "nested/temp.o").touch()
        bad, bad_leaf = self.cache("project/dd-no-proof")
        (bad / "Logs/Build/LogStoreManifest.plist").write_bytes(plistlib.dumps({"logs": {}}))
        _, eligible = self.cache("project/dd-eligible")

        def check_busy(root, workspace):
            return root == busy

        self.assertEqual(self.clean(False, busy=check_busy)["paths"], [str(eligible)])
        self.assertTrue(eligible.exists())
        self.assertEqual(self.clean(True, busy=check_busy)["paths"], [str(eligible)])
        self.assertTrue(all(p.exists() for p in (busy_leaf, fresh_leaf, bad_leaf)))

    def test_discovery_filters_project_children_by_name_before_requesting_metadata(self):
        root, leaf = self.cache("project/dd-old")
        unrelated = root.parent / "renders"
        unrelated.mkdir()
        (root.parent / "swift-test.log").touch()
        with patch.object(cleaner, "directory", wraps=cleaner.directory) as check:
            rows = cleaner.candidates(self.volume, self.device, self.start - cleaner.AGE)
        self.assertIn(leaf, [r / c for _, r, _, c in rows])
        checked = {call.args[0] for call in check.call_args_list}
        self.assertTrue({unrelated, root.parent / "swift-test.log"}.isdisjoint(checked))

    def test_no_walk_above_watermark_or_with_scratch_unmounted(self):
        with patch.object(cleaner, "candidates", side_effect=AssertionError("unnecessary scan")):
            self.assertEqual(self.clean(True, lambda _: cleaner.LOW)["status"], "healthy")
        with (
            patch.object(os.path, "ismount", return_value=False),
            patch.object(cleaner, "space", side_effect=AssertionError),
        ):
            result = {}
            cleaner.clean(self.volume, self.state, True, result)
            self.assertEqual(result["status"], "volume unavailable")

    def test_signing_assets_hold_the_entire_cache(self):
        retained = []
        for suffix in (
            "p8",
            "P12",
            "pfx",
            "keychain",
            "keychain-db",
            "mobileprovision",
            "provisionprofile",
            "pem",
            "key",
            "cer",
        ):
            _, leaf = self.cache("project/dd-" + suffix)
            signing_file = leaf / "nested" / ("signing." + suffix)
            signing_file.write_bytes(b"test fixture")
            for path in (signing_file, signing_file.parent):
                os.utime(path, (self.old, self.old))
            retained.append(signing_file)
        self.assertEqual(self.clean(True)["paths"], [])
        for path in retained:
            self.assertEqual(path.read_bytes(), b"test fixture")

    def test_no_follow_symlinks_or_enter_project_source_archives_or_vm_paths(self):
        _, target = self.cache(".tart-kidsports/vms")
        _, temp = self.cache("tmp/build")
        _, archive = self.cache("p/dd")
        (archive / "Export.xcarchive").mkdir()
        _, copied = self.cache("root/dd-home")
        outside = self.volume.parent / "outside"
        outside.mkdir()
        (outside / "important").write_text("keep")
        (copied / "external").symlink_to(outside, target_is_directory=True)
        old = self.old
        os.utime(copied / "external", (old, old), follow_symlinks=False)
        os.utime(copied, (old, old))
        (self.volume / "not-a-project").symlink_to(self.volume / ".tart-kidsports", target_is_directory=True)
        self.assertEqual(self.clean(True)["paths"], [str(copied)])
        self.assertTrue(all(p.exists() for p in (target, temp, archive, outside / "important")))

    def test_process_check_excludes_own_fd_and_detects_other_holders_and_build_args(self):
        root = Path("/Volumes/Scratch/app/dd")
        workspace = Path("/Volumes/Scratch/app/Example.xcodeproj")

        def busy(other, argv=b""):
            own = b"p%d\0n" % os.getpid() + os.fsencode(root / "Build") + b"\0"
            out = subprocess.CompletedProcess([], 0, stdout=own + b"\np7\0n" + other + b"\0", stderr=b"")
            with (
                patch.object(subprocess, "run", return_value=out),
                patch.object(subprocess, "check_output", return_value=argv),
            ):
                return cleaner.active(root, workspace)

        self.assertFalse(busy(b"/Volumes/Scratch/app/dd-extra/Build"))
        self.assertTrue(busy(os.fsencode(root / "Build/xyz")))
        self.assertTrue(busy(b"/dev/null", b"xcodebuild -workspace " + os.fsencode(workspace)))

    def test_interrupted_detach_finishes_original_inode_leaving_new_build_untouched(self):
        old, leaf = self.cache("DerivedData/GWW")
        journal = self.state / "pending.json"
        with patch.object(cleaner, "finish", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.clean(True)
        self.assertTrue(journal.exists())
        leaf.mkdir()
        marker = leaf / "new-build"
        marker.touch()
        self.assertEqual(self.clean(True, lambda _: cleaner.TARGET)["status"], "healthy")
        self.assertTrue(journal.exists())
        self.clean(True)
        self.assertTrue(marker.exists())
        self.assertFalse((old / "Build" / cleaner.PENDING).exists())

    def test_replacement_volume_or_held_build_slot_blocks_deletion(self):
        _, leaf = self.cache("p/dd")
        self.pin.write_text("other disk")
        self.assertEqual(self.clean(True)["status"], "unrecognized Scratch volume")
        self.pin.write_text("original")
        with cleaner.build_slots(self.state) as locked:
            self.assertTrue(locked)
            self.assertEqual(self.clean(True)["status"], "host build active")
        self.assertTrue(leaf.exists())

    def test_real_build_in_a_new_third_slot_respects_cleanup_across_state_directories(self):
        home = self.volume.parent / "home"
        self.state = home / ".local/state/mac-artifact-clean"
        self.state.mkdir(parents=True)
        custom = self.volume.parent / "custom"
        slots = custom / "mac-build"
        slots.mkdir(parents=True)
        _, leaf = self.cache("project/dd")
        env = dict(os.environ, HOME=str(home), MAC_STATE_DIR=str(custom), MAC_BUILD_SLOTS="3")
        env.pop("MAC_BUILD_SLOT", None)
        build = Path(__file__).parent / "mac/bin/mac-build"
        child = "import os, sys; print(os.environ['MAC_BUILD_SLOT'], flush=True); sys.stdin.read(1)"
        with (slots / "slot.1").open("w") as first, (slots / "slot.2").open("w") as second:
            for slot in (first, second):
                fcntl.flock(slot, fcntl.LOCK_EX)
            with ExitStack() as processes:
                with cleaner.build_slots(self.state) as clean_idle:
                    self.assertTrue(clean_idle)
                    p = subprocess.Popen(
                        [build, sys.executable, "-c", child],
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        env=env,
                    )
                    processes.callback(p.communicate, b"x", timeout=5)
                    self.assertFalse(select.select([p.stdout], [], [], 1)[0], "build started during cleanup")
                self.assertTrue(select.select([p.stdout], [], [], 5)[0])
                self.assertEqual(p.stdout.readline(), b"3\n")
                self.assertEqual(self.clean(True)["status"], "host build active")
                self.assertTrue(leaf.exists())
            self.assertEqual(p.returncode, 0)
            with patch.dict(os.environ, {"MAC_STATE_DIR": str(custom)}):
                self.assertEqual(
                    self.clean(True)["status"], "host build active", "legacy custom slots remain protected"
                )
        self.assertEqual(self.clean(True)["paths"], [str(leaf)])

    def test_interrupted_pressure_cycle_continues_above_low_watermark(self):
        _, leaf = self.cache("p/dd")
        with cleaner.build_slots(self.state):
            self.assertEqual(self.clean(True)["status"], "host build active")
        result = self.clean(True, lambda _: cleaner.LOW + 10 if leaf.exists() else cleaner.TARGET)
        self.assertEqual(result["paths"], [str(leaf)])
        self.assertFalse((self.state / "pressure").exists())

    def test_explicit_sdk_cache_requires_a_flat_directory_of_old_compiled_files(self):
        valid, _ = self.cache("project/dd-releasetest")
        sdk = valid / "SDKExplicitPrecompiledModules"
        sdk.mkdir()
        for name in ("Foundation-ABC.pcm", "UIKit.swiftmodule", "modules.timestamp"):
            f = sdk / name
            f.write_text("compiled output")
            os.utime(f, (self.old, self.old))
        os.utime(sdk, (self.old, self.old))

        held = []
        for name in ("notes.txt", "nested.pcm", "alias.pcm", "recent.swiftmodule"):
            root, _ = self.cache("project/dd-" + name)
            folder = root / sdk.name
            folder.mkdir()
            entry = folder / name
            if name == "nested.pcm":
                entry.mkdir()
            elif name == "alias.pcm":
                entry.symlink_to(sdk / "Foundation-ABC.pcm")
            else:
                entry.write_text("keep")
            if name != "recent.swiftmodule":
                os.utime(entry, (self.old, self.old), follow_symlinks=False)
            os.utime(folder, (self.old, self.old))
            held.append(entry)

        self.assertIn(str(sdk), self.clean(False)["paths"])
        self.assertTrue(sdk.exists())
        self.assertIn(str(sdk), self.clean(True)["paths"])
        self.assertFalse(sdk.exists())
        self.assertTrue(all(os.path.lexists(entry) for entry in held))

    def test_generated_index_and_compiler_cache_but_not_dependency_checkouts(self):
        root, leaf = self.cache("p/dd")
        for name in ("Index.noindex", "ModuleCache.noindex", "SourcePackages"):
            artifact = root / name
            artifact.mkdir()
            f = artifact / "item"
            f.write_text("content")
            os.utime(f, (self.old, self.old))
            os.utime(artifact, (self.old, self.old))
        result = self.clean(True)
        self.assertEqual(
            set(result["paths"]), {str(leaf), str(root / "Index.noindex"), str(root / "ModuleCache.noindex")}
        )
        self.assertTrue((root / "SourcePackages/item").exists())

    def test_copied_build_metadata_inside_backup_is_kept(self):
        root, original = self.cache("real/dd")
        import shutil

        backup = self.volume / "backups" / "dd"
        shutil.copytree(root, backup)
        self.clean(True)
        self.assertFalse(original.exists())
        self.assertTrue((backup / "Build" / "Intermediates.noindex").exists())

    def test_preexisting_quarantine_is_never_taken_or_replaced(self):
        root, leaf = self.cache("project/dd-old")
        (root / "Build" / cleaner.PENDING).mkdir()
        self.assertEqual(self.clean(True)["paths"], [])
        self.assertTrue(leaf.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

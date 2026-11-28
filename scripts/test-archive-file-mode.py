#!/usr/bin/env python3
"""Every archive_file that zips files from disk pins their mode, so a Lambda's hash does not depend on the checkout.

The archive provider stores each file's permission bits in the zip, and Lambda's source_code_hash is the zip's
hash. On 2026-11-28 a drift check run from a checkout made under umask 022 (files 0644) reported 16 Lambda functions
and an alias as changed, while the same commit checked out under umask 002 (0664, as the applies use) planned
nothing: same code, different hash. output_file_mode = "0664" makes every checkout build the same zip, the one
already deployed. Archives built only from inline `source { content }` blocks are left alone: their entries
already get a fixed mode, so they never drifted, and pinning them would change their hashes for nothing.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def archives():
    for path in sorted(ROOT.glob("*.tf")) + sorted((ROOT / "modules").rglob("*.tf")):
        text = path.read_text()
        for m in re.finditer(r'data "archive_file" "([^"]+)" \{\n', text):
            end = text.index("\n}\n", m.end())
            yield path.relative_to(ROOT), m.group(1), text[m.end():end]


class ArchiveFileMode(unittest.TestCase):
    def test_file_based_archives_pin_the_mode_the_deployed_zips_have(self):
        checked = 0
        for path, name, block in archives():
            if not re.search(r'^\s*source_(file|dir)\s*=', block, re.M):
                continue
            checked += 1
            with self.subTest(archive=f"{path}:{name}"):
                self.assertRegex(block, r'(?m)^\s*output_file_mode\s*=\s*"0664"\s*$')
        self.assertGreater(checked, 0, "no file-based archive_file found: the parser no longer matches")

    def test_content_only_archives_are_left_alone(self):
        for path, name, block in archives():
            if re.search(r'^\s*source_(file|dir)\s*=', block, re.M):
                continue
            with self.subTest(archive=f"{path}:{name}"):
                self.assertNotIn("output_file_mode", block)


if __name__ == "__main__":
    unittest.main(buffer=True)

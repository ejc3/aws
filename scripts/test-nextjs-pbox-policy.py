#!/usr/bin/env python3
"""Test the pbox policy's file updates without talking to the live guests."""
import importlib.util
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("nextjs_pbox_policy", HERE / "nextjs-pbox-policy.py")
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)
CONTENT = (HERE / "nextjs-pbox-policy.md").read_bytes()


class PolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name).resolve()

    def test_prepend_preserves_private_bytes_mode_and_owner(self):
        path = self.home / "AGENTS.md"
        private = b"private\x80 characters\r\n## Last line has no newline"
        path.write_bytes(private)
        path.chmod(0o640)
        old = path.stat()
        self.assertEqual(policy.install_policy(path, CONTENT), "updated")
        self.assertEqual(path.read_bytes(), policy.BEGIN + CONTENT.rstrip() + b"\n" + policy.END + b"\n" + private)
        new = path.stat()
        self.assertEqual((new.st_uid, new.st_gid, new.st_mode), (old.st_uid, old.st_gid, old.st_mode))
        self.assertEqual(policy.install_policy(path, CONTENT), "unchanged")
        self.assertEqual(path.stat().st_ino, new.st_ino)

    def test_revising_our_section_does_not_touch_either_adjacent_section(self):
        prefix, suffix = b"private heading\n\n", b"\nprivate footer\xff"
        previous = prefix + policy.BEGIN + b"old managed text\n" + policy.END + suffix
        self.assertEqual(policy.render(previous, b"new managed text"),
                         prefix + policy.BEGIN + b"new managed text\n" + policy.END + suffix)

    def test_ambiguous_marker_does_not_replace_a_private_file(self):
        path = self.home / "AGENTS.md"
        private = policy.BEGIN + b"old block that was never closed\n"
        path.write_bytes(private)
        with self.assertRaisesRegex(ValueError, "incomplete"):
            policy.install_policy(path, CONTENT)
        self.assertEqual(path.read_bytes(), private)

    def test_oversize_private_instructions_are_left_alone(self):
        path = self.home / "AGENTS.md"
        for size in (policy.MAX_INSTRUCTIONS_BYTES, policy.MAX_INSTRUCTIONS_BYTES + 1):
            with self.subTest(size=size):
                private = b"x" * size
                path.write_bytes(private)
                before = path.stat()
                with self.assertRaisesRegex(ValueError, "safety limit"):
                    policy.install_policy(path, CONTENT)
                self.assertEqual(path.stat().st_ino, before.st_ino)
                self.assertEqual(path.read_bytes(), private)

    def test_member_default_is_private_and_overrides_are_respected(self):
        path = policy.effective_codex_file(self.home, create_directory=True)
        self.assertEqual(path.name, "AGENTS.md")
        self.assertEqual(policy.install_policy(path, CONTENT, create=True), "updated")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        override = path.with_name("AGENTS.override.md")
        override.write_text("Private override")
        self.assertEqual(policy.effective_codex_file(self.home), override)

    def test_instructions_file_and_directory_links_are_not_followed(self):
        elsewhere = self.home / "secret"
        elsewhere.write_text("private")
        link = self.home / "AGENTS.md"
        link.symlink_to(elsewhere)
        with self.assertRaises(OSError):
            policy.install_policy(link, CONTENT)
        self.assertEqual(elsewhere.read_text(), "private")
        self.assertTrue(link.is_symlink())
        (self.home / ".codex").symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "directory link"):
            policy.install_policy(self.home / ".codex" / "AGENTS.md", CONTENT)

    def test_owner_check_never_creates_or_writes_an_instructions_file(self):
        self.assertEqual(policy.owner_status(self.home), "pending central owner instructions")
        self.assertFalse((self.home / ".codex").exists())
        path = policy.effective_codex_file(self.home, create_directory=True).with_name("AGENTS.override.md")
        before = policy.OWNER_POLICY + b"\n\n**RAM comes first.**\nUse pbox up 3 memory-32 and pbox down 3.\n"
        path.write_bytes(before)
        inode = path.stat().st_ino
        self.assertEqual(policy.owner_status(self.home), "supplied by the central owner instructions")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(path.stat().st_ino, inode)


if __name__ == "__main__":
    unittest.main(buffer=True)

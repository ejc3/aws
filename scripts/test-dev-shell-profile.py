#!/usr/bin/env python3
"""dev-user-data.tf: the shell profile it writes for the dev box user exports no cargo target
directory. One target shared by every checkout lets cargo run a sibling worktree's test binary
(ejc3/fcvm CLAUDE.md, "Never share CARGO_TARGET_DIR across worktrees"). Offline."""
import re
import unittest
from pathlib import Path

TF = (Path(__file__).resolve().parent.parent / "dev-user-data.tf").read_text()


class DevShellProfileTests(unittest.TestCase):
    def test_the_profile_exports_no_shared_cargo_target(self):
        m = re.search(r"cat > ~/\.zshrc << 'ZSH'\n(.*?)\nZSH\n", TF, re.S)
        self.assertIsNotNone(m, "the ~/.zshrc heredoc is gone: update this test with where the profile is written")
        exports = [line for line in m.group(1).splitlines()
                   if "CARGO_TARGET_DIR" in line and not line.lstrip().startswith("#")]
        self.assertEqual(exports, [])


if __name__ == "__main__":
    unittest.main(buffer=True)

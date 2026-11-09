#!/usr/bin/env python3
"""Offline tests for the dev box's fcvm host kernel install (ejc3/fcvm#659)."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "firecracker-dev.tf").read_text()
SCRIPT = ROOT / "scripts/fcvm-host-kernel-install.sh"


def block(header):
    start = TF.index(header)
    return TF[start:TF.index("\n}\n", start) + 2]


class HostKernelTests(unittest.TestCase):
    def test_the_association_installs_the_pinned_commit_once_and_never_reboots(self):
        assoc = block('resource "aws_ssm_association" "firecracker_dev_host_kernel"')
        self.assertNotIn("schedule_expression", assoc, "a schedule would rebuild the kernel on a timer")
        self.assertIn("${var.fcvm_host_kernel_commit}", assoc)
        self.assertIn('file("${path.module}/scripts/fcvm-host-kernel-install.sh")', assoc)
        commit = re.search(r'variable "fcvm_host_kernel_commit" \{.*?default\s+= "([0-9a-f]+)"', TF, re.S).group(1)
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        text = SCRIPT.read_text()
        self.assertNotRegex(text, r"(?m)^[^#]*\breboot\b", "the box is shared: the owner picks the reboot")
        self.assertIn("make install-host-kernel", text)

    def test_the_script_refuses_a_short_commit(self):
        run = subprocess.run(["bash", str(SCRIPT), "a5a71329"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertIn("full fcvm commit", run.stderr)


if __name__ == "__main__":
    unittest.main()

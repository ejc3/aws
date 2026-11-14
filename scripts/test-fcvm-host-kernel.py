#!/usr/bin/env python3
"""Offline tests for the dev box's fcvm host kernel install (ejc3/fcvm#659)."""
import os
import re
import subprocess
import tempfile
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
        self.assertIn('can(regex("^[0-9a-f]{40}$", var.fcvm_host_kernel_commit))',
                      block('variable "fcvm_host_kernel_commit"'))
        text = SCRIPT.read_text()
        self.assertNotRegex(text, r"(?m)^[^#]*\breboot\b", "the box is shared: the owner picks the reboot")
        self.assertIn('env "FCVM_CONFIG_DIR=$scratch/config" make', text)

    def test_the_script_refuses_a_short_commit(self):
        run = subprocess.run(["bash", str(SCRIPT), "a5a71329"], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2, run.stderr)
        self.assertIn("full lowercase fcvm commit", run.stderr)


    def test_selection_proves_installed_exact_kernel_and_generated_grub(self):
        kernel = "7.3.3-fcvm-deadbeef"
        with tempfile.TemporaryDirectory() as scratch:
            tmp = Path(scratch)
            boot = tmp / "boot"
            (boot / "grub").mkdir(parents=True)
            bin_dir = tmp / "bin"
            bin_dir.mkdir()
            for name, code in {
                "dpkg-query": 'case "$3" in linux-image-7.3.3-fcvm-deadbeef) printf "%s\\n" "$TEST_STATUS";; *) exit 1;; esac',
                "grub-script-check": 'test -s "$1"',
                "grub-editenv": 'printf "%s\\n" "$TEST_GRUBENV"',
            }.items():
                command = bin_dir / name
                command.write_text("#!/bin/bash\n" + code + "\n")
                command.chmod(0o755)
            for name in [f"vmlinuz-{kernel}", f"initrd.img-{kernel}"]:
                (boot / name).write_text("file is present")
            default = tmp / "default"
            cfg = boot / "grub/grub.cfg"
            entry = f"Advanced options for Ubuntu>Ubuntu, with Linux {kernel}"
            default.write_text(f'GRUB_DEFAULT="{entry}"\n')

            def verify(status="installed", menu=entry, grubenv="", expected=kernel):
                cfg.write_text(f'   set default="{menu}"\n'
                               "submenu 'Advanced options for Ubuntu' {\n"
                               f"  menuentry 'Ubuntu, with Linux {kernel}' {{}}\n}}\n")
                return subprocess.run(
                    ["bash", "-c", 'source "$1"; verify_kernel_selection "$4" "$2" "$3"',
                     "bash", str(SCRIPT), str(boot), str(default), expected],
                    env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                         "TEST_STATUS": status, "TEST_GRUBENV": grubenv},
                    capture_output=True, text=True,
                )

            valid = verify()
            self.assertEqual(valid.returncode, 0, valid.stderr)
            self.assertEqual(valid.stdout.strip(), kernel)
            self.assertNotEqual(verify(status="config-files").returncode, 0)
            self.assertNotEqual(verify(menu=entry.replace(kernel, "7.3.3-fcvm-cafebabe")).returncode, 0)
            self.assertNotEqual(verify(grubenv="next_entry=old-kernel").returncode, 0)
            self.assertNotEqual(verify(expected="7.3.3-fcvm-cafebabe").returncode, 0)


if __name__ == "__main__":
    unittest.main(buffer=True)

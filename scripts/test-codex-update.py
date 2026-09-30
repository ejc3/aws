#!/usr/bin/env python3
"""codex-update.tf: the weekly Codex refresh on the ubuntu boxes. Offline.

Pins what makes it safe: it runs the installer as ubuntu (never as root), it never restarts a
daemon, it targets exactly the four ubuntu boxes (not nextjs-dev, which refreshes itself), and the
codex-restart it installs is the repo's script.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "codex-update.tf").read_text()
CODE = re.sub(r"(?m)^\s*#.*$", "", TF)   # comments may say "root" and "restart"; the code must not do them


class CodexUpdateTests(unittest.TestCase):
    def test_the_installer_runs_as_ubuntu_not_root(self):
        self.assertRegex(CODE, r"runuser -u ubuntu -- env HOME=/home/ubuntu [^\n]*\\\n\s+sh -c 'cd /home/ubuntu && curl -fsSL https://chatgpt.com/codex/install.sh \| sh'")
        self.assertNotRegex(CODE, r"curl[^\n]*\|\s*(sudo )?(ba)?sh(?!')", "no piping a download into a root shell")

    def test_it_refreshes_but_never_restarts(self):
        for word in ("systemctl", "codex-restart --restart", "remote-control stop", "kill", "reboot"):
            self.assertNotIn(word, CODE.replace("codex-restart --restart\"", ""), word)
        self.assertIn("codex-restart --restart", TF)   # only in the message telling a human what to do

    def test_it_targets_the_four_ubuntu_boxes_only(self):
        block = TF.split("codex_update_instances = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertEqual(re.findall(r"aws_instance\.(\w+)\[0\]\.id", block),
                         ["jumpbox", "jumpbox_2", "firecracker_dev", "x86_dev"])
        self.assertNotIn("nextjs", CODE)

    def test_it_is_a_weekly_association_with_one_at_a_time(self):
        assoc = TF.split('resource "aws_ssm_association" "codex_update"', 1)[1]
        self.assertIn('name                = "AWS-RunShellScript"', assoc)
        self.assertIn('schedule_expression = "cron(0 9 ? * MON *)"', assoc)
        self.assertIn('max_concurrency     = "1"', assoc)
        self.assertIn('key    = "InstanceIds"', assoc)

    def test_the_restart_helper_it_installs_is_the_repo_script(self):
        self.assertIn('base64encode(file("${path.module}/scripts/codex-restart.sh"))', TF)
        dev = (ROOT / "dev-selfupdate.tf").read_text()
        self.assertIn('base64encode(file("${path.module}/scripts/codex-restart.sh"))', dev)
        for name, count in (("dev-user-data.tf", 2), ("nextjs-user-data.tf", 1)):
            self.assertEqual((ROOT / name).read_text().count("${local.codex_restart_setup}"), count, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)

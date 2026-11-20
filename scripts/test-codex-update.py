#!/usr/bin/env python3
"""codex-update.tf: the weekly Codex refresh on the ubuntu boxes. Offline.

Pins what makes it safe: it runs the installer as ubuntu (never as root), it never restarts a
daemon, it targets exactly the four ubuntu boxes (not nextjs-dev, which refreshes itself), and the
codex-restart it installs is the repo's script.
"""
import os
import re
import subprocess
import tempfile
import textwrap
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
        block = TF.split("codex_update_instances = merge(", 1)[1].split("\n  )\n", 1)[0]
        targets = re.findall(r"var\.(\w+) \? \{ ([\w-]+) = aws_instance\.(\w+)\[0\]\.id \} : \{\}", block)
        self.assertEqual(targets, [
            ("enable_jumpbox", "jumpbox", "jumpbox"),
            ("enable_jumpbox_2", "jumpbox-2", "jumpbox_2"),
            ("enable_firecracker_instance", "fcvm-metal-arm", "firecracker_dev"),
            ("enable_x86_dev_instance", "fcvm-metal-x86", "x86_dev"),
        ])
        self.assertNotIn("nextjs", CODE)

    def test_each_host_has_an_independent_weekly_delivery(self):
        assoc = TF.split('resource "aws_ssm_association" "codex_update"', 1)[1]
        self.assertIn("for_each = local.codex_update_instances", assoc)
        self.assertIn('name                = "AWS-RunShellScript"', assoc)
        self.assertIn('schedule_expression = "cron(0 9 ? * MON *)"', assoc)
        self.assertIn('key    = "InstanceIds"', assoc)
        self.assertIn("values = [each.value]", assoc)
        self.assertIn('association_name    = each.key == "jumpbox" ? "codex-update" : "codex-update-${each.key}"', assoc)
        self.assertNotRegex(assoc, r"apply_only_at_cron_interval\s*=\s*true")

    def test_noninteractive_zsh_path_repair_only_appends_once_to_the_users_file(self):
        start = "runuser -u ubuntu -- env HOME=/home/ubuntu PATH=/usr/local/bin:/usr/bin:/bin sh -eu <<'CODEX_ZSH_PATH' || exit 1\n"
        setup = textwrap.dedent(TF.split(start, 1)[1].split("    CODEX_ZSH_PATH\n", 1)[0])
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            local = home / ".codex/packages/standalone/current/bin"
            local.mkdir(parents=True)
            (local / "codex").touch(mode=0o755)
            # Simulate a host resolving the obsolete CLI, even on CI images without zsh.
            (home / "zsh").write_text("#!/bin/sh\nexit 0\n")
            (home / "zsh").chmod(0o755)
            profile = home / ".zshenv"
            profile.write_text("# existing user settings\n")
            env = dict(os.environ, HOME=folder, PATH=f"{folder}:/usr/bin:/bin")
            for _ in range(2):
                subprocess.run(["sh", "-eu"], input=setup, text=True, env=env, check=True)
            saved = profile.read_text()
            self.assertTrue(saved.startswith("# existing user settings\n"))
            self.assertEqual(saved.count('path=("$HOME/.codex/packages/standalone/current/bin" $path)'), 1)

            # A custom working CLI also keeps its chosen PATH.
            (home / "zsh").write_text("#!/bin/sh\nexit 1\n")
            profile.write_text("# my CLI\n")
            subprocess.run(["sh", "-eu"], input=setup, text=True, env=env, check=True)
            self.assertEqual(profile.read_text(), "# my CLI\n")

    def test_the_restart_helper_it_installs_is_the_repo_script(self):
        self.assertIn('base64encode(file("${path.module}/scripts/codex-restart.sh"))', TF)
        dev = (ROOT / "dev-selfupdate.tf").read_text()
        self.assertIn('base64encode(file("${path.module}/scripts/codex-restart.sh"))', dev)
        for name, count in (("dev-user-data.tf", 2), ("nextjs-user-data.tf", 1)):
            self.assertEqual((ROOT / name).read_text().count("${local.codex_restart_setup}"), count, name)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

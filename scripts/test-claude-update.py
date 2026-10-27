#!/usr/bin/env python3
"""claude-update.tf: the daily Claude Code update on the ubuntu boxes. Offline.

The association's commands are rendered from the Terraform and run for real against a fake runuser and claude, with
ubuntu's home moved to a temp dir. Pins what makes it safe: it updates as ubuntu (never as root), without the disabled
updater, never restarts anything, and targets exactly the four ubuntu boxes (not nextjs-dev, whose containers update
themselves)."""
import os
import re
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "claude-update.tf").read_text()
CODE = re.sub(r"(?m)^\s*#.*$", "", TF)   # comments may say "root" and "restart"; the code must not do them
COMMANDS = textwrap.dedent(TF.split("claude_update_commands = <<-CMD\n", 1)[1].split("\n  CMD\n", 1)[0]).replace("$${", "${")


def fake(path, body):
    path.write_text("#!/bin/bash\n" + body)
    path.chmod(0o755)


class RunTests(unittest.TestCase):
    def run_commands(self, claude="2.1.296", update_ok=True):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(d)])
        home, bin_ = d / "ubuntu", d / "bin"
        (home / ".local/bin").mkdir(parents=True)
        bin_.mkdir()
        # runuser -u ubuntu -- CMD...: record who it ran as, then run CMD.
        fake(bin_ / "runuser", f'echo "runuser $2" >> {d}/log\nshift 3\nexec "$@"\n')
        if claude:
            (d / "version").write_text(claude)
            fake(home / ".local/bin/claude", f"""case "$1" in
  --version) echo "$(cat {d}/version) (Claude Code)" ;;
  update) echo "update DISABLE_AUTOUPDATER=${{DISABLE_AUTOUPDATER:-unset}} cwd=$PWD HOME=$HOME" >> {d}/log
          {'echo 2.1.311 > ' + str(d) + '/version' if update_ok else 'exit 1'} ;;
esac
""")
        script = COMMANDS.replace("/home/ubuntu", str(home))
        env = {"PATH": f"{bin_}:/usr/bin:/bin", "DISABLE_AUTOUPDATER": "1"}
        r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)
        log = (d / "log").read_text() if (d / "log").exists() else ""
        return r, log, home

    def test_it_updates_to_the_latest_as_ubuntu_without_the_disabled_updater(self):
        r, log, home = self.run_commands()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("claude: 2.1.296 (Claude Code) -> 2.1.311 (Claude Code).", r.stdout)
        self.assertIn("update DISABLE_AUTOUPDATER=unset cwd=%s HOME=%s" % (home, home), log)
        self.assertEqual(set(re.findall(r"runuser (\S+)", log)), {"ubuntu"}, "every step runs as ubuntu")

    def test_a_failed_update_fails_the_run(self):
        r, _, _ = self.run_commands(update_ok=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("claude update FAILED (was 2.1.296 (Claude Code))", r.stdout)

    def test_a_box_without_claude_is_reported_and_left_alone(self):
        r, log, _ = self.run_commands(claude=None)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("no native install", r.stdout)
        self.assertNotIn("update", log)


class TerraformTests(unittest.TestCase):
    def test_it_never_restarts_installs_or_runs_a_download(self):
        for word in ("systemctl", "kill", "reboot", "curl", "install.sh", "sudo"):
            self.assertNotIn(word, CODE, word)

    def test_it_targets_the_four_ubuntu_boxes_only(self):
        block = TF.split("claude_update_instances = concat(", 1)[1].split("\n  )\n", 1)[0]
        self.assertEqual(re.findall(r"aws_instance\.(\w+)\[0\]\.id", block), ["jumpbox", "jumpbox_2", "firecracker_dev", "x86_dev"])
        self.assertNotIn("nextjs", CODE)

    def test_it_is_a_daily_association(self):
        assoc = TF.split('resource "aws_ssm_association" "claude_update"', 1)[1]
        self.assertIn('name                = "AWS-RunShellScript"', assoc)
        self.assertIn('schedule_expression = "cron(0 8 * * ? *)"', assoc)
        self.assertIn('key    = "InstanceIds"', assoc)
        self.assertIn("commands         = local.claude_update_commands", assoc)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

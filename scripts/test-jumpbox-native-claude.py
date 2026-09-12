#!/usr/bin/env python3
"""Offline guards for native-first jumpbox Claude installation, never live bootstrap."""

from pathlib import Path
import subprocess
import unittest

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "jumpbox2-user-data.tf").read_text()
CLAUDE = SOURCE.split("# ---------------------------------------------------------------- claude code\n", 1)[1].split(
    "# ---------------------------------------------------------------- codex, with remote control", 1
)[0]


class JumpboxNativeClaudeTests(unittest.TestCase):
    def test_native_installer_runs_as_ubuntu_and_fails_closed(self):
        self.assertIn("sudo -u ubuntu -H bash -c 'set -o pipefail;", CLAUDE)
        self.assertIn('[ -x "$HOME/.local/bin/claude" ] || curl -fsSL https://claude.ai/install.sh | bash', CLAUDE)
        self.assertNotRegex(CLAUDE, r"(?m)^command -v claude")
        self.assertNotRegex(CLAUDE, r"npm\s+install")

    def test_cleanup_requires_working_native_binary_and_skips_lifecycle_scripts(self):
        gate = "if sudo -u ubuntu -H /home/ubuntu/.local/bin/claude --version >/dev/null 2>&1; then"
        self.assertIn(gate, CLAUDE)
        guarded = CLAUDE.split(gate, 1)[1].split("else", 1)[0]
        self.assertIn("npm uninstall -g --ignore-scripts @anthropic-ai/claude-code", guarded)
        self.assertIn("keeping the npm copy", CLAUDE.split(gate, 1)[1].split("else", 1)[1])

    def test_login_history_and_running_sessions_are_not_modified(self):
        for forbidden in (".credentials", "auth.json", "history", "claude login", "claude auth", "systemctl", "pkill", "killall", "rm "):
            commands = "\n".join(line for line in CLAUDE.splitlines() if not line.startswith("#"))
            self.assertNotIn(forbidden, commands)
        self.assertIn('export PATH=\\"\\$HOME/.local/bin:\\$PATH\\"', CLAUDE)

    def test_source_is_shell_syntax_valid(self):
        subprocess.run(["bash", "-n"], input=CLAUDE, text=True, check=True)

    def run_with_fake_installer(self, native_status):
        # Intercept every sudo/npm call: never invoke an installer, read a login,
        # or write a real user's shell configuration during these offline tests.
        harness = """
sudo() {
  if [ "$4" = /home/ubuntu/.local/bin/claude ]; then
    return NATIVE_STATUS
  fi
  return 0
}
claude_test_npm_calls=
npm() { claude_test_npm_calls="$*"; }
""".replace("NATIVE_STATUS", str(native_status))
        report = '\nif [ -n "$claude_test_npm_calls" ]; then printf "NPM:%s\\n" "$claude_test_npm_calls"; fi\n'
        return subprocess.run(["bash"], input=harness + CLAUDE + report, text=True,
                              capture_output=True, check=True).stdout

    def test_working_native_permits_only_the_legacy_package_removal(self):
        output = self.run_with_fake_installer(0)
        self.assertEqual(output.strip(), "NPM:uninstall -g --ignore-scripts @anthropic-ai/claude-code")

    def test_failed_native_check_preserves_legacy_package(self):
        output = self.run_with_fake_installer(1)
        self.assertNotIn("NPM:", output)
        self.assertIn("keeping the npm copy", output)

    def test_s3_publish_does_not_add_bootstrap_replay(self):
        instance = (ROOT / "jumpbox2.tf").read_text()
        self.assertRegex(instance, r"(?s)ignore_changes\s*=\s*\[.*?user_data,")
        self.assertRegex(SOURCE, r'(?s)resource "aws_s3_object" "jumpbox_2_user_data".*?content\s*=\s*local.jumpbox_2_user_data')
        for forbidden in ('resource "aws_ssm_association"', 'provisioner "', 'resource "null_resource"', 'resource "terraform_data"'):
            self.assertNotIn(forbidden, SOURCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)

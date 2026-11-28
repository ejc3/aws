#!/usr/bin/env python3
"""Exercise the per-account installer offline with a tiny release archive and real hash checks."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tarfile
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = (ROOT / "scripts/antigravity-install.sh").read_text()


class AntigravityInstall(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.cli = self.home / ".local/bin/agy"
        self.profile = self.home / ".zshenv"
        self.profile.write_text("# my default settings\n")
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{self.home}:/usr/bin:/bin",
                        ZDOTDIR=str(self.home), PLATFORM="Linux aarch64",
                        AGY_MODEL_GUIDE=(ROOT / "scripts/antigravity-models.md").read_text().strip())
        self.tool("uname", 'echo "$PLATFORM"')
        self.tool("id", 'echo 1001')
        self.tool("curl", '''while [ "$1" != "-o" ]; do shift; done
echo "$3" >> "$HOME/requests"
cp "$HOME/release.tgz" "$2"''')
        binary = self.tool("antigravity", 'test "$1" = --version && echo 1.5.18')
        self.release = self.home / "release.tgz"
        with tarfile.open(self.release, "w:gz") as archive:
            archive.add(binary, "antigravity", recursive=False)
        # Pin the same miniature archive for each OS in this offline copy.
        digest = hashlib.sha512(self.release.read_bytes()).hexdigest()
        self.script, count = re.subn(r"(?<=digest=)[0-9a-f]{128}\b", digest, INSTALLER)
        self.assertEqual(count, 4)

    def tool(self, name, body):
        path = self.home / name
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o700)
        return path

    def run_installer(self):
        return subprocess.run(["/bin/bash", "-s"], input=self.script, text=True, env=self.env, capture_output=True)

    def test_all_platforms_and_noninteractive_zsh(self):
        for platform, package in [
            ("Linux aarch64", "linux-arm/cli_linux_arm64"),
            ("Linux x86_64", "linux-x64/cli_linux_x64"),
            ("Darwin arm64", "darwin-arm/cli_mac_arm64"),
            ("Darwin x86_64", "darwin-x64/cli_mac_x64"),
        ]:
            with self.subTest(platform=platform):
                self.env["PLATFORM"] = platform
                result = self.run_installer()
                self.assertEqual(result.returncode, 0, result.stderr)
                url = (self.home / "requests").read_text().splitlines()[-1]
                self.assertTrue(url.startswith("https://storage.googleapis.com/antigravity-public/antigravity-cli/"))
                self.assertTrue(url.endswith(f"/{package}.tar.gz"), url)
                self.assertEqual(self.cli.stat().st_mode & 0o777, 0o700)
                self.assertEqual(subprocess.check_output(["zsh", "-c", "agy --version"], env=self.env, text=True).strip(),
                                 "1.5.18")
                self.cli.unlink()
        self.assertTrue(self.profile.read_text().startswith("# my default settings\n"))
        self.assertEqual(self.profile.read_text().count("$HOME/.local/bin/agy ]]"), 1)

    def test_corrupt_release_fails_before_replacing_an_existing_file(self):
        self.cli.parent.mkdir(parents=True)
        self.cli.write_text("previous")
        replacement = self.tool("different-build", 'echo 1.5.18')
        with tarfile.open(self.release, "w:gz") as archive:
            archive.add(replacement, "antigravity", recursive=False)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SHA-512 mismatch", result.stderr)
        self.assertEqual(self.cli.read_text(), "previous")
        self.assertEqual(list(self.cli.parent.iterdir()), [self.cli])

    def test_existing_self_updated_binary_needs_no_download(self):
        self.cli.parent.mkdir(parents=True)
        self.tool(".local/bin/agy", 'echo 2.0.0')
        for _ in range(2):
            result = self.run_installer()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("2.0.0", result.stdout)
        self.assertFalse((self.home / "requests").exists())
        rule = self.home / ".gemini/config/rules/ejc3-antigravity-models.md"
        self.assertEqual(rule.read_text(), "---\ntrigger: always_on\n---\n" + self.env["AGY_MODEL_GUIDE"] + "\n")

    def test_ssm_and_container_installs_drop_privileges_and_select_the_intended_users(self):
        ssm = (ROOT / "antigravity.tf").read_text()
        self.assertIn("for_each = local.codex_update_instances", ssm)
        self.assertIn("exec runuser -u ubuntu -- env HOME=/home/ubuntu", ssm)
        self.assertIn('file("${path.module}/scripts/antigravity-install.sh")', ssm)
        self.assertIn('--machine="$u" --uid="$u" --setenv="HOME=/home/$u"', ssm)
        self.assertIn('mountpoint -q "$HOME"', ssm)
        accounts = re.search(r"for u in (.+); do", ssm)[1].split()
        self.assertEqual(set(accounts), {"ejc3", "colton", "connor", "colin"})
        policy = (ROOT / "scripts/nextjs-pbox-policy-install.sh").read_text()
        self.assertIn("colton|connor|colin) policy=$member_policy", policy)

    def test_jumpboxes_receive_only_the_binary_during_scheduled_runs(self):
        tf = (ROOT / "antigravity.tf").read_text()
        self.assertIn('startswith(each.key, "jumpbox") ? "--binary-only" : ""', tf)
        self.assertIn('each.key == "fcvm-metal-x86" ? local.antigravity_owner_models : ""', tf)
        self.cli.parent.mkdir(parents=True)
        self.tool(".local/bin/agy", "echo 1.5.18")
        subprocess.run(["/bin/bash", "-s", "--", "--binary-only"], input=self.script, text=True, env=self.env,
                       capture_output=True, check=True)
        self.assertFalse((self.home / ".gemini").exists())

    def test_global_guidance_preserves_private_files_and_respects_codex_override(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        personal = self.home / ".claude/CLAUDE.md"
        personal.parent.mkdir()
        personal.write_text("my own CLAUDE.md\n")
        fallback = self.home / ".codex/AGENTS.md"
        fallback.parent.mkdir()
        fallback.write_text("other instructions\n")
        override = fallback.with_name("AGENTS.override.md")
        override.write_text("my effective rules\n")
        worker = ["perl", str(ROOT / "scripts/antigravity-owner-models.pl")]
        for _ in range(2):
            subprocess.run(worker, env=self.env, capture_output=True, check=True)
        self.assertEqual(personal.read_text(), "my own CLAUDE.md\n")
        self.assertEqual(fallback.read_text(), "other instructions\n")
        self.assertTrue(override.read_text().startswith("my effective rules\n"))
        self.assertEqual(override.read_text().count(self.env["AGY_MODEL_GUIDE"]), 1)
        modular = personal.parent / "rules/ejc3-antigravity-models.md"
        self.assertIn(self.env["AGY_MODEL_GUIDE"], modular.read_text())
        override.write_text(" \n\t")
        subprocess.run(worker, env=self.env, capture_output=True, check=True)
        self.assertEqual(override.read_text(), " \n\t")
        self.assertTrue(fallback.read_text().startswith("other instructions\n"))
        self.assertEqual(fallback.read_text().count(self.env["AGY_MODEL_GUIDE"]), 1)
        override.unlink()
        override.symlink_to(self.home / "auth")
        self.assertNotEqual(subprocess.run(worker, env=self.env, capture_output=True).returncode, 0)
        self.assertFalse((self.home / "auth").exists())

    def test_offline_mac_does_not_prevent_delivery_to_other_accounts(self):
        tf = (ROOT / "antigravity.tf").read_text()
        mac = tf.split("<<'MACS'\n", 1)[1].split("      MACS\n", 1)[0]
        mac = textwrap.dedent(mac).replace("${local.antigravity_setup}", ":").replace(
            "${local.antigravity_owner_models}", ":").replace(
            '${chomp(file("${path.module}/scripts/mac/bin/mac-agents-md"))}', "#!/bin/zsh").replace(
            '${file("${path.module}/scripts/antigravity-tunnel-status.sh")}',
            (ROOT / "scripts/antigravity-tunnel-status.sh").read_text())
        self.tool("ssh", '''while [ "$#" -gt 2 ]; do shift; done
echo "$1" >> "$HOME/contacted"
cat > "$HOME/received"
if [ "$1" = air ]; then echo "$SSH_ERROR" >&2; exit "$SSH_STATUS"; fi''')
        self.tool("aws", """echo '{"client_id":"fake","client_secret":"test-only"}'""")
        self.tool("curl", 'cat > "$HOME/headers"; printf "%s" "$PROBE"')
        contacted = self.home / "contacted"
        for error, status, response, deferred in [
            ("websocket: bad handshake", 255, "error code: 1033\n530", True),
            ("websocket: bad handshake", 255, "error code: 1033\r\n\n530", True),
            ("websocket: bad handshake", 255, '{"error_code":1033}\n530', True),
            ("websocket: bad handshake", 255, "Forbidden\n403", False),
            ("websocket: bad handshake", 255, "SSO\n302", False),
            ("Host key verification failed.", 255, "error code: 1033\n530", False),
            ("Permission denied (publickey).", 255, "error code: 1033\n530", False),
            ("installer failed", 42, "error code: 1033\n530", False),
        ]:
            with self.subTest(error=error, status=status, response=response):
                contacted.write_text("")
                env = dict(self.env, SSH_ERROR=error, SSH_STATUS=str(status), PROBE=response)
                result = subprocess.run(["/bin/bash", "-s"], input=mac, text=True, env=env, capture_output=True)
                self.assertEqual(result.returncode == 0, deferred, result.stderr)
                self.assertIn("air: deferred" if deferred else "air: install failed", result.stdout + result.stderr)
                self.assertEqual(contacted.read_text().splitlines(), ["mac", "air", "mini", "colton-mac", "colin-mac"])
        self.assertIn("CF-Access-Client-Secret: test-only", (self.home / "headers").read_text())
        subprocess.run(["/bin/bash", "-n", str(self.home / "received")], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

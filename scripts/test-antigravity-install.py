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
        self.release.write_bytes(b"truncated download")
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

    def test_offline_mac_does_not_prevent_delivery_to_other_accounts(self):
        tf = (ROOT / "antigravity.tf").read_text()
        mac = tf.split("<<'MACS'\n", 1)[1].split("      MACS\n", 1)[0]
        mac = textwrap.dedent(mac).replace("${local.antigravity_setup}", ":").replace(
            '${chomp(file("${path.module}/scripts/mac/bin/mac-agents-md"))}', "#!/bin/zsh")
        self.tool("ssh", '''while [ "$#" -gt 2 ]; do shift; done
echo "$1" >> "$HOME/contacted"
cat > "$HOME/received"
test "$1" != air''')
        result = subprocess.run(["/bin/bash", "-s"], input=mac, text=True, env=self.env, capture_output=True)
        self.assertNotEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "contacted").read_text().splitlines(),
                         ["mac", "air", "mini", "colton-mac", "colin-mac"])
        subprocess.run(["/bin/bash", "-n", str(self.home / "received")], check=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

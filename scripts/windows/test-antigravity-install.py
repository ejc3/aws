"""Exercise the installer in a disposable Windows account without signing in."""

import base64
import codecs
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import winreg


HERE = Path(__file__).resolve().parent
GUIDE = (HERE.parent / "antigravity-models.md").read_text(encoding="utf-8")
RULE_NAME = "ejc3-antigravity-models.md"
USER_TOOLS = r"%AGY_TEST_TOOLS%\bin"


class WindowsInstallTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.profile = Path(temp.name) / "profile with spaces"
        self.local = self.profile / "AppData/Local"
        self.local.mkdir(parents=True)
        self.env = dict(os.environ, USERPROFILE=str(self.profile), LOCALAPPDATA=str(self.local),
                        AGY_TEST_TOOLS=str(self.profile / "personal-tools"), AGY_CLI_DISABLE_AUTO_UPDATE="true")
        self.env.pop("CODEX_HOME", None)
        self.env.pop("CLAUDE_CONFIG_DIR", None)
        self.key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE)
        self.addCleanup(self.key.Close)
        try:
            value, kind = winreg.QueryValueEx(self.key, "Path")
        except FileNotFoundError:
            self.addCleanup(winreg.DeleteValue, self.key, "Path")
        else:
            self.addCleanup(winreg.SetValueEx, self.key, "Path", 0, kind, value)
        winreg.SetValueEx(self.key, "Path", 0, winreg.REG_EXPAND_SZ, USER_TOOLS)

    def install(self, guide=GUIDE):
        dispatcher = (HERE / "antigravity-stdin.ps1").read_text(encoding="utf-8")
        encoded = base64.b64encode(dispatcher.encode("utf-16le")).decode("ascii")
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]
        installer = base64.b64encode((HERE / "antigravity-install.ps1").read_bytes())
        payload = installer + b"\n" + base64.b64encode(guide.encode("utf-8"))
        subprocess.run(command, input=payload, env=self.env, timeout=240, check=True)

    def test_first_install_preserves_personal_settings(self):
        settings = self.profile / ".gemini/antigravity-cli/settings.json"
        settings.parent.mkdir(parents=True)
        personal = {
            "permissions": {"deny": ["command(touch)"], "ask": ["command(git push)"]},
            "nested": {"personal": [True, "café", None, 2**55 + 1]},
        }
        for original in (json.dumps(personal, indent=2, ensure_ascii=False).encode("utf-8"),
                         b'{"unfinished":'):
            with self.subTest(original=original):
                settings.write_bytes(original)
                os.utime(settings, (1_000_000_000, 1_000_000_000))
                self.install()
                self.assertEqual(settings.read_bytes(), original)
                self.assertEqual(settings.stat().st_mtime, 1_000_000_000)
                # Each subtest starts with an absent executable.
                (self.local / "agy/bin/agy.exe").unlink()

    def test_native_install_rerun_and_instruction_precedence(self):
        rules = self.profile / ".gemini/config/rules"
        rules.mkdir(parents=True)
        personal = rules / "personal-rule.md"
        personal.write_bytes(b"My own instructions\r\n")
        settings = rules.parent / "config.json"
        settings.write_bytes(b'{"personal":true}\r\n')
        claude = self.profile / ".claude"
        claude.mkdir()
        claude_md = claude / "CLAUDE.md"
        claude_md.write_bytes(b"Claude personal notes\r\n")
        codex = self.profile / ".codex/AGENTS.md"
        codex.parent.mkdir()
        before = "Personal café notes\r\nDo not reformat.\r\n".encode("utf-8")
        codex.write_bytes(before)

        self.install()
        executable = self.local / "agy/bin/agy.exe"
        cli_preferences = self.profile / ".gemini/antigravity-cli/settings.json"
        self.assertEqual(json.loads(cli_preferences.read_bytes()), {"toolPermission": "always-proceed"})
        fresh_shell = r"""
            $ErrorActionPreference = 'Stop'
            $user = [Environment]::ExpandEnvironmentVariables([Environment]::GetEnvironmentVariable('Path', 'User'))
            if (($user -split ';') -notcontains (Join-Path $env:AGY_TEST_TOOLS 'bin')) { throw 'Lost personal PATH' }
            $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + $user
            $resolved = (Get-Command agy -CommandType Application).Source
            if ($resolved -ne (Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe')) { throw 'Wrong agy on PATH' }
            agy --version
            exit $LASTEXITCODE
        """
        version = subprocess.check_output(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", fresh_shell],
            env=self.env, text=True, timeout=30,
        )
        self.assertRegex(version.strip(), r"^\d+\.\d+\.\d+([+-][\w.+-]+)?$")
        agy_rule = rules / RULE_NAME
        claude_rule = claude / "rules" / RULE_NAME
        expected = "---\ntrigger: always_on\n---\n" + GUIDE.rstrip("\r\n") + "\n"
        self.assertEqual(agy_rule.read_bytes(), expected.encode("utf-8"))
        self.assertIn(GUIDE.rstrip(), claude_rule.read_text(encoding="utf-8"))
        self.assertTrue(codex.read_bytes().startswith(before))
        self.assertEqual(codex.read_text(encoding="utf-8").count(GUIDE.rstrip()), 1)
        self.assertFalse((codex.parent / "AGENTS.override.md").exists())

        # A fixed timestamp makes even an immediate rewrite with identical bytes detectable.
        for path in (executable, agy_rule):
            os.utime(path, (1_000_000_000, 1_000_000_000))
        default_content = codex.read_bytes()
        cli_preferences.unlink()
        self.install()
        self.assertFalse(cli_preferences.exists(), "Reconciliation recreated user-deleted preferences")
        for path in (executable, agy_rule):
            self.assertEqual(path.stat().st_mtime, 1_000_000_000)
        self.assertEqual(codex.read_bytes(), default_content)

        # Use the installed block to exercise a real upgrade in an active external override.
        external = self.profile.parent / "external codex home"
        external.mkdir()
        self.env["CODEX_HOME"] = str(external)
        inactive = external / "AGENTS.md"
        inactive.write_bytes(b"Inactive instructions\r\n")
        override = external / "AGENTS.override.md"
        after = b"\r\nPersonal footer after the managed block\r\n"
        begin = b"<!-- BEGIN ejc3-antigravity-models (managed) -->"
        override.write_bytes(codecs.BOM_UTF8 + default_content[default_content.index(begin):] + after)
        next_guide = GUIDE.rstrip() + "\n\nUpdated smoke-test guidance.\n"
        self.install(next_guide)
        self.assertTrue(override.read_bytes().startswith(codecs.BOM_UTF8 + begin))
        self.assertTrue(override.read_bytes().endswith(after))
        self.assertEqual(override.read_text(encoding="utf-8").count(next_guide.rstrip()), 1)
        self.assertEqual(override.read_text(encoding="utf-8").count(GUIDE.rstrip()), 1)
        self.assertEqual(codex.read_bytes(), default_content)
        self.assertEqual(inactive.read_bytes(), b"Inactive instructions\r\n")
        self.assertEqual(claude_md.read_bytes(), b"Claude personal notes\r\n")
        self.assertEqual(personal.read_bytes(), b"My own instructions\r\n")
        self.assertEqual(settings.read_bytes(), b'{"personal":true}\r\n')
        self.assertEqual(winreg.QueryValueEx(self.key, "Path"),
                         (str(executable.parent) + ";" + USER_TOOLS, winreg.REG_EXPAND_SZ))


if __name__ == "__main__":
    unittest.main()

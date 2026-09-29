#!/usr/bin/env python3
"""opencode with DeepSeek on Bedrock on the metal boxes (dev-user-data.tf).

opencode's Bedrock provider walks the AWS credential chain only when it sees a profile, keys, a
bearer token, a web identity or container credentials -- never for an EC2 instance role -- so on
the metal boxes it asked for a key. The opencode() function in the managed ~/.zshrc names the
[default] profile, which makes the chain reach the instance role. This runs that exact function
text (it is bash-compatible) against a fake `opencode`, and pins the install and the default
config. Offline.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
USER_DATA = (ROOT / "dev-user-data.tf").read_text()
IAM = (ROOT / "dev-instance-common.tf").read_text()


def function_text():
    m = re.search(r"^opencode\(\) \{\n.*?^\}\n", USER_DATA, re.S | re.M)
    assert m, "opencode() missing from the managed ~/.zshrc"
    return m.group()


class OpencodeFunctionTests(unittest.TestCase):
    def run_function(self, env_extra, config="[default]\nregion = us-west-1\n"):
        """AWS_PROFILE as the fake opencode saw it ('' when unset)."""
        with tempfile.TemporaryDirectory() as home:
            bindir = Path(home, "bin")
            bindir.mkdir()
            fake = bindir / "opencode"
            fake.write_text('#!/bin/sh\nprintf "%s|%s" "${AWS_PROFILE-}" "$*"\n')
            fake.chmod(0o755)
            if config is not None:
                Path(home, ".aws").mkdir()
                Path(home, ".aws", "config").write_text(config)
            env = {"PATH": "%s:/usr/bin:/bin" % bindir, "HOME": home, **env_extra}
            out = subprocess.run(["bash", "-c", function_text() + 'opencode run "hi there"'],
                                 env=env, capture_output=True, text=True, check=True).stdout
            profile, args = out.split("|", 1)
            self.assertEqual(args, "run hi there", "arguments pass through unchanged")
            return profile

    def test_a_clean_environment_gets_the_default_profile(self):
        self.assertEqual(self.run_function({}), "default")

    def test_anything_else_that_supplies_credentials_wins(self):
        for name in ("AWS_PROFILE", "AWS_ACCESS_KEY_ID", "AWS_BEARER_TOKEN_BEDROCK", "AWS_WEB_IDENTITY_TOKEN_FILE",
                     "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI"):
            seen = self.run_function({name: "work" if name == "AWS_PROFILE" else "x"})
            self.assertEqual(seen, "work" if name == "AWS_PROFILE" else "", name)

    def test_no_default_profile_means_no_profile(self):
        # The aws CLI refuses AWS_PROFILE=default when [default] does not exist.
        self.assertEqual(self.run_function({}, config="[profile other]\nregion = us-west-2\n"), "")
        self.assertEqual(self.run_function({}, config=None), "")


class OpencodeSetupTests(unittest.TestCase):
    def test_both_metal_scripts_run_the_setup_once(self):
        for name in ("arm_user_data", "x86_user_data"):
            body = USER_DATA.split("  %s = <<-SCRIPT" % name, 1)[1].split("\nSCRIPT\n", 1)[0]
            self.assertEqual(body.count("${local.opencode_setup}"), 1, name)

    def test_the_pinned_install_is_the_one_on_path(self):
        setup = USER_DATA.split("opencode_setup   = <<-OPENCODE", 1)[1].split("\nOPENCODE\n", 1)[0]
        self.assertRegex(USER_DATA, r'opencode_version = "\d+\.\d+\.\d+"')
        # Installed (or kept) at exactly the pinned version in ~/.opencode/bin...
        self.assertIn('[ "$(~/.opencode/bin/opencode --version 2>/dev/null || true)" = "${local.opencode_version}" ] ||\n'
                      "  curl -fsSL https://opencode.ai/install | bash -s -- --version ${local.opencode_version} "
                      "--no-modify-path", setup)
        self.assertNotIn("command -v opencode", setup, "another copy on PATH must not stand in for the pinned one")
        # ...and that directory precedes every other copy on the managed PATH.
        zshrc = USER_DATA.split("cat > ~/.zshrc << 'ZSH'", 1)[1].split("\nZSH\n", 1)[0]
        path_line = zshrc.strip().splitlines()[0]
        self.assertRegex(path_line, r'^export PATH=".*\$HOME/\.opencode/bin:\$PATH"$')
        self.assertIn("grep -qs '^\\[default\\]' ~/.aws/config || printf '[default]", setup)
        self.assertIn("if [ ! -e ~/.config/opencode/opencode.jsonc ] && [ ! -e ~/.config/opencode/opencode.json ]",
                      setup, "an existing config must never be overwritten")

    def test_the_default_model_is_one_the_role_may_invoke(self):
        setup = USER_DATA.split("opencode_setup   = <<-OPENCODE", 1)[1].split("\nOPENCODE\n", 1)[0]
        for model in re.findall(r'"(?:small_)?model": "amazon-bedrock/([^"]+)"', setup):
            self.assertIn('"arn:aws:bedrock:*::foundation-model/%s"' % model, IAM, model)


if __name__ == "__main__":
    unittest.main(verbosity=2)

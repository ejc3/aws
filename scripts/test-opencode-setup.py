#!/usr/bin/env python3
"""opencode with DeepSeek on Bedrock on the metal boxes and in the nextjs-ct containers (dev-user-data.tf).

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
AI = (ROOT / "dev-ai-services.tf").read_text()
CT = (ROOT / "nextjs-ct-user-data.tf").read_text()
PROVISION = (ROOT / "scripts/nextjs-ct/ct/provision").read_text()


def user_setup():
    return USER_DATA.split("opencode_user_setup = <<-OPENCODE", 1)[1].split("\nOPENCODE\n", 1)[0]


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
        setup = user_setup()
        self.assertRegex(USER_DATA, r'opencode_version +=  *"\d+\.\d+\.\d+"')
        # Installed (or kept) at exactly the pinned version in ~/.opencode/bin...
        self.assertIn('have=$(~/.opencode/bin/opencode --version 2>/dev/null || true); have=$${have##* }\n'
                      '[ "$${have#v}" = "${local.opencode_version}" ] ||\n'
                      "  curl -fsSL https://opencode.ai/v2/install | bash -s -- --version ${local.opencode_version} "
                      "--no-modify-path", setup)
        self.assertNotIn("command -v opencode", setup, "another copy on PATH must not stand in for the pinned one")
        # ...and that directory precedes every other copy on the managed PATH.
        zshrc = USER_DATA.split("cat > ~/.zshrc << 'ZSH'", 1)[1].split("\nZSH\n", 1)[0]
        path_line = zshrc.strip().splitlines()[0]
        self.assertRegex(path_line, r'^export PATH=".*\$HOME/\.opencode/bin:\$PATH"$')
        self.assertIn("grep -qs '^\\[default\\]' ~/.aws/config || printf '[default]", setup)
        self.assertIn("if [ ! -e ~/.config/opencode/opencode.jsonc ] && [ ! -e ~/.config/opencode/opencode.json ]",
                      setup, "an existing config must never be overwritten")

    def test_the_version_check_matches_what_opencode_prints(self):
        # OpenCode 2 prints "opencode v2.1.2" (v1 printed "1.18.33"): the check takes the last word and drops the "v",
        # so an installed pinned release is kept rather than reinstalled on every run. Runs the check's two lines.
        version = re.search(r'opencode_version += "([^"]+)"', USER_DATA).group(1)
        lines = [l for l in user_setup().splitlines() if l.startswith("have=") or l.startswith('[ "$${have')]
        self.assertEqual(len(lines), 2, lines)
        check = "\n".join(lines).replace("$${", "${").replace("${local.opencode_version}", version)
        check = check.replace("$(~/.opencode/bin/opencode --version 2>/dev/null || true)", '"$1"')
        self.assertTrue(check.endswith(" ||"), check)
        check = check[:-3] + " && echo keep || echo install"
        for printed, want in ((f"opencode v{version}", "keep"), (version, "keep"), ("opencode v1.18.33", "install"), ("", "install")):
            out = subprocess.run(["bash", "-c", check, "x", printed], capture_output=True, text=True).stdout.strip()
            self.assertEqual(out, want, printed)

    def test_the_default_model_is_one_the_role_may_invoke(self):
        nextjs = AI.split('resource "aws_iam_policy" "nextjs_dev_bedrock_deepseek"', 1)[1].split("\n}\n", 1)[0]
        models = re.findall(r'"(?:small_)?model": "amazon-bedrock/([^"]+)"', user_setup())
        self.assertTrue(models)
        for model in models:
            self.assertIn('"arn:aws:bedrock:*::foundation-model/%s"' % model, IAM, model)
            self.assertIn('"arn:aws:bedrock:*::foundation-model/%s"' % model, nextjs, model)

    def test_the_metal_boxes_run_it_as_ubuntu(self):
        line = re.search(r"^  opencode_setup += (.*)$", USER_DATA, re.M).group(1)
        self.assertTrue(line.startswith('"sudo -u ubuntu -H bash <<\'OPENCODESETUP\' ||'), line)
        self.assertIn("\\n${local.opencode_user_setup}OPENCODESETUP\\n\"", line)

    def test_every_nextjs_container_account_runs_it_as_itself(self):
        self.assertIn('"/var/lib/nextjs-ct/shared/snippets/opencode-setup.sh"             = local.opencode_user_setup', CT)
        self.assertIn('as_user bash < "$SHARED/snippets/opencode-setup.sh"', PROVISION)
        # After the shell env, which carries the opencode() function that points it at the credentials.
        self.assertLess(PROVISION.index("snippets/user-shell-env.sh"), PROVISION.index("snippets/opencode-setup.sh"))
        self.assertNotIn("sudo", user_setup(), "the shared body runs as the account, never escalates")

    def test_the_nextjs_grant_is_deepseek_only(self):
        nextjs = AI.split('resource "aws_iam_policy" "nextjs_dev_bedrock_deepseek"', 1)[1].split("\n}\n", 1)[0]
        resources = re.findall(r'"(arn:aws:bedrock:[^"]+)"', nextjs)
        self.assertEqual(len(resources), 3)
        for r in resources:
            self.assertRegex(r, r"(foundation-model/deepseek\.(v3\.2|r1-v1:0)|inference-profile/us\.deepseek\.\*)$")
        self.assertIn("role       = aws_iam_role.nextjs_dev.name", AI.split('"nextjs_dev_bedrock_deepseek" {', 2)[2])


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

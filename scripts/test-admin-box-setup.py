#!/usr/bin/env python3
"""Offline checks for the shared admin-box script and the host-aware Codex guidance.

Nothing here touches a real host: scripts run in temp directories with fake binaries.
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADMIN_TF = (ROOT / "jumpbox2-user-data.tf").read_text()
CODEX_TF = (ROOT / "codex-remote-control.tf").read_text()
JUMPBOX_TF = (ROOT / "jumpbox.tf").read_text()


def between(text, start, end):
    return text.split(start, 1)[1].split(end, 1)[0]


def rendered(template):
    """Approximate Terraform's heredoc rendering for shell checks."""
    template = re.sub(r"(?<!\$)\$\{local\.[a-z0-9_]+\}", ":", template)
    return template.replace("$${", "${").replace("%%{", "%{")


ADMIN_SCRIPT = rendered(between(ADMIN_TF, "jumpbox_2_user_data = <<-SCRIPT\n", "\nSCRIPT\n"))
ET = between(ADMIN_SCRIPT, "# ---------------------------------------------------------------- eternal terminal (prebuilt)\n",
             "# ---------------------------------------------------------------- shell env")
KEY = between(ADMIN_SCRIPT, "# ---------------------------------------------------------------- fcvm-ec2 key\n",
              'echo "admin box ready"')
REFRESH = rendered(between(CODEX_TF, "cat > /usr/local/bin/codex-agents-refresh <<'GENAGENTS'\n", "\nGENAGENTS\n"))


def executable(path, body):
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


class AdminBoxScriptTests(unittest.TestCase):
    def test_whole_script_is_shell_syntax_valid(self):
        subprocess.run(["bash", "-n"], input=ADMIN_SCRIPT, text=True, check=True)

    def select_et(self, usr_bin=None, usr_local=None):
        tmp = tempfile.mkdtemp()
        paths = []
        for name, body in (("usr-bin-etserver", usr_bin), ("usr-local-etserver", usr_local)):
            path = os.path.join(tmp, name)
            if body is not None:
                executable(path, body)
            paths.append(path)
        loop = 'ET_BIN=""\n' + between(ET, 'ET_BIN=""\n', "\ndone\n") + "\ndone\n"
        loop = loop.replace("/usr/bin/etserver /usr/local/bin/etserver", " ".join(paths))
        out = subprocess.run(["bash"], input=loop + 'printf "%s" "$ET_BIN"', text=True,
                             capture_output=True, check=True).stdout
        return os.path.basename(out) if out else ""

    def test_a_working_etserver_of_any_version_is_kept(self):
        version_6 = '#!/bin/sh\necho "et version 6.2.11"\nexit 1\n'
        self.assertEqual(self.select_et(usr_local=version_6), "usr-local-etserver")
        self.assertEqual(self.select_et(usr_bin="#!/bin/sh\nexit 1\n", usr_local=version_6), "usr-local-etserver")
        self.assertEqual(self.select_et(usr_bin='#!/bin/sh\necho "et version 7.0.1"\n', usr_local=version_6),
                         "usr-bin-etserver")
        self.assertEqual(self.select_et(), "")

    def test_install_only_without_a_working_etserver_and_never_restarts(self):
        self.assertIn('if [ -z "$ET_BIN" ]; then', ET)
        self.assertIn("ExecStart=$ET_BIN --port 2022", ET)
        self.assertNotRegex(ET, r"systemctl\s+(restart|stop|try-restart|reload-or-restart|kill)")
        self.assertIn("systemctl enable --now etserver.service", ET)

    def test_fcvm_key_is_never_overwritten_and_never_traced(self):
        self.assertIn('if [ -s "$FCVM_KEY_FILE" ]; then', KEY)
        fetch = KEY.index("secretsmanager get-secret-value")
        self.assertLess(KEY.index("set +x"), fetch)
        self.assertIn("unset FCVM_KEY", KEY[fetch:])
        self.assertIn("set -x", KEY[fetch:])

    def test_admin_role_is_set_right_before_the_codex_block(self):
        self.assertRegex(ADMIN_TF, r"FLEET_HOST_ROLE=admin\n\$\{local\.codex_remote_control\}")

    def test_original_jumpbox_fetches_the_shared_script_without_replacement(self):
        instance = between(JUMPBOX_TF, 'resource "aws_instance" "jumpbox" {', "\n}\n")
        self.assertIn("s3://ejc3-dev-scripts/user-data/jumpbox.sh", instance)
        self.assertRegex(instance, r"(?s)ignore_changes\s*=\s*\[.*?user_data,")
        self.assertIn("depends_on = [aws_s3_object.jumpbox_user_data]", instance)
        self.assertRegex(ADMIN_TF, r'(?s)resource "aws_s3_object" "jumpbox_user_data".*?key\s*=\s*"user-data/jumpbox.sh"'
                                   r'.*?content\s*=\s*local\.jumpbox_2_user_data')


class CodexGuidanceTests(unittest.TestCase):
    def render(self, role, devhop=None):
        tmp = tempfile.mkdtemp()
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, ".ssh"))
        role_file = os.path.join(tmp, "fleet-host-role")
        if role is not None:
            with open(role_file, "w") as fh:
                fh.write(role + "\n")
        if devhop is not None:
            with open(os.path.join(home, ".ssh", "config.d-devhop"), "w") as fh:
                fh.write(devhop)
        script = (REFRESH.replace("/etc/fleet-host-role", role_file)
                  .replace("/home/ubuntu", home)
                  .replace("install -d -o ubuntu -g ubuntu -m 755", "install -d -m 755")
                  .replace("install -m 644 -o ubuntu -g ubuntu", "install -m 644"))
        subprocess.run(["bash"], input=script, text=True, capture_output=True, check=True)
        return Path(home, "Documents", "Codex", "AGENTS.md").read_text()

    def test_refresh_script_is_shell_syntax_valid(self):
        subprocess.run(["bash", "-n"], input=REFRESH, text=True, check=True)

    def test_admin_boxes_are_told_they_are_not_a_sandbox(self):
        text = self.render("admin")
        for expected in ("AWS AdministratorAccess", "not a sandbox", "fresh git",
                         "Ask before restarting", "connect back to a jumpbox"):
            self.assertIn(expected, text)
        # "jumpbox" contains "pbox", so look for the stale key's file name and phrasing instead.
        for forbidden in ("passwordless sudo", "isolation boundary", "spot instance", "pbox-key", "`pbox`"):
            self.assertNotIn(forbidden, text)

    def test_metal_boxes_list_their_real_hop_aliases(self):
        devhop = ("Host fcvm-arm fcvm-metal-arm\n    HostName 1.2.3.4\n    User ubuntu\n\n"
                  "Host dolphin dolphin-labs\n    HostName 5.6.7.8\n    User ejc3\n")
        text = self.render("metal", devhop)
        for expected in ("passwordless sudo", "ssh fcvm-arm\n", "ssh dolphin\n", "no key to any jumpbox"):
            self.assertIn(expected, text)
        for forbidden in ("AdministratorAccess", "pbox-key", "`pbox`"):
            self.assertNotIn(forbidden, text)

    def test_a_missing_role_file_defaults_to_metal(self):
        self.assertIn("passwordless sudo", self.render(None, ""))

    def test_the_setup_records_the_role_with_metal_as_default(self):
        self.assertIn("""printf '%s\\n' "$${FLEET_HOST_ROLE:-metal}" > /etc/fleet-host-role""", CODEX_TF)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""A Windows PC on its Cloudflare route with Windows-MCP: colin-pc-ssh.tf, scripts/windows-pc-setup.sh, the bootstrap
(scripts/windows-pc-bootstrap.sh, scripts/windows/bootstrap.ps1) and the client side (scripts/windows/windows-mcp-bridge,
windows-mcp-client-setup.sh). Offline: reads the sources, runs the generator against a throwaway home."""
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "colin-pc-ssh.tf").read_text()
SETUP = (ROOT / "scripts" / "windows-pc-setup.sh").read_text()
CLAW_WIN = (ROOT / "scripts" / "claw-master-windows-setup.sh").read_text()
BOOT_GEN = ROOT / "scripts" / "windows-pc-bootstrap.sh"
BOOT_PS = (ROOT / "scripts" / "windows" / "bootstrap.ps1").read_text()
BRIDGE = (ROOT / "scripts" / "windows" / "windows-mcp-bridge").read_text()
CLIENT = (ROOT / "scripts" / "windows" / "windows-mcp-client-setup.sh").read_text()
LOCK = json.loads((ROOT / "scripts" / "windows" / "mcp-remote" / "package-lock.json").read_text())


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def step(n):
    text = code(SETUP)
    start = text.index('echo "%d/7' % n)
    nxt = text.find('echo "%d/7' % (n + 1), start)
    return text[start:nxt if nxt >= 0 else len(text)]


def pin(text, name):
    return re.search(r"^%s=(\S+)$" % name, text, re.M).group(1)


class Route(unittest.TestCase):
    def test_built_like_coltons_mac_with_an_admin_only_connector_token(self):
        self.assertIn('colin_pc_ssh_hostname = "colin-pc-ssh.cc-games.dev"', TF)
        self.assertIn('service  = "ssh://127.0.0.1:22"', TF)
        self.assertIn("required  = true", TF)  # the connector refuses a request without an Access token
        self.assertIn('decision         = "non_identity"', TF)
        policy = TF[TF.index('resource "aws_secretsmanager_secret_policy" "colin_pc_ssh_tunnel"'):]
        self.assertIn("ArnNotEquals = { \"aws:PrincipalArn\" = local.colin_pc_ssh_admins }", policy.split("\n}\n")[0])

    def test_the_clients_are_exactly_fcvm_arm_for_now(self):
        clients = TF[TF.index("colin_pc_ssh_clients = {"):TF.index("\n  }\n", TF.index("colin_pc_ssh_clients = {"))]
        self.assertEqual(re.findall(r"^\s+([a-z-]+)\s+= \{", clients, re.M), ["fcvm"])
        self.assertIn('"ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn', TF)
        self.assertNotIn("nextjs_dev", TF)
        self.assertIn('CLIENTS="fcvm:ubuntu"', SETUP)


class Setup(unittest.TestCase):
    def test_cloudflared_pins_match_claw_masters(self):
        for name in ("CFD_VERSION", "CFD_SHA256_WINDOWS_AMD64"):
            self.assertEqual(pin(SETUP, name), pin(CLAW_WIN, name))
        self.assertRegex(pin(SETUP, "UV_SHA256_WINDOWS_AMD64_ZIP"), r"^[0-9a-f]{64}$")
        self.assertRegex(pin(SETUP, "WINDOWS_MCP_VERSION"), r"^\d+\.\d+\.\d+$")

    def test_every_download_is_checked_before_it_is_used(self):
        s4, s7 = step(4), step(7)
        self.assertLess(s4.index("-ne $CfdSha) { Remove-Item"), s4.index('Move-Item -Force "$exe.new" $exe'))
        self.assertLess(s7.index("-ne $UvSha) { Remove-Item"), s7.index("Expand-Archive"))
        self.assertIn('"windows-mcp==$McpVersion"', s7)

    def test_sshd_is_closed_to_the_network_and_checked_before_restart(self):
        s2 = step(2)
        self.assertIn("Disable-NetFirewallRule", s2)
        for line in ("ListenAddress 127.0.0.1", "PasswordAuthentication no", "ChallengeResponseAuthentication no"):
            self.assertIn(line, s2)
        self.assertLess(s2.index("sshd.exe\" -t"), s2.index("Restart-Service sshd"))
        self.assertIn("restored it'; exit 1", s2)
        self.assertIn("-UserId SYSTEM", s2)  # a restart from the session would die with it

    def test_the_token_goes_through_stdin_into_a_system_only_folder(self):
        s4, s5 = step(4), step(5)
        self.assertIn("icacls $dir /inheritance:r /grant 'Administrators:(OI)(CI)F' /grant 'SYSTEM:(OI)(CI)F'", s4)
        self.assertIn("{ set +x; } 2>/dev/null", s5)
        self.assertIn('--secret-id "$TUNNEL_SECRET"', s5)
        self.assertIn("|\n  on_pc", s5)
        self.assertIn("[Console]::In.ReadToEnd()", s5)
        self.assertNotRegex(code(SETUP), r"ps_vars[^\n]*(TOKEN|SECRET)")
        self.assertNotRegex(code(SETUP), r"--token(?!-file)")

    def test_the_connector_is_a_system_task_at_boot_restarted_out_of_band(self):
        s6 = step(6)
        self.assertIn("New-ScheduledTaskTrigger -AtStartup", s6)
        self.assertIn("-UserId SYSTEM -LogonType ServiceAccount", s6)
        self.assertIn("run --token-file", s6)
        self.assertIn('Start-ScheduledTask -TaskName "$name restart"', s6)
        self.assertIn("'connector: unchanged and running; not restarted'; exit 0", s6)

    def test_windows_mcp_has_no_telemetry_a_key_and_stays_on_loopback(self):
        s7 = step(7)
        self.assertIn("SetEnvironmentVariable('ANONYMIZED_TELEMETRY', 'false', 'User')", s7)
        self.assertIn("RandomNumberGenerator", s7)
        self.assertIn("--host 127.0.0.1 --port 8000", s7)
        self.assertNotRegex(s7, r'"[^"\n]*\$key')  # the key is never printed
        self.assertLess(s7.index("Stop-ScheduledTask -TaskName windows-mcp-server"), s7.index("tool install"))

    def test_scripts_parse(self):
        for path in ("scripts/windows-pc-setup.sh", "scripts/windows-pc-bootstrap.sh", "scripts/windows/windows-mcp-bridge",
                     "scripts/windows/windows-mcp-client-setup.sh"):
            r = subprocess.run(["bash", "-n", str(ROOT / path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, path + r.stderr)


class Bootstrap(unittest.TestCase):
    def test_the_paste_is_one_block_closed_before_sshd_starts(self):
        self.assertIn("\n& {\n", BOOT_PS)  # a throw stops the whole paste
        body = BOOT_PS[BOOT_PS.index("& {"):]
        self.assertLess(body.index("Disable-NetFirewallRule"), body.index("Start-Service sshd"))
        for placeholder in ("@@BOX@@", "@@BOX_HOST_KEY_LINE@@", "@@CLIENT_KEY@@", "@@PORT@@"):
            self.assertIn(placeholder, body)
        self.assertIn("StrictHostKeyChecking=yes", body)
        self.assertIn("BatchMode=yes", body)

    def test_allow_gives_the_tunnel_only_and_replaces_its_own_line(self):
        with tempfile.TemporaryDirectory() as home:
            ssh = Path(home) / ".ssh"
            ssh.mkdir()
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ssh / "to-test-pc")], check=True)
            env = dict(os.environ, HOME=home)
            r = subprocess.run(["bash", str(BOOT_GEN), "allow", "test-pc", "21899", (ssh / "to-test-pc.pub").read_text().strip()],
                               capture_output=True, text=True, env=env)
            self.assertEqual(r.returncode, 0, r.stderr)
            line = (ssh / "authorized_keys").read_text().strip()
            self.assertTrue(line.startswith('restrict,port-forwarding,permitlisten="127.0.0.1:21899",permitopen="127.0.0.1:1",command="/bin/false" ssh-ed25519 '))
            self.assertNotIn('permitopen="none"', line)
            again = subprocess.run(["bash", str(BOOT_GEN), "allow", "test-pc", "21899", (ssh / "to-test-pc.pub").read_text().strip()],
                                   capture_output=True, text=True, env=env)
            self.assertEqual(again.returncode, 0)
            self.assertEqual((ssh / "authorized_keys").read_text().count("test-pc-bootstrap-tunnel"), 1)
            bad = subprocess.run(["bash", str(BOOT_GEN), "allow", "test-pc", "21899", "ssh-ed25519 AAAA\nssh-rsa x"],
                                 capture_output=True, text=True, env=env)
            self.assertNotEqual(bad.returncode, 0)


class Client(unittest.TestCase):
    def test_the_key_travels_in_a_private_header_file_never_an_argument(self):
        self.assertIn('--header-file "$HEADERS"', BRIDGE)
        self.assertNotRegex(code(BRIDGE), r"--header [^-]")
        self.assertIn("umask 077", BRIDGE)
        self.assertNotRegex(code(BRIDGE), r"echo[^\n]*\$key")

    def test_the_forward_is_kept_from_a_launchers_proxy(self):
        self.assertIn("env -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy -u ALL_PROXY -u all_proxy", BRIDGE)
        self.assertIn('"127.0.0.1:$PORT:127.0.0.1:8000"', BRIDGE)
        self.assertIn("trap 'kill \"$FWD\"", BRIDGE)

    def test_mcp_remote_is_locked_and_installed_from_the_lock(self):
        top = LOCK["packages"][""]["dependencies"]
        self.assertEqual(top, {"mcp-remote": "0.14.3"})
        self.assertTrue(all("integrity" in p for k, p in LOCK["packages"].items() if k))
        self.assertIn("npm ci --no-fund --no-audit --omit=dev --ignore-scripts", CLIENT)


if __name__ == "__main__":
    unittest.main()

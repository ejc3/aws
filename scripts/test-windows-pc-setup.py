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

    def test_the_clients_are_fcvm_arm_and_colins_own_container(self):
        clients = TF[TF.index("colin_pc_ssh_clients = {"):TF.index("\n  }\n", TF.index("colin_pc_ssh_clients = {"))]
        self.assertEqual(re.findall(r"^\s+([a-z-]+)\s+= \{", clients, re.M), ["fcvm", "nextjs-colin"])
        self.assertIn('"ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn', TF)
        grant = TF[TF.index('resource "aws_iam_role_policy" "nextjs_dev_colin_pc_ssh"'):]
        self.assertIn('Resource = aws_secretsmanager_secret.colin_pc_ssh_access["nextjs-colin"].arn', grant.split("\n}\n")[0])
        self.assertIn('CLIENTS="fcvm:ubuntu nextjs:colin"', SETUP)
        self.assertIn('windows_mcp_pcs = ["colin-pc"]', TF)


class Setup(unittest.TestCase):
    def test_cloudflared_pins_match_claw_masters(self):
        for name in ("CFD_VERSION", "CFD_SHA256_WINDOWS_AMD64"):
            self.assertEqual(pin(SETUP, name), pin(CLAW_WIN, name))
        self.assertRegex(pin(SETUP, "UV_SHA256_WINDOWS_AMD64_ZIP"), r"^[0-9a-f]{64}$")
        self.assertRegex(pin(SETUP, "WINDOWS_MCP_VERSION"), r"^\d+\.\d+\.\d+$")

    def test_every_download_is_checked_before_it_is_used(self):
        s4, s6 = step(4), step(6)
        self.assertLess(s4.index("-ne $CfdSha) { Remove-Item"), s4.index('Move-Item -Force "$exe.new" $exe'))
        self.assertLess(s6.index("-ne $UvSha) { Remove-Item"), s6.index("Expand-Archive"))
        self.assertIn('"windows-mcp==$McpVersion"', s6)

    def test_sshd_is_judged_by_its_effective_settings_and_its_listeners(self):
        # A text check passed a config that also had ListenAddress 0.0.0.0 or an earlier PasswordAuthentication yes.
        s2 = step(2)
        self.assertIn("Disable-NetFirewallRule", s2)
        # every connection field, so a Match LocalAddress/LocalPort block is evaluated too
        self.assertIn('& $sshd -T -C "user=$env:USERNAME,host=localhost,addr=127.0.0.1,laddr=127.0.0.1,lport=22"', SETUP)
        # every listen address must be loopback, and both password paths off
        self.assertIn("(@($listen | Where-Object { $_ -notmatch '^127\\.0\\.0\\.1:' }).Count -eq 0)", SETUP)
        self.assertIn("($o -contains 'passwordauthentication no')", SETUP)
        self.assertIn("(($o -contains 'kbdinteractiveauthentication no') -or ($o -contains 'challengeresponseauthentication no'))", SETUP)
        self.assertIn("@(Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { $_.LocalAddress })", SETUP)
        self.assertIn("(@($l | Where-Object { $_ -ne '127.0.0.1' }).Count -eq 0)", SETUP)
        # a config changed after the daemon started is not in force: restart
        self.assertIn("$p.CreationDate -gt (Get-Item \"$env:ProgramData\\ssh\\sshd_config\").LastWriteTime", SETUP)
        self.assertNotIn("$lines -notcontains", SETUP)
        # the early return needs both the effective settings and the running daemon's listeners
        # a rewrite that still fails the check is restored and stops the script
        self.assertIn("if (-not $ok) { Copy-Item \"$cfg.before-windows-pc-setup\" $cfg -Force; throw 'sshd_config is still not keys only and loopback only; restored it' }", s2)
        self.assertIn("(Test-Listening) -and (Test-StartedAfterConfig)) { 'sshd: already", s2)
        self.assertIn("-UserId SYSTEM", s2)  # a restart from the session would die with it
        # and the restarted daemon is checked before anything else runs
        after = s2[s2.index("Start-ScheduledTask -TaskName 'windows-pc-setup sshd restart'"):]
        self.assertIn("throw 'sshd is not running on 127.0.0.1 only'", after)

    def test_the_token_goes_through_stdin_into_a_system_only_folder(self):
        s4, s5 = step(4), step(5)
        self.assertIn("icacls $dir /inheritance:r /grant 'Administrators:(OI)(CI)F' /grant 'SYSTEM:(OI)(CI)F'", s4)
        self.assertIn("{ set +x; } 2>/dev/null", s5)
        self.assertIn('--secret-id "$TUNNEL_SECRET"', s5)
        self.assertIn("|\n  on_pc", s5)
        self.assertIn("[Console]::In.ReadToEnd()", s5)
        self.assertNotRegex(code(SETUP), r"ps_vars[^\n]*(TOKEN|SECRET)")
        self.assertNotRegex(code(SETUP), r"--token(?!-file)")

    def test_the_connector_is_a_system_task_at_boot_restarted_last(self):
        s6 = step(7)
        # nothing runs after the restart that would cut its session
        self.assertNotIn("on_pc", code(SETUP)[code(SETUP).index("Start-ScheduledTask -TaskName \"$name restart\""):])
        self.assertIn("New-ScheduledTaskTrigger -AtStartup", s6)
        self.assertIn("-UserId SYSTEM -LogonType ServiceAccount", s6)
        self.assertIn("run --token-file", s6)
        self.assertIn('Start-ScheduledTask -TaskName "$name restart"', s6)
        self.assertIn("'connector: unchanged and running; not restarted'; exit 0", s6)

    def test_windows_mcp_has_no_telemetry_a_key_and_stays_on_loopback(self):
        s7 = step(6)
        self.assertIn("SetEnvironmentVariable('ANONYMIZED_TELEMETRY', 'false', 'User')", s7)
        self.assertIn("RandomNumberGenerator", s7)
        self.assertIn("--host 127.0.0.1 --port 8000", s7)
        self.assertNotRegex(s7, r'"[^"\n]*\$key')  # the key is never printed
        # always stopped, installed or not, so the server that runs is the one with today's settings
        before_check = s7[:s7.index("if (@(& $uv tool list")]
        self.assertIn("\nStop-ScheduledTask -TaskName windows-mcp-server -ErrorAction SilentlyContinue\n", before_check)
        self.assertRegex(before_check, r"\nGet-CimInstance Win32_Process -Filter \"Name = 'python\.exe'\" \| Where-Object \{ \$_\.CommandLine -like '\*windows_mcp\*' \}")
        # and it must refuse a request without the key
        self.assertIn('if ($status -ne 401) {', s7)
        self.assertLess(s7.index("Invoke-WebRequest -UseBasicParsing -Method Post -Uri http://127.0.0.1:8000/mcp"), s7.index("refusing requests without its key"))

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
            self.assertRegex(line, r'^restrict,port-forwarding,permitlisten="127\.0\.0\.1:21899",permitopen="127\.0\.0\.1:1",'
                                   r'command="/bin/false",expiry-time="\d{12}" ssh-ed25519 ')
            self.assertNotIn('permitopen="none"', line)
            again = subprocess.run(["bash", str(BOOT_GEN), "allow", "test-pc", "21899", (ssh / "to-test-pc.pub").read_text().strip()],
                                   capture_output=True, text=True, env=env)
            self.assertEqual(again.returncode, 0)
            self.assertEqual((ssh / "authorized_keys").read_text().count("test-pc-bootstrap-tunnel"), 1)
            bad = subprocess.run(["bash", str(BOOT_GEN), "allow", "test-pc", "21899", "ssh-ed25519 AAAA\nssh-rsa x"],
                                 capture_output=True, text=True, env=env)
            self.assertNotEqual(bad.returncode, 0)


class BootstrapPrint(unittest.TestCase):
    def test_print_fills_every_value_from_the_box_with_no_metadata_lookup(self):
        gen = BOOT_GEN.read_text()
        # the IMDS token used to reach curl's arguments: the box's address is FCVM_HOST, as in the other scripts
        self.assertNotIn("169.254.169.254", gen)
        self.assertNotIn("curl", gen)
        with tempfile.TemporaryDirectory() as home:
            ssh = Path(home) / ".ssh"
            ssh.mkdir()
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ssh / "to-test-pc")], check=True)
            r = subprocess.run(["bash", str(BOOT_GEN), "print", "test-pc", "21899"], capture_output=True, text=True,
                               env=dict(os.environ, HOME=home, FCVM_HOST="203.0.113.7"))
            self.assertEqual(r.returncode, 0, r.stderr)
            body = r.stdout[r.stdout.index("& {"):]
            self.assertNotIn("@@", body)
            self.assertIn("$DevBox     = '%s@203.0.113.7'" % os.environ.get("USER", subprocess.run(["id", "-un"], capture_output=True, text=True).stdout.strip()), body)
            self.assertIn("$Port       = 21899", body)
            self.assertIn((ssh / "to-test-pc.pub").read_text().split()[1], body)


class Client(unittest.TestCase):
    def test_the_key_travels_in_a_private_header_file_never_an_argument(self):
        self.assertIn('--header-file "$HEADERS"', BRIDGE)
        self.assertNotRegex(code(BRIDGE), r"--header [^-]")
        self.assertIn("umask 077", BRIDGE)
        self.assertNotRegex(code(BRIDGE), r"echo[^\n]*\$key")

    def test_the_forward_is_ssh_itself_so_it_ends_with_the_session(self):
        # Backgrounding the ssh_pc function made $! a subshell's pid: the forward outlived every session.
        self.assertRegex(BRIDGE, r"\nssh -o BatchMode=yes -o ConnectTimeout=15 -N [^\n]*\\\n  -L \"127\.0\.0\.1:\$PORT:127\.0\.0\.1:8000\" \"\$ALIAS\" </dev/null >&2 &\nFWD=\$!")
        self.assertIn('wait "$FWD"', BRIDGE)

    def test_the_bootstrap_uses_only_windows_own_openssh(self):
        self.assertNotIn("winget", BOOT_PS)  # a winget install lands outside System32\OpenSSH, where the paste looks

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

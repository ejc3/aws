#!/usr/bin/env python3
"""Exercise the real `ssh mac` installer and OpenSSH's config parser without a live connection."""

from pathlib import Path
import grp
import os
import pwd
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "mac-ssh-client.tf").read_text()
SETUP = re.search(r"mac_ssh_client_setup = <<-MACSETUP\n(.*?)\nMACSETUP", SOURCE, re.S).group(1)
HOST_KEYS = re.search(r"mac_tunnel_host_keys = <<-MACHOSTKEYS\n(.*?\n)MACHOSTKEYS\n", SOURCE, re.S).group(1)
SETUP = SETUP.replace("${local.mac_tunnel_host_keys}", HOST_KEYS).replace("$${", "${")
TUNNEL = (ROOT / "macbook-ssh.tf").read_text()
REMOTE_LOGIN_KEY = "AAAAC3NzaC1lZDI1NTE5AAAAID1DlX7dlpsRWSQuPOHoiDxtiyZd7/W4I1toKoLi7zfL"


def block(source, start):
    """The text of the user_data block that begins with `start` (up to the next top-level SCRIPT end)."""
    rest = source.split(start, 1)[1]
    return rest.split("\nSCRIPT\n", 1)[0]


class MacSshClient(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mac-ssh-client-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ssh = self.home / ".ssh"
        self.ssh.mkdir(mode=0o700)
        self.config = self.ssh / "config"
        self.user = pwd.getpwuid(os.getuid()).pw_name
        self.group = grp.getgrgid(os.getgid()).gr_name
        # ssh expands ~ from the password database, not $HOME, so point the config's ~ at the temp home.
        self.setup = SETUP.replace("~/.ssh", str(self.ssh))
        self.chown = 'chown "$MAC_USER:$MAC_USER"', f'chown "{self.user}:{self.group}"'
        self.setup = self.setup.replace(*self.chown).replace(
            '-o "$MAC_USER" -g "$MAC_USER"', f'-o "{self.user}" -g "{self.group}"')

    def install(self, secret="macbook-ssh-access"):
        env = dict(os.environ, MAC_USER=self.user, MAC_HOME=str(self.home), MAC_SECRET=secret)
        script = "MAC_USER=$MAC_USER\nMAC_SECRET=$MAC_SECRET\n" + self.setup
        subprocess.run(["bash", "-eu", "-c", script], check=True, capture_output=True, text=True,
                       timeout=10, env=env)

    def effective(self, alias):
        result = subprocess.run(["ssh", "-G", "-F", str(self.config), alias],
                                check=True, capture_output=True, text=True, timeout=5)
        return dict(line.split(" ", 1) for line in result.stdout.splitlines())

    def check_mac(self, secret="macbook-ssh-access"):
        mac = self.effective("mac")
        self.assertEqual((mac["hostname"], mac["port"]), ("macbook-ssh.cc-games.dev", "22"))
        self.assertIn("mac-access-proxy", mac["proxycommand"])
        self.assertEqual(mac["user"], "ejcampbell")
        self.assertEqual(mac["identityfile"], str(self.ssh / "to-mac"))
        self.assertEqual(mac["identitiesonly"], "yes")
        self.assertEqual(mac["hostkeyalias"], "ejs-macbook-pro-via-fcvm")
        self.assertEqual(mac["stricthostkeychecking"], "true")
        self.assertEqual(mac["userknownhostsfile"], str(self.ssh / "known_hosts.mac"))
        proxy = self.ssh / "mac-access-proxy"
        self.assertEqual(proxy.stat().st_mode & 0o777, 0o700)
        text = proxy.read_text()
        self.assertIn(f"--secret-id {secret} ", text, "each account reads its own service token")
        self.assertNotIn("@MAC_SECRET@", text)
        run = [l for l in text.splitlines() if "cloudflared access ssh" in l]
        self.assertEqual(len(run), 1)
        for flag in ("--secret", "--id", "--service-token"):
            self.assertNotIn(flag, run[0], "the token reaches cloudflared through its environment, never argv")
        # The old names are gone: ssh treats them as plain host names again.
        for old in ("macbook", "macbook-desktop"):
            config = self.effective(old)
            self.assertEqual(config["hostname"], old)
            self.assertNotIn("proxycommand", config)
            self.assertNotIn("proxyjump", config)
        known = (self.ssh / "known_hosts.mac").read_text().splitlines()
        self.assertEqual(known[0], f"ejs-macbook-pro-via-fcvm ssh-ed25519 {REMOTE_LOGIN_KEY}",
                         "Remote Login's key is pinned first")
        self.assertLessEqual(len(known), 2, "at most the retired desktop sshd's key besides, for the switch")

    def test_fresh_install_writes_one_alias(self):
        self.install()
        self.check_mac()
        for name in ("config", "config.d-mac", "known_hosts.mac"):
            self.assertEqual((self.ssh / name).stat().st_mode & 0o777, 0o600)
        managed = (self.ssh / "config.d-mac").read_text()
        self.assertEqual(re.findall(r"(?im)^\s*host\s+(.*)$", managed), ["mac"])
        # The installer's ~/.ssh is the temp home here (see setUp).
        self.assertEqual(self.config.read_text().splitlines()[0], f"Include {self.ssh}/config.d-mac")

    def test_each_account_gets_its_own_secret(self):
        self.install(secret="macbook-ssh-access-nextjs-ejc3")
        self.check_mac(secret="macbook-ssh-access-nextjs-ejc3")

    def test_migrates_fcvm_arms_old_files_and_stanzas(self):
        other = self.ssh / "config.d-devhop"
        other.write_text("Host fcvm-arm\n    HostName 184.72.40.255\n    User ubuntu\n")
        for old in ("config.d-mac-tunnels", "known_hosts.mac-tunnels", "macbook-access-proxy"):
            (self.ssh / old).write_text("old\n")
        original = (f"Include {self.ssh}/config.d-mac-tunnels\n\n"
                    f"Include {other}\n"
                    "Host *\n    User wrong-user\n"
                    "Host mac\n    Port 1111\n"
                    "Host macbook # old manual configuration\n    Port 9999\n"
                    "# GitHub runners\n"
                    "Host runner-host\n    HostName 192.0.2.1\n    Port 2200\n"
                    "\n"
                    "# The MacBook's desktop session, written by hand.\n"
                    "# Second line of its comment.\n"
                    "Host macbook-desktop\n    HostName 127.0.0.1\n    Port 22222\n    ProxyJump macbook\n"
                    "\n"
                    "# Keep me: the comment of the host after a retired one.\n"
                    "Host after-host\n    Port 2201\n")
        self.config.write_text(original)
        self.install()
        self.check_mac()
        for old in ("config.d-mac-tunnels", "known_hosts.mac-tunnels", "macbook-access-proxy"):
            self.assertFalse((self.ssh / old).exists(), old)
        self.assertEqual(self.effective("fcvm-arm")["hostname"], "184.72.40.255")
        self.assertEqual(self.effective("fcvm-arm")["user"], "ubuntu")
        self.assertEqual(self.effective("runner-host")["port"], "2200")
        self.assertEqual(self.effective("after-host")["port"], "2201")
        text = self.config.read_text()
        for gone in ("config.d-mac-tunnels", "Port 9999", "Port 1111", "Port 22222", "desktop session", "Second line"):
            self.assertNotIn(gone, text)
        for kept in ("# GitHub runners\nHost runner-host",
                     "# Keep me: the comment of the host after a retired one.\nHost after-host"):
            self.assertIn(kept, text)
        backups = [p for p in self.ssh.iterdir() if p.name.startswith("config.before-mac.")]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        before = {p.name: p.read_bytes() for p in self.ssh.iterdir()}
        self.install()
        self.assertEqual({p.name: p.read_bytes() for p in self.ssh.iterdir()}, before, "a rerun changes nothing")

    def test_migrates_nextjs_ejc3s_hand_installed_aliases(self):
        (self.ssh / "config.d-macbook").write_text("Host macbook\n    Port 2222\n")
        self.config.write_text("Include config.d-mac\n\nHost mac\n    HostName 127.0.0.1\n    Port 2222\n")
        self.install(secret="macbook-ssh-access-nextjs-ejc3")
        self.check_mac(secret="macbook-ssh-access-nextjs-ejc3")
        self.assertFalse((self.ssh / "config.d-macbook").exists())
        self.assertNotIn("2222", self.config.read_text())
        self.assertNotIn("Include config.d-mac\n", self.config.read_text())


class Wiring(unittest.TestCase):
    def test_fcvm_arm_gets_it_as_ubuntu_with_its_token_and_x86_does_not(self):
        source = (ROOT / "dev-user-data.tf").read_text()
        arm, x86 = source.split("  arm_user_data = <<-SCRIPT", 1)[1].split("  x86_user_data = <<-SCRIPT", 1)
        self.assertIn("MAC_USER=ubuntu\nMAC_SECRET=macbook-ssh-access\n${local.mac_cloudflared_install}\n"
                      "${local.mac_ssh_client_setup}", arm)
        self.assertNotIn("${local.mac_ssh_client_setup}", x86)

    def test_nextjs_dev_installs_it_for_ejc3_only(self):
        source = (ROOT / "nextjs-user-data.tf").read_text()
        self.assertEqual(source.count("${local.mac_ssh_client_setup}"), 1)
        self.assertIn("if id ejc3 >/dev/null 2>&1; then\nMAC_USER=ejc3\nMAC_SECRET=macbook-ssh-access-nextjs-ejc3\n"
                      "${local.mac_cloudflared_install}\n${local.mac_ssh_client_setup}\nfi\n", source)

    def test_the_pinned_cloudflared_is_checked_before_and_after_download(self):
        cfd = re.search(r"mac_cloudflared_install = <<-MACCFD\n(.*?)\nMACCFD", SOURCE, re.S).group(1)
        self.assertIn("${local.cloudflared_version}/cloudflared-linux-arm64", cfd)
        self.assertEqual(cfd.count("${local.cloudflared_sha256_linux_arm}"), 2)
        self.assertIn('"/home/$MAC_USER/.local/bin/cloudflared"', cfd)
        self.assertIn('PATH="$HOME/.local/bin:$PATH" exec cloudflared', SETUP)


class Cloudflare(unittest.TestCase):
    def test_the_route_ends_at_remote_login(self):
        services = re.findall(r'service\s*=\s*"(ssh://[^"]+)"', TUNNEL)
        self.assertEqual(services, ["ssh://127.0.0.1:22"])

    def test_access_admits_exactly_the_two_service_tokens_and_no_person(self):
        policy = re.search(r'resource "cloudflare_zero_trust_access_policy" "macbook_ssh_fcvm" \{(.*?)\n\}', TUNNEL, re.S).group(1)
        self.assertIn('decision         = "non_identity"', policy)
        tokens = re.findall(r"service_token = \{ token_id = cloudflare_zero_trust_access_service_token\.(\w+)\.id \}", policy)
        self.assertEqual(tokens, ["macbook_ssh_fcvm", "macbook_ssh_nextjs_ejc3"])
        self.assertNotRegex(policy, r"email|group|everyone|ip\s*=")

    def test_the_nextjs_token_is_readable_by_administration_and_nextjs_dev_only(self):
        readers = re.search(r"macbook_ssh_nextjs_readers = \[(.*?)\n  \]", TUNNEL, re.S).group(1)
        self.assertIn("aws_iam_role.nextjs_dev.arn", readers)
        self.assertNotIn("dev_server", readers)
        grant = re.search(r'resource "aws_iam_role_policy" "nextjs_dev_macbook_ssh" \{(.*?)\n\}', TUNNEL, re.S).group(1)
        self.assertIn("aws_secretsmanager_secret.macbook_ssh_access_nextjs_ejc3.arn", grant)
        self.assertNotIn("macbook_ssh_access.arn", grant)
        self.assertNotIn("macbook_ssh_tunnel", grant)


if __name__ == "__main__":
    unittest.main(verbosity=2)

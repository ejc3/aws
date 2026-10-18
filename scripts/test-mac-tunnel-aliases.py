#!/usr/bin/env python3
"""Exercise the real installer and OpenSSH's config parser without a live tunnel."""

from pathlib import Path
import grp
import os
import pwd
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "mac-reverse-tunnels.tf").read_text()
SETUP = re.search(r"mac_reverse_tunnel_setup = <<-MACSETUP\n(.*?)\nMACSETUP", SOURCE, re.S).group(1)
HOST_KEYS = re.search(r"mac_tunnel_host_keys = <<-MACHOSTKEYS\n(.*?\n)MACHOSTKEYS\n", SOURCE, re.S).group(1)
SETUP = SETUP.replace("${local.mac_tunnel_host_keys}", HOST_KEYS)


class MacTunnelAliases(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mac-tunnel-aliases-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.ssh = self.home / ".ssh"
        self.ssh.mkdir(mode=0o700)
        self.config = self.ssh / "config"
        user = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        self.setup = (SETUP.replace("/home/ubuntu", str(self.home))
                     .replace("~/.ssh", str(self.ssh))
                     .replace("-o ubuntu -g ubuntu", f"-o {user} -g {group}")
                     .replace("chown ubuntu:ubuntu", f"chown {user}:{group}"))

    def install(self):
        subprocess.run(["bash", "-eu", "-c", self.setup], check=True,
                       capture_output=True, text=True, timeout=10)

    def effective(self, alias):
        result = subprocess.run(["ssh", "-G", "-F", str(self.config), alias],
                                check=True, capture_output=True, text=True, timeout=5)
        return dict(line.split(" ", 1) for line in result.stdout.splitlines())

    def check_destinations(self):
        # One alias: the MacBook through Cloudflare Access, ending at its desktop-session sshd.
        mac = self.effective("mac")
        self.assertEqual((mac["hostname"], mac["port"]), ("macbook-ssh.cc-games.dev", "22"))
        self.assertIn("mac-access-proxy", mac["proxycommand"])
        proxy = self.ssh / "mac-access-proxy"
        self.assertEqual(proxy.stat().st_mode & 0o777, 0o700)
        self.assertIn("TUNNEL_SERVICE_TOKEN_SECRET", proxy.read_text())
        run = [l for l in proxy.read_text().splitlines() if "cloudflared access ssh" in l]
        self.assertEqual(len(run), 1)
        for flag in ("--secret", "--id", "--service-token"):
            self.assertNotIn(flag, run[0], "the token reaches cloudflared through its environment, never argv")
        self.assertEqual(mac["user"], "ejcampbell")
        self.assertEqual(mac["identityfile"], str(self.ssh / "to-mac"))
        self.assertEqual(mac["identitiesonly"], "yes")
        self.assertEqual(mac["hostkeyalias"], "ejs-macbook-pro-via-fcvm")
        self.assertEqual(mac["stricthostkeychecking"], "true")
        self.assertEqual(mac["userknownhostsfile"], str(self.ssh / "known_hosts.mac-tunnels"))
        # The old names are gone: ssh treats them as plain host names again.
        for old in ("macbook", "macbook-desktop"):
            config = self.effective(old)
            self.assertEqual(config["hostname"], old)
            self.assertNotIn("proxycommand", config)
            self.assertNotIn("proxyjump", config)
        self.assertFalse((self.ssh / "macbook-access-proxy").exists())

    def test_fresh_install_pins_one_alias(self):
        self.install()
        self.check_destinations()
        result = subprocess.run(["ssh-keygen", "-lf", str(self.ssh / "known_hosts.mac-tunnels")],
                                check=True, capture_output=True, text=True)
        self.assertIn("ED25519", result.stdout)
        for name in ("config", "config.d-mac-tunnels", "known_hosts.mac-tunnels"):
            self.assertEqual((self.ssh / name).stat().st_mode & 0o777, 0o600)
        managed = (self.ssh / "config.d-mac-tunnels").read_text()
        self.assertEqual(re.findall(r"(?im)^\s*host\s+(.*)$", managed), ["mac"])

    def test_migration_preserves_other_hosts_and_overrides_stale_defaults(self):
        other = self.ssh / "config.d-devhop"
        other.write_text("Host fcvm-arm\n    HostName 184.72.40.255\n    User ubuntu\n")
        (self.ssh / "macbook-access-proxy").write_text("#!/bin/sh\n")
        original = (f"Include {other}\n"
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
        # An earlier migration's backup is never overwritten; this one gets its own name.
        (self.ssh / "config.before-mac-tunnels").write_text("earlier\n")
        self.install()
        self.check_destinations()
        self.assertEqual(self.effective("fcvm-arm")["hostname"], "184.72.40.255")
        self.assertEqual(self.effective("fcvm-arm")["user"], "ubuntu")
        self.assertEqual(self.effective("runner-host")["port"], "2200")
        self.assertEqual(self.effective("after-host")["port"], "2201")
        text = self.config.read_text()
        for gone in ("Port 9999", "Port 1111", "Port 22222", "desktop session", "Second line"):
            self.assertNotIn(gone, text)
        for kept in ("# GitHub runners\nHost runner-host", "# Keep me: the comment of the host after a retired one.\nHost after-host"):
            self.assertIn(kept, text)
        self.assertEqual((self.ssh / "config.before-mac-tunnels").read_text(), "earlier\n")
        backups = [p for p in self.ssh.iterdir() if p.name.startswith("config.before-mac-tunnels.")]
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), original)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        before = {p.name: p.read_bytes() for p in self.ssh.iterdir()}
        self.install()
        self.assertEqual({p.name: p.read_bytes() for p in self.ssh.iterdir()}, before)

    def test_installed_only_on_arm_tunnel_endpoint(self):
        source = (ROOT / "dev-user-data.tf").read_text()
        arm, x86 = source.split("  arm_user_data = <<-SCRIPT", 1)[1].split(
            "  x86_user_data = <<-SCRIPT", 1)
        self.assertIn("${local.mac_reverse_tunnel_setup}", arm)
        self.assertNotIn("${local.mac_reverse_tunnel_setup}", x86)


class MacbookCloudflared(unittest.TestCase):
    def test_a_rebuilt_arm_box_gets_the_pinned_cloudflared_the_proxy_runs(self):
        block = re.search(r"macbook_cloudflared_install = <<-MACCFD\n(.*?)\nMACCFD", SOURCE, re.S).group(1)
        self.assertIn("${local.cloudflared_version}/cloudflared-linux-arm64", block)
        self.assertEqual(block.count("${local.cloudflared_sha256_linux_arm}"), 2, "both the installed and the new binary are checked")
        self.assertIn("/home/ubuntu/.local/bin/cloudflared", block)
        user_data = (ROOT / "dev-user-data.tf").read_text()
        self.assertLess(user_data.index("${local.macbook_cloudflared_install}"),
                        user_data.index("${local.mac_reverse_tunnel_setup}"))
        self.assertIn('PATH="$HOME/.local/bin:$PATH" exec cloudflared', SETUP)

    def test_the_cloudflare_route_ends_at_the_desktop_session_sshd(self):
        tunnel = (ROOT / "macbook-ssh.tf").read_text()
        services = re.findall(r'service\s*=\s*"(ssh://[^"]+)"', tunnel)
        self.assertEqual(services, ["ssh://127.0.0.1:22222"], "one ingress, to the Mac's desktop-session sshd")

if __name__ == "__main__":
    unittest.main(verbosity=2)

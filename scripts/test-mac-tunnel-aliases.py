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
        for alias, port in (("mac", "2222"), ("macbook", "2223")):
            config = self.effective(alias)
            self.assertEqual(config["hostname"], "127.0.0.1")
            self.assertEqual(config["port"], port)
            self.assertEqual(config["user"], "ejcampbell")
            self.assertEqual(config["identityfile"], str(self.ssh / "to-mac"))
        book = self.effective("macbook")
        self.assertEqual(book["hostkeyalias"], "ejs-macbook-pro-via-fcvm")
        self.assertEqual(book["stricthostkeychecking"], "true")
        self.assertEqual(book["userknownhostsfile"], str(self.ssh / "known_hosts.mac-tunnels"))

    def test_fresh_install_pins_two_distinct_destinations(self):
        self.install()
        self.check_destinations()
        result = subprocess.run(["ssh-keygen", "-lf", str(self.ssh / "known_hosts.mac-tunnels")],
                                check=True, capture_output=True, text=True)
        self.assertIn("ED25519", result.stdout)
        for name in ("config", "config.d-mac-tunnels", "known_hosts.mac-tunnels"):
            self.assertEqual((self.ssh / name).stat().st_mode & 0o777, 0o600)

    def test_migration_preserves_other_hosts_and_overrides_stale_defaults(self):
        other = self.ssh / "config.d-devhop"
        other.write_text("Host fcvm-arm\n    HostName 184.72.40.255\n    User ubuntu\n")
        original = (f"Include {other}\n"
                    "Host *\n    User wrong-user\n"
                    "Host mac\n    Port 1111\n"
                    "Host macbook # old manual configuration\n    Port 9999\n"
                    "Host runner-host\n    HostName 192.0.2.1\n    Port 2200\n")
        self.config.write_text(original)
        self.install()
        self.check_destinations()
        self.assertEqual(self.effective("fcvm-arm")["hostname"], "184.72.40.255")
        self.assertEqual(self.effective("fcvm-arm")["user"], "ubuntu")
        self.assertEqual(self.effective("runner-host")["port"], "2200")
        self.assertNotIn("Port 9999", self.config.read_text())
        self.assertEqual((self.ssh / "config.before-mac-tunnels").read_text(), original)
        before = {p.name: p.read_bytes() for p in self.ssh.iterdir()}
        self.install()
        self.assertEqual({p.name: p.read_bytes() for p in self.ssh.iterdir()}, before)

    def test_installed_only_on_arm_tunnel_endpoint(self):
        source = (ROOT / "dev-user-data.tf").read_text()
        arm, x86 = source.split("  arm_user_data = <<-SCRIPT", 1)[1].split(
            "  x86_user_data = <<-SCRIPT", 1)
        self.assertIn("${local.mac_reverse_tunnel_setup}", arm)
        self.assertNotIn("${local.mac_reverse_tunnel_setup}", x86)


if __name__ == "__main__":
    unittest.main(verbosity=2)

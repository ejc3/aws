#!/usr/bin/env python3
"""Offline regressions for the real ndev shell snippets; no host services are used."""

from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "nextjs-user-data.tf").read_text()
DOLPHIN = "dolphin-labs.dev"
SSH_HOST = "ssh." + DOLPHIN


def snippet(name, marker):
    match = re.search(
        rf"cat > /usr/local/bin/{re.escape(name)} <<'{marker}'\n(.*?)\n{marker}\n",
        SOURCE,
        re.S,
    )
    if not match:
        raise AssertionError(f"Missing real {name} shell heredoc")
    return match.group(1).replace("$${", "${").replace(
        "${local.dolphin_domain}", DOLPHIN
    )


class DolphinSSHTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-dolphin-ssh.")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.registry = self.root / "registry"
        self.config = self.root / "config"
        self.homes = self.root / "home"
        for directory in (self.bin, self.registry, self.config, self.homes):
            directory.mkdir()
        self.calls = self.root / "systemctl-calls"
        self.env = os.environ.copy()
        self.env.update(
            PATH=f"{self.bin}:/usr/bin:/bin",
            TMPDIR=str(self.root),
            TEST_SYSTEMCTL_CALLS=str(self.calls),
            SUDO_USER="ejc3",
        )
        self.install("ndev-zone", """#!/bin/bash
set -eu
if [ "$1" = --tunnel ]; then
  case "$2" in
    dolphin-labs.dev) echo dolphin-test-tunnel ;;
    cc-games.dev) echo games-test-tunnel ;;
    *) exit 1 ;;
  esac
else
  case "$1" in
    ejc3|skevh) echo dolphin-labs.dev ;;
    colton|connor) echo cc-games.dev ;;
    *) exit 1 ;;
  esac
fi
""")
        self.install("systemctl", """#!/bin/bash
printf '%s\\n' "$*" >> "$TEST_SYSTEMCTL_CALLS"
exit 0
""")
        self.install("id", "#!/bin/bash\nexit 0\n")
        for name, marker in (("ndev-rebuild", "REBUILD"), ("ndev-register", "REG")):
            rendered = snippet(name, marker)
            for original, replacement in (
                ("/usr/local/bin/", str(self.bin) + "/"),
                ("/var/lib/ndev", str(self.registry)),
                ("/etc/cloudflared", str(self.config)),
                ("/home/", str(self.homes) + "/"),
            ):
                rendered = rendered.replace(original, replacement)
            self.install(name, rendered)

    def install(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o700)

    def run_script(self, name, *args):
        return subprocess.run(
            ["bash", str(self.bin / name), *args],
            env=self.env,
            text=True,
            capture_output=True,
            timeout=5,
        )

    def set_registry(self, zone, rows):
        (self.registry / ("registry-" + zone)).write_text(rows)

    def read_config(self, zone):
        return (self.config / ("config-" + zone + ".yml")).read_text()

    def rebuild(self, zone, expected=10):
        result = self.run_script("ndev-rebuild", zone)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return self.read_config(zone)

    def project(self):
        project = self.homes / "ejc3" / "project"
        project.mkdir(parents=True)
        (project / "package.json").write_text("{}\n")
        return str(project)

    def test_empty_dolphin_has_one_ssh_route_before_catchall(self):
        config = self.rebuild(DOLPHIN)
        self.assertEqual(config.count("hostname: " + SSH_HOST), 1)
        self.assertEqual(config.count("service: ssh://127.0.0.1:22"), 1)
        self.assertLess(config.index(SSH_HOST), config.index("http_status:404"))
        self.assertEqual(config.count("service:"), 2)

    def test_dolphin_preserves_existing_http_routes(self):
        self.set_registry(DOLPHIN, "ejc3.dolphin-labs.dev\t3501\tejc3\t/project\n"
                          "preview.dolphin-labs.dev\t3902\tskevh\t/other\n")
        config = self.rebuild(DOLPHIN)
        self.assertIn("hostname: " + SSH_HOST, config)
        for host, port in (("ejc3.dolphin-labs.dev", 3501),
                           ("preview.dolphin-labs.dev", 3902)):
            route = f"  - hostname: {host}\n    service: http://127.0.0.1:{port}\n"
            self.assertIn(route, config)
            self.assertLess(config.index(SSH_HOST), config.index(host))
        self.assertEqual(config.count("service:"), 4)

    def test_cc_games_output_remains_unchanged(self):
        self.set_registry("cc-games.dev", "colton.cc-games.dev\t3729\tcolton\t/game\n")
        self.assertEqual(self.rebuild("cc-games.dev"),
                         "tunnel: games-test-tunnel\n"
                         f"credentials-file: {self.config}/creds-cc-games.dev.json\n"
                         "ingress:\n"
                         "  - hostname: colton.cc-games.dev\n"
                         "    service: http://127.0.0.1:3729\n"
                         "  - service: http_status:404\n")

    def test_repeated_rebuild_does_not_rewrite_config(self):
        original = self.rebuild(DOLPHIN)
        path = self.config / ("config-" + DOLPHIN + ".yml")
        before = path.stat()
        self.assertEqual(self.rebuild(DOLPHIN, expected=0), original)
        after = path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns),
                         (after.st_ino, after.st_mtime_ns))

    def test_reserved_registry_row_cannot_override_ssh(self):
        self.set_registry(DOLPHIN, f"{SSH_HOST}\t9999\tejc3\t/conflict\n"
                          "ejc3.dolphin-labs.dev\t3501\tejc3\t/project\n")
        config = self.rebuild(DOLPHIN)
        self.assertEqual(config.count("hostname: " + SSH_HOST), 1)
        self.assertIn("service: ssh://127.0.0.1:22", config)
        self.assertNotIn(":9999", config)
        self.assertIn("service: http://127.0.0.1:3501", config)

    def test_register_reserved_hostname_refuses_before_writes(self):
        self.set_registry(DOLPHIN, "ejc3.dolphin-labs.dev\t3501\n")
        before = {p.name: p.read_bytes() for p in self.registry.iterdir()}
        result = self.run_script("ndev-register", SSH_HOST, "3999", "ejc3", self.project())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("reserv", (result.stdout + result.stderr).lower())
        self.assertEqual({p.name: p.read_bytes() for p in self.registry.iterdir()}, before)
        self.assertEqual(list(self.config.iterdir()), [])
        self.assertFalse(self.calls.exists(), "Refused registration touched services")

    def test_register_restarts_only_a_changed_tunnel(self):
        project = self.project()
        for first in (True, False):
            with self.subTest(first=first):
                self.calls.write_text("")
                result = self.run_script("ndev-register", "ejc3.dolphin-labs.dev",
                                         "3501", "ejc3", project)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                tunnel_changes = [line for line in self.calls.read_text().splitlines()
                                  if "cloudflared@" in line and
                                  line.split()[0] in ("restart", "reload", "reload-or-restart")]
                self.assertEqual(tunnel_changes,
                                 ["restart cloudflared@dolphin-labs.dev"] if first else [])

    def test_template_does_not_claim_or_send_a_hup_reload(self):
        self.assertNotIn("ExecReload=/bin/kill -HUP", SOURCE)
        self.assertNotIn("cloudflared re-reads its config on SIGHUP", SOURCE)


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""Offline tests for ndev-prune and ndev-run's dead-directory guard.

Runs the real heredoc bodies from nextjs-user-data.tf against a temporary root, with a
systemctl shim that records calls. A published project whose directory was deleted must
be unpublished completely, and nothing that still has a directory may be touched.
"""

from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "nextjs-user-data.tf").read_text()
DOLPHIN = "dolphin-labs.dev"
GAMES = "cc-games.dev"
USERS = "colton connor ejc3 skevh"


def render(text):
    return (text.replace("$${", "${")
            .replace("${local.dolphin_domain}", DOLPHIN)
            .replace('${join(" ", local.nextjs_users)}', USERS))


def heredoc(target, marker):
    match = re.search(rf"cat > {re.escape(target)} <<'{marker}'\n(.*?)\n{marker}\n", SOURCE, re.S)
    if not match:
        raise AssertionError(f"Missing real {target} heredoc")
    body = render(match.group(1))
    if re.search(r"\$\{(local|var|join)", body):
        raise AssertionError(f"Unrendered Terraform interpolation in {target}")
    return body


def tool(name, marker):
    return heredoc(f"/usr/local/bin/{name}", marker)


class NdevPruneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="test-ndev-prune.")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.ndev = self.root / "ndev"
        self.config = self.root / "cloudflared"
        self.homes = self.root / "home"
        self.systemd = self.root / "systemd"
        for d in (self.bin, self.ndev / "instances", self.config, self.homes, self.systemd):
            d.mkdir(parents=True)
        self.calls = self.root / "systemctl-calls"
        self.npm_calls = self.root / "npm-calls"
        self.env = os.environ.copy()
        self.env.update(PATH=f"{self.bin}:/usr/bin:/bin", TMPDIR=str(self.root),
                        TEST_SYSTEMCTL_CALLS=str(self.calls), TEST_NPM_CALLS=str(self.npm_calls))
        self.install("ndev-zone", """#!/bin/bash
set -eu
if [ "$1" = --tunnel ]; then
  case "$2" in
    dolphin-labs.dev) echo dolphin-test-tunnel ;;
    cc-games.dev) echo games-test-tunnel ;;
    *) exit 1 ;;
  esac
fi
""")
        self.install("systemctl", "#!/bin/bash\nprintf '%s\\n' \"$*\" >> \"$TEST_SYSTEMCTL_CALLS\"\nexit 0\n")
        for name in ("npm", "npx", "pnpm", "node"):
            self.install(name, "#!/bin/bash\nprintf '%s %s\\n' \"$0\" \"$*\" >> \"$TEST_NPM_CALLS\"\nexit 0\n")
        for name, marker in (("ndev-rebuild", "REBUILD"), ("ndev-prune", "PRUNE"), ("ndev-run", "RUN")):
            self.install(name, self.localize(tool(name, marker)))

    def localize(self, text):
        for original, replacement in (
            ("/usr/local/bin/", str(self.bin) + "/"),
            ("/var/lib/ndev", str(self.ndev)),
            ("/etc/cloudflared", str(self.config)),
            ("/etc/systemd/system", str(self.systemd)),
            ("/home/", str(self.homes) + "/"),
        ):
            text = text.replace(original, replacement)
        return text

    def install(self, name, content):
        path = self.bin / name
        path.write_text(content)
        path.chmod(0o700)

    def run_tool(self, name, *args):
        return subprocess.run(["bash", str(self.bin / name), *args], env=self.env,
                              text=True, capture_output=True, timeout=10)

    # ------------------------------------------------------------------ fixtures
    def publish(self, label, who, zone, port, live=True, legacy=False, subdir=None):
        """Record a project the way ndev-register does: env, drop-in, registry row."""
        d = self.homes / who / (subdir or ("worktrees/" + label))
        if live:
            d.mkdir(parents=True, exist_ok=True)
            (d / "package.json").write_text("{}\n")
        host = f"{label}.{zone}"
        env = f"HOST={host}\nPORT={port}\nDIR={d}\nWHO={who}\n"
        (self.ndev / "instances" / f"{label}.env").write_text(env)
        if legacy:
            (self.ndev / f"{label}.env").write_text(env)
        drop = self.systemd / f"ndev@{label}.service.d"
        drop.mkdir(exist_ok=True)
        (drop / "user.conf").write_text(f"[Service]\nUser={who}\n")
        reg = self.ndev / f"registry-{zone}"
        rows = reg.read_text() if reg.exists() else ""
        rows += f"{host}\t{port}\t{who}\t{d}\n"
        reg.write_text("".join(sorted(set(rows.splitlines(keepends=True)))))
        return host, d

    def rebuild_all(self):
        for zone in (DOLPHIN, GAMES):
            r = self.run_tool("ndev-rebuild", zone)
            self.assertIn(r.returncode, (0, 10), r.stdout + r.stderr)

    def snapshot(self):
        out = {}
        for base in (self.ndev, self.config, self.systemd):
            for p in sorted(base.rglob("*")):
                if p.is_file():
                    st = p.stat()
                    out[str(p)] = (p.read_bytes(), st.st_ino, st.st_mtime_ns)
        return out

    def calls_list(self):
        return self.calls.read_text().splitlines() if self.calls.exists() else []

    def fleet(self):
        """skevh's incident: two deleted worktrees next to live projects in both zones."""
        self.dead1, _ = self.publish("skevh-comeback-panel", "skevh", DOLPHIN, 3301, live=False)
        self.dead2, _ = self.publish("skevh-per-game-follow", "skevh", DOLPHIN, 3302, live=False)
        self.base, _ = self.publish("skevh", "skevh", DOLPHIN, 3300, legacy=True, subdir="dolphin-labs/web")
        self.family, _ = self.publish("ejc3-family", "ejc3", DOLPHIN, 3722, subdir="family")
        self.colton, _ = self.publish("colton", "colton", GAMES, 3729, legacy=True, subdir="game")
        self.rebuild_all()
        self.calls.write_text("")

    # --------------------------------------------------------------------- tests
    def test_dead_projects_are_unpublished_completely(self):
        self.fleet()
        games_before = (self.config / f"config-{GAMES}.yml").read_bytes()
        r = self.run_tool("ndev-prune")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for label, host in (("skevh-comeback-panel", self.dead1), ("skevh-per-game-follow", self.dead2)):
            with self.subTest(label=label):
                self.assertFalse((self.ndev / "instances" / f"{label}.env").exists())
                self.assertFalse((self.systemd / f"ndev@{label}.service.d").exists())
                self.assertNotIn(host, (self.ndev / f"registry-{DOLPHIN}").read_text())
                self.assertNotIn(host, (self.config / f"config-{DOLPHIN}.yml").read_text())
                self.assertIn(f"disable --now ndev@{label}.service", self.calls_list())
        config = (self.config / f"config-{DOLPHIN}.yml").read_text()
        for host, port in ((self.base, 3300), (self.family, 3722)):
            self.assertIn(f"  - hostname: {host}\n    service: http://127.0.0.1:{port}\n", config)
        self.assertIn("hostname: ssh.dolphin-labs.dev", config)
        self.assertTrue(config.endswith("  - service: http_status:404\n"))
        calls = self.calls_list()
        self.assertIn(f"restart cloudflared@{DOLPHIN}", calls)
        self.assertNotIn(f"restart cloudflared@{GAMES}", calls)
        self.assertEqual((self.config / f"config-{GAMES}.yml").read_bytes(), games_before)
        self.assertIn("daemon-reload", calls)
        self.assertLess(calls.index("disable --now ndev@skevh-comeback-panel.service"),
                        calls.index(f"restart cloudflared@{DOLPHIN}"))

    def test_live_projects_are_never_touched(self):
        self.fleet()
        live = ("skevh", "ejc3-family", "colton")
        self.run_tool("ndev-prune")
        for label in live:
            with self.subTest(label=label):
                self.assertTrue((self.ndev / "instances" / f"{label}.env").exists())
                self.assertTrue((self.systemd / f"ndev@{label}.service.d" / "user.conf").exists())
                for call in self.calls_list():
                    self.assertNotIn(f"ndev@{label}.", call)
        self.assertTrue((self.ndev / "skevh.env").exists())
        self.assertTrue((self.ndev / "colton.env").exists())
        self.assertIn(self.colton, (self.ndev / f"registry-{GAMES}").read_text())

    def test_nothing_dead_means_no_side_effects(self):
        self.publish("ejc3", "ejc3", DOLPHIN, 3500, legacy=True, subdir="dolphin-labs/web")
        self.publish("colton", "colton", GAMES, 3729, legacy=True, subdir="game")
        self.rebuild_all()
        self.calls.write_text("")
        before = self.snapshot()
        r = self.run_tool("ndev-prune")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.calls_list(), [])
        self.assertEqual(self.snapshot(), before)

    def test_second_run_is_a_no_op(self):
        self.fleet()
        first = self.run_tool("ndev-prune")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.calls.write_text("")
        before = self.snapshot()
        second = self.run_tool("ndev-prune")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.calls_list(), [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(second.stdout, "")

    def test_dead_base_label_removes_legacy_env_too(self):
        # The setup script migrates /var/lib/ndev/<user>.env back into instances/ when the
        # instance file is missing, so leaving the legacy file would resurrect the project.
        host, _ = self.publish("connor", "connor", GAMES, 3641, live=False, legacy=True)
        self.rebuild_all()
        r = self.run_tool("ndev-prune")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse((self.ndev / "instances" / "connor.env").exists())
        self.assertFalse((self.ndev / "connor.env").exists())
        self.assertNotIn(host, (self.config / f"config-{GAMES}.yml").read_text())
        self.assertIn("disable --now ndev@connor.service", self.calls_list())

    def test_base_label_kept_while_any_record_is_live(self):
        # Instance file live, legacy file stale: ndev-run serves the instance, so keep it.
        self.publish("ejc3", "ejc3", DOLPHIN, 3500, subdir="dolphin-labs/web")
        (self.ndev / "ejc3.env").write_text(
            f"HOST=ejc3.{DOLPHIN}\nPORT=3500\nDIR={self.homes}/ejc3/deleted\nWHO=ejc3\n")
        self.rebuild_all()
        self.calls.write_text("")
        before = self.snapshot()
        self.run_tool("ndev-prune")
        self.assertEqual(self.calls_list(), [])
        self.assertEqual(self.snapshot(), before)

    def test_registry_row_pointing_at_a_live_dir_survives(self):
        host, _ = self.publish("ejc3-gone", "ejc3", DOLPHIN, 3600, live=False)
        live_dir = self.homes / "ejc3" / "elsewhere"
        live_dir.mkdir(parents=True)
        reg = self.ndev / f"registry-{DOLPHIN}"
        reg.write_text(f"{host}\t3600\tejc3\t{live_dir}\n")
        self.rebuild_all()
        self.run_tool("ndev-prune")
        self.assertIn(host, reg.read_text())
        self.assertFalse((self.ndev / "instances" / "ejc3-gone.env").exists())

    def test_malformed_env_is_left_alone(self):
        (self.ndev / "instances" / "ejc3-odd.env").write_text(f"HOST=ejc3-odd.{DOLPHIN}\nPORT=1\nDIR=\nWHO=ejc3\n")
        (self.ndev / "instances" / "ejc3-root.env").write_text(f"HOST=ejc3-root.{DOLPHIN}\nPORT=1\nDIR=/nonexistent\nWHO=ejc3\n")
        r = self.run_tool("ndev-prune")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.ndev / "instances" / "ejc3-odd.env").exists())
        self.assertTrue((self.ndev / "instances" / "ejc3-root.env").exists())
        self.assertEqual(self.calls_list(), [])

    def test_non_user_legacy_file_is_ignored(self):
        (self.ndev / "notauser.env").write_text(f"HOST=x\nPORT=1\nDIR={self.homes}/x/gone\nWHO=x\n")
        r = self.run_tool("ndev-prune")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.ndev / "notauser.env").exists())
        self.assertEqual(self.calls_list(), [])

    def test_interrupted_run_is_finished_by_the_next(self):
        # Registry already cleaned but ingress and env left behind (a crash mid-run).
        host, _ = self.publish("skevh-x", "skevh", DOLPHIN, 3303, live=False)
        self.rebuild_all()
        (self.ndev / f"registry-{DOLPHIN}").write_text("")
        self.calls.write_text("")
        self.run_tool("ndev-prune")
        self.assertNotIn(host, (self.config / f"config-{DOLPHIN}.yml").read_text())
        self.assertIn(f"restart cloudflared@{DOLPHIN}", self.calls_list())
        self.assertFalse((self.ndev / "instances" / "skevh-x.env").exists())

    def test_clean_ingress_is_not_restarted(self):
        # Interrupted after the ingress rebuild: only the unit and files are left to remove.
        self.publish("skevh-y", "skevh", DOLPHIN, 3305, live=False)
        (self.ndev / f"registry-{DOLPHIN}").write_text("")
        self.rebuild_all()
        self.calls.write_text("")
        r = self.run_tool("ndev-prune")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("disable --now ndev@skevh-y.service", self.calls_list())
        self.assertFalse([c for c in self.calls_list() if "cloudflared" in c])
        self.assertFalse((self.ndev / "instances" / "skevh-y.env").exists())

    def test_non_user_legacy_file_cannot_keep_a_dead_instance(self):
        # ndev-run serves instances/<label>.env; a stray same-named file beside it is not a record.
        self.publish("ejc3-foo", "ejc3", DOLPHIN, 3306, live=False)
        live = self.homes / "ejc3" / "live"
        live.mkdir(parents=True)
        (self.ndev / "ejc3-foo.env").write_text(f"HOST=ejc3-foo.{DOLPHIN}\nPORT=1\nDIR={live}\nWHO=ejc3\n")
        self.run_tool("ndev-prune")
        self.assertFalse((self.ndev / "instances" / "ejc3-foo.env").exists())
        self.assertTrue((self.ndev / "ejc3-foo.env").exists())

    # ------------------------------------------------------------------ ndev-run
    def test_run_check_skips_a_deleted_project(self):
        self.publish("skevh-gone", "skevh", DOLPHIN, 3304, live=False)
        r = self.run_tool("ndev-run", "--check", "skevh-gone")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stderr.count("\n"), 1)
        self.assertIn("project dir", r.stderr)
        self.assertIn("is gone", r.stderr)
        self.assertIn("ndev-prune", r.stderr)

    def test_run_check_skips_an_unpublished_label(self):
        r = self.run_tool("ndev-run", "--check", "nobody")
        self.assertEqual(r.returncode, 1)
        self.assertIn("has not published", r.stderr)

    def test_run_check_passes_a_live_project_without_starting_it(self):
        self.publish("skevh", "skevh", DOLPHIN, 3300, subdir="web")
        r = self.run_tool("ndev-run", "--check", "skevh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.npm_calls.exists())

    def test_run_on_a_deleted_project_exits_cleanly_without_serving(self):
        self.publish("skevh-gone", "skevh", DOLPHIN, 3304, live=False)
        r = self.run_tool("ndev-run", "skevh-gone")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("is gone", r.stderr)
        self.assertFalse(self.npm_calls.exists())

    def test_run_still_serves_a_live_project(self):
        _, d = self.publish("skevh", "skevh", DOLPHIN, 3300, subdir="web")
        (d / "node_modules").mkdir()
        r = self.run_tool("ndev-run", "skevh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("--port 3300 --hostname 127.0.0.1", self.npm_calls.read_text())

    # --------------------------------------------------------------- wiring
    def test_unit_uses_the_check_as_its_exec_condition(self):
        unit = heredoc("/etc/systemd/system/ndev@.service", "UNIT")
        self.assertIn("ExecCondition=/usr/local/bin/ndev-run --check %i\n", unit)
        self.assertLess(unit.index("ExecCondition="), unit.index("ExecStart=/usr/local/bin/ndev-run %i"))
        self.assertIn("Restart=always", unit)

    def test_prune_timer_is_installed_and_enabled(self):
        timer = heredoc("/etc/systemd/system/ndev-prune.timer", "UNIT")
        self.assertIn("OnCalendar=", timer)
        self.assertIn("Persistent=true", timer)
        self.assertTrue(heredoc("/etc/systemd/system/ndev-prune.service", "UNIT")
                        .endswith("\nExecStart=/usr/local/bin/ndev-prune"))
        self.assertIn("systemctl enable --now ndev-prune.timer", SOURCE)

    def test_setup_prunes_before_rebuild_and_reenable(self):
        call = SOURCE.index("\n/usr/local/bin/ndev-prune ||")
        self.assertGreater(call, SOURCE.index("cat > /usr/local/bin/ndev-prune <<'PRUNE'"))
        self.assertGreater(call, SOURCE.index("# Migrate legacy /var/lib/ndev/$USER.env"))
        self.assertLess(call, SOURCE.index("\n${local.nextjs_zone_rebuild}\n"))
        self.assertLess(call, SOURCE.index("# Re-enable previously published projects"))

    def test_setup_sync_prunes_before_its_health_check(self):
        sync = heredoc("/usr/local/bin/setup-sync.new", "SETUPSYNC")
        self.assertLess(sync.index('bash "$NEXT"'), sync.index("/usr/local/bin/ndev-prune"))
        self.assertLess(sync.index("/usr/local/bin/ndev-prune"), sync.index('FAILED=""'))

    def test_reenable_loop_skips_a_missing_dir(self):
        start = SOURCE.index("# Re-enable previously published projects")
        loop = SOURCE[start:SOURCE.index("\ndone\n", start)]
        guard = loop.index('[ -d "$DIR_LINE" ]')
        self.assertLess(guard, loop.index('systemctl enable --now "ndev@$label.service"'))
        self.assertIn('[ -d "$(sed -n \'s/^DIR=//p\' "/var/lib/ndev/$u.env" | head -1)" ]', loop)


if __name__ == "__main__":
    unittest.main(verbosity=2)

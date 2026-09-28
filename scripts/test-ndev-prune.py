#!/usr/bin/env python3
"""Offline tests for ndev-prune and ndev-run's dead-directory guard.

Runs the real heredoc bodies from nextjs-user-data.tf against a temporary root, with a
systemctl shim that records calls. A published project whose directory was deleted must
be unpublished completely, and nothing that still has a directory may be touched.
"""

from pathlib import Path
import os
import re
import fcntl
import subprocess
import tempfile
import time
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
else
  case "$1" in
    ejc3|skevh) echo dolphin-labs.dev ;;
    colton|connor) echo cc-games.dev ;;
  esac
fi
""")
        # Records every call. Optional hooks: slow every call down, recreate a project dir
        # when its unit is disabled, and fail tunnel restarts while a flag file exists.
        self.install("systemctl", """#!/bin/bash
printf '%s\\n' "$*" >> "$TEST_SYSTEMCTL_CALLS"
[ -z "${TEST_SYSTEMCTL_SLEEP:-}" ] || sleep "$TEST_SYSTEMCTL_SLEEP"
if [ -n "${TEST_REVIVE_LABEL:-}" ] && [ "$*" = "disable --now ndev@$TEST_REVIVE_LABEL.service" ]; then
  mkdir -p "$TEST_REVIVE_DIR"
fi
if [ "$1" = restart ] && [ -e "${TEST_FAIL_RESTART:-/nonexistent}" ] &&
   { [ -z "${TEST_FAIL_ZONE:-}" ] || [ "$2" = "cloudflared@$TEST_FAIL_ZONE" ]; }; then exit 1; fi
exit 0
""")
        self.install("id", "#!/bin/bash\nexit 0\n")
        for name in ("npm", "npx", "pnpm", "node"):
            self.install(name, "#!/bin/bash\nprintf '%s %s\\n' \"$0\" \"$*\" >> \"$TEST_NPM_CALLS\"\nexit 0\n")
        for name, marker in (("ndev-rebuild", "REBUILD"), ("ndev-prune", "PRUNE"),
                             ("ndev-run", "RUN"), ("ndev-register", "REG")):
            self.install(name, self.localize(tool(name, marker)))
        # The real ndev-rebuild, failing while a flag file exists.
        (self.bin / "ndev-rebuild").rename(self.bin / "ndev-rebuild.real")
        self.install("ndev-rebuild", f"""#!/bin/bash
if [ -e "${{TEST_FAIL_REBUILD:-/nonexistent}}" ] &&
   {{ [ -z "${{TEST_FAIL_ZONE:-}}" ] || [ "$1" = "$TEST_FAIL_ZONE" ]; }}; then exit 1; fi
exec bash {self.bin}/ndev-rebuild.real "$@"
""")

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
                if p.is_file() and p.name != ".lock":
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

    # ------------------------------------------------------ locking and retries
    def hold_lock(self):
        fd = os.open(self.ndev / ".lock", os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fd

    def spawn(self, name, *args, env=None):
        return subprocess.Popen(["bash", str(self.bin / name), *args], env=env or self.env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_prune_waits_for_the_shared_lock(self):
        self.fleet()
        fd = self.hold_lock()
        proc = self.spawn("ndev-prune")
        time.sleep(0.7)
        self.assertIsNone(proc.poll(), "ndev-prune ran while the lock was held")
        self.assertEqual(self.calls_list(), [])
        self.assertTrue((self.ndev / "instances" / "skevh-comeback-panel.env").exists())
        os.close(fd)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertFalse((self.ndev / "instances" / "skevh-comeback-panel.env").exists())
        self.assertEqual([p.name for p in self.ndev.glob(".prune.*")], [])

    def test_register_waits_for_the_shared_lock(self):
        project = self.homes / "ejc3" / "worktrees" / "new"
        project.mkdir(parents=True)
        (project / "package.json").write_text("{}\n")
        env = dict(self.env, SUDO_USER="ejc3")
        fd = self.hold_lock()
        proc = self.spawn("ndev-register", f"ejc3-new.{DOLPHIN}", "3400", "ejc3", str(project), env=env)
        time.sleep(0.7)
        self.assertIsNone(proc.poll(), "ndev-register wrote while the lock was held")
        self.assertFalse((self.ndev / "instances" / "ejc3-new.env").exists())
        self.assertFalse((self.ndev / f"registry-{DOLPHIN}").exists())
        os.close(fd)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertTrue((self.ndev / "instances" / "ejc3-new.env").exists())

    def test_concurrent_prunes_serialize(self):
        self.fleet()
        env = dict(self.env, TEST_SYSTEMCTL_SLEEP="0.2")
        procs = [self.spawn("ndev-prune", env=env) for _ in range(2)]
        for proc in procs:
            out, err = proc.communicate(timeout=30)
            self.assertEqual(proc.returncode, 0, out + err)
        calls = self.calls_list()
        for label in ("skevh-comeback-panel", "skevh-per-game-follow"):
            self.assertEqual(calls.count(f"disable --now ndev@{label}.service"), 1, calls)
        self.assertEqual(calls.count(f"restart cloudflared@{DOLPHIN}"), 1, calls)

    def test_project_dir_back_before_cleanup_is_not_removed(self):
        # The dir reappears after classification (here, as its unit is disabled): the
        # re-check before deletion must keep the records and undo the disable.
        self.fleet()
        revived = self.homes / "skevh" / "worktrees" / "skevh-comeback-panel"
        env = dict(self.env, TEST_REVIVE_LABEL="skevh-comeback-panel", TEST_REVIVE_DIR=str(revived))
        proc = self.spawn("ndev-prune", env=env)
        out, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertTrue((self.ndev / "instances" / "skevh-comeback-panel.env").exists())
        self.assertTrue((self.systemd / "ndev@skevh-comeback-panel.service.d" / "user.conf").exists())
        self.assertIn(self.dead1, (self.ndev / f"registry-{DOLPHIN}").read_text())
        self.assertIn("enable --now ndev@skevh-comeback-panel.service", self.calls_list())
        # The other dead label is still pruned.
        self.assertFalse((self.ndev / "instances" / "skevh-per-game-follow.env").exists())

    def test_failed_rebuild_keeps_records_for_the_next_run(self):
        self.fleet()
        flag = self.root / "fail-rebuild"
        flag.write_text("")
        self.env["TEST_FAIL_REBUILD"] = str(flag)
        first = self.run_tool("ndev-prune")
        self.assertNotEqual(first.returncode, 0)
        for label in ("skevh-comeback-panel", "skevh-per-game-follow"):
            self.assertTrue((self.ndev / "instances" / f"{label}.env").exists())
            self.assertTrue((self.systemd / f"ndev@{label}.service.d").exists())
        self.assertIn(self.dead1, (self.config / f"config-{DOLPHIN}.yml").read_text())
        flag.unlink()
        self.calls.write_text("")
        second = self.run_tool("ndev-prune")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertFalse((self.ndev / "instances" / "skevh-comeback-panel.env").exists())
        self.assertNotIn(self.dead1, (self.config / f"config-{DOLPHIN}.yml").read_text())
        self.assertIn(f"restart cloudflared@{DOLPHIN}", self.calls_list())

    def test_failed_restart_is_retried_although_the_config_is_already_clean(self):
        self.fleet()
        flag = self.root / "fail-restart"
        flag.write_text("")
        self.env["TEST_FAIL_RESTART"] = str(flag)
        first = self.run_tool("ndev-prune")
        self.assertNotEqual(first.returncode, 0)
        self.assertTrue((self.ndev / "instances" / "skevh-comeback-panel.env").exists())
        self.assertNotIn(self.dead1, (self.config / f"config-{DOLPHIN}.yml").read_text())
        flag.unlink()
        self.calls.write_text("")
        second = self.run_tool("ndev-prune")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn(f"restart cloudflared@{DOLPHIN}", self.calls_list())
        self.assertFalse((self.ndev / "instances" / "skevh-comeback-panel.env").exists())
        self.assertFalse((self.ndev / f".retry-{DOLPHIN}").exists())
        self.calls.write_text("")
        self.assertEqual(self.run_tool("ndev-prune").returncode, 0)
        self.assertEqual(self.calls_list(), [])

    def test_failure_in_one_zone_does_not_hold_back_the_other(self):
        self.fleet()
        dead_game, _ = self.publish("connor-old", "connor", GAMES, 3642, live=False)
        # Fail only dolphin: point its tunnel lookup at nothing.
        self.install("ndev-zone", (self.bin / "ndev-zone").read_text().replace(
            "dolphin-labs.dev) echo dolphin-test-tunnel ;;", "dolphin-labs.dev) exit 1 ;;"))
        r = self.run_tool("ndev-prune")
        self.assertNotEqual(r.returncode, 0)
        self.assertTrue((self.ndev / "instances" / "skevh-comeback-panel.env").exists())
        self.assertFalse((self.ndev / "instances" / "connor-old.env").exists())
        self.assertNotIn(dead_game, (self.config / f"config-{GAMES}.yml").read_text())

    # ------------------------------------------- aliases and retained labels
    def family_with_alias(self, live):
        """ejc3-family on dolphin plus its pinned cc-games alias to the same checkout."""
        host, d = self.publish("ejc3-family", "ejc3", DOLPHIN, 3722, live=live, subdir="family")
        self.colton, _ = self.publish("colton", "colton", GAMES, 3729, subdir="game")
        reg = self.ndev / f"registry-{GAMES}"
        reg.write_text("".join(sorted([reg.read_text(), f"family.{GAMES}\t3722\tejc3\t{d}\n"])))
        self.rebuild_all()
        self.calls.write_text("")
        return host, d

    def test_cross_zone_alias_of_a_dead_project_is_removed(self):
        host, _ = self.family_with_alias(live=False)
        self.assertIn(f"family.{GAMES}", (self.config / f"config-{GAMES}.yml").read_text())
        r = self.run_tool("ndev-prune")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for zone, gone in ((DOLPHIN, host), (GAMES, f"family.{GAMES}")):
            self.assertNotIn(gone, (self.ndev / f"registry-{zone}").read_text())
            self.assertNotIn(gone, (self.config / f"config-{zone}.yml").read_text())
            self.assertIn(f"restart cloudflared@{zone}", self.calls_list())
        self.assertIn(self.colton, (self.config / f"config-{GAMES}.yml").read_text())
        # Setup's pin_route must not bring the alias back while the checkout is gone.
        self.run_setup_stretch()
        self.assertNotIn(f"family.{GAMES}", (self.ndev / f"registry-{GAMES}").read_text())
        self.assertNotIn(f"family.{GAMES}", (self.config / f"config-{GAMES}.yml").read_text())

    def test_alias_to_a_live_checkout_is_kept(self):
        self.family_with_alias(live=True)
        before = self.snapshot()
        self.run_tool("ndev-prune")
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.calls_list(), [])

    def test_pin_route_drops_a_stale_alias_even_without_an_env(self):
        (self.ndev / f"registry-{GAMES}").write_text(f"family.{GAMES}\t3722\tejc3\t{self.homes}/ejc3/family\n")
        self.run_setup_stretch()
        self.assertNotIn(f"family.{GAMES}", (self.ndev / f"registry-{GAMES}").read_text())

    def test_retained_label_is_republished_when_its_dir_returns(self):
        # Only one dead label, so the retry run has nothing dead and must not exit early.
        host, d = self.publish("skevh-back", "skevh", DOLPHIN, 3310, live=False)
        self.publish("skevh", "skevh", DOLPHIN, 3300, subdir="web")
        self.rebuild_all()
        flag = self.root / "fail-rebuild"
        flag.write_text("")
        self.env["TEST_FAIL_REBUILD"] = str(flag)
        self.assertNotEqual(self.run_tool("ndev-prune").returncode, 0)
        self.assertTrue((self.ndev / ".retained-skevh-back").exists())
        self.assertNotIn(host, (self.ndev / f"registry-{DOLPHIN}").read_text())
        flag.unlink()
        d.mkdir(parents=True)
        (d / "package.json").write_text("{}\n")
        self.calls.write_text("")
        r = self.run_tool("ndev-prune")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("enable --now ndev@skevh-back.service", self.calls_list())
        self.assertIn(f"{host}\t3310\tskevh\t{d}", (self.ndev / f"registry-{DOLPHIN}").read_text())
        self.assertIn(f"  - hostname: {host}\n    service: http://127.0.0.1:3310\n",
                      (self.config / f"config-{DOLPHIN}.yml").read_text())
        self.assertTrue((self.ndev / "instances" / "skevh-back.env").exists())
        self.assertFalse((self.ndev / ".retained-skevh-back").exists())
        self.assertFalse((self.ndev / f".retry-{DOLPHIN}").exists())
        self.calls.write_text("")
        self.assertEqual(self.run_tool("ndev-prune").returncode, 0)
        self.assertEqual(self.calls_list(), [])

    def test_alias_zone_rebuild_failure_is_retried_after_its_row_is_gone(self):
        self.family_with_alias(live=False)
        flag = self.root / "fail-rebuild"
        flag.write_text("")
        self.env.update(TEST_FAIL_REBUILD=str(flag), TEST_FAIL_ZONE=GAMES)
        self.assertNotEqual(self.run_tool("ndev-prune").returncode, 0)
        self.assertNotIn(f"family.{GAMES}", (self.ndev / f"registry-{GAMES}").read_text())
        self.assertIn(f"family.{GAMES}", (self.config / f"config-{GAMES}.yml").read_text())
        flag.unlink()
        self.calls.write_text("")
        r = self.run_tool("ndev-prune")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn(f"family.{GAMES}", (self.config / f"config-{GAMES}.yml").read_text())
        self.assertIn(f"restart cloudflared@{GAMES}", self.calls_list())
        self.assertFalse((self.ndev / f".retry-{GAMES}").exists())
        self.assertFalse((self.ndev / "instances" / "ejc3-family.env").exists())

    def test_old_two_field_row_is_removed_by_hostname(self):
        host, _ = self.publish("skevh-old", "skevh", DOLPHIN, 3311, live=False)
        reg = self.ndev / f"registry-{DOLPHIN}"
        reg.write_text(f"{host}\t3311\n")
        self.rebuild_all()
        self.run_tool("ndev-prune")
        self.assertNotIn(host, reg.read_text())
        self.assertNotIn(host, (self.config / f"config-{DOLPHIN}.yml").read_text())

    def test_restored_route_is_rebuilt_even_when_its_own_zone_succeeded(self):
        # Kept only because the alias zone failed; its own zone was rebuilt and restarted
        # cleanly, so it has no retry marker. When the dir returns, its route must come back.
        host, d = self.family_with_alias(live=False)
        flag = self.root / "fail-restart"
        flag.write_text("")
        self.env.update(TEST_FAIL_RESTART=str(flag), TEST_FAIL_ZONE=GAMES)
        self.assertNotEqual(self.run_tool("ndev-prune").returncode, 0)
        self.assertFalse((self.ndev / f".retry-{DOLPHIN}").exists())
        self.assertNotIn(host, (self.config / f"config-{DOLPHIN}.yml").read_text())
        flag.unlink()
        d.mkdir(parents=True)
        (d / "package.json").write_text("{}\n")
        r = self.run_tool("ndev-prune")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"  - hostname: {host}\n    service: http://127.0.0.1:3722\n",
                      (self.config / f"config-{DOLPHIN}.yml").read_text())
        self.assertIn("enable --now ndev@ejc3-family.service", self.calls_list())
        # The pinned alias is setup's to restore, and it does once the checkout is back.
        self.run_setup_stretch()
        self.assertIn(f"family.{GAMES}", (self.config / f"config-{GAMES}.yml").read_text())

    def test_alias_zone_failure_is_retried_after_its_row_is_gone(self):
        # The cc-games restart fails; by the retry the alias row is already gone, so only the
        # zone's retry marker can bring cc-games back for the restart it still needs.
        host, _ = self.family_with_alias(live=False)
        flag = self.root / "fail-restart"
        flag.write_text("")
        self.env.update(TEST_FAIL_RESTART=str(flag), TEST_FAIL_ZONE=GAMES)
        self.assertNotEqual(self.run_tool("ndev-prune").returncode, 0)
        self.assertTrue((self.ndev / "instances" / "ejc3-family.env").exists())
        self.assertTrue((self.ndev / f".retry-{GAMES}").exists())
        self.assertFalse((self.ndev / f".retry-{DOLPHIN}").exists())
        flag.unlink()
        self.calls.write_text("")
        r = self.run_tool("ndev-prune")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"restart cloudflared@{GAMES}", self.calls_list())
        self.assertFalse((self.ndev / f".retry-{GAMES}").exists())
        self.assertFalse((self.ndev / "instances" / "ejc3-family.env").exists())
        self.assertFalse((self.ndev / ".retained-ejc3-family").exists())

    # ------------------------------------------------- setup under the same lock
    def setup_stretch(self):
        """The real setup lines from taking the ndev lock to releasing it after the rebuild."""
        start = SOURCE.index("ndev_lock() {")
        end = SOURCE.index("\n${local.nextjs_zone_rebuild}\nndev_unlock\n") + len("\n${local.nextjs_zone_rebuild}\nndev_unlock\n")
        body = SOURCE[start:end]
        template = re.search(r'nextjs_zone_rebuild = join\("\\n", \[\n\s+for z, t in local\.nextjs_zone_tunnel :\n\s+"(.*)"\n',
                             SOURCE).group(1).replace("\\n", "\n").replace('\\"', '"')
        rebuild = "\n".join(template.replace("${z}", z) for z in (GAMES, DOLPHIN))
        pinned = f"pin_route {GAMES} family.{GAMES} 3722 ejc3 /home/ejc3/family"
        body = (body.replace("${local.nextjs_zone_rebuild}", rebuild)
                .replace("${local.nextjs_pinned_rows}", pinned))
        body = self.localize(render(body))
        self.assertNotRegex(body, r"\$\{(local|var|join)")
        path = self.root / "setup-stretch.sh"
        path.write_text("#!/bin/bash\nset -uo pipefail\n" + body)
        return path

    def run_setup_stretch(self, held=None):
        proc = self.start_setup_stretch()
        try:
            if held is not None:
                time.sleep(0.7)
                self.assertIsNone(proc.poll(), "setup wrote /var/lib/ndev while the lock was held")
                self.assertEqual(self.calls_list(), [])
                self.assertTrue((self.ndev / "instances" / "skevh-comeback-panel.env").exists())
                self.assertNotIn(f"family.{GAMES}", (self.ndev / f"registry-{GAMES}").read_text())
                os.close(held)
                held = None
            out, err = proc.communicate(timeout=30)
        finally:
            self.stop(proc, held)
        self.assertEqual(proc.returncode, 0, out + err)
        return out + err

    def start_setup_stretch(self):
        # Own process group, so a failed test can kill a helper blocked on the lock too.
        return subprocess.Popen(["bash", str(self.setup_stretch())], env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                start_new_session=True)

    def stop(self, proc, held=None):
        if held is not None:
            os.close(held)
        if proc.poll() is None:
            os.killpg(proc.pid, 9)
            proc.communicate()

    def assert_lock_free(self):
        fd = os.open(self.ndev / ".lock", os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def test_setup_registry_stretch_waits_for_the_lock(self):
        self.fleet()
        # The first write in the stretch, before any helper that locks on its own.
        shared = self.ndev / "registry"
        shared.write_text(f"connor.{GAMES}\t3641\tconnor\t{self.homes}/connor/game\n")
        held = self.hold_lock()
        proc = self.start_setup_stretch()
        try:
            time.sleep(0.7)
            self.assertTrue(shared.exists(), "setup migrated the shared registry without the lock")
            self.assertFalse((self.ndev / "registry.migrated").exists())
            os.close(held)
            held = None
            out, err = proc.communicate(timeout=30)
        finally:
            self.stop(proc, held)
        self.assertEqual(proc.returncode, 0, out + err)
        self.assertTrue((self.ndev / "registry.migrated").exists())
        self.assertFalse((self.ndev / "instances" / "skevh-comeback-panel.env").exists())
        self.assertIn(f"family.{GAMES}", (self.ndev / f"registry-{GAMES}").read_text())
        self.assert_lock_free()

    def test_prune_under_setup_reuses_its_lock_instead_of_deadlocking(self):
        self.fleet()
        began = time.monotonic()
        self.run_setup_stretch()
        self.assertLess(time.monotonic() - began, 20)
        self.assertIn("disable --now ndev@skevh-comeback-panel.service", self.calls_list())
        self.assertFalse((self.ndev / "instances" / "skevh-comeback-panel.env").exists())
        self.assertNotIn(self.dead1, (self.config / f"config-{DOLPHIN}.yml").read_text())
        self.assert_lock_free()

    def test_reenable_stretch_holds_the_lock(self):
        start = SOURCE.index("# Re-enable previously published projects")
        loop = SOURCE[start:SOURCE.index("\ndone\n", start)]
        self.assertLess(loop.index("\n  ndev_lock\n"), loop.index("for envf in /var/lib/ndev/instances/*.env"))
        self.assertGreater(loop.index("\n  ndev_unlock"), loop.index('systemctl enable --now "ndev@$u.service"'))

    def register_with(self, fd9_source, extra_env):
        project = self.homes / "ejc3" / "worktrees" / "new"
        project.mkdir(parents=True, exist_ok=True)
        (project / "package.json").write_text("{}\n")
        env = dict(self.env, SUDO_USER="ejc3", **extra_env)
        return subprocess.Popen(
            ["bash", "-c", 'exec 9<"$1"; shift; exec bash "$@"', "_", fd9_source,
             str(self.bin / "ndev-register"), f"ejc3-new.{DOLPHIN}", "3400", "ejc3", str(project)],
            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def test_caller_cannot_talk_register_out_of_the_lock(self):
        # Neither an environment flag (sudo passes it under NOPASSWD: ALL) nor a fd 9 that is
        # not a held lock may let ndev-register write while someone else holds the lock.
        (self.ndev / ".lock").touch()
        for label, source, extra in (("env flag", "/dev/null", {"NDEV_LOCK_HELD": "1"}),
                                     ("fd 9 elsewhere", str(self.root / "decoy"), {}),
                                     ("fd 9 on the unheld lock", str(self.ndev / ".lock"), {})):
            with self.subTest(label):
                (self.root / "decoy").touch()
                held = self.hold_lock()
                proc = self.register_with(source, extra)
                try:
                    time.sleep(0.7)
                    self.assertIsNone(proc.poll(), "ndev-register skipped the lock")
                    self.assertFalse((self.ndev / "instances" / "ejc3-new.env").exists())
                finally:
                    os.close(held)
                    out, err = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0, out + err)
                self.assertTrue((self.ndev / "instances" / "ejc3-new.env").exists())
                (self.ndev / "instances" / "ejc3-new.env").unlink()

    def test_no_environment_switch_for_the_lock(self):
        self.assertNotIn("NDEV_LOCK_HELD", SOURCE)
        self.assertNotIn("closefrom_override", SOURCE)

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

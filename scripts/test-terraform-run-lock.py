#!/usr/bin/env python3
"""The jumpbox terraform wrapper (scripts/terraform-run-lock.sh): one state-touching run at a time, cheap commands pass."""
import os
import pathlib
import re
import signal
import stat
import subprocess
import tempfile
import textwrap
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WRAPPER = ROOT / "scripts/terraform-run-lock.sh"
HOOK = (ROOT / ".claude/hooks/verify-consistent.sh").read_text()
INSTALLER = ROOT / "scripts/terraform-run-lock-install.sh"
TF = (ROOT / "terraform-run-lock.tf").read_text()

FAKE = """#!/bin/bash
# A stand-in terraform: records when it ran and with what, sleeps if asked, can leave a child running like a provider.
# Like Terraform, SIGINT makes it stop (and say so) instead of dying silently.
trap 'echo "$(date +%s.%N) interrupted $*" >> "$FAKE_LOG"; kill $sp 2>/dev/null; exit 130' INT
echo $$ > "$FAKE_LOG.pid"
[ -n "${FAKE_CHILD:-}" ] && { sleep 30 >/dev/null 2>&1 & echo $! > "$FAKE_LOG.child"; }
echo "$(date +%s.%N) start $*" >> "$FAKE_LOG"
echo "TF_PLUGIN_CACHE_DIR=${TF_PLUGIN_CACHE_DIR:-}" >> "$FAKE_LOG.env"
[ -n "${FAKE_READ:-}" ] && { read -r line; echo "$(date +%s.%N) read $line" >> "$FAKE_LOG"; }
[ -n "${FAKE_SLEEP:-}" ] && { sleep "$FAKE_SLEEP" >/dev/null 2>&1 & sp=$!; wait $sp; }
echo "$(date +%s.%N) end $*" >> "$FAKE_LOG"
exit "${FAKE_RC:-0}"
"""


class WrapperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.real = self.tmp / "real-terraform"
        self.real.write_text(FAKE)
        self.real.chmod(0o755)
        self.lock = self.tmp / "lock"
        self.lock.touch()
        self.log = self.tmp / "log"
        # No added -parallelism and no shared cache unless a test asks: the other tests read the arguments as given.
        self.env = dict(os.environ, TF_RUN_LOCK_REAL=str(self.real), TF_RUN_LOCK_FILE=str(self.lock),
                        FAKE_LOG=str(self.log), TF_RUN_LOCK_PARALLELISM="",
                        TF_RUN_LOCK_PLUGIN_CACHE=str(self.tmp / "no-cache"))
        for k in ("TF_RUN_LOCK_NOWAIT", "TF_PLUGIN_CACHE_DIR"):
            self.env.pop(k, None)

    def start(self, *args, **extra):
        # Its own process group, so a test can kill the run with everything it started and nothing else.
        return subprocess.Popen(["bash", str(WRAPPER), *args], env=dict(self.env, **extra),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)

    def end(self, proc):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.communicate(timeout=10)

    def lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def wait_started(self, what, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if any(f"start {what}" in line for line in self.lines()):
                return
            time.sleep(0.05)
        self.fail(f"{what} never started")

    def test_a_second_plan_waits_for_the_first_and_says_who_holds_the_lock(self):
        a = self.start("plan", "-out=a", FAKE_SLEEP="1.5")
        self.wait_started("plan -out=a")
        b = self.start("apply", "b")
        _, b_err = b.communicate(timeout=20)
        a.communicate(timeout=20)
        self.assertEqual(b.returncode, 0, b_err)
        self.assertIn("waiting for the Terraform run by pid", b_err)
        events = [line.split(" ", 2) for line in self.lines()]
        a_end = next(float(t) for t, kind, rest in events if kind == "end" and rest.startswith("plan"))
        b_start = next(float(t) for t, kind, rest in events if kind == "start" and rest.startswith("apply"))
        self.assertGreaterEqual(b_start, a_end)  # never overlapped

    def test_nowait_gives_up_with_75_while_busy(self):
        a = self.start("plan", FAKE_SLEEP="20")
        self.wait_started("plan")
        b = self.start("plan", "-detailed-exitcode", TF_RUN_LOCK_NOWAIT="1")
        _, err = b.communicate(timeout=10)
        self.end(a)
        self.assertEqual(b.returncode, 75)
        self.assertIn("not waiting", err)
        self.assertEqual(sum("start plan -detailed-exitcode" in line for line in self.lines()), 0)

    def test_a_wait_cap_gives_up_with_75(self):
        a = self.start("apply", FAKE_SLEEP="20")
        self.wait_started("apply")
        b = self.start("plan", TF_RUN_LOCK_WAIT="1")
        _, err = b.communicate(timeout=10)
        self.end(a)
        self.assertEqual(b.returncode, 75)
        self.assertIn("giving up", err)

    def test_cheap_commands_pass_straight_through_while_a_run_holds_the_lock(self):
        a = self.start("apply", FAKE_SLEEP="30")
        self.wait_started("apply")
        for args in (["fmt", "-check"], ["version"], ["output", "-json"], ["show"], ["-help"],
                     ["-chdir=sub", "providers"], ["graph"]):
            t = time.time()
            r = subprocess.run(["bash", str(WRAPPER), *args], env=self.env, capture_output=True, text=True,
                               timeout=10)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertLess(time.time() - t, 5, args)
            self.assertNotIn("waiting", r.stderr)
        self.end(a)

    def test_chdir_before_the_subcommand_still_takes_the_lock(self):
        a = self.start("apply", FAKE_SLEEP="20")
        self.wait_started("apply")
        b = self.start("-chdir=elsewhere", "plan", TF_RUN_LOCK_NOWAIT="1")
        b.communicate(timeout=10)
        self.end(a)
        self.assertEqual(b.returncode, 75)

    def wait_free(self, timeout=8):
        """Retry a no-wait plan until the host lock is free; returns its result."""
        end = time.time() + timeout
        while True:
            b = subprocess.run(["bash", str(WRAPPER), "plan"], env=dict(self.env, TF_RUN_LOCK_NOWAIT="1"),
                               capture_output=True, text=True, timeout=10)
            if b.returncode != 75 or time.time() > end:
                return b
            time.sleep(0.2)

    def test_a_killed_caller_cancels_the_run_and_releases_the_lock(self):
        # SIGKILL to the caller's whole process group, as a stopped background task does: Terraform is in a session of
        # its own, so it is not killed mid-run; the supervisor sees the caller gone and interrupts it, Terraform stops
        # (releasing its remote state lock, for the real one) and the host lock goes with it.
        a = self.start("apply", FAKE_SLEEP="30")
        self.wait_started("apply")
        os.killpg(a.pid, signal.SIGKILL)
        a.wait(timeout=5)
        b = self.wait_free()
        self.assertEqual(b.returncode, 0, b.stderr)
        self.assertTrue(any("interrupted apply" in line for line in self.lines()), self.lines())
        self.assertFalse(any("end apply" in line for line in self.lines()))

    def test_sigterm_to_the_wrapper_interrupts_terraform_and_returns_its_exit_code(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(sig=sig):
                self.log.unlink(missing_ok=True)
                a = self.start("plan", FAKE_SLEEP="30")
                self.wait_started("plan")
                t = time.time()
                a.send_signal(sig)  # the wrapper only, not its group
                a.communicate(timeout=10)
                self.assertEqual(a.returncode, 130)  # Terraform's own code after its interrupt
                self.assertLess(time.time() - t, 5)
                self.assertTrue(any("interrupted plan" in line for line in self.lines()), self.lines())

    def test_stdin_still_reaches_terraform(self):
        r = subprocess.run(["bash", str(WRAPPER), "apply"], env=dict(self.env, FAKE_READ="1"), input="yes\n",
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(any(line.endswith(" read yes") for line in self.lines()), self.lines())

    def test_plan_and_apply_get_a_default_parallelism_unless_one_is_given(self):
        env = dict(self.env, TF_RUN_LOCK_PARALLELISM="30")
        for args, expect in ((["plan", "-out=p"], "start plan -parallelism=30 -out=p"),
                             (["-chdir=sub", "apply", "p.plan"], "start -chdir=sub apply -parallelism=30 p.plan"),
                             (["plan", "-parallelism=5"], "start plan -parallelism=5"),
                             (["init", "-input=false"], "start init -input=false")):
            self.log.unlink(missing_ok=True)
            r = subprocess.run(["bash", str(WRAPPER), *args], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertEqual([line.split(" ", 1)[1] for line in self.lines() if " start " in line], [expect])

    def test_the_shared_provider_cache_is_used_when_present_and_a_callers_choice_wins(self):
        cache = self.tmp / "cache"
        cache.mkdir()
        envlog = self.tmp / "log.env"
        for extra, expect in (({}, "TF_PLUGIN_CACHE_DIR="),  # no cache directory on this host: nothing set
                              ({"TF_RUN_LOCK_PLUGIN_CACHE": str(cache)}, f"TF_PLUGIN_CACHE_DIR={cache}"),
                              ({"TF_RUN_LOCK_PLUGIN_CACHE": str(cache), "TF_PLUGIN_CACHE_DIR": "/elsewhere"},
                               "TF_PLUGIN_CACHE_DIR=/elsewhere")):
            envlog.unlink(missing_ok=True)
            r = subprocess.run(["bash", str(WRAPPER), "init"], env=dict(self.env, **extra), capture_output=True,
                               text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(envlog.read_text().strip(), expect, extra)

    def test_a_provider_that_outlives_a_killed_terraform_does_not_keep_the_lock(self):
        a = self.start("apply", FAKE_SLEEP="30", FAKE_CHILD="1")
        self.wait_started("apply")
        tf_pid = int((self.tmp / "log.pid").read_text())
        child = int((self.tmp / "log.child").read_text())
        try:
            os.kill(tf_pid, signal.SIGKILL)  # Terraform alone dies (an OOM kill); its "provider" keeps running
            a.wait(timeout=5)
            os.kill(child, 0)  # still alive
            b = self.wait_free()
            self.assertEqual(b.returncode, 0, b.stderr)
        finally:
            for pid in (child,):
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_providers_lock_mirror_and_schema_take_the_lock_but_plain_providers_does_not(self):
        a = self.start("apply", FAKE_SLEEP="30")
        self.wait_started("apply")
        for args, expect in ((["providers", "lock", "-platform=linux_arm64"], 75), (["providers", "mirror", "d"], 75),
                             (["console"], 75), (["console", "-plan"], 75), (["validate"], 75),
                             (["providers"], 0), (["providers", "schema", "-json"], 75)):
            r = subprocess.run(["bash", str(WRAPPER), *args], env=dict(self.env, TF_RUN_LOCK_NOWAIT="1"),
                               capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, expect, (args, r.stderr))
        self.end(a)

    def test_the_exit_code_and_arguments_are_the_real_binarys(self):
        r = subprocess.run(["bash", str(WRAPPER), "plan", "-detailed-exitcode", "-no-color"],
                           env=dict(self.env, FAKE_RC="2"), capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 2)
        self.assertIn("start plan -detailed-exitcode -no-color", self.log.read_text())

    def test_it_never_runs_itself(self):
        r = subprocess.run(["bash", str(WRAPPER), "plan"], env=dict(self.env, TF_RUN_LOCK_REAL=str(WRAPPER)),
                           capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 127)
        # Found on PATH: a directory holding only a link to the wrapper is skipped for the real one behind it.
        shim = self.tmp / "shim"
        shim.mkdir()
        (shim / "terraform").symlink_to(WRAPPER)
        realdir = self.tmp / "realdir"
        realdir.mkdir()
        (realdir / "terraform").symlink_to(self.real)
        env = dict(self.env, PATH=f"{shim}:{realdir}:/usr/bin:/bin")
        env.pop("TF_RUN_LOCK_REAL")
        r = subprocess.run([str(shim / "terraform"), "version"], env=env, capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("start version", self.log.read_text())

    def test_without_the_host_lock_file_it_uses_a_per_user_lock(self):
        env = dict(self.env, XDG_RUNTIME_DIR=str(self.tmp), TF_RUN_LOCK_NOWAIT="1")  # never wait on a host's real lock
        env.pop("TF_RUN_LOCK_FILE")
        r = subprocess.run(["bash", str(WRAPPER), "plan"], env=env, capture_output=True, text=True, timeout=10)
        # /var/lib/terraform-run-lock/lock may exist on a jumpbox; only check the fallback where it does not.
        if not pathlib.Path("/var/lib/terraform-run-lock/lock").exists():
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((self.tmp / f"terraform-run-lock-{os.getuid()}").exists())


class WiringTests(unittest.TestCase):
    def test_wrapper_never_adds_lock_false(self):
        self.assertNotIn("-lock=false", WRAPPER.read_text().split("set -u", 1)[1])

    def test_hook_uses_the_wrapper_without_waiting_and_skips_on_75(self):
        self.assertIn("TF_RUN_LOCK_NOWAIT=1 terraform plan", HOOK)
        self.assertIn('if [ "$rc" -eq 75 ]; then', HOOK)
        self.assertNotIn("aws-stop-hook-tfplan-", HOOK)  # one lock, the wrapper's
        self.assertIn("pgrep -x 'terraform|terraform-provi'", HOOK)

    def test_the_association_runs_daily_on_every_admin_box(self):
        self.assertIn('schedule_expression = "rate(1 day)"', TF)
        self.assertIn("values = values(local.admin_tmux_boxes)", TF)
        self.assertIn('chomp(file("${path.module}/scripts/terraform-run-lock.sh"))', TF)
        self.assertIn('chomp(file("${path.module}/scripts/terraform-run-lock-install.sh"))', TF)


def render_association_script():
    """The association's commands as Terraform renders them: file() inlined by indent(4, ...), then <<- strips 4."""
    body = re.search(r"terraform_run_lock_install = <<-EOT\n(.*?)\n  EOT", TF, re.S).group(1)
    def inline(m):
        return textwrap.indent((ROOT / m.group(1)).read_text().rstrip("\n"), " " * 4)[4:]
    body = re.sub(r'\$\{indent\(4, chomp\(file\("\$\{path\.module\}/([^"]+)"\)\)\)\}', inline, body)
    return textwrap.dedent(body)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.dirs = {k: self.tmp / k for k in ("bin", "lib", "lockdir", "cache")}
        self.dirs["bin"].mkdir()
        # PATH without any real terraform, so nothing can stand in for the binary under test (a clean CI runner).
        self.env = dict(os.environ, BIN_DIR=str(self.dirs["bin"]), LIB_DIR=str(self.dirs["lib"]),
                        LOCK_DIR=str(self.dirs["lockdir"]), CACHE_DIR=str(self.dirs["cache"]), PATH="/usr/bin:/bin")
        self.script = self.tmp / "association.sh"
        self.script.write_text(render_association_script())

    def put_real(self, version):
        p = self.dirs["bin"] / "terraform"
        p.write_text(f"#!/bin/bash\necho 'Terraform v{version}'\n")
        p.chmod(0o755)

    def run_install(self):
        return subprocess.run(["bash", str(self.script)], env=self.env, capture_output=True, text=True, timeout=30)

    def test_the_rendered_script_carries_the_files_byte_for_byte(self):
        text = render_association_script()
        self.assertIn(WRAPPER.read_text(), text)
        self.assertIn(INSTALLER.read_text(), text)

    def test_first_install_moves_the_real_binary_and_puts_the_wrapper_in_its_place(self):
        self.put_real("9.9.1")
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.dirs["bin"] / "terraform").read_text(), WRAPPER.read_text())
        self.assertIn("v9.9.1", (self.dirs["lib"] / "terraform").read_text())
        lock = self.dirs["lockdir"] / "lock"
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o666)
        self.assertIn("Terraform v9.9.1", r.stdout)  # the wrapper's passthrough reached the moved binary
        # The shared provider cache: group-writable and setgid, so every administrator's runs share one copy.
        self.assertEqual(stat.S_IMODE(self.dirs["cache"].stat().st_mode), 0o2775)

    def test_a_rerun_changes_nothing_and_an_upgrade_is_repaired(self):
        self.put_real("9.9.1")
        self.run_install()
        r = self.run_install()
        self.assertNotIn("moved", r.stdout)
        self.assertNotIn("installed the wrapper", r.stdout)
        self.put_real("9.9.2")  # an upgrade overwrote the wrapper
        r = self.run_install()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("v9.9.2", (self.dirs["lib"] / "terraform").read_text())
        self.assertEqual((self.dirs["bin"] / "terraform").read_text(), WRAPPER.read_text())

    def test_no_real_binary_means_no_wrapper(self):
        r = self.run_install()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.dirs["bin"] / "terraform").exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

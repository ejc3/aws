#!/usr/bin/env python3
"""The jumpbox terraform wrapper (scripts/terraform-run-lock.sh): one state-touching run at a time, cheap commands pass."""
import os
import pathlib
import re
import shutil
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
                        # Never the host's real binary: on a jumpbox the default path exists, and a test that drops
                        # TF_RUN_LOCK_REAL would run real Terraform instead of the fake (and fail reading its log).
                        TF_RUN_LOCK_DEFAULT_REAL=str(self.tmp / "no-default-real"),
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

    def test_a_signal_while_queued_abandons_the_run(self):
        # Review of #543: with the lock held, flock is still waiting and there is no supervisor; a Ctrl-C or TERM must
        # end the queued run, not be swallowed so that Terraform starts once the lock frees.
        for sig, code in ((signal.SIGINT, 130), (signal.SIGTERM, 143), (signal.SIGHUP, 129)):
            with self.subTest(sig=sig):
                self.log.unlink(missing_ok=True)
                a = self.start("apply", "-first", FAKE_SLEEP="15")  # the holder probe alone can take seconds on a busy host
                self.wait_started("apply -first")
                b = self.start("apply", "-queued")
                end = time.time() + 10  # until b's flock is queued on the lock
                while not subprocess.run(["ps", "-o", "args=", "--ppid", str(b.pid)], capture_output=True,
                                         text=True).stdout.startswith("flock -o"):
                    self.assertLess(time.time(), end, "the queued run never reached flock")
                    time.sleep(0.05)
                time.sleep(0.2)
                t = time.time()
                b.send_signal(sig)
                _, err = b.communicate(timeout=10)
                self.assertEqual(b.returncode, code, err)
                self.assertLess(time.time() - t, 3)
                self.assertIn("cancelled while waiting", err)
                self.end(a)  # the lock frees: the abandoned run must not start now
                time.sleep(1)
                self.assertFalse(any("start apply -queued" in line for line in self.lines()), self.lines())

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
                             (["plan", "--parallelism=5"], "start plan --parallelism=5"),
                             (["init", "-input=false"], "start init -input=false")):
            self.log.unlink(missing_ok=True)
            r = subprocess.run(["bash", str(WRAPPER), *args], env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, (args, r.stderr))
            self.assertEqual([line.split(" ", 1)[1] for line in self.lines() if " start " in line], [expect])

    def test_parallelism_from_tf_cli_args_is_respected(self):
        for extra in ({"TF_CLI_ARGS": "-parallelism=4"}, {"TF_CLI_ARGS_plan": "-no-color -parallelism=4"}):
            self.log.unlink(missing_ok=True)
            r = subprocess.run(["bash", str(WRAPPER), "plan"], env=dict(self.env, TF_RUN_LOCK_PARALLELISM="30", **extra),
                               capture_output=True, text=True, timeout=10)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual([line.split(" ", 1)[1] for line in self.lines() if " start " in line], ["start plan"], extra)
        # TF_CLI_ARGS_apply says nothing about a plan: the default still goes on.
        self.log.unlink(missing_ok=True)
        subprocess.run(["bash", str(WRAPPER), "plan"], env=dict(self.env, TF_RUN_LOCK_PARALLELISM="30",
                                                                TF_CLI_ARGS_apply="-parallelism=4"),
                       capture_output=True, text=True, timeout=10)
        self.assertEqual([line.split(" ", 1)[1] for line in self.lines() if " start " in line], ["start plan -parallelism=30"])

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

    def test_hook_delegates_publication_without_waiting(self):
        self.assertIn("bash scripts/publish-applied-status.sh --if-idle", HOOK)
        self.assertNotIn("terraform plan ", HOOK)
        self.assertIn("setsid -f", HOOK)  # detached: the hook never waits for the plan
        self.assertIn("flock -n -o", HOOK)  # one background check at a time; Terraform does not inherit the lock
        # The hook reads the header publish-applied-status.sh writes; the two must not drift apart.
        self.assertIn("printf 'main=%s checked_at=%s terraform_exit=%s\\n'", (ROOT / "scripts/publish-applied-status.sh").read_text())
        self.assertIn("s/^main=([0-9a-f]{40}) checked_at=([0-9TZ:-]+) terraform_exit=([0-9]+)$/", HOOK)

    def test_the_association_runs_daily_on_every_admin_box(self):
        self.assertIn('schedule_expression = "rate(1 day)"', TF)
        self.assertIn("values = values(local.admin_tmux_boxes)", TF)
        self.assertIn('chomp(file("${path.module}/scripts/terraform-run-lock.sh"))', TF)
        self.assertIn('chomp(file("${path.module}/scripts/terraform-run-lock-install.sh"))', TF)


class StopHookBackgroundTests(unittest.TestCase):
    """The Stop hook judges drift by the last finished check of origin/main and refreshes it detached."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        git = lambda *a, cwd=self.tmp: subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
        git("init", "-q", "--bare", "-b", "main", "origin.git")
        git("clone", "-q", str(self.tmp / "origin.git"), "repo")
        self.repo = self.tmp / "repo"
        (self.repo / ".claude/hooks").mkdir(parents=True)
        (self.repo / ".claude/hooks/verify-consistent.sh").write_text(HOOK)
        (self.repo / ".gitignore").write_text(".terraform/\n")
        (self.repo / "scripts").mkdir()
        # A stand-in publisher: records that it ran, takes a while as the real plan does, exits as told.
        (self.repo / "scripts/publish-applied-status.sh").write_text(
            '#!/bin/bash\necho "$(date +%s) publish $*" >> "$MARK"\nsleep "${PUBLISH_SLEEP:-0}"\nexit "${PUBLISH_RC:-0}"\n')
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "add", "-A", cwd=self.repo)
        git("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init", cwd=self.repo)
        git("push", "-q", "origin", "main", cwd=self.repo)
        (self.repo / ".terraform").mkdir()  # an initialized checkout, as on a jumpbox
        self.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True).stdout.strip()
        bindir = self.tmp / "bin"
        bindir.mkdir()
        (bindir / "terraform").write_text('#!/bin/bash\necho "terraform $*" >> "$MARK"\n')
        (bindir / "terraform").chmod(0o755)
        self.state = self.tmp / "state" / "aws-applied-status"
        self.lock = self.state / "background.lock"
        self.mark = self.tmp / "mark"
        self.env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", XDG_STATE_HOME=str(self.tmp / "state"),
                        CLAUDE_PROJECT_DIR=str(self.repo), MARK=str(self.mark), PUBLISH_SLEEP="0")

    def tearDown(self):
        self.wait_idle()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_hook(self):
        start = time.monotonic()
        r = subprocess.run(["bash", str(self.repo / ".claude/hooks/verify-consistent.sh")], input="{}", env=self.env,
                           cwd=self.repo, capture_output=True, text=True, timeout=30)
        return r, time.monotonic() - start

    def last_check(self, sha, rc, seconds_ago=0):
        self.state.mkdir(parents=True, exist_ok=True)
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))
        (self.state / "last-plan.log").write_text(f"main={sha} checked_at={at} terraform_exit={rc}\nplan output\n")

    def publishes(self):
        return self.mark.read_text().count("publish --if-idle") if self.mark.exists() else 0

    def wait_idle(self):
        """Until no background check holds the lock (each finishes on its own: PUBLISH_SLEEP is short)."""
        for _ in range(200):
            if not self.lock.exists() or subprocess.run(["flock", "-n", str(self.lock), "true"]).returncode == 0:
                return
            time.sleep(0.1)

    def started(self):
        for _ in range(100):
            if self.publishes():
                return True
            time.sleep(0.1)
        return False

    def test_no_result_returns_at_once_and_starts_one_background_check(self):
        self.env["PUBLISH_SLEEP"] = "3"
        r, took = self.run_hook()
        self.assertLess(took, 10, "the hook waited for the plan")
        self.assertEqual(r.stdout, "", "no finished result must not block")
        self.assertTrue(self.started(), "no background check started")
        self.assertIn("terraform init -input=false -lockfile=readonly", self.mark.read_text())
        self.run_hook()  # a second stop while the first check runs starts no second one
        self.wait_idle()
        self.assertEqual(self.publishes(), 1)

    def test_an_uninitialized_checkout_is_not_initialized_by_the_hook(self):
        (self.repo / ".terraform").rmdir()
        self.run_hook()
        self.assertTrue(self.started())
        self.wait_idle()
        self.assertNotIn("terraform init", self.mark.read_text())

    def test_a_drift_result_for_this_main_blocks_and_rechecks(self):
        self.last_check(self.sha, 2)
        r, _ = self.run_hook()
        self.assertIn('"decision":"block"', r.stdout)
        self.assertIn("Terraform drift", r.stdout)
        self.assertTrue(self.started(), "a drift result is re-checked, so a fix clears it")

    def test_an_old_drift_result_for_this_main_still_blocks(self):
        self.last_check(self.sha, 2, seconds_ago=7 * 3600)
        r, _ = self.run_hook()
        self.assertIn("Terraform drift", r.stdout)

    def test_a_clean_fresh_result_passes_without_rechecking(self):
        self.last_check(self.sha, 0)
        r, _ = self.run_hook()
        self.assertEqual(r.stdout, "")
        time.sleep(1)
        self.assertEqual(self.publishes(), 0)

    def test_a_clean_result_older_than_the_refresh_interval_is_rechecked(self):
        self.last_check(self.sha, 0, seconds_ago=3600)
        r, _ = self.run_hook()
        self.assertEqual(r.stdout, "")
        self.assertTrue(self.started())

    def test_a_result_for_another_commit_never_blocks_and_refreshes(self):
        self.last_check("0" * 40, 2)
        r, _ = self.run_hook()
        self.assertEqual(r.stdout, "")
        self.assertTrue(self.started())

    def test_a_background_check_that_fails_before_planning_blocks_the_next_stop(self):
        self.env["PUBLISH_RC"] = "1"  # e.g. STS or init failed: no header is written
        r, _ = self.run_hook()
        self.assertEqual(r.stdout, "")
        self.assertTrue(self.started())
        self.wait_idle()
        r, _ = self.run_hook()
        self.assertIn('"decision":"block"', r.stdout)
        self.assertIn("background drift check", r.stdout)
        self.assertIn("exit 1", r.stdout)
        for _ in range(100):  # the retry starts detached
            if self.publishes() >= 2:
                break
            time.sleep(0.1)
        self.assertGreaterEqual(self.publishes(), 2, "a failed check is retried at the next stop")

    def test_a_busy_or_ineligible_check_does_not_block(self):
        self.env["PUBLISH_RC"] = "75"
        self.run_hook()
        self.assertTrue(self.started())
        self.wait_idle()
        r, _ = self.run_hook()
        self.assertEqual(r.stdout, "")


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
        if shutil.which("getfacl"):  # the default ACL that keeps provider directories group-writable (review of #543)
            acl = subprocess.run(["getfacl", "-p", str(self.dirs["cache"])], capture_output=True, text=True).stdout
            self.assertIn("default:group:adm:rwx", acl)
        else:
            self.assertIn("no setfacl", r.stderr)

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
    unittest.main(verbosity=2, buffer=True)

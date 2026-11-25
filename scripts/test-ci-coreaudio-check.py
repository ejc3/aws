#!/usr/bin/env python3
"""The CI VM's coreaudiod check (scripts/mac/ci/ci-coreaudio-check, #1068) against a fake probe, ps and killall."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

SOURCE = Path(__file__).with_name("mac") / "ci" / "ci-coreaudio-check"
MINUTE = 60

# The probe answers at once unless the fake coreaudiod is hung; killall -KILL coreaudiod "restarts" it, and the
# daemon comes back hung again when FAKE_STAYS_HUNG is set. Every killall is recorded.
PROBE = r'''#!/usr/bin/env python3
import os, time
if os.path.exists(os.environ["FAKE_HUNG"]):
    time.sleep(60)
'''
KILLALL = r'''#!/usr/bin/env python3
import os, sys
print(*sys.argv[1:], file=open(os.environ["FAKE_KILLS"], "a"))
if not os.environ.get("FAKE_STAYS_HUNG"):
    try:
        os.unlink(os.environ["FAKE_HUNG"])
    except FileNotFoundError:
        pass
'''
PS = r'''#!/usr/bin/env python3
import os, sys
if os.environ.get("FAKE_PS_FAIL"):
    sys.exit(1)
print("/sbin/launchd")
print("/Users/admin/runners/mini-1/bin/Runner.Listener run --startuptype service")
for line in filter(None, os.environ.get("FAKE_PS", "").split(";")):
    print(line)
'''


def load():
    loader = importlib.machinery.SourceFileLoader("ci_coreaudio_check", str(SOURCE))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Check(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        for name, text in (("probe", PROBE), ("killall", KILLALL), ("ps", PS)):
            (self.root / name).write_text(text)
            (self.root / name).chmod(0o755)
        self.hung_flag, self.kills = self.root / "hung", self.root / "kills"
        saved = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(saved)))
        for key in ("FAKE_PS", "FAKE_PS_FAIL", "FAKE_STAYS_HUNG"):
            os.environ.pop(key, None)
        os.environ.update(FAKE_HUNG=str(self.hung_flag), FAKE_KILLS=str(self.kills))
        self.m = load()
        self.m.PROBE = [sys.executable, str(self.root / "probe")]
        self.m.KILLALL, self.m.PS = str(self.root / "killall"), str(self.root / "ps")
        self.m.STATE = self.root / "state"
        self.m.PROBE_TIMEOUT, self.m.SETTLE = 0.5, 0
        self.now = 1_800_000_000.0

    def hang(self):
        self.hung_flag.touch()

    def job(self):
        os.environ["FAKE_PS"] = "/Users/admin/runners/mini-2/bin.2.338.2/Runner.Worker spawnclient 156 161"

    def killed(self):
        return self.kills.read_text().splitlines() if self.kills.exists() else []

    def run_at(self, state, minutes):
        return self.m.check(state, self.now + minutes * MINUTE)

    def test_an_answering_coreaudiod_is_left_alone_and_logs_nothing(self):
        state = {}
        self.assertIsNone(self.run_at(state, 0))
        self.assertEqual(self.killed(), [])
        self.assertEqual(state, {})

    def test_no_answer_with_no_job_restarts_once_with_sigkill_and_checks_again(self):
        self.hang()
        state = {}
        line = self.run_at(state, 0)
        self.assertEqual(self.killed(), ["-KILL coreaudiod"])
        self.assertIn("restarted coreaudiod", line)
        self.assertIn("it answers", line)
        self.assertIsNone(state["hung_since"])
        # Answering from then on: nothing more happens, nothing is logged.
        self.assertIsNone(self.run_at(state, 5))
        self.assertEqual(len(self.killed()), 1)

    def test_a_running_job_holds_the_restart_until_30_minutes_without_an_answer(self):
        self.hang()
        self.job()
        state = {}
        for minute in (0, 5, 25):
            with self.subTest(minute=minute):
                line = self.run_at(state, minute)
                self.assertIn("a job is running, the restart waits", line)
                self.assertEqual(self.killed(), [])
        self.assertIn(f"for 25 min", self.run_at(state, 25))
        line = self.run_at(state, 30)
        self.assertEqual(self.killed(), ["-KILL coreaudiod"])
        self.assertIn("restarted coreaudiod after 30 min", line)

    def test_the_restart_goes_ahead_as_soon_as_the_job_ends(self):
        self.hang()
        self.job()
        state = {}
        self.run_at(state, 0)
        os.environ.pop("FAKE_PS")
        self.assertIn("restarted coreaudiod after 5 min", self.run_at(state, 5))

    def test_an_unreadable_process_list_counts_as_a_job(self):
        self.hang()
        os.environ["FAKE_PS_FAIL"] = "1"
        self.assertIn("a job is running", self.run_at({}, 0))
        self.assertEqual(self.killed(), [])

    def test_at_most_one_restart_in_15_minutes_even_when_it_does_not_help(self):
        self.hang()
        os.environ["FAKE_STAYS_HUNG"] = "1"
        state = {}
        self.assertIn("still does not answer", self.run_at(state, 0))
        for minute in (5, 10):
            with self.subTest(minute=minute):
                self.assertIn("the next restart waits", self.run_at(state, minute))
        self.assertEqual(len(self.killed()), 1)
        self.assertIn("restarted coreaudiod after 15 min", self.run_at(state, 15))
        self.assertEqual(len(self.killed()), 2)

    def test_recovery_on_its_own_is_logged_once(self):
        self.hang()
        self.job()
        state = {}
        self.run_at(state, 0)
        self.hung_flag.unlink()
        self.assertEqual(self.run_at(state, 10), "coreaudiod answers again after 10 min")
        self.assertIsNone(self.run_at(state, 15))
        self.assertEqual(self.killed(), [])

    def test_a_probe_that_cannot_start_or_fails_is_never_a_reason_to_restart(self):
        self.hang()
        for probe in ([str(self.root / "missing")], [sys.executable, "-c", "raise SystemExit(3)"]):
            with self.subTest(probe=probe):
                self.m.PROBE = probe
                self.assertIsNone(self.run_at({}, 0))
        self.assertEqual(self.killed(), [])

    def test_a_hung_probe_is_killed_not_left_behind(self):
        self.hang()
        self.m.PROBE = [sys.executable, "-c", f"import os, time; open({str(self.root / 'pid')!r}, 'w').write(str(os.getpid())); time.sleep(60)"]
        self.assertFalse(self.m.answers())
        pid = int((self.root / "pid").read_text())
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_main_keeps_state_between_runs_and_logs_minutes_only(self):
        self.hang()
        self.job()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.m.main([]), 0)
        saved = json.loads((self.m.STATE / "state.json").read_text())
        self.assertEqual(set(saved), {"hung_since"})
        self.assertRegex(out.getvalue(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ci-coreaudio-check: coreaudiod has not "
                                         r"answered for 0 min; a job is running, the restart waits\n$")
        self.assertNotIn("Runner.Worker", out.getvalue())
        # The next run picks the state up: still waiting on the job, with the time counted from the first run.
        saved["hung_since"] -= 12 * MINUTE
        (self.m.STATE / "state.json").write_text(json.dumps(saved))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.m.main([]), 0)
        self.assertIn("for 12 min", out.getvalue())

    def test_corrupt_or_odd_state_is_read_as_empty(self):
        self.m.STATE.mkdir()
        for text in ("not json", "[1, 2]", '{"hung_since": "yesterday", "restarted_at": true}'):
            with self.subTest(text=text):
                (self.m.STATE / "state.json").write_text(text)
                self.assertEqual(self.m.load(self.m.STATE / "state.json"), {})

    def test_a_second_run_while_one_holds_the_lock_does_nothing(self):
        import fcntl
        self.hang()
        self.m.STATE.mkdir()
        with (self.m.STATE / "lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.assertEqual(self.m.main([]), 0)
        self.assertEqual(self.killed(), [])


if __name__ == "__main__":
    unittest.main(buffer=True)

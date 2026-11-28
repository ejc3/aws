#!/usr/bin/env python3
"""The CI VM's clock and runner keepalive (scripts/mac/ci/ci-runner-keepalive, #1148) against a fake sntp and launchctl."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import pwd
import tempfile
import unittest

SOURCE = Path(__file__).with_name("mac") / "ci" / "ci-runner-keepalive"

# launchctl: `print gui/<uid>/<label>` answers from FAKE_STATES (label -> "running" | "not running" | "unloaded");
# every kickstart is recorded.
LAUNCHCTL = r'''#!/usr/bin/env python3
import json, os, sys
states = json.loads(os.environ.get("FAKE_STATES", "{}"))
if sys.argv[1] == "print":
    label = sys.argv[2].rsplit("/", 1)[1]
    state = states.get(label, "unloaded")
    if state == "unloaded":
        sys.exit(113)
    print(f"{sys.argv[2]} = {{\n\tactive count = 0\n\tstate = {state}\n}}")
elif sys.argv[1] == "kickstart":
    print(sys.argv[2], file=open(os.environ["FAKE_KICKS"], "a"))
'''
SNTP = r'''#!/usr/bin/env python3
import os, sys
if os.environ.get("FAKE_SNTP_FAIL"):
    sys.exit(1)
print(os.environ.get("FAKE_OFFSET", "+0.012") + " +/- 0.030 time.apple.com 17.253.4.125")
print(sys.argv[1:], file=open(os.environ["FAKE_SNTP_CALLS"], "a"))
'''


class Keepalive(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.root = Path(workspace.name)
        for name, text in (("launchctl", LAUNCHCTL), ("sntp", SNTP)):
            (self.root / name).write_text(text)
            (self.root / name).chmod(0o755)
        self.kicks = self.root / "kicks"
        self.sntp_calls = self.root / "sntp-calls"
        me = pwd.getpwuid(os.getuid())
        self.home = self.root / "home"
        self.agents = self.home / "Library" / "LaunchAgents"
        self.agents.mkdir(parents=True)
        self.env = {"CI_KEEPALIVE_USER": me.pw_name, "CI_KEEPALIVE_SNTP": str(self.root / "sntp"),
                    "CI_KEEPALIVE_LAUNCHCTL": str(self.root / "launchctl"),
                    "CI_KEEPALIVE_STATE": str(self.root / "state.json"),
                    "FAKE_KICKS": str(self.kicks), "FAKE_SNTP_CALLS": str(self.sntp_calls)}
        self.saved = {k: os.environ.get(k) for k in list(self.env) + ["FAKE_STATES", "FAKE_OFFSET", "FAKE_SNTP_FAIL"]}
        os.environ.update(self.env)
        self.addCleanup(self.restore)
        self.uid = os.getuid()

    def restore(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def load(self):
        loader = importlib.machinery.SourceFileLoader("ci_runner_keepalive", str(SOURCE))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        # The home of the CI user is this test's stand-in.
        real = module.pwd.getpwnam
        module.pwd.getpwnam = lambda name: type("User", (), {"pw_uid": real(name).pw_uid, "pw_dir": str(self.home)})()
        return module

    def agent(self, *labels):
        for label in labels:
            (self.agents / f"{label}.plist").write_text("<plist/>")

    def run_once(self, states, now=1000.0, **env):
        os.environ["FAKE_STATES"] = json.dumps(states)
        for k, v in env.items():
            os.environ[k] = v
        m = self.load()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            m.sync_clock()
            m.keep_runners(now=now)
        return out.getvalue()

    def kicked(self):
        return self.kicks.read_text().splitlines() if self.kicks.exists() else []

    def test_a_loaded_runner_that_is_not_running_is_kickstarted_once_per_15_minutes(self):
        label = "actions.runner.dolphin-labs-hq.air-ci-1"
        self.agent(label)
        out = self.run_once({label: "not running"})
        self.assertEqual(self.kicked(), [f"gui/{self.uid}/{label}"])
        self.assertIn(f"{label}: loaded but not running; kickstart ok", out)
        self.run_once({label: "not running"}, now=1000.0 + 5 * 60)
        self.assertEqual(len(self.kicked()), 1, "no second kickstart within 15 minutes")
        self.run_once({label: "not running"}, now=1000.0 + 16 * 60)
        self.assertEqual(len(self.kicked()), 2)

    def test_running_and_unloaded_runners_are_left_alone(self):
        self.agent("actions.runner.dolphin-labs-hq.mini-3", "actions.runner.dolphin-labs-hq.mini-4")
        self.run_once({"actions.runner.dolphin-labs-hq.mini-3": "running"})
        self.assertEqual(self.kicked(), [], "a running runner and an unloaded one (svc.sh stop) are not touched")

    def test_only_runner_agents_are_considered(self):
        self.agent("com.ejcampbell.ci-disk-clean")
        self.run_once({"com.ejcampbell.ci-disk-clean": "not running"})
        self.assertEqual(self.kicked(), [])

    def test_the_clock_is_set_from_time_apple_com_and_a_large_correction_is_logged(self):
        out = self.run_once({}, FAKE_OFFSET="-1023.4")
        self.assertEqual(self.sntp_calls.read_text().splitlines(), ["['-sS', 'time.apple.com']"])
        self.assertIn("clock: corrected by -1023.4 s", out)
        out = self.run_once({}, FAKE_OFFSET="+0.010")
        self.assertNotIn("clock:", out, "a small offset is not news")

    def test_a_failed_clock_sync_still_checks_the_runners(self):
        label = "actions.runner.dolphin-labs-hq.air-ci-1"
        self.agent(label)
        out = self.run_once({label: "not running"}, FAKE_SNTP_FAIL="1")
        self.assertIn("clock: sntp exited 1", out)
        self.assertEqual(len(self.kicked()), 1)


if __name__ == "__main__":
    unittest.main(buffer=True)

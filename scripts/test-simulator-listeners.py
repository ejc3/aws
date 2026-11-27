#!/usr/bin/env python3
"""simulator-listeners.sh with a fake gh: the shared ios-simulator label on exactly the two listeners."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("simulator-listeners.sh")
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
assert args[0] == "api"
runners = json.loads(os.environ["FAKE_RUNNERS"])
if "--paginate" in args:
    assert args[2] == "orgs/dolphin-labs-hq/actions/runners?per_page=100"
    for r in runners:
        print(f"{r['id']}\t{r['name']}\t{'true' if 'ios-simulator' in r['labels'] else 'false'}")
else:
    print(" ".join(args[1:]), file=open(os.environ["CALLS"], "a"))
'''


def runner(id, name, *labels):
    return {"id": id, "name": name, "labels": ["self-hosted", "macOS", *labels]}


class SimulatorListeners(unittest.TestCase):
    def run_script(self, runners, *args):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = Path(tmp, "bin")
            bin_dir.mkdir()
            gh = bin_dir / "gh"
            gh.write_text(FAKE)
            gh.chmod(0o755)
            calls = Path(tmp, "calls")
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", FAKE_RUNNERS=json.dumps(runners),
                       CALLS=str(calls))
            result = subprocess.run(["bash", str(SCRIPT), *args], env=env, capture_output=True, text=True)
            return result, calls.read_text().splitlines() if calls.exists() else []

    def test_adds_the_label_to_each_listener_that_lacks_it(self):
        result, calls = self.run_script([
            runner(717, "mini-4", "ios-pool", "mini", "mini-simulator"),
            runner(318, "macbook-kidsports-1", "ios-pool", "macbook"),
            runner(701, "mini-1", "ios-pool", "mini"),
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            "-X POST orgs/dolphin-labs-hq/actions/runners/717/labels -f labels[]=ios-simulator --silent",
            "-X POST orgs/dolphin-labs-hq/actions/runners/318/labels -f labels[]=ios-simulator --silent",
        ])

    def test_removes_it_from_any_other_runner(self):
        result, calls = self.run_script([
            runner(717, "mini-4", "ios-simulator"),
            runner(318, "macbook-kidsports-1", "ios-simulator"),
            runner(702, "mini-2", "ios-simulator"),
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["-X DELETE orgs/dolphin-labs-hq/actions/runners/702/labels/ios-simulator --silent"])

    def test_already_right_changes_nothing(self):
        result, calls = self.run_script([
            runner(717, "mini-4", "ios-simulator"),
            runner(318, "macbook-kidsports-1", "ios-simulator"),
        ])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])
        self.assertIn("as it should be", result.stdout)

    def test_a_missing_or_doubled_listener_changes_nothing(self):
        for runners in (
            [runner(717, "mini-4")],
            [runner(717, "mini-4"), runner(318, "macbook-kidsports-1"), runner(319, "macbook-kidsports-1")],
        ):
            result, calls = self.run_script(runners)
            self.assertEqual(result.returncode, 1)
            self.assertIn("macbook-kidsports-1 is registered", result.stderr)
            self.assertEqual(calls, [])

    def test_a_dry_run_says_and_does_not_change(self):
        result, calls = self.run_script([runner(717, "mini-4"), runner(318, "macbook-kidsports-1")], "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])
        self.assertIn("mini-4: add ios-simulator", result.stdout)
        self.assertIn("macbook-kidsports-1: add ios-simulator", result.stdout)

    def test_anything_else_is_refused(self):
        result, _ = self.run_script([], "--now")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main(buffer=True)

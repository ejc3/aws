#!/usr/bin/env python3
"""Exercise the mini VM's registration loop with fake SSH and GitHub clients."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("mac-vm-setup.sh")
FAKE = r'''#!/usr/bin/env python3
import json, os, re, sys
from pathlib import Path
config = json.loads(os.environ["FAKE_RUNNERS"])
args = sys.argv[1:]
if Path(sys.argv[0]).name == "ssh":
    cmd = args[-1].replace("\\", "")
    if "Print :CFBundleShortVersionString" in cmd:
        print("27.0\n27A123")
    elif ".ci-vm-copied 2>" in cmd:
        print("27.0 27A123")
    elif "test -f" in cmd and ".runner" in cmd:
        name = re.search(r"/runners/(mini-[1-4])/", cmd)[1]
        if config.get("ssh_failed") and name == "mini-4":
            sys.exit(255)
        print("no" if config.get("fresh") else "yes")
    elif "cat /Users/admin/runners/" in cmd:
        sys.stdout.buffer.write(b'\xef\xbb\xbf{"agentId": 717}')
    elif "./config.sh --unattended" in cmd:
        print(*re.search(r"--name (mini-[1-4]) --labels ([a-z0-9,-]+)", cmd).groups(),
              file=open(os.environ["CALLS"], "a"))
else:
    endpoint = next(x for x in args if x.startswith("orgs/"))
    if "--paginate" in args:
        assert endpoint == "orgs/dolphin-labs-hq/actions/runners?per_page=100"
        print(config.get("slot", "" if config.get("fresh") else "717\tmini-4\tonline\ttrue\tfalse"))
    elif endpoint.endswith("/registration-token"):
        print("test-registration")
    elif endpoint.endswith("/labels"):
        assert args[args.index("-X") + 1] == "POST"
        assert args[args.index("-f") + 1] == "labels[]=mini-simulator"
        print("add", endpoint, file=open(os.environ["CALLS"], "a"))
    else:
        raise RuntimeError("Unexpected GitHub call")
'''


class MiniRunnerRouting(unittest.TestCase):
    def run_setup(self, **config):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            for tool in ("gh", "ssh"):
                path = base / tool
                path.write_text(FAKE)
                path.chmod(0o755)
            calls = base / "calls"
            calls.touch()
            env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"],
                       FAKE_RUNNERS=json.dumps(config), CALLS=str(calls))
            result = subprocess.run(["bash", SCRIPT, "ci", "--lan", "test", "--runners"],
                                    env=env, capture_output=True, text=True, timeout=30)
            return result, calls.read_text().splitlines()

    def test_fresh_labels_keep_all_four_eligible_for_general_jobs(self):
        result, calls = self.run_setup(fresh=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [
            "mini-1 gww-ios,mini,ios-pool", "mini-2 gww-ios,mini,ios-pool",
            "mini-3 gww-ios,mini,ios-pool", "mini-4 gww-ios,mini,ios-pool,mini-simulator",
        ])

    def test_existing_label_is_added_to_the_registered_id_without_replacing_it(self):
        result, calls = self.run_setup()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, ["add orgs/dolphin-labs-hq/actions/runners/717/labels"])
        result, calls = self.run_setup(slot="717\tmini-4\tonline\ttrue\ttrue")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_conflicts_dont_mutate_the_existing_runners(self):
        for case in (dict(slot="81\tother\tonline\tfalse\ttrue"), dict(slot=""),
                     dict(slot="717\tmini-4\tonline\ttrue\ttrue\n81\tother\tonline\tfalse\ttrue"),
                     dict(slot="81\tmini-4\tonline\tfalse\tfalse"), dict(ssh_failed=True)):
            with self.subTest(case=case):
                result, calls = self.run_setup(**case)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_rebuild_reclaims_only_its_own_offline_simulator_registration(self):
        for state, allowed in [("mini-4\toffline\tfalse", True), ("other\toffline\tfalse", False),
                                ("mini-4\tonline\ttrue", False)]:
            with self.subTest(state=state):
                result, calls = self.run_setup(fresh=True, slot=f"717\t{state}\tfalse")
                self.assertEqual(result.returncode == 0, allowed, result.stderr)
                self.assertEqual(any(c.startswith("mini-4 ") for c in calls), allowed)


if __name__ == "__main__":
    unittest.main(buffer=True)

#!/usr/bin/env python3
"""Every test file is quiet when it passes (owner, 2026-10-22: "we can't have noisy tests").

A unittest file runs with buffer=True, so what the code under test prints is held and shown only for a test that fails.
The one custom runner (test-runner-lambdas.py) holds each case's output the same way. Offline.
"""
import pathlib
import re
import unittest

SCRIPTS = pathlib.Path(__file__).resolve().parent


class QuietTests(unittest.TestCase):
    def test_every_unittest_main_buffers_output(self):
        for path in sorted(SCRIPTS.glob("test-*.py")):
            for call in re.findall(r"unittest\.main\([^)]*\)", path.read_text()):
                self.assertIn("buffer=True", call, f"{path.name}: {call}")

    def test_the_custom_runner_holds_each_cases_output(self):
        text = (SCRIPTS / "test-runner-lambdas.py").read_text()
        self.assertIn("contextlib.redirect_stdout(held), contextlib.redirect_stderr(held)", text)
        self.assertNotIn('print(f"ok ', text)


if __name__ == "__main__":
    unittest.main(buffer=True)

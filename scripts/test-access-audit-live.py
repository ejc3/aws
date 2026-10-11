"""Live Linux sensor test for the host access audit agent. Needs root; makes no AWS calls.

fanotify reports another process opening a registered path, with its executable,
pid and process ancestry even when the reader exits at once, including through a hard
link under another name, ignores the agent's own opens and
unregistered paths, and keeps reporting after the file is replaced and re-marked.
macOS is verified through the installed service instead: eslogger does not report
processes in its own login session, so a single-process test cannot observe it.
Run: sudo python3 -S -B scripts/test-access-audit-live.py
"""
import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

SPEC = importlib.util.spec_from_file_location("access_audit", Path(__file__).resolve().parent / "access-audit.py")
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)


class LiveSensorTest(unittest.TestCase):
    def setUp(self):
        self.assertEqual(os.geteuid(), 0, "run as root")
        directory = os.path.realpath(tempfile.mkdtemp())
        self.path = directory + "/registered.env"
        self.other = directory + "/unregistered.env"
        for path in (self.path, self.other):
            Path(path).write_text("x\n")
        self.reports = []
        self.sensor = agent.FanotifySensor([self.path], self.reports.append, [])
        threading.Thread(target=self.sensor.run, daemon=True).start()

    def wait_for(self, count, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline and len([r for r in self.reports if r["kind"] == "file-read"]) < count:
            time.sleep(0.1)
        return [r for r in self.reports if r["kind"] == "file-read"]

    def test_other_process_open_is_reported_with_its_identity(self):
        Path(self.path).read_text()  # the agent's own open is ignored
        subprocess.run(["/bin/cat", self.other], stdout=subprocess.DEVNULL, check=True)
        reader = subprocess.Popen(["/bin/cat", self.path], stdout=subprocess.DEVNULL)
        reader.wait()
        reads = self.wait_for(1)
        time.sleep(1)
        reads = [r for r in self.reports if r["kind"] == "file-read"]
        self.assertEqual(len(reads), 1, reads)
        self.assertEqual(reads[0]["path"], self.path)
        self.assertEqual(reads[0]["pid"], reader.pid)
        self.assertTrue(reads[0]["exe"].endswith("/cat"), reads[0])
        # Captured while the reader was held: the chain starts at the reader, then this test.
        self.assertEqual([e["pid"] for e in reads[0]["ancestry"][:2]], [reader.pid, os.getpid()])

    def test_read_through_a_hard_link_is_reported_under_the_registered_path(self):
        alias = os.path.dirname(self.path) + "/alias"
        os.link(self.path, alias)
        subprocess.run(["/bin/cat", alias], stdout=subprocess.DEVNULL, check=True)
        reads = self.wait_for(1)
        self.assertEqual(len(reads), 1, reads)
        self.assertEqual(reads[0]["path"], self.path)
        self.assertEqual(reads[0]["opened_path"], alias)

    def test_replaced_file_is_still_watched_after_remark(self):
        os.unlink(self.path)
        Path(self.path).write_text("y\n")
        self.sensor.mark(self.path)
        subprocess.run(["/bin/cat", self.path], stdout=subprocess.DEVNULL, check=True)
        self.assertEqual(len(self.wait_for(1)), 1)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Exercise the sampler against a fake procfs and a bounded on-disk history."""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).with_name("nextjs-memory-history.py")
SPEC = importlib.util.spec_from_file_location("nextjs_memory_history", SCRIPT)
recorder = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = recorder
SPEC.loader.exec_module(recorder)


class Samples(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proc = Path(tmp.name) / "proc"
        self.cgroup = Path(tmp.name) / "cgroup"
        (self.proc / "pressure").mkdir(parents=True)
        (self.proc / "vmstat").write_text("pswpin 15\npswpout 80\npgmajfault 500\n")
        self.host_memory(available_kib=8_388_608, swap_free_kib=2_300_000)
        self.container("alice", 7_500_000_000, 1_700_000_000)
        self.container("bob", 2_500_000_000, 100_000_000)

    def host_memory(self, *, available_kib, swap_free_kib):
        (self.proc / "meminfo").write_text(
            f"MemTotal:       33554432 kB\nMemAvailable:   {available_kib} kB\n"
            f"SwapTotal:      4194304 kB\nSwapFree:       {swap_free_kib} kB\n"
        )
        for name in ("memory", "io"):
            (self.proc / "pressure" / name).write_text(
                "some avg10=0.00 avg60=0.05 avg300=0.00 total=1100\n"
                "full avg10=0.00 avg60=0.00 avg300=0.00 total=60\n"
            )

    def container(self, name, ram, swap):
        path = self.cgroup / "machine.slice" / f"systemd-nspawn@{name}.service"
        path.mkdir(parents=True)
        (path / "memory.current").write_text(f"{ram}\n")
        (path / "memory.swap.current").write_text(f"{swap}\n")

    def process(self, pid, name, uid, rss, swap, cgroup):
        path = self.proc / str(pid)
        path.mkdir()
        (path / "status").write_text(
            f"Name:\t{name}\nPid:\t{pid}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n"
            f"VmRSS:\t{rss} kB\nVmSwap:\t{swap} kB\n"
        )
        (path / "cgroup").write_text(f"0::{cgroup}\n")
        (path / "cmdline").write_bytes(b"secret-command-line-arg\0")
        (path / "environ").write_bytes(b"API_KEY=secret-environment-var\0")

    def test_quiet_samples_preserve_each_container_totals_without_process_detail(self):
        self.process(50, "node", 65536001, 840_000, 0, "/machine.slice/systemd-nspawn@alice.service")
        data = recorder.collect(self.proc, self.cgroup)
        host = data["host"]
        self.assertEqual(host["mem_total_bytes"], 32 * 1024 ** 3)
        self.assertEqual(host["mem_available_bytes"], 8 * 1024 ** 3)
        self.assertEqual(host["swap_total_bytes"], 4 * 1024 ** 3)
        self.assertEqual(host["swap_used_bytes"], (4_194_304 - 2_300_000) * 1024)
        self.assertEqual(set(data["containers"]), {"alice", "bob"})
        self.assertEqual(data["containers"]["alice"]["memory_bytes"], 7_500_000_000)
        self.assertEqual(data["containers"]["alice"]["swap_bytes"], 1_700_000_000)
        self.assertEqual(data["containers"]["bob"]["memory_bytes"], 2_500_000_000)
        self.assertEqual(data["containers"]["bob"]["swap_bytes"], 100_000_000)
        details = data.get("top") or {}
        self.assertFalse(details.get("rss"))
        self.assertFalse(details.get("swap"))
        forced = recorder.collect(self.proc, self.cgroup, pressure_details=True)
        self.assertEqual(forced["top"]["rss"][0]["pid"], 50)

    def test_pressure_captures_distinct_ram_and_swap_users_despite_disappearing_pid(self):
        self.host_memory(available_kib=600_000, swap_free_kib=1_000)
        self.process(50, "node", 65536001, 840_000, 10_000,
                     "/machine.slice/systemd-nspawn@alice.service/user.slice/build.service")
        self.process(51, "compiler", 65601537, 20_000, 700_000,
                     "/machine.slice/systemd-nspawn@bob.service/user.slice/build.service")
        self.process(52, "daemon", 1000, 37_000, 3_000, "/system.slice/daemon.service")
        (self.proc / "45").mkdir()  # PID disappeared before status could be read.
        (self.proc / "46" / "status").mkdir(parents=True)  # Error opening a process record.

        data = recorder.collect(self.proc, self.cgroup)
        rss, swap = data["top"]["rss"], data["top"]["swap"]
        self.assertEqual(rss[0]["pid"], 50)
        self.assertEqual(swap[0]["pid"], 51)
        self.assertEqual(rss[0]["container"], "alice")
        self.assertEqual(swap[0]["container"], "bob")
        self.assertEqual(rss[0]["rss_bytes"], 840_000 * 1024)
        self.assertEqual(swap[0]["swap_bytes"], 700_000 * 1024)
        self.assertNotIn(45, {entry["pid"] for entry in rss + swap})
        self.assertNotIn(46, {entry["pid"] for entry in rss + swap})
        serialized = json.dumps(data)
        self.assertNotIn("secret-command-line-arg", serialized)
        self.assertNotIn("secret-environment-var", serialized)


class History(unittest.TestCase):
    @staticmethod
    def read(path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_retention_bounds_the_bytes_and_keeps_newest_complete_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            for sequence in range(6):
                recorder.append_record(logs, {"sequence": sequence, "message": "x" * 60},
                                       max_bytes=128, archives=2)
            names = {path.name for path in logs.glob("samples.jsonl*")}
            self.assertEqual(names, {"samples.jsonl", "samples.jsonl.1", "samples.jsonl.2"})
            recovered = []
            for name in ("samples.jsonl.2", "samples.jsonl.1", "samples.jsonl"):
                path = logs / name
                self.assertLessEqual(path.stat().st_size, 128)
                recovered.extend(self.read(path))
            self.assertEqual([sample["sequence"] for sample in recovered], [3, 4, 5])

    def test_a_torn_tail_does_not_corrupt_complete_samples(self):
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            path = logs / "samples.jsonl"
            recorder.append_record(logs, {"sequence": 1})
            with path.open("ab") as raw:
                raw.write(b'{"partial":"' + b"x" * 9000)
            recorder.append_record(logs, {"sequence": 2})
            self.assertEqual(self.read(path), [{"sequence": 1}, {"sequence": 2}])

    def test_a_fresh_writer_preserves_preexisting_history(self):
        writer = """
import importlib.util
import sys
from pathlib import Path
script, directory, sequence = sys.argv[1:]
spec = importlib.util.spec_from_file_location("recorder", script)
recorder = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recorder
spec.loader.exec_module(recorder)
recorder.append_record(Path(directory), {"sequence": int(sequence)})
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "samples.jsonl"
            for sequence in (1, 2):
                subprocess.run([sys.executable, "-B", "-c", writer, str(SCRIPT), directory, str(sequence)],
                               check=True)
                self.assertEqual(self.read(path), [{"sequence": n} for n in range(1, sequence + 1)])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Boot-time Codex seeding: every folder that gets a tmux window also gets a Codex thread.

The seeder (scripts/codex-seed-thread.py) is run for real against a fake `codex` whose
`app-server proxy` speaks the same thing the real daemon does over the proxy: a WebSocket
upgrade, then one JSON-RPC message per text frame. Nothing here touches a real daemon or model.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEEDER = ROOT / "scripts" / "codex-seed-thread.py"
CLAUDE_RC = (ROOT / "claude-remote-control.tf").read_text()
CODEX_RC = (ROOT / "codex-remote-control.tf").read_text()
NEXTJS = (ROOT / "nextjs-user-data.tf").read_text()

# The fake daemon. Threads live in a JSON file: {cwd: [thread ids]}.
FAKE_CODEX = r'''#!/usr/bin/env python3
import json, os, struct, sys
state_path = os.environ["FAKE_STATE"]
log = open(os.environ["FAKE_LOG"], "a")
if sys.argv[1:3] != ["app-server", "proxy"] or os.environ.get("FAKE_DOWN"):
    sys.exit(1)
rd, wr = sys.stdin.buffer, sys.stdout.buffer
head = b""
while b"\r\n\r\n" not in head:
    c = rd.read(1)
    if not c:
        sys.exit(0)
    head += c
assert b"Upgrade: websocket" in head
wr.write(b"HTTP/1.1 101 Switching Protocols\r\nupgrade: websocket\r\nconnection: Upgrade\r\n\r\n"); wr.flush()

def recv():
    h = rd.read(2)
    if len(h) < 2:
        return None
    op, n = h[0] & 0x0F, h[1] & 0x7F
    assert h[1] & 0x80, "client frames must be masked"
    if n == 126: n = struct.unpack(">H", rd.read(2))[0]
    elif n == 127: n = struct.unpack(">Q", rd.read(8))[0]
    mask = rd.read(4)
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(rd.read(n)))
    return None if op == 0x8 else json.loads(data)

def send(msg):
    data = json.dumps(msg).encode()
    n = len(data)
    head = bytes([0x81]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n))
    wr.write(head + data); wr.flush()

threads = json.load(open(state_path)) if os.path.exists(state_path) else {}
while True:
    m = recv()
    if m is None:
        break
    method = m.get("method")
    log.write(method + " " + json.dumps(m.get("params", {})) + "\n"); log.flush()
    if "id" not in m:
        continue
    p = m.get("params") or {}
    if method == "initialize":
        send({"id": m["id"], "result": {"userAgent": "fake"}})
    elif method == "thread/list":
        send({"id": m["id"], "result": {"data": [{"id": t} for t in threads.get(p["cwd"], [])]}})
    elif method == "thread/start":
        tid = "t%d" % (sum(len(v) for v in threads.values()) + 1)
        threads.setdefault(p["cwd"], []).append(tid)
        json.dump(threads, open(state_path, "w"))
        send({"id": m["id"], "result": {"thread": {"id": tid}}})
    elif method == "turn/start":
        send({"id": m["id"], "result": {"turn": {"id": "u1", "status": "inProgress"}}})
        send({"method": "turn/started", "params": {"threadId": p["threadId"]}})
        send({"method": "turn/completed", "params": {"threadId": p["threadId"], "turn": {"status": "completed"}}})
    else:
        send({"id": m["id"], "result": {}})
'''


class SeederTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="test-codex-seed."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.tmp)])
        self.codex = self.tmp / "codex"
        self.codex.write_text(FAKE_CODEX)
        self.codex.chmod(0o755)
        self.state, self.log = self.tmp / "threads.json", self.tmp / "log"
        self.log.touch()
        self.a, self.b = self.tmp / "repo-a", self.tmp / "repo-b"
        self.a.mkdir()
        self.b.mkdir()

    def run_seeder(self, *args, down=False):
        env = dict(os.environ, CODEX_BIN=str(self.codex), FAKE_STATE=str(self.state), FAKE_LOG=str(self.log))
        if down:
            env["FAKE_DOWN"] = "1"
        return subprocess.run(["python3", str(SEEDER), *args], capture_output=True, text=True, env=env, timeout=60)

    def threads(self):
        return json.loads(self.state.read_text()) if self.state.exists() else {}

    def test_each_folder_gets_one_thread_with_one_seed_message(self):
        proc = self.run_seeder(str(self.a), str(self.b))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(sorted(self.threads()), sorted([str(self.a), str(self.b)]))
        log = self.log.read_text()
        self.assertEqual(log.count("turn/start "), 2)
        starts = [json.loads(l.split(" ", 1)[1]) for l in log.splitlines() if l.startswith("thread/start ")]
        for p in starts:
            self.assertEqual(p["sandbox"], "read-only", "the seed turn must not be able to change anything")
            self.assertEqual(p["approvalPolicy"], "never")

    def test_a_folder_that_already_has_a_thread_is_left_alone(self):
        self.state.write_text(json.dumps({str(self.a): ["existing"]}))
        proc = self.run_seeder(str(self.a), str(self.b))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.threads()[str(self.a)], ["existing"])
        self.assertEqual(len(self.threads()[str(self.b)]), 1)
        self.assertIn("already has a thread", proc.stdout)

    def test_a_second_run_seeds_nothing(self):
        self.run_seeder(str(self.a))
        self.log.write_text("")
        proc = self.run_seeder(str(self.a))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("thread/start", self.log.read_text())

    def test_from_file_skips_missing_folders_and_comments(self):
        lst = self.tmp / "dirs"
        lst.write_text("# boot list\n%s\n\n%s\n%s\n" % (self.a, self.tmp / "gone", self.a))
        proc = self.run_seeder("--from", str(lst))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(list(self.threads()), [str(self.a)], "a duplicate line must not seed twice")
        self.assertIn("is not a directory, skipped", proc.stderr)

    def test_an_unreachable_daemon_fails_loudly(self):
        proc = self.run_seeder(str(self.a), down=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(self.threads(), {})


class WiringTests(unittest.TestCase):
    def test_both_boxes_install_the_same_seeder(self):
        self.assertIn('${file("${path.module}/scripts/codex-seed-thread.py")}', CODEX_RC)
        self.assertIn("${local.codex_seed_thread_install}", CLAUDE_RC)
        self.assertIn("${local.codex_seed_thread_install}", NEXTJS)

    def test_fcvm_seeds_exactly_the_folders_its_launcher_opened(self):
        self.assertIn('cp "$REPOS" "$STATE/repos"', CLAUDE_RC)
        unit = CLAUDE_RC.split("cat > /etc/systemd/system/fcvm-codex-seed.service <<'UNIT'", 1)[1].split("\nUNIT\n", 1)[0]
        self.assertIn("After=fcvm-claude-rc.service codex-rc@ubuntu.service", unit)
        self.assertIn("--from /home/ubuntu/.local/state/fcvm-claude/repos", unit)
        self.assertIn("codex login status", unit, "skip cleanly while Codex is not logged in")
        self.assertIn("systemctl start --no-block fcvm-codex-seed.service", CLAUDE_RC)
        timer = CLAUDE_RC.split("cat > /etc/systemd/system/fcvm-codex-seed.timer <<'TIMER'", 1)[1].split("\nTIMER\n", 1)[0]
        self.assertIn("OnUnitInactiveSec=15min", timer, "a boot while logged out must retry, not wait for a reboot")
        self.assertIn("systemctl enable --now fcvm-codex-seed.timer", CLAUDE_RC)
        self.assertIn("RemainAfterExit=yes", unit, "without it the timer would re-run a successful seed forever")

    def test_nextjs_seeds_only_ejc3_colton_and_connor(self):
        self.assertIn('nextjs_codex_seed_users = ["colton", "connor", "ejc3"]', NEXTJS)
        self.assertNotIn("skevh", NEXTJS.split("nextjs_codex_seed_users =", 1)[1].split("\n", 1)[0])
        self.assertIn('case " ${join(" ", local.nextjs_codex_seed_users)} " in *" $u "*)', NEXTJS)
        unit = NEXTJS.split("cat > /etc/systemd/system/codex-seed@.service <<'UNIT'", 1)[1].split("\nUNIT\n", 1)[0]
        self.assertIn("After=claude-rc@%i.service codex-rc@%i.service", unit)
        self.assertIn("--from /home/%i/.local/state/agents-start/dirs", unit)
        self.assertIn("User=%i", unit)

    def test_agents_start_records_every_folder_it_opens(self):
        start = NEXTJS.split("cat > /usr/local/bin/agents-start <<'AGENTS'", 1)[1].split("\nAGENTS\n", 1)[0]
        self.assertIn("printf '%s\\n' \"$WORKDIR\" > \"$DIRS_NEW\"", start)
        self.assertIn("printf '%s\\n' \"$XD\" >> \"$DIRS_NEW\"", start)
        self.assertIn('mv -f "$DIRS_NEW" "$DIRS_STATE/dirs"', start)


if __name__ == "__main__":
    unittest.main()

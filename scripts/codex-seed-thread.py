#!/usr/bin/env python3
"""codex-seed-thread [--from FILE] [<dir> ...]

Installed as /usr/local/bin/codex-seed-thread by codex-remote-control.tf, and run at boot for
every folder the boot launcher opened a tmux window for (fcvm-codex-seed.service on the metal
boxes, codex-seed@<user>.service on nextjs-dev). --from reads one folder per line.

Make sure the running Codex app-server (the remote-control daemon) has a thread whose cwd is
each <dir>, so the repository shows up in the Codex app. A directory that already has one is
left alone. Otherwise a thread is started there and seeded with ONE message, through
`codex app-server proxy` -- the same daemon and protocol the phone app uses, so the thread is an
interactive (app-listed) session, unlike `codex exec`, whose sessions the app does not list.

The seed turn runs read-only with approvals off: it cannot change anything, and the Codex app
sends its own approval and sandbox settings with every later turn.
"""
import base64
import json
import os
import select
import struct
import subprocess
import sys
import time

CODEX = os.environ.get("CODEX_BIN") or os.path.expanduser("~/.local/bin/codex")
SEED = ("This thread was opened automatically so this repository appears in Codex. "
        "Reply with one short line saying you are ready, and take no other action.")
TURN_TIMEOUT = 300


class Proxy:
    """JSON-RPC to the running daemon. `codex app-server proxy` bridges stdio to the daemon's
    control socket, which speaks WebSocket: one JSON message per text frame (a plain line of
    JSON gets no answer). Client frames are masked, as RFC 6455 requires."""

    def __init__(self):
        self.p = subprocess.Popen([CODEX, "app-server", "proxy"], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.fd = self.p.stdout.fileno()
        self.pending = b""
        self.next_id = 0
        key = base64.b64encode(os.urandom(16)).decode()
        self.p.stdin.write(("GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                            "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\n"
                            "Sec-WebSocket-Version: 13\r\n\r\n" % key).encode())
        self.p.stdin.flush()
        head = self._until(b"\r\n\r\n", 30)
        if not head.startswith(b"HTTP/1.1 101"):
            raise RuntimeError("app-server proxy refused the WebSocket upgrade: %r" % head[:80])

    def _fill(self, timeout):
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return False
        chunk = os.read(self.fd, 65536)
        if not chunk:
            raise RuntimeError("app-server proxy closed: %s" % self.p.stderr.read()[-500:])
        self.pending += chunk
        return True

    def _until(self, marker, timeout):
        deadline = time.monotonic() + timeout
        while marker not in self.pending:
            if not self._fill(max(0.0, deadline - time.monotonic())) and time.monotonic() >= deadline:
                raise TimeoutError("no WebSocket handshake from the app-server")
        head, self.pending = self.pending.split(marker, 1)
        return head

    def _take(self, n, deadline):
        while len(self.pending) < n:
            if not self._fill(max(0.0, deadline - time.monotonic())) and time.monotonic() >= deadline:
                return None
        out, self.pending = self.pending[:n], self.pending[n:]
        return out

    def _frame(self, opcode, payload):
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.p.stdin.write(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        self.p.stdin.flush()

    def send(self, msg):
        self._frame(0x1, json.dumps(msg).encode())

    def read(self, timeout):
        """One whole message, or None if nothing complete arrived within `timeout`."""
        deadline = time.monotonic() + timeout
        parts = []
        while True:
            h = self._take(2, deadline)
            if h is None:
                if parts:
                    raise TimeoutError("a WebSocket message stopped arriving mid-way")
                return None
            fin, opcode, n = h[0] & 0x80, h[0] & 0x0F, h[1] & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._take(2, deadline + 30))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._take(8, deadline + 30))[0]
            payload = self._take(n, deadline + 30) if n else b""
            if opcode == 0x8:
                raise RuntimeError("app-server closed the connection")
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            parts.append(payload)
            if fin:
                return json.loads(b"".join(parts))

    def request(self, method, params, timeout=60):
        self.next_id += 1
        rid = self.next_id
        self.send({"id": rid, "method": method, "params": params})
        msg = self.wait(lambda m: m.get("id") == rid and "method" not in m, timeout)
        if "error" in msg:
            raise RuntimeError("%s failed: %s" % (method, msg["error"]))
        return msg.get("result", {})

    def wait(self, pred, timeout):
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError("timed out waiting on the app-server")
            msg = self.read(left)
            if msg is None:
                continue
            if "method" in msg and "id" in msg:
                # A server->client request (an approval): not expected read-only with approvals
                # off. Decline rather than leave the turn hanging.
                self.send({"id": msg["id"], "result": {"decision": "decline"}})
                continue
            if pred(msg):
                return msg

    def close(self):
        try:
            self._frame(0x8, b"")
            self.p.stdin.close()
            self.p.wait(timeout=5)
        except Exception:
            self.p.kill()


def seed(px, cwd):
    cwd = os.path.realpath(cwd)
    if px.request("thread/list", {"cwd": cwd, "limit": 1}).get("data"):
        print("codex-seed-thread: %s already has a thread" % cwd)
        return
    thread = px.request("thread/start", {"cwd": cwd, "sandbox": "read-only", "approvalPolicy": "never"})["thread"]
    tid = thread["id"]
    px.request("turn/start", {"threadId": tid, "input": [{"type": "text", "text": SEED}]})
    done = px.wait(lambda m: m.get("method") == "turn/completed"
                   and (m.get("params") or {}).get("threadId") == tid, TURN_TIMEOUT)
    status = ((done.get("params") or {}).get("turn") or {}).get("status")
    try:
        px.request("thread/name/set", {"threadId": tid, "name": os.path.basename(cwd)})
    except Exception as e:  # a name is cosmetic
        print("codex-seed-thread: could not name %s: %s" % (tid, e))
    print("codex-seed-thread: %s seeded thread %s (turn %s)" % (cwd, tid, status))


def main(argv):
    dirs = []
    while argv:
        a = argv.pop(0)
        if a == "--from":
            with open(argv.pop(0)) as f:
                dirs += [line.strip() for line in f if line.strip() and not line.startswith("#")]
        else:
            dirs.append(a)
    missing = [d for d in dirs if not os.path.isdir(d)]
    for d in missing:
        print("codex-seed-thread: %s is not a directory, skipped" % d, file=sys.stderr)
    dirs = [d for d in dict.fromkeys(dirs) if d not in missing]
    if not dirs:
        print("codex-seed-thread: nothing to seed")
        return 0
    px = Proxy()
    try:
        px.request("initialize", {"clientInfo": {"name": "fcvm_codex_seed", "title": "fcvm boot seed", "version": "1"}})
        px.send({"method": "initialized"})
        failed = 0
        for d in dirs:
            try:
                seed(px, d)
            except Exception as e:
                failed += 1
                print("codex-seed-thread: %s: %s" % (d, e), file=sys.stderr)
        return 1 if failed else 0
    finally:
        px.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

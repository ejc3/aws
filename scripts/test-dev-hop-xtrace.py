#!/usr/bin/env python3
"""The dev-hop private key must never reach xtrace output.

local.dev_hop_setup (dev-hop-key.tf) runs inside boot scripts that use `set -euxo pipefail`.
Before the fix it printed the key into cloud-init-output.log, the journal, dev-selfupdate.log
and the EC2 serial console. These tests run the real heredoc with stubbed commands and a fake
key, so nothing here touches AWS or a real home directory.
"""
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCK = (ROOT / "dev-hop-key.tf").read_text().split("  dev_hop_setup = <<-HOP\n", 1)[1].split("\nHOP\n", 1)[0]

MARKER = "FAKEPRIVATEKEYMATERIAL0123456789"
SECRET_JSON = json.dumps({
    "private": f"-----BEGIN OPENSSH PRIVATE KEY-----\n{MARKER}\n-----END OPENSSH PRIVATE KEY-----",
    "public": "ssh-ed25519 AAAAFAKEPUBLICKEY dev-hop",
})
STUBS = {
    "aws": '#!/bin/bash\n[ "${FAKE_AWS_FAIL:-}" = 1 ] && exit 255\nprintf \'%s\\n\' "$FAKE_SECRET_JSON"\n',
    "install": '#!/bin/bash\nmkdir -p "${@: -1}"\n',
    "chown": "#!/bin/sh\nexit 0\n",
    "shred": '#!/bin/sh\nshift\nrm -f "$@"\n',
}


def run(block, prelude="set -euxo pipefail", fail_aws=False):
    tmp = tempfile.mkdtemp()
    home = os.path.join(tmp, "home")
    stubs = os.path.join(tmp, "stubs")
    os.makedirs(home)
    os.makedirs(stubs)
    for name, body in STUBS.items():
        path = os.path.join(stubs, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    rendered = (block.replace("${local.dev_hop_ssh_config}", "Host fake-hop\n  HostName 192.0.2.10")
                .replace("/home/ubuntu", home))
    script = f"{prelude}\n{rendered}\necho after-dev-hop-block\n"
    env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", FAKE_SECRET_JSON=SECRET_JSON,
               FAKE_AWS_FAIL="1" if fail_aws else "")
    proc = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    return proc, Path(home)


class DevHopXtraceTests(unittest.TestCase):
    def test_the_key_never_reaches_trace_output_and_tracing_comes_back(self):
        proc, home = run(BLOCK)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertNotIn(MARKER, proc.stdout + proc.stderr)
        self.assertIn(MARKER, (home / ".ssh" / "dev_hop").read_text())
        self.assertIn("+ echo after-dev-hop-block", proc.stderr)

    def test_tracing_stays_off_when_the_caller_had_it_off(self):
        proc, home = run(BLOCK, prelude="set -euo pipefail")
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertNotIn(MARKER, proc.stdout + proc.stderr)
        self.assertFalse([line for line in proc.stderr.splitlines() if line.startswith("+")], proc.stderr)
        self.assertTrue((home / ".ssh" / "dev_hop").exists())

    def test_an_unreadable_secret_warns_continues_and_restores_tracing(self):
        proc, home = run(BLOCK, fail_aws=True)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn("WARNING: could not read dev-hop-ssh-key", proc.stdout)
        self.assertIn("+ echo after-dev-hop-block", proc.stderr)
        self.assertFalse((home / ".ssh" / "dev_hop").exists())

    def test_the_harness_catches_a_leak(self):
        # Without the `set +x`, the same run must expose the fake key, or the tests above
        # would pass without proving anything.
        self.assertIn("\nset +x\n", BLOCK)
        proc, _ = run(BLOCK.replace("\nset +x\n", "\n", 1))
        self.assertIn(MARKER, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

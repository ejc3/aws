#!/usr/bin/env python3
"""The scroll-native tmux must install beside the normal one, and only when it is genuine.

t-claude gives the terminal's own scrollback (swipe-to-scroll on a phone) the lines that scroll
out of a pane, but only with a tmux that has `scroll-replay`; a stock build loses them on any
big scroll. These tests run the real heredoc from nextjs-user-data.tf against stubbed downloads,
so nothing here touches the network or a real box.
"""
import os
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEXTJS = (ROOT / "nextjs-user-data.tf").read_text()
SELFUPDATE = (ROOT / "dev-selfupdate.tf").read_text()
START = "# The scroll-native tmux, installed BESIDE the normal one as tmux-scroll rather than over it."
END = "# Claude Code -- the NATIVE installer, per user."
RELEASE = "binaries-scroll-native"


def block():
    if START not in NEXTJS or END not in NEXTJS:
        raise AssertionError("the tmux-scroll block markers moved; update this harness")
    return NEXTJS.split(START, 1)[1].split(END, 1)[0]


BLOCK = block()


def make_tarball(path, payload):
    """A release tarball holding one binary named tmux-scroll."""
    tmp = Path(path).parent / "tmux-scroll"
    tmp.write_bytes(payload)
    tmp.chmod(0o755)
    with tarfile.open(path, "w:gz") as tar:
        tar.add(tmp, arcname="tmux-scroll")
    tmp.unlink()


# A stand-in for the real binary: `-V` works and the file contains the option name the
# installer greps for, which is what distinguishes the patched build from a stock one.
GENUINE = b"#!/bin/sh\n# scroll-replay\n[ \"$1\" = -V ] && echo 'tmux next-3.8'\nexit 0\n"
STOCK = b"#!/bin/sh\n[ \"$1\" = -V ] && echo 'tmux 3.7b'\nexit 0\n"


class TmuxScrollInstallTests(unittest.TestCase):
    def run_block(self, payload=GENUINE, download_fails=False, preinstalled=None):
        tmp = Path(tempfile.mkdtemp(prefix="test-tmux-scroll."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        bindir, stubs = tmp / "usr-local-bin", tmp / "stubs"
        bindir.mkdir()
        stubs.mkdir()
        tarball = tmp / "release.tar.gz"
        make_tarball(tarball, payload)

        calls = tmp / "calls"
        calls.touch()
        (stubs / "curl").write_text(
            "#!/bin/bash\n"
            f'echo "curl $*" >> {calls}\n'
            '[ -n "${FAIL_CURL:-}" ] && exit 22\n'
            'out=""; while [ $# -gt 0 ]; do [ "$1" = -o ] && { out="$2"; shift; }; shift; done\n'
            f'cp {tarball} "$out"\n')
        (stubs / "curl").chmod(0o755)

        if preinstalled is not None:
            (bindir / "tmux-scroll").write_bytes(preinstalled)
            (bindir / "tmux-scroll").chmod(0o755)

        rendered = BLOCK.replace("/usr/local/bin", str(bindir))
        env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", FAIL_CURL="1" if download_fails else "")
        proc = subprocess.run(["bash", "-c", f"set -uxo pipefail\n{rendered}\necho done-tmux-scroll\n"],
                              capture_output=True, text=True, env=env)
        self.assertIn("done-tmux-scroll", proc.stdout, proc.stderr[-2000:])
        return proc, bindir / "tmux-scroll", calls.read_text()

    def test_a_genuine_build_is_installed_beside_the_normal_tmux(self):
        proc, installed, calls = self.run_block()
        self.assertTrue(installed.exists(), proc.stderr[-1500:])
        self.assertIn(b"scroll-replay", installed.read_bytes())
        self.assertTrue(installed.stat().st_mode & stat.S_IXUSR)
        self.assertIn(RELEASE, calls)
        self.assertNotIn("/tmux ", str(installed), "it must not overwrite the normal tmux")

    def test_a_stock_build_is_refused(self):
        proc, installed, calls = self.run_block(payload=STOCK)
        self.assertFalse(installed.exists(), "a build without scroll-replay was installed")
        self.assertIn("WARNING", proc.stdout + proc.stderr)
        self.assertIn(RELEASE, calls)

    def test_a_refused_download_leaves_a_good_copy_alone(self):
        marker = GENUINE + b"# installed earlier\n"
        proc, installed, _ = self.run_block(payload=STOCK, preinstalled=marker)
        self.assertEqual(installed.read_bytes(), marker, "the good copy was replaced")

    def test_nothing_is_installed_when_the_download_fails(self):
        proc, installed, _ = self.run_block(download_fails=True)
        self.assertFalse(installed.exists())
        self.assertIn("WARNING", proc.stdout + proc.stderr)

    def test_an_installed_genuine_build_is_not_downloaded_again(self):
        _, installed, calls = self.run_block(preinstalled=GENUINE)
        self.assertEqual(calls.strip(), "", "it re-downloaded a binary that was already good")
        self.assertTrue(installed.exists())

    def test_a_stale_stock_binary_is_replaced(self):
        _, installed, calls = self.run_block(preinstalled=STOCK)
        self.assertIn(RELEASE, calls, "a stock binary already in place must be upgraded")
        self.assertIn(b"scroll-replay", installed.read_bytes())

    def test_the_metal_boxes_get_it_through_the_prebuilt_binary_table(self):
        table = SELFUPDATE.split("TABLE='", 1)[1].split("'", 1)[0]
        rows = [r.split("|") for r in table.splitlines() if r.strip()]
        scroll = [r for r in rows if r[1] == RELEASE]
        self.assertEqual(len(scroll), 1, table)
        repo, tag, prefix, binaries, dest, service, vercmd = scroll[0]
        self.assertEqual(repo, "ejc3/tmux")
        self.assertEqual(prefix, "tmux-scroll", "the asset prefix must match the release asset name")
        self.assertEqual(binaries, "tmux-scroll", "installing it as `tmux` would replace the normal one")
        self.assertEqual(dest, "/usr/local/bin")
        self.assertEqual(service, "", "no service restarts for a binary nothing runs yet")
        self.assertEqual(vercmd, f"{dest}/{binaries} -V")
        for row in rows:
            self.assertEqual(len(row), 7, f"every row needs 7 fields: {row}")


if __name__ == "__main__":
    unittest.main()

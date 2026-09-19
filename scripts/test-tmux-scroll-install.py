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
TABLE = SELFUPDATE.split("TABLE='", 1)[1].split("'", 1)[0]


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
        rows = [r.split("|") for r in TABLE.splitlines() if r.strip()]
        scroll = [r for r in rows if r[1] == RELEASE]
        self.assertEqual(len(scroll), 1, TABLE)
        repo, tag, prefix, binaries, dest, service, vercmd, marker = scroll[0]
        self.assertEqual(repo, "ejc3/tmux")
        self.assertEqual(prefix, "tmux-scroll", "the asset prefix must match the release asset name")
        self.assertEqual(binaries, "tmux-scroll", "installing it as `tmux` would replace the normal one")
        self.assertEqual(dest, "/usr/local/bin")
        self.assertEqual(service, "", "no service restarts for a binary nothing runs yet")
        self.assertEqual(vercmd, f"{dest}/{binaries} -V")
        self.assertEqual(marker, "scroll-replay", "-V alone cannot tell the patched build from a stock one")
        for row in rows:
            self.assertIn(len(row), (7, 8), f"a row has 7 fields, or 8 with a marker: {row}")


class PrebuiltBinaryUpdaterTests(unittest.TestCase):
    """The weekly updater on the metal boxes, run for real against stubbed releases."""

    def run_updater(self, payload=GENUINE, installed=None, live_session=False):
        tmp = Path(tempfile.mkdtemp(prefix="test-bin-update."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        bindir, usrbin, stubs = tmp / "usr-local-bin", tmp / "usr-bin", tmp / "stubs"
        socks, state, assets = tmp / "socks", tmp / "state", tmp / "assets"
        for d in (bindir, usrbin, stubs, socks, state, assets):
            d.mkdir()

        # One release tarball per asset prefix, so every row in the real table resolves.
        for prefix, names in (("tmux-scroll", ["tmux-scroll"]), ("tmux", ["tmux"]),
                              ("et", ["et", "etserver", "etterminal"])):
            body = payload if prefix == "tmux-scroll" else GENUINE
            staging = assets / prefix
            staging.mkdir()
            for n in names:
                (staging / n).write_bytes(body)
                (staging / n).chmod(0o755)
            with tarfile.open(assets / f"{prefix}.tar.gz", "w:gz") as tar:
                for n in names:
                    tar.add(staging / n, arcname=n)

        calls = tmp / "calls"
        calls.touch()
        (stubs / "curl").write_text(
            "#!/bin/bash\n"
            f'echo "curl $*" >> {calls}\n'
            'out=""; url=""\n'
            'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; http*) url="$1";; esac; shift; done\n'
            'name=$(basename "$url"); prefix=${name%%-*}\n'
            'case "$name" in tmux-scroll-*) prefix=tmux-scroll;; esac\n'
            f'cp {assets}/$prefix.tar.gz "$out" 2>/dev/null\n')
        (stubs / "sudo").write_text('#!/bin/bash\n[ "$1" = -u ] && shift 2\nexec "$@"\n')
        (stubs / "systemctl").write_text(f'#!/bin/bash\necho "systemctl $*" >> {calls}\nexit 0\n')
        for f in ("curl", "sudo", "systemctl"):
            (stubs / f).chmod(0o755)

        if installed is not None:
            (bindir / "tmux-scroll").write_bytes(installed)
            (bindir / "tmux-scroll").chmod(0o755)
        if live_session:
            # A server is up: the client answers `list-sessions`, which is the updater's probe.
            (socks / "tmux-1000").mkdir()
            for name in ("tmux-scroll", "tmux"):
                (bindir / name).write_bytes(
                    b'#!/bin/sh\n[ "$1" = list-sessions ] && exit 0\n[ "$1" = -V ] && echo "tmux next-3.8"\nexit 0\n')
                (bindir / name).chmod(0o755)

        script = SELFUPDATE.split("  bin_update = <<-EOT\n", 1)[1].split("\nEOT\n", 1)[0]
        script = script.split("cat > /usr/local/bin/dev-bin-update.sh <<'BINUPD'\n", 1)[1].split("\nBINUPD\n", 1)[0]
        for original, replacement in (("$${", "${"), ("/usr/local/bin", str(bindir)), ("/usr/bin", str(usrbin)),
                                      ("/var/lib/dev-bin-update", str(state)), ("/tmp/tmux-*", f"{socks}/tmux-*")):
            script = script.replace(original, replacement)
        env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}")
        proc = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=120)
        return proc, bindir / "tmux-scroll", calls.read_text()

    def test_a_genuine_asset_is_installed(self):
        proc, installed, _ = self.run_updater()
        self.assertTrue(installed.exists(), proc.stdout[-1500:] + proc.stderr[-800:])
        self.assertIn(b"scroll-replay", installed.read_bytes())

    def test_an_asset_without_the_marker_is_refused(self):
        marked = GENUINE + b"# already installed\n"
        proc, installed, _ = self.run_updater(payload=STOCK, installed=marked)
        self.assertIn("does not contain", proc.stdout, proc.stdout[-1500:])
        self.assertEqual(installed.read_bytes(), marked, "a stock build replaced the patched one")

    def test_an_update_is_deferred_while_a_server_is_live(self):
        """Swapping a tmux binary under a live server locks its sessions out on the next
        attach (protocol mismatch), so both tmux rows must wait, not just the original one."""
        proc, _, _ = self.run_updater(live_session=True)
        deferred = [l for l in proc.stdout.splitlines() if "has live sessions" in l]
        self.assertTrue(any("[tmux-scroll]" in l for l in deferred), proc.stdout[-1500:])
        self.assertTrue(any("[tmux]" in l for l in deferred), proc.stdout[-1500:])


if __name__ == "__main__":
    unittest.main()

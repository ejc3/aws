#!/usr/bin/env python3
"""The scroll-native tmux must install beside the normal one, and only when it is genuine.

t-claude gives the terminal's own scrollback (swipe-to-scroll on a phone) the lines that scroll
out of a pane, but only with a tmux that has `scroll-replay`; a stock build loses them on any
big scroll. These tests run the real heredoc from nextjs-user-data.tf against stubbed downloads,
so nothing here touches the network or a real box.
"""
import hashlib
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NEXTJS = (ROOT / "nextjs-user-data.tf").read_text()
SELFUPDATE = (ROOT / "dev-selfupdate.tf").read_text()
PIN = (ROOT / "tmux-scroll.tf").read_text()
INSTALLER = ROOT / "scripts" / "admin-tmux-tclaude.sh"
SSM_WRAPPER = (ROOT / "scripts" / "ssm-admin-tmux-tclaude.sh").read_text()
START = "# The scroll-native tmux, installed BESIDE the normal one as tmux-scroll rather than over it."
END = "# Claude Code -- the NATIVE installer, per user."


def pin(name):
    m = re.search(r'^\s*%s\s*=\s*"([^"]+)"' % re.escape(name), PIN, re.M)
    if m is None:
        raise AssertionError("tmux-scroll.tf has no %s" % name)
    return m.group(1)


RELEASE = pin("tmux_scroll_tag")
PINNED_SHA = pin("tmux_scroll_sha256_aarch64")
TCLAUDE_REF = pin("tclaude_ref")
RAW_TABLE = SELFUPDATE.split("TABLE='", 1)[1].split("'", 1)[0]
TABLE = RAW_TABLE.replace("${local.tmux_scroll_tag}", RELEASE)


def stub(path, body):
    path.write_text(body)
    path.chmod(0o755)


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
    def run_block(self, payload=GENUINE, download_fails=False, preinstalled=None, state_sha=None,
                  pinned_sha=None, arch="aarch64"):
        tmp = Path(tempfile.mkdtemp(prefix="test-tmux-scroll."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        bindir, stubs, state = tmp / "usr-local-bin", tmp / "stubs", tmp / "state"
        bindir.mkdir()
        stubs.mkdir()
        tarball = tmp / "release.tar.gz"
        make_tarball(tarball, payload)
        tar_sha = hashlib.sha256(tarball.read_bytes()).hexdigest()
        if state_sha is not None:
            state.mkdir()
            (state / "sha256").write_text(state_sha + "\n")
        stub(stubs / "uname", f'#!/bin/sh\n[ "$1" = -m ] && echo {arch} || exec /bin/uname "$@"\n')

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

        rendered = (BLOCK.replace("/usr/local/bin", str(bindir)).replace("/var/lib/tmux-scroll", str(state))
                    .replace("${local.tmux_scroll_tag}", RELEASE)
                    .replace("${local.tmux_scroll_sha256_aarch64}", pinned_sha or tar_sha))
        env = dict(os.environ, PATH=f"{stubs}:{os.environ['PATH']}", FAIL_CURL="1" if download_fails else "")
        proc = subprocess.run(["bash", "-c", f"set -uxo pipefail\n{rendered}\necho done-tmux-scroll\n"],
                              capture_output=True, text=True, env=env)
        self.assertIn("done-tmux-scroll", proc.stdout, proc.stderr[-2000:])
        self.tar_sha, self.state = tar_sha, state
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

    def test_the_pinned_build_already_installed_is_not_downloaded_again(self):
        tarball = Path(tempfile.mkdtemp()) / "t.tgz"
        make_tarball(tarball, GENUINE)
        sha = hashlib.sha256(tarball.read_bytes()).hexdigest()
        _, installed, calls = self.run_block(preinstalled=GENUINE, state_sha=sha, pinned_sha=sha)
        self.assertEqual(calls.strip(), "", "it re-downloaded the build the pin already names")
        self.assertTrue(installed.exists())

    def test_a_genuine_build_from_an_older_pin_is_upgraded(self):
        # The bug this replaces: "install only if missing" kept a box on its first build forever.
        older = GENUINE + b"# next-3.8\n"
        proc, installed, calls = self.run_block(preinstalled=older, state_sha="0" * 64)
        self.assertIn(RELEASE, calls, "a genuine but outdated build was never upgraded")
        self.assertEqual(installed.read_bytes(), GENUINE)
        self.assertEqual((self.state / "sha256").read_text().strip(), self.tar_sha)
        self.assertEqual((installed.parent / "tmux-scroll.prev").read_bytes(), older)

    def test_a_tarball_that_does_not_match_the_pinned_sha_is_refused(self):
        marker = GENUINE + b"# installed earlier\n"
        proc, installed, _ = self.run_block(preinstalled=marker, pinned_sha="f" * 64)
        self.assertEqual(installed.read_bytes(), marker, "an unpinned tarball replaced the installed copy")
        self.assertIn("sha256", proc.stdout + proc.stderr)

    def test_a_box_without_a_pinned_build_keeps_its_copy(self):
        marker = GENUINE + b"# installed earlier\n"
        proc, installed, calls = self.run_block(preinstalled=marker, arch="x86_64")
        self.assertEqual(calls.strip(), "")
        self.assertEqual(installed.read_bytes(), marker)
        self.assertIn("no pinned tmux-scroll build", proc.stdout + proc.stderr)

    def test_a_stale_stock_binary_is_replaced(self):
        _, installed, calls = self.run_block(preinstalled=STOCK)
        self.assertIn(RELEASE, calls, "a stock binary already in place must be upgraded")
        self.assertIn(b"scroll-replay", installed.read_bytes())

    def test_the_metal_boxes_get_it_through_the_prebuilt_binary_table(self):
        self.assertIn("ejc3/tmux|${local.tmux_scroll_tag}|tmux-scroll|", RAW_TABLE,
                      "the metal updater must take its tag from the one pin in tmux-scroll.tf")
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
        for original, replacement in (("${local.tmux_scroll_tag}", RELEASE), ("$${", "${"),
                                      ("/usr/local/bin", str(bindir)), ("/usr/bin", str(usrbin)),
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



class AdminInstallerTests(unittest.TestCase):
    """scripts/admin-tmux-tclaude.sh, run for real as the current user against stubbed downloads."""

    def run_installer(self, payload=GENUINE, pinned_sha=None, home_files=None):
        tmp = Path(tempfile.mkdtemp(prefix="test-admin-tmux."))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        home, stubs, assets = tmp / "home", tmp / "stubs", tmp / "assets"
        for d in (home, stubs, assets):
            d.mkdir()
        make_tarball(assets / "tmux-scroll.tgz", payload)
        (assets / "t-claude.zsh").write_text("# t-claude @ pin\nt-claude() { :; }\n")
        (assets / "nosync-wrap").write_text("#!/usr/bin/env python3\nprint('nosync')\n")
        sha = hashlib.sha256((assets / "tmux-scroll.tgz").read_bytes()).hexdigest()
        for rel, body in (home_files or {}).items():
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            (home / rel).write_bytes(body)
        calls = tmp / "calls"
        calls.touch()
        stub(stubs / "curl", "#!/bin/bash\n"
             f'echo "curl $*" >> {calls}\n'
             'out=""; url=""\n'
             'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift;; http*) url="$1";; esac; shift; done\n'
             f'case "$url" in *.tar.gz) cp {assets}/tmux-scroll.tgz "$out";; *) cp {assets}/$(basename "$url") "$out";; esac\n')
        stub(stubs / "uname", '#!/bin/sh\n[ "$1" = -m ] && echo aarch64 || exec /bin/uname "$@"\n')
        if shutil.which("zsh") is None:
            stub(stubs / "zsh", "#!/bin/sh\nexit 0\n")
        env = dict(os.environ, HOME=str(home), PATH=f"{stubs}:{os.environ['PATH']}",
                   TMUX_SCROLL_TAG=RELEASE, TMUX_SCROLL_SHA256=pinned_sha or sha, TCLAUDE_REF=TCLAUDE_REF)
        proc = subprocess.run(["bash", str(INSTALLER)], capture_output=True, text=True, env=env, timeout=60)
        return proc, home, calls.read_text()

    def test_installs_all_three_into_the_users_home(self):
        proc, home, calls = self.run_installer()
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        self.assertIn(b"scroll-replay", (home / ".local/bin/tmux-scroll").read_bytes())
        self.assertIn("t-claude @ pin", (home / ".config/t-claude.zsh").read_text())
        self.assertTrue((home / ".local/bin/nosync-wrap").stat().st_mode & stat.S_IXUSR)
        self.assertIn(f"releases/download/{RELEASE}/tmux-scroll-aarch64.tar.gz", calls)
        self.assertIn(f"ejc3/t-claude/{TCLAUDE_REF}/t-claude.zsh", calls)

    def test_a_second_run_changes_nothing(self):
        proc, home, _ = self.run_installer()
        files = {rel: (home / rel).read_bytes() for rel in
                 (".local/bin/tmux-scroll", ".local/bin/nosync-wrap", ".config/t-claude.zsh")}
        proc2, home2, _ = self.run_installer(home_files=files)
        self.assertEqual(proc2.returncode, 0, proc2.stderr[-1500:])
        self.assertEqual(proc2.stdout.count("already current"), 3, proc2.stdout)

    def test_a_tarball_that_does_not_match_the_pin_changes_nothing(self):
        old = GENUINE + b"# older build\n"
        proc, home, _ = self.run_installer(pinned_sha="f" * 64, home_files={".local/bin/tmux-scroll": old})
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual((home / ".local/bin/tmux-scroll").read_bytes(), old)
        self.assertFalse((home / ".config/t-claude.zsh").exists(), "it kept going after a sha mismatch")

    def test_a_stock_build_is_refused(self):
        proc, home, _ = self.run_installer(payload=STOCK)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse((home / ".local/bin/tmux-scroll").exists())

    def test_the_ssm_wrapper_runs_it_as_ubuntu_and_fails_on_failure(self):
        self.assertIn("runuser -u ubuntu -- env HOME=/home/ubuntu", SSM_WRAPPER)
        self.assertIn('if [ "$status" != Success ]', SSM_WRAPPER)
        self.assertIn("base64 -w0", SSM_WRAPPER)

    def test_the_pin_is_well_formed(self):
        self.assertRegex(PINNED_SHA, r"^[0-9a-f]{64}$")
        self.assertRegex(TCLAUDE_REF, r"^[0-9a-f]{40}$")
        # One pin: no install path may name a release tag of its own.
        for name in ("nextjs-user-data.tf", "dev-selfupdate.tf", "jumpbox2-user-data.tf"):
            self.assertNotIn("binaries-scroll-native", (ROOT / name).read_text(), name)


if __name__ == "__main__":
    unittest.main()

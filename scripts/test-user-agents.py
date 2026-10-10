#!/usr/bin/env python3
"""user-agents.tf: starter user-level agent instructions for an account that has none. Offline.

Runs the real seeder (scripts/user-agents-seed.sh) against throwaway home directories and pins
what makes it safe to put in every setup script: it only creates, it never touches an account
that already has either file, a re-run changes nothing, the text has one source, and the
instances that receive it all ignore user_data changes (so publishing it replaces no box).
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "scripts" / "user-agents-seed.sh"
TEXT = ROOT / "scripts" / "user-agents.md"
TF = (ROOT / "user-agents.tf").read_text()


def seed(home, *args, src=TEXT):
    env = {"PATH": os.environ["PATH"], "HOME": str(home), "USER_AGENTS_SRC": str(src)}
    return subprocess.run(["bash", str(SEED), *args], env=env, capture_output=True, text=True)


def tree(home):
    """Every path under home with what it is: a link target, or a file's bytes and mtime."""
    out = {}
    for p in sorted(Path(home).rglob("*")):
        rel = str(p.relative_to(home))
        if p.is_symlink():
            out[rel] = ("link", os.readlink(p))
        elif p.is_file():
            out[rel] = ("file", p.read_bytes(), p.stat().st_mtime_ns)
        else:
            out[rel] = ("dir", oct(p.stat().st_mode & 0o777))
    return out


class SeederTests(unittest.TestCase):
    def setUp(self):
        self.assertNotEqual(os.geteuid(), 0, "run as an ordinary account: as root the seeder only dispatches")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.agents = self.home / ".codex" / "AGENTS.md"
        self.claude = self.home / ".claude" / "CLAUDE.md"

    def test_an_account_with_neither_file_gets_one_real_file_and_a_link_to_it(self):
        r = seed(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.agents.is_file() and not self.agents.is_symlink())
        self.assertEqual(self.agents.read_bytes(), TEXT.read_bytes())
        self.assertEqual(self.agents.stat().st_mode & 0o777, 0o644)
        # Relative, so the link survives a home directory that moves.
        self.assertEqual(os.readlink(self.claude), "../.codex/AGENTS.md")
        self.assertEqual(self.claude.resolve(), self.agents.resolve())
        self.assertEqual(self.claude.read_bytes(), TEXT.read_bytes())
        for d in (".codex", ".claude"):
            self.assertEqual((self.home / d).stat().st_mode & 0o777, 0o700, d)
        self.assertEqual(sorted(p.name for p in (self.home / ".codex").iterdir()), ["AGENTS.md"], "no temp file left")

    def test_a_second_run_changes_nothing_and_keeps_a_later_edit(self):
        self.assertEqual(seed(self.home).returncode, 0)
        self.agents.write_text("edited on the box\n")
        before = tree(self.home)
        r = seed(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("leaving the account alone", r.stdout)
        self.assertEqual(tree(self.home), before)

    def existing(self, make):
        """An account that already has something: the run must leave the whole home as it was."""
        make()
        before = tree(self.home)
        r = seed(self.home)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("leaving the account alone", r.stdout)
        self.assertEqual(tree(self.home), before)

    def test_an_existing_claude_file_is_left_alone_and_no_agents_file_appears(self):
        def make():
            self.claude.parent.mkdir()
            self.claude.write_text("mine\n")
        self.existing(make)
        self.assertFalse((self.home / ".codex").exists())

    def test_an_existing_agents_file_is_left_alone_and_no_link_appears(self):
        def make():
            self.agents.parent.mkdir()
            self.agents.write_text("mine\n")
        self.existing(make)
        self.assertFalse((self.home / ".claude").exists())

    def test_two_different_existing_files_are_both_left_alone(self):
        def make():
            self.agents.parent.mkdir()
            self.agents.write_text("for codex\n")
            self.claude.parent.mkdir()
            self.claude.write_text("for claude\n")
        self.existing(make)

    def test_a_dangling_link_counts_as_existing(self):
        def make():
            self.claude.parent.mkdir()
            self.claude.symlink_to("/nonexistent/elsewhere.md")
        self.existing(make)
        self.assertFalse((self.home / ".codex").exists())

    def test_a_link_already_pointing_somewhere_else_is_not_re_linked(self):
        def make():
            (self.home / "notes.md").write_text("mine\n")
            self.agents.parent.mkdir()
            self.agents.symlink_to("../notes.md")
        self.existing(make)

    def test_a_missing_text_fails_without_creating_anything(self):
        r = seed(self.home, src=self.home / "absent.md")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(tree(self.home), {})

    def test_only_root_may_name_accounts(self):
        r = seed(self.home, "ubuntu")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(tree(self.home), {})


class TextTests(unittest.TestCase):
    def test_the_file_is_a_title_the_session_values_paragraph_and_the_models_section(self):
        text = TEXT.read_text()
        lines = text.split("\n")
        self.assertRegex(lines[0], r"^# \S")
        self.assertEqual((lines[1], lines[3]), ("", ""))
        self.assertTrue(lines[2].startswith("Session values stay out of the repository. "))
        self.assertTrue(lines[2].endswith("before every push."))
        # The owner, 2026-10-10: every account is told which cheaper models exist and when to hand them work.
        self.assertEqual(lines[4], "## Cheaper models for straightforward work")
        self.assertEqual([l for l in lines if l.startswith("#")], [lines[0], lines[4]], "one title, one section")
        for model in ("`haiku`", "`sonnet`", "Opus 5.5"):
            self.assertIn(model, text)
        self.assertTrue(text.endswith("\n") and not text.endswith("\n\n"))

    def test_the_text_has_one_source(self):
        # Terraform embeds the file; no .tf or other script carries a copy that could drift.
        self.assertIn('base64encode(file("${path.module}/scripts/user-agents.md"))', TF)
        self.assertIn('base64encode(file("${path.module}/scripts/user-agents-seed.sh"))', TF)
        opening = "Session values stay out of the repository"
        for p in [*ROOT.glob("*.tf"), *(ROOT / "scripts").iterdir()]:
            if p in (TEXT, Path(__file__).resolve()) or not p.is_file():
                continue
            self.assertNotIn(opening, p.read_text(errors="replace"), p.name)
        for doc in ("README.md", "AGENTS.md"):
            self.assertIn("scripts/user-agents.md", (ROOT / doc).read_text(), f"{doc} points at the one source")


class WiringTests(unittest.TestCase):
    def render(self, prefix):
        """local.user_agents_seed_install as Terraform renders it, with /usr/local moved under prefix."""
        body = TF.split("user_agents_seed_install = <<-INSTALL\n", 1)[1].split("\nINSTALL\n", 1)[0]

        def embed(m):
            import base64
            return base64.b64encode((ROOT / "scripts" / m.group(1)).read_bytes()).decode()

        body, n = re.subn(r'\$\{base64encode\(file\("\$\{path\.module\}/scripts/([\w.-]+)"\)\)\}', embed, body)
        self.assertEqual(n, 2)
        self.assertNotIn("${", body, "nothing else is interpolated")
        return body.replace("/usr/local/", f"{prefix}/")

    def test_the_rendered_install_puts_the_repo_files_in_place_and_the_installed_seeder_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp) / "usr-local"
            (prefix / "share").mkdir(parents=True)
            (prefix / "bin").mkdir()
            for _ in range(2):   # a second install over the first is the re-run every box does
                r = subprocess.run(["bash", "-euo", "pipefail", "-c", self.render(prefix)], capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
            text = prefix / "share" / "user-agents" / "AGENTS.md"
            tool = prefix / "bin" / "user-agents-seed"
            self.assertEqual(text.read_bytes(), TEXT.read_bytes())
            self.assertEqual(tool.read_bytes(), SEED.read_bytes())
            self.assertEqual(text.stat().st_mode & 0o777, 0o644)
            self.assertEqual(tool.stat().st_mode & 0o777, 0o755)
            self.assertEqual(sorted(p.name for p in tool.parent.iterdir()), ["user-agents-seed"], "no .new left")
            home = Path(tmp) / "home"
            home.mkdir()
            env = {"PATH": os.environ["PATH"], "HOME": str(home), "USER_AGENTS_SRC": str(text)}
            r = subprocess.run([str(tool)], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((home / ".claude" / "CLAUDE.md").read_bytes(), TEXT.read_bytes())

    def test_the_default_text_path_is_the_one_the_install_writes(self):
        self.assertIn("SRC=${USER_AGENTS_SRC:-/usr/local/share/user-agents/AGENTS.md}", SEED.read_text())
        self.assertIn("mv -f /usr/local/share/user-agents/AGENTS.md.new /usr/local/share/user-agents/AGENTS.md", TF)

    def test_root_never_writes_into_a_home_itself(self):
        code = re.sub(r"(?m)^\s*#.*$", "", SEED.read_text())
        root_branch = code.split('if [ "$(id -u)" = 0 ]; then', 1)[1].split("\nfi\n", 1)[0]
        self.assertIn('runuser -u "$u" -- env HOME="$home" USER_AGENTS_SRC="$SRC" "$0"', root_branch)
        self.assertRegex(root_branch, r"exit \"\$rc\"\s*$")
        for word in ("mkdir", "ln ", "cat ", "mktemp", ">"):
            self.assertNotIn(word, root_branch.replace(">/dev/null 2>&1", "").replace(">&2", ""), word)

    def test_it_never_forces_or_replaces(self):
        code = re.sub(r"(?m)^\s*#.*$", "", SEED.read_text())
        self.assertNotRegex(code, r"\bln\s+-\w*f")
        self.assertNotRegex(code, r"\b(mv|cp|install|tee|sed|chown)\b")
        self.assertEqual(re.findall(r"(?m)^\s*rm\b.*$", code), [])   # only the temp file, in the EXIT trap
        self.assertIn("trap 'rm -f \"$tmp\"' EXIT", code)

    def test_every_setup_script_that_provisions_homes_seeds_them(self):
        seed_ubuntu = TF.split("user_agents_seed_ubuntu = <<-SEED\n", 1)[1].split("\nSEED\n", 1)[0]
        self.assertEqual(seed_ubuntu.splitlines(), [
            "${local.user_agents_seed_install}",
            '/usr/local/bin/user-agents-seed ubuntu || echo "WARNING: user-agents-seed failed for ubuntu"',
        ])
        dev = (ROOT / "dev-user-data.tf").read_text()
        shell_setup = dev.split("shell_setup = <<-SHELLSETUP\n", 1)[1].split("\nSHELLSETUP\n", 1)[0]
        self.assertEqual(shell_setup.count("${local.user_agents_seed_ubuntu}"), 1)
        for box in ("arm_user_data", "x86_user_data"):   # both metal boxes run shell_setup
            body = dev.split(f"{box} = <<-SCRIPT\n", 1)[1].split("\nSCRIPT\n", 1)[0]
            self.assertEqual(body.count("${local.shell_setup}"), 1, box)
        admin = (ROOT / "jumpbox2-user-data.tf").read_text()
        self.assertEqual(admin.count("${local.user_agents_seed_ubuntu}"), 1)
        nextjs = (ROOT / "nextjs-user-data.tf").read_text()
        self.assertEqual(nextjs.count("${local.user_agents_seed_install}"), 1)
        self.assertIn('\n/usr/local/bin/user-agents-seed ${join(" ", local.nextjs_users)} ubuntu || echo "WARNING: ', nextjs)
        # Seeded after the accounts exist, or a fresh box would skip every one of them.
        self.assertLess(nextjs.index("useradd -m -s /bin/bash"), nextjs.index("${local.user_agents_seed_install}"))

    def test_publishing_it_replaces_no_instance(self):
        # It reaches a box inside a setup script published to S3. That is only non-disruptive
        # because every instance that fetches one ignores user_data changes.
        for name, resource in (("firecracker-dev.tf", "firecracker_dev"), ("x86-dev.tf", "x86_dev"),
                               ("nextjs-dev.tf", "nextjs_dev"), ("jumpbox.tf", "jumpbox"), ("jumpbox2.tf", "jumpbox_2")):
            tf = (ROOT / name).read_text()
            block = tf.split(f'resource "aws_instance" "{resource}"', 1)[1]
            ignore = block.split("ignore_changes = [\n", 1)[1].split("\n    ]\n", 1)[0]
            ignored = re.findall(r"(?m)^\s*([a-z_0-9]+),", ignore)
            self.assertIn("user_data", ignored, name)
            self.assertIn("user_data_base64", ignored, name)
            self.assertNotRegex(re.sub(r"(?m)#.*$", "", tf), r"user_data_replace_on_change\s*=\s*true", name)
        for name in ("user-agents.tf", "dev-user-data.tf", "nextjs-user-data.tf", "jumpbox2-user-data.tf"):
            code = re.sub(r"(?m)^\s*#.*$", "", (ROOT / name).read_text())
            for kind in ("aws_launch_template", "aws_autoscaling_group", "aws_ssm_association", "terraform_data"):
                self.assertNotIn(f'resource "{kind}"', code, f"{name}: nothing here runs on or relaunches a box")


if __name__ == "__main__":
    unittest.main(verbosity=2)

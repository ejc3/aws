#!/usr/bin/env python3
"""agents-md.tf: the owner's instructions from ejc3/agents-md reach exactly the two dev accounts, one way, verified."""
import hashlib
import pathlib
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TF = (ROOT / "agents-md.tf").read_text()
CODEX_COPY = re.search(r"^  agents_md_codex_copy = <<-EOT\n(.*?)^  EOT\n", TF, re.M | re.S).group(1)
CODEX_COPY = "\n".join(l[4:] if l.startswith("    ") else l for l in CODEX_COPY.splitlines())
INSTRUCTIONS = "# Instructions (example)\n\nRead this first.\n\n@~/.claude/local.md\n\n## Later section\n\n  indented \\\\n backslash line\nlast line without newline"
LOCAL = "# Machine-local (example)\n\nUse the example tool at 127.0.0.1:0.\n"


def block(kind, name):
    m = re.search(r'^(?:resource|data|ephemeral) "%s" "%s" \{\n(.*?)^\}' % (kind, name), TF, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class AgentsMdTests(unittest.TestCase):
    def test_exactly_the_two_dev_accounts_and_never_a_jumpbox(self):
        targets = re.search(r"agents_md_targets = \{(.*?)\n  \}", TF, re.S).group(1)
        self.assertEqual(re.findall(r"\"([^\"]+)\"\s*=\s*\{", targets), ["fcvm-metal-arm/ubuntu", "nextjs-dev/ejc3"])
        self.assertNotIn("jumpbox", targets)
        self.assertIn("aws_instance.firecracker_dev[*].id", targets)
        self.assertIn("[local.nextjs_live_instance.id]", targets)

    def test_read_from_main_through_the_app_with_the_key_never_in_state(self):
        data = block("github_repository_file", "agents_md")
        self.assertIn('repository = "agents-md"', data)
        self.assertIn('branch     = "main"', data)
        self.assertIn("provider   = github.agents_md", data)
        self.assertIn('ephemeral "aws_secretsmanager_secret_version" "agents_md_github_app"', TF)

    def test_nextjs_dev_reads_only_its_own_prefix(self):
        pol = block("aws_iam_role_policy", "nextjs_dev_agents_md")
        self.assertEqual(re.findall(r'Action\s*=\s*"([^"]+)"', pol), ["s3:GetObject"])
        self.assertIn('/agents-md/nextjs-dev/*"', pol)
        self.assertNotIn("s3:Put", TF)

    def test_install_is_verified_written_by_the_account_and_stays_in_its_home(self):
        cmds = block("aws_ssm_association", "agents_md")
        self.assertIn("sha256(data.github_repository_file.agents_md[each.key].content)", cmds)
        self.assertIn('sha256sum -c --quiet', cmds)
        self.assertIn('runuser -u "$user" --', cmds)
        self.assertIn('case "$target" in "$home"/*) ;;', cmds)
        self.assertIn('mv "$1.agents-md.tmp" "$1"', cmds)
        self.assertIn('schedule_expression = "rate(1 day)"', cmds)
        self.assertNotRegex(cmds, r"systemctl|reboot|restart")



class Install(unittest.TestCase):
    """Runs the rendered association command against a temporary home: the install, then Codex's copy."""

    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="agents-md-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.temp)])
        self.home = self.temp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.obj = self.temp / "object"
        self.obj.write_text(INSTRUCTIONS)
        stubs = self.temp / "stubs"
        stubs.mkdir()
        for name, body in {
            "aws": f'[ "$1 $2" = "s3 cp" ] && cp {shlex.quote(str(self.obj))} "$4"',
            "getent": f'echo "example:x:1000:1000::{self.home}:/bin/sh"',
            "runuser": 'shift 3; exec "$@"',
        }.items():
            (stubs / name).write_text("#!/bin/sh\n" + body + "\n")
            (stubs / name).chmod(0o755)
        self.path = f"{stubs}:/usr/bin:/bin"
        commands = block("aws_ssm_association", "agents_md").split("<<-EOT\n", 1)[1].split("\n    EOT", 1)[0]
        commands = "\n".join(l[6:] if l.startswith("      ") else l for l in commands.splitlines())
        self.script = (commands
                       .replace("${each.value.user}", "example")
                       .replace("${sha256(data.github_repository_file.agents_md[each.key].content)}",
                                hashlib.sha256(INSTRUCTIONS.encode()).hexdigest())
                       .replace("${aws_s3_bucket.dev_scripts.id}", "example-bucket")
                       .replace("${aws_s3_object.agents_md[each.key].key}", "agents-md/example/AGENTS.md")
                       .replace("${var.aws_region}", "us-west-1")
                       .replace("${each.key}", "example-box/example")
                       .replace("${local.agents_md_codex_copy}", CODEX_COPY))
        self.assertNotIn("${", self.script)
        self.codex = self.home / ".codex" / "AGENTS.override.md"

    def run_(self):
        return subprocess.run(["sh", "-c", self.script], capture_output=True, text=True, timeout=20,
                              env={"PATH": self.path, "HOME": str(self.home)})

    def test_codex_gets_the_instructions_with_local_md_inlined_where_claude_imports_it(self):
        (self.home / ".claude" / "local.md").write_text(LOCAL)
        r = self.run_()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.home / ".claude" / "CLAUDE.md").read_text(), INSTRUCTIONS, "Claude keeps the import")
        self.assertEqual(self.codex.read_text(), INSTRUCTIONS.replace("@~/.claude/local.md\n", LOCAL) + "\n")
        self.assertEqual(self.codex.stat().st_mode & 0o777, 0o600, "it can hold local.md's private text")
        self.assertNotIn("example tool", r.stdout + r.stderr, "the text is never echoed")

    def test_without_local_md_the_import_line_is_dropped(self):
        r = self.run_()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.codex.read_text(), INSTRUCTIONS.replace("@~/.claude/local.md\n", "") + "\n")

    def test_a_local_md_without_a_final_newline_does_not_join_the_next_line(self):
        (self.home / ".claude" / "local.md").write_text(LOCAL.rstrip("\n"))
        self.assertEqual(self.run_().returncode, 0)
        self.assertIn("127.0.0.1:0.\n\n## Later section", self.codex.read_text())

    def test_the_shared_codex_md_layout_gets_the_same_copy(self):
        (self.home / ".codex").mkdir()
        (self.home / ".codex" / "AGENTS.md").write_text("old\n")
        (self.home / ".claude" / "CLAUDE.md").symlink_to("../.codex/AGENTS.md")
        self.assertEqual(self.run_().returncode, 0)
        self.assertEqual((self.home / ".codex" / "AGENTS.md").read_text(), INSTRUCTIONS)
        self.assertEqual(self.codex.read_text(), INSTRUCTIONS.replace("@~/.claude/local.md\n", "") + "\n")

    def test_a_codex_copy_linked_out_of_the_home_is_refused(self):
        outside = self.temp / "outside.md"
        (self.home / ".codex").mkdir()
        self.codex.symlink_to(outside)
        r = self.run_()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("outside", r.stderr)
        self.assertFalse(outside.exists())

    def test_concurrent_copies_leave_one_complete_file_and_no_temporaries(self):
        # The agents-md and agent-local-md associations run on their own schedules and can overlap; each ends with this
        # step. Run only the step, many times at once, over a long file so the writes overlap.
        big = "".join(f"line {i} of the instructions\n" for i in range(3000)) + "@~/.claude/local.md\n## End\n"
        (self.home / ".claude" / "CLAUDE.md").write_text(big)
        (self.home / ".claude" / "local.md").write_text(LOCAL)
        step = "set -eu\nuser=example\nhome=" + shlex.quote(str(self.home)) + "\n" + CODEX_COPY
        env = {"PATH": self.path, "HOME": str(self.home)}
        procs = [subprocess.Popen(["sh", "-c", step], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                 for _ in range(12)]
        results = [(p.wait(timeout=60), p.stderr.read().decode()) for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        self.assertEqual([r for r, _ in results], [0] * 12, [e for _, e in results if e])
        self.assertEqual(self.codex.read_text(), big.replace("@~/.claude/local.md\n", LOCAL))
        self.assertEqual(sorted(p.name for p in (self.home / ".codex").iterdir()), ["AGENTS.override.md"])

    def test_a_changed_object_is_refused_and_nothing_written(self):
        self.obj.write_text(INSTRUCTIONS + "tampered\n")
        self.assertNotEqual(self.run_().returncode, 0)
        self.assertFalse((self.home / ".claude" / "CLAUDE.md").exists())
        self.assertFalse(self.codex.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)

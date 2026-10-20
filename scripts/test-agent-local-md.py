#!/usr/bin/env python3
"""agent-local-md.tf: each distributed account's ~/.claude/local.md comes from an administration-only secret (never from
git), through its own box's object, verified and written by the account. Runs the rendered association command against a
temporary home with a made-up file."""
import hashlib
import os
import pathlib
import re
import shlex
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TF = (ROOT / "agent-local-md.tf").read_text()
CODEX_COPY = re.search(r"^  agents_md_codex_copy = <<-EOT\n(.*?)^  EOT\n", (ROOT / "agents-md.tf").read_text(), re.M | re.S).group(1)
CODEX_COPY = "\n".join(l[4:] if l.startswith("    ") else l for l in CODEX_COPY.splitlines())
EXAMPLE = "# Machine-local instructions (example-box, example-account)\n\nUse the example tool at 127.0.0.1:0.\n"


def block(kind, name):
    m = re.search(r'^(?:resource|data|output) "%s" "%s" \{\n(.*?)^\}' % (kind, name), TF, re.M | re.S)
    assert m, (kind, name)
    return m.group(1)


class Wiring(unittest.TestCase):
    def test_one_secret_per_distributed_account_named_after_it(self):
        sec = block("aws_secretsmanager_secret", "agent_local_md")
        self.assertIn("for_each                = local.agents_md_targets", sec)
        self.assertIn('name                    = "agent-local-md/${each.key}"', sec)
        agents = (ROOT / "agents-md.tf").read_text()
        targets = re.search(r"agents_md_targets = \{(.*?)\n  \}", agents, re.S).group(1)
        self.assertEqual(re.findall(r"\"([^\"]+)\"\s*=\s*\{", targets), ["fcvm-metal-arm/ubuntu", "nextjs-dev/ejc3"])

    def test_the_secret_is_administration_only(self):
        pol = block("aws_secretsmanager_secret_policy", "agent_local_md")
        self.assertIn('Effect    = "Deny"', pol)
        self.assertIn('"aws:PrincipalArn" = local.games_mp_admin_principals', pol)
        self.assertNotRegex(pol, r"dev_server|nextjs_dev")

    def test_nothing_is_read_until_its_gate_is_set_and_the_gates_start_closed(self):
        ready = re.search(r"agent_local_md_ready = \{(.*?)\n  \}", TF, re.S).group(1)
        self.assertEqual(re.findall(r'"([^"]+)"\s*=\s*(true|false)', ready),
                         [("fcvm-metal-arm/ubuntu", "false"), ("nextjs-dev/ejc3", "false")])
        self.assertIn("agent_local_md_live = { for k, v in local.agents_md_targets : k => v if local.agent_local_md_ready[k] }", TF)
        for kind, name in (("aws_secretsmanager_secret_version", "agent_local_md"), ("aws_s3_object", "agent_local_md")):
            self.assertIn("for_each", block(kind, name))
            self.assertIn("local.agent_local_md_live", block(kind, name))
        self.assertIn("local.agent_local_md_live", block("aws_ssm_association", "agent_local_md"))

    def test_each_object_is_readable_only_by_administration_and_its_box(self):
        readers = re.search(r"agent_local_md_readers = \{(.*?)\n  \}", TF, re.S).group(1)
        self.assertIn('"fcvm-metal-arm/ubuntu" = { role = aws_iam_role.dev_server.arn, shared = true, instances = aws_instance.firecracker_dev[*].arn }', readers)
        self.assertIn('"nextjs-dev/ejc3"       = { role = aws_iam_role.nextjs_dev.arn, shared = false, instances = [] }', readers)
        pol = block("aws_s3_bucket_policy", "dev_scripts")
        self.assertEqual(pol.count('Effect    = "Deny"'), 2)
        self.assertEqual(pol.count('Resource  = "${aws_s3_bucket.dev_scripts.arn}/agents-md/${k}/local.md"'), 2,
                         "the denials name the local.md objects only")
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, [r.role])', pol)
        # A shared role keeps its instance denial even with no instance (the box disabled): it fails closed.
        self.assertIn('"ec2:SourceInstanceARN" = length(r.instances) > 0 ? r.instances : [local.agent_local_md_no_instance]', pol)
        self.assertIn("} if r.shared]", pol)
        self.assertNotIn("if length(r.instances) > 0]", pol)
        self.assertRegex(TF, r'agent_local_md_no_instance = "arn:aws:ec2:\$\{var.aws_region\}:\$\{data.aws_caller_identity.current.account_id\}:instance/none"')
        self.assertNotIn("Allow", pol)
        self.assertIn('key          = "agents-md/${each.key}/local.md"', block("aws_s3_object", "agent_local_md"))

    def test_the_text_is_never_in_git(self):
        tracked = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout.split()
        for path in tracked:
            p = ROOT / path
            if p.is_file() and p.suffix in (".md", ".tf", ".tftpl", ".txt"):
                self.assertNotIn("# Machine-local instructions (", p.read_text(errors="replace"), path)


class Install(unittest.TestCase):
    def setUp(self):
        self.temp = pathlib.Path(tempfile.mkdtemp(prefix="agent-local-md-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.temp)])
        self.home = self.temp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self.obj = self.temp / "object"
        self.obj.write_text(EXAMPLE)
        stubs = self.temp / "stubs"
        stubs.mkdir()
        for name, body in {
            "aws": f'[ "$1 $2" = "s3 cp" ] && cp {shlex.quote(str(self.obj))} "$4"',
            "getent": f'echo "example:x:1000:1000::{self.home}:/bin/sh"',
            "runuser": 'shift 3; exec "$@"',
            "chown": f'echo "chown $*" >> {shlex.quote(str(self.temp / "chown.log"))}',
        }.items():
            (stubs / name).write_text("#!/bin/sh\n" + body + "\n")
            (stubs / name).chmod(0o755)
        self.path = f"{stubs}:/usr/bin:/bin"
        commands = block("aws_ssm_association", "agent_local_md").split("<<-EOT\n", 1)[1].split("\n    EOT", 1)[0]
        commands = "\n".join(l[6:] if l.startswith("      ") else l for l in commands.splitlines())
        self.script = (commands
                       .replace("${each.value.user}", "example")
                       .replace("${nonsensitive(sha256(data.aws_secretsmanager_secret_version.agent_local_md[each.key].secret_string))}",
                                hashlib.sha256(EXAMPLE.encode()).hexdigest())
                       .replace("${aws_s3_bucket.dev_scripts.id}", "example-bucket")
                       .replace("${aws_s3_object.agent_local_md[each.key].key}", "agents-md/example/local.md")
                       .replace("${var.aws_region}", "us-west-1")
                       .replace("${each.key}", "example-box/example")
                       .replace("${local.agents_md_codex_copy}", CODEX_COPY))
        self.assertNotIn("${", self.script)

    def run_(self):
        return subprocess.run(["sh", "-c", self.script], capture_output=True, text=True, timeout=20,
                              env={"PATH": self.path, "HOME": str(self.home)})

    def test_installs_the_verified_file(self):
        r = self.run_()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual((self.home / ".claude" / "local.md").read_text(), EXAMPLE)
        self.assertEqual((self.home / ".claude" / "local.md").stat().st_mode & 0o777, 0o600, "private to the account")
        self.assertRegex((self.temp / "chown.log").read_text(), r"^chown example /", "the account owns the temporary copy")
        self.assertNotIn("Use the example tool", r.stdout + r.stderr, "the text is never echoed")

    def test_codex_gets_its_copy_with_the_file_inlined(self):
        (self.home / ".claude" / "CLAUDE.md").write_text("# Instructions\n\n@~/.claude/local.md\n\n## After\n")
        r = self.run_()
        self.assertEqual(r.returncode, 0, r.stderr)
        copy = self.home / ".codex" / "AGENTS.override.md"
        self.assertEqual(copy.read_text(), "# Instructions\n\n" + EXAMPLE + "\n## After\n")
        self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("Use the example tool", r.stdout + r.stderr, "the text is never echoed")

    def test_no_claude_md_means_no_codex_copy(self):
        r = self.run_()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse((self.home / ".codex" / "AGENTS.override.md").exists())

    def test_a_changed_object_is_refused_and_nothing_written(self):
        self.obj.write_text(EXAMPLE + "tampered\n")
        r = self.run_()
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse((self.home / ".claude" / "local.md").exists())

    def test_a_link_out_of_the_home_is_refused(self):
        outside = self.temp / "outside.md"
        (self.home / ".claude" / "local.md").symlink_to(outside)
        r = self.run_()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("outside", r.stderr)
        self.assertFalse(outside.exists())

    def test_it_restarts_nothing(self):
        self.assertNotRegex(self.script, r"systemctl|reboot|restart|kill")


class SkillsMirror(unittest.TestCase):
    """The documented step (AGENTS.md) that mirrors ~/.claude/skills into Codex's ~/.agents/skills."""

    def test_the_documented_command_mirrors_the_skills_and_skips_synced(self):
        agents = (ROOT / "AGENTS.md").read_text()
        cmd = re.search(r"`(mkdir -p ~/\.agents/skills && find ~/\.agents/skills .*? && for s in ~/\.claude/skills/\*/;.*?done)`", agents).group(1)
        home = pathlib.Path(tempfile.mkdtemp(prefix="skills-mirror-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(home)])
        skills = home / ".claude" / "skills"
        (skills / "monid").mkdir(parents=True)  # a skill kept in place
        (skills / "synced").mkdir()
        (skills / ".trash").mkdir()
        (home / "claude-skills" / "drive-mac").mkdir(parents=True)
        (skills / "drive-mac").symlink_to(home / "claude-skills" / "drive-mac")  # a link into the checkout
        for shell in ("bash", "zsh"):
            if subprocess.run(["sh", "-c", f"command -v {shell}"], capture_output=True).returncode:
                continue
            with self.subTest(shell=shell):
                subprocess.run(["rm", "-rf", str(home / ".agents")], check=True)
                subprocess.run([shell, "-c", cmd], check=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
                mirror = home / ".agents" / "skills"
                self.assertEqual(sorted(p.name for p in mirror.iterdir()), ["drive-mac", "monid"])
                self.assertEqual(os.readlink(mirror / "drive-mac"), str(home / "claude-skills" / "drive-mac"))
                self.assertEqual(os.readlink(mirror / "monid"), str(skills / "monid"))
                subprocess.run([shell, "-c", cmd], check=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
                self.assertEqual(sorted(p.name for p in mirror.iterdir()), ["drive-mac", "monid"], "a rerun adds nothing")
                (skills / "monid").rename(skills / ".trash" / "monid")  # a skill removed from Claude Code
                (mirror / "own").mkdir()  # something that is not a link is never touched
                subprocess.run([shell, "-c", cmd], check=True, env={"HOME": str(home), "PATH": "/usr/bin:/bin"})
                self.assertEqual(sorted(p.name for p in mirror.iterdir()), ["drive-mac", "own"], "and it leaves Codex too")
                (skills / ".trash" / "monid").rename(skills / "monid")


if __name__ == "__main__":
    unittest.main(verbosity=2)

#!/usr/bin/env python3
"""agents-md.tf: the owner's instructions from ejc3/agents-md reach exactly the two dev accounts, one way, verified."""
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TF = (ROOT / "agents-md.tf").read_text()


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
        self.assertIn("aws_instance.nextjs_dev[*].id", targets)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

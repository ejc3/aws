#!/usr/bin/env python3
"""claude-master-server.tf: the shared claude-master server is private, pinned and idle until its
logins exist. Offline: reads the Terraform source and the two scripts."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "claude-master-server.tf").read_text()
ENROLL = (ROOT / "scripts" / "claude-master-enroll.sh").read_text()
LOGIN = (ROOT / "scripts" / "claude-master-login.sh").read_text()
TUNNEL = (ROOT / "scripts" / "claude-master-tunnel.sh").read_text()


def code(text):
    """The script without its comment lines: assertions are about what runs, not what is explained."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n.*?^\}' % (kind, name), TF, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


class NetworkTests(unittest.TestCase):
    def test_nothing_inbound_is_open_to_the_internet(self):
        sg = block("aws_security_group", "claude_master_server")
        ingress = re.findall(r"ingress \{.*?\n  \}", sg, re.S)
        self.assertEqual(len(ingress), 2, "only the proxy port and SSH")
        for rule in ingress:
            self.assertNotIn("0.0.0.0/0", rule)
            self.assertNotIn("::/0", rule)
        proxy, ssh = ingress
        self.assertIn("local.claude_master_server_port", proxy)
        self.assertIn("[for s in local.dev_fleet_subnets : s.cidr_block]", proxy)
        self.assertIn("from_port   = 22", ssh)
        self.assertIn("local.claude_master_admin_cidrs", ssh)

    def test_ssh_comes_from_the_two_admin_boxes_by_address(self):
        self.assertRegex(TF, r'aws_instance\.jumpbox\[0\]\.private_ip\}/32')
        self.assertRegex(TF, r'aws_instance\.jumpbox_2\[0\]\.private_ip\}/32')

    def test_the_address_is_fixed_and_public_ip_is_for_outbound_only(self):
        inst = block("aws_instance", "claude_master_server")
        self.assertIn("private_ip                  = local.claude_master_server_ip", inst)
        self.assertIn("associate_public_ip_address = true # outbound only", inst)
        self.assertIn('http_tokens = "required"', inst)
        self.assertIn("prevent_destroy = true", inst)
        self.assertIn("delete_on_termination = false", inst)
        self.assertRegex(TF, r'claude_master_server_ip\s+=\s+"10\.0\.1\.50"')

    def test_it_is_big_enough_to_survive_its_first_boot(self):
        # t4g.nano was OOM-killed during apt on first boot; swap exists before any apt run.
        inst = block("aws_instance", "claude_master_server")
        self.assertIn('instance_type               = "t4g.micro"', inst)
        bootstrap = inst[inst.index("user_data"):]
        self.assertLess(bootstrap.index("fallocate -l 1G"), bootstrap.index("apt-get update"))

    def test_the_server_listens_on_its_private_address_never_everywhere(self):
        self.assertIn("--listen ${local.claude_master_server_ip}:${local.claude_master_server_port}", TF)
        self.assertNotRegex(TF, r"--listen\s+(0\.0\.0\.0|\[::\]|:)")


class SupplyChainTests(unittest.TestCase):
    def test_the_binary_is_pinned_by_tag_and_sha256_and_checked_before_install(self):
        self.assertRegex(TF, r'claude_master_tag\s+=\s+"claude-master-[0-9a-f]{7}"')
        self.assertRegex(TF, r'claude_master_sha256_aarch64\s+=\s+"[0-9a-f]{64}"')
        self.assertIn("github.com/ejc3/CLIProxyAPI/releases/download/$TAG/claude-master-linux-arm64", TF)
        self.assertIn('sha256sum -c -', TF)
        self.assertLess(TF.index("sha256sum -c -"), TF.index("mv -f /usr/local/bin/claude-master.new"))

    def test_a_running_server_is_never_restarted_by_the_bootstrap(self):
        script = TF[TF.index("claude_master_server_user_data"):TF.index('resource "aws_s3_object"')]
        self.assertNotRegex(script, r"systemctl (restart|stop)")
        self.assertIn("keeps its old binary until it is restarted", script)

    def test_no_secret_is_in_the_source(self):
        self.assertNotRegex(TF, r"sk-ant-[A-Za-z0-9_-]{6,}")
        self.assertNotIn("BEGIN PRIVATE KEY", TF)
        self.assertNotIn("BEGIN EC PRIVATE KEY", TF)


class ServiceTests(unittest.TestCase):
    def test_the_unit_waits_for_every_login_and_is_sandboxed(self):
        profiles = re.search(r"claude_master_profiles\s*=\s*\[(.*?)\]", TF).group(1)
        names = re.findall(r'"([a-z0-9-]+)"', profiles)
        self.assertEqual(len(names), 3)
        self.assertIn("ConditionPathExists=/var/lib/claude-master/.local/share/claude-master/profiles/${p}/current", TF)
        for flag in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ReadWritePaths=/var/lib/claude-master", "PrivateTmp=yes"):
            self.assertIn(flag, TF)
        self.assertIn("User=claude-master", TF)

    def test_the_backup_key_is_handed_over_in_the_environment_not_argv_or_disk(self):
        self.assertIn("export CLAUDE_MASTER_BACKUP_API_KEY=", TF)
        self.assertNotRegex(TF, r"--backup-api-key")
        self.assertNotRegex(TF, r">\s*/(tmp|var|etc)[^\n]*key")

    def test_the_rendered_script_is_valid_bash(self):
        m = re.search(r"claude_master_server_user_data = <<SCRIPT\n(.*?)\nSCRIPT", TF, re.S)
        self.assertTrue(m)
        # Render the three interpolations that matter, then let bash parse it.
        text = m.group(1)
        text = re.sub(r"\$\{local\.claude_master_tag\}", "claude-master-0000000", text)
        text = re.sub(r"\$\{local\.claude_master_sha256_aarch64\}", "0" * 64, text)
        text = re.sub(r"\$\{local\.claude_master_server_ip\}", "10.0.1.50", text)
        text = re.sub(r"\$\{local\.claude_master_server_port\}", "8443", text)
        text = re.sub(r"\$\{var\.aws_region\}", "us-west-1", text)
        text = re.sub(r"\$\{join\(.*\)\}", "x", text)
        text = re.sub(r"\$\{substr\(.*?\)\}", "0" * 16, text)
        text = re.sub(r"\$\{local\.claude_master_profiles\[0\]\}", "claude-connor", text)
        text = text.replace("$${", "${")
        out = subprocess.run(["bash", "-n"], input=text, text=True, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)


class IamTests(unittest.TestCase):
    def test_the_role_reads_only_its_own_bootstrap_script(self):
        policy = block("aws_iam_role_policy", "claude_master_server_bootstrap")
        self.assertIn('"${aws_s3_bucket.dev_scripts.arn}/user-data/claude-master-server.sh"', policy)
        self.assertNotIn('"*"', policy)

    def test_it_is_not_given_the_kids_box_or_dev_server_powers(self):
        role = block("aws_iam_role", "claude_master_server")
        self.assertIn("ec2.amazonaws.com", role)
        self.assertNotIn("AdministratorAccess", TF)


class ScriptTests(unittest.TestCase):
    def test_enrollment_signs_over_ssm_and_never_moves_a_key(self):
        self.assertIn("aws ssm send-command", ENROLL)
        self.assertIn("claude-master-sign", ENROLL)
        # The key is made on the enrolled machine; only the request goes up.
        self.assertIn("client-init", ENROLL)
        self.assertNotRegex(ENROLL, r"client\.key")
        self.assertIn("--days must be 1-90", ENROLL)
        self.assertRegex(ENROLL, r"\^\[a-z0-9\]\[a-z0-9-\]\{0,62\}\$")

    def test_enrollment_swaps_only_after_the_new_certificate_is_in_place(self):
        self.assertLess(ENROLL.index('client.pem'), ENROLL.index("mv '$NEW' '$DIR'"))
        self.assertIn("rm -rf '$DIR.old'", ENROLL)

    def test_the_login_script_drives_the_server_and_never_restarts_a_running_one(self):
        self.assertIn("--server", LOGIN)
        self.assertIn("sudo claude-master-login", LOGIN)
        self.assertIn("!= active", LOGIN)
        self.assertNotRegex(LOGIN, r"systemctl restart")

    def test_enrollment_runs_on_a_mac(self):
        # BSD base64 (macOS) has no -w, so the request is encoded with plain base64 and tr.
        self.assertNotRegex(code(ENROLL), r"base64 -w")
        self.assertIn("tr -d '\\n'", code(ENROLL))

    def test_the_tunnel_uses_the_remote_host_session_to_the_private_address(self):
        # The plain forwarding session reaches the instance's loopback, where nothing listens.
        self.assertIn("AWS-StartPortForwardingSessionToRemoteHost", code(TUNNEL))
        self.assertNotRegex(code(TUNNEL), r"AWS-StartPortForwardingSession[^T]")
        self.assertIn("host=$REMOTE_HOST", TUNNEL)
        self.assertRegex(TUNNEL, r'CLAUDE_MASTER_SERVER_HOST:-10\.0\.1\.50')

    def test_the_scripts_parse(self):
        for path in ("claude-master-enroll.sh", "claude-master-login.sh", "claude-master-tunnel.sh"):
            r = subprocess.run(["bash", "-n", str(ROOT / "scripts" / path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

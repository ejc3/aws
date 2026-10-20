#!/usr/bin/env python3
"""claw-master-server.tf: the shared claude-master server is private, pinned and idle until its
logins exist. Offline: reads the Terraform source and the two scripts."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "claw-master-server.tf").read_text()
TUN = (ROOT / "claw-master-tunnel.tf").read_text()
BUNDLE = (ROOT / "scripts" / "claw-master-mac-bundle.sh").read_text()
MACSETUP = (ROOT / "scripts" / "claw-master-mac-setup.sh").read_text()
WINSETUP = (ROOT / "scripts" / "claw-master-windows-setup.sh").read_text()
ENROLL = (ROOT / "scripts" / "claw-master-enroll.sh").read_text()
CONVERGE = (ROOT / "scripts" / "ssm-claw-master-server.sh").read_text()
LOGIN = (ROOT / "scripts" / "claw-master-login.sh").read_text()
TUNNEL = (ROOT / "scripts" / "claw-master-tunnel.sh").read_text()
DASH = (ROOT / "claw-master-dashboard.tf").read_text()


def code(text):
    """The script without its comment lines: assertions are about what runs, not what is explained."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def tblock(kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n.*?^\}' % (kind, name), TUN, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


def block(kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n.*?^\}' % (kind, name), TF, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


class NetworkTests(unittest.TestCase):
    def test_nothing_inbound_is_open_to_the_internet(self):
        sg = block("aws_security_group", "claw_master_server")
        ingress = re.findall(r"ingress \{.*?\n  \}", sg, re.S)
        self.assertEqual(len(ingress), 2, "only the proxy port and SSH")
        for rule in ingress:
            self.assertNotIn("0.0.0.0/0", rule)
            self.assertNotIn("::/0", rule)
        proxy, ssh = ingress
        self.assertIn("local.claw_master_server_port", proxy)
        self.assertIn("[for s in local.dev_fleet_subnets : s.cidr_block]", proxy)
        self.assertIn("from_port   = 22", ssh)
        self.assertIn("local.claw_master_admin_cidrs", ssh)

    def test_ssh_comes_from_the_two_admin_boxes_by_address(self):
        self.assertRegex(TF, r'aws_instance\.jumpbox\[0\]\.private_ip\}/32')
        self.assertRegex(TF, r'aws_instance\.jumpbox_2\[0\]\.private_ip\}/32')

    def test_the_address_is_fixed_and_public_ip_is_for_outbound_only(self):
        inst = block("aws_instance", "claw_master_server")
        self.assertIn("private_ip                  = local.claw_master_server_ip", inst)
        self.assertIn("associate_public_ip_address = true # outbound only", inst)
        self.assertIn('http_tokens = "required"', inst)
        self.assertIn("prevent_destroy = true", inst)
        self.assertIn("delete_on_termination = false", inst)
        self.assertRegex(TF, r'claw_master_server_ip\s+=\s+"10\.0\.1\.50"')

    def test_it_is_big_enough_to_survive_its_first_boot(self):
        # t4g.nano was OOM-killed during apt on first boot; swap exists before any apt run.
        inst = block("aws_instance", "claw_master_server")
        self.assertIn('instance_type               = "t4g.micro"', inst)
        bootstrap = inst[inst.index("user_data"):]
        self.assertLess(bootstrap.index("fallocate -l 1G"), bootstrap.index("apt-get update"))

    def test_the_server_listens_on_its_private_address_never_everywhere(self):
        # Each server listens on the private address (its own port); Envoy takes the client port on the same address.
        self.assertIn("--listen ${local.claw_master_server_ip}:$port", TF)
        self.assertIn('cert = "${local.claw_master_server_ip}:${local.claw_master_server_port}"', TF)
        self.assertNotRegex(TF, r"--listen\s+(0\.0\.0\.0|\[::\]|:)")
        self.assertNotIn("0.0.0.0", envoy_config())


class SupplyChainTests(unittest.TestCase):
    def test_the_binary_is_pinned_by_tag_and_sha256_and_checked_before_install(self):
        self.assertRegex(TF, r'claw_master_tag\s+=\s+"claw-master-[0-9a-f]{7}"')
        self.assertRegex(TF, r'claw_master_sha256_aarch64\s+=\s+"[0-9a-f]{64}"')
        self.assertIn("github.com/ejc3/CLIProxyAPI/releases/download/$TAG/claw-master-linux-arm64", TF)
        self.assertIn('sha256sum -c -', TF)
        self.assertLess(TF.index("sha256sum -c -"), TF.index("mv -f /usr/local/bin/claw-master.new"))

    def test_the_old_names_stay_as_links_swapped_in_by_a_rename(self):
        # claude-master (the binary) and every claude-master-* helper keep working after the rename, as links that replace
        # the old files by a rename, so a running server keeps the file it started from.
        script = TF[TF.index("claw_master_server_user_data"):TF.index('resource "aws_s3_object"')]
        self.assertIn("ln -sfn claw-master /usr/local/bin/claude-master.new && mv -Tf /usr/local/bin/claude-master.new /usr/local/bin/claude-master", script)
        self.assertLess(script.index("mv -f /usr/local/bin/claw-master.new /usr/local/bin/claw-master"), script.index("ln -sfn claw-master /usr/local/bin/claude-master.new"))
        loop = re.search(r"for h in ([a-z -]+); do\n(.*?)\ndone", script, re.S)
        self.assertTrue(loop)
        helpers = set(re.findall(r"cat > /usr/local/bin/claw-master-([a-z-]+) <<", script))
        self.assertEqual(set(loop.group(1).split()), helpers, "every helper keeps its old name")
        self.assertIn('mv -Tf "/usr/local/bin/claude-master-$h.new" "/usr/local/bin/claude-master-$h"', loop.group(2))
        self.assertNotIn("claude-master-", "".join(re.findall(r"cat > /usr/local/bin/(\S+) <<", script)))

    def test_a_running_server_is_never_restarted_by_the_bootstrap(self):
        # The bootstrap writes claude-master-rollout (an operator tool, checked in EnvoyTests) but never runs it,
        # never restarts or stops a server or Envoy, and only starts Envoy when it is not running.
        script = TF[TF.index("claw_master_server_user_data"):TF.index('resource "aws_s3_object"')]
        script = script[:script.index("cat > /usr/local/bin/claw-master-rollout")] + script[script.index("chmod 0755 /usr/local/bin/claw-master-rollout"):]
        # claw-master-add-finish rolls out after a new login; the bootstrap installs it and never runs it.
        script = script[:script.index("cat > /usr/local/bin/claw-master-add-finish")] + script[script.index("chmod 0755 /usr/local/bin/claw-master-add-finish"):]
        self.assertNotRegex(script, r"systemctl (restart|stop)")
        self.assertNotRegex(script, r"\n\s*(sudo )?(/usr/local/bin/)?cla(ude|w)-master-rollout")
        self.assertIn("systemctl is-active --quiet envoy.service || systemctl start envoy.service", script)
        self.assertIn("keeps its old binary until it is restarted", script)

    def test_no_secret_is_in_the_source(self):
        self.assertNotRegex(TF, r"sk-ant-[A-Za-z0-9_-]{6,}")
        self.assertNotIn("BEGIN PRIVATE KEY", TF)
        self.assertNotIn("BEGIN EC PRIVATE KEY", TF)


class ServiceTests(unittest.TestCase):
    def test_the_unit_waits_for_any_login_and_is_sandboxed(self):
        # The list is only the preferred order: the unit waits for at least one login, not for each listed one, so a
        # missing or new login never keeps the server from starting.
        profiles = re.search(r"claw_master_profiles\s*=\s*\[(.*?)\]", TF).group(1)
        names = re.findall(r'"([a-z0-9-]+)"', profiles)
        self.assertEqual(len(names), 6)
        self.assertIn("ConditionPathExistsGlob=/var/lib/claude-master/.local/share/claude-master/profiles/*/current", TF)
        self.assertNotIn("ConditionPathExists=/var/lib/claude-master/.local/share/claude-master/profiles/${p}/current", TF)
        for flag in ("NoNewPrivileges=yes", "ProtectSystem=strict", "ReadWritePaths=/var/lib/claude-master", "PrivateTmp=yes"):
            self.assertIn(flag, TF)
        self.assertIn("User=claude-master", TF)

    def test_the_backup_key_is_handed_over_in_the_environment_not_argv_or_disk(self):
        serve = TF[TF.index("cat > /usr/local/bin/claw-master-serve"):TF.index("chmod 0755 /usr/local/bin/claw-master-serve")]
        self.assertIn('export "$keyvar=$key"', serve)
        self.assertNotRegex(TF, r"--backup-api-key")
        self.assertNotRegex(TF, r">\s*/(tmp|var|etc)[^\n]*key")

    def test_the_rendered_script_is_valid_bash(self):
        m = re.search(r"claw_master_server_user_data = <<SCRIPT\n(.*?)\nSCRIPT", TF, re.S)
        self.assertTrue(m)
        # Render the three interpolations that matter, then let bash parse it.
        text = m.group(1)
        text = re.sub(r"\$\{local\.claw_master_tag\}", "claw-master-0000000", text)
        text = re.sub(r"\$\{local\.claw_master_sha256_aarch64\}", "0" * 64, text)
        text = re.sub(r"\$\{local\.claw_master_server_ip\}", "10.0.1.50", text)
        text = re.sub(r"\$\{local\.claw_master_server_port\}", "8443", text)
        text = re.sub(r"\$\{var\.aws_region\}", "us-west-1", text)
        text = re.sub(r"\$\{join\(.*\)\}", "x", text)
        text = re.sub(r"\$\{substr\(.*?\)\}", "0" * 16, text)
        text = re.sub(r"\$\{local\.claw_master_profiles\[0\]\}", "claude-connor", text)
        text = text.replace("$${", "${")
        out = subprocess.run(["bash", "-n"], input=text, text=True, capture_output=True)
        self.assertEqual(out.returncode, 0, out.stderr)


def labels_helper():
    """The account-labels helper as the server gets it, rendered and pointed at `etc` instead of /etc/claude-master."""
    script = TF[TF.index("cat > /usr/local/bin/claw-master-account-labels <<'EOF'\n"):]
    script = script[script.index("\n") + 1:script.index("\nEOF\n")]
    return script.replace("${var.aws_region}", "us-west-1")


class AccountLabelsTests(unittest.TestCase):
    """Names for the incoming accounts come from Secrets Manager, written before each start, never fatal."""

    def run_helper(self, value, existing="# old\n", aws_fails=False):
        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            etc = Path(d) / "etc"
            etc.mkdir()
            (etc / "account-labels").write_text(existing)
            bin_ = Path(d) / "bin"
            bin_.mkdir()
            (bin_ / "aws").write_text("#!/bin/bash\n" + ("exit 255\n" if aws_fails else "cat \"$AWS_VALUE_FILE\"\n"))
            (bin_ / "chown").write_text("#!/bin/bash\nexit 0\n")  # the test does not run as root
            for f in bin_.iterdir():
                f.chmod(0o755)
            (Path(d) / "value").write_text(value)
            script = labels_helper().replace("/etc/claude-master", str(etc))
            env = dict(os.environ, PATH="%s:%s" % (bin_, os.environ["PATH"]), AWS_VALUE_FILE=str(Path(d) / "value"))
            out = subprocess.run(["bash", "-c", script], env=env, text=True, capture_output=True)
            left = sorted(x.name for x in etc.iterdir())
            return out, (etc / "account-labels").read_text(), left

    def test_a_good_value_replaces_the_file(self):
        value = "# who is who\n0a1b2c3d-0000-4000-8000-00000000000a=alice\n\n0a1b2c3d-0000-4000-8000-00000000000b = bob\n"
        out, text, left = self.run_helper(value)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(text, value)  # aws prints the value; the helper ends it with one newline
        self.assertEqual(left, ["account-labels"], "no temporary file is left behind")
        self.assertIn("written with 2 names", out.stderr)
        self.assertNotIn("alice", out.stderr + out.stdout)

    def test_a_line_claw_master_would_refuse_keeps_the_old_file_and_still_starts(self):
        for bad in ("alice\n", "not-hex=alice\n", "0a1b2c3d-0000-4000-8000-00000000000a=\n", "0a1b2c3d alice\n"):
            out, text, left = self.run_helper(bad)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(text, "# old\n", bad)
            self.assertEqual(left, ["account-labels"])
            self.assertIn("not ACCOUNT_UUID=NAME", out.stderr)

    def test_no_value_or_no_access_keeps_the_old_file_and_still_starts(self):
        for kwargs in ({"value": ""}, {"value": "None"}, {"value": "x", "aws_fails": True}):
            out, text, _ = self.run_helper(**kwargs)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(text, "# old\n")
            self.assertIn("keeping", out.stderr)

    def test_it_runs_as_root_before_each_start_and_can_never_stop_it(self):
        unit = TF[TF.index("cat > /etc/systemd/system/claude-master-server@.service"):]
        unit = unit[:unit.index("\nEOF\n")]
        self.assertIn("ExecStartPre=-+/usr/local/bin/claw-master-account-labels", unit)
        self.assertLess(unit.index("ExecStartPre="), unit.index("ExecStart=/usr/local/bin/claw-master-serve"))
        helper = code(labels_helper())
        self.assertNotRegex(helper, r"exit [1-9]")
        self.assertIn("{ set +x; } 2>/dev/null", labels_helper())
        self.assertIn("timeout 20 aws secretsmanager get-secret-value", helper)
        self.assertIn('mv -f "$tmp" "$file"', helper)
        self.assertIn("chown root:claude-master", helper)
        self.assertIn("chmod 0640", helper)

    def test_only_administration_and_the_server_read_the_names(self):
        secret = block("aws_secretsmanager_secret", "claw_master_account_labels")
        self.assertIn('name                    = "claude-master/account-labels"', secret)
        self.assertNotIn("aws_secretsmanager_secret_version", TF[TF.index("claw_master_account_labels"):TF.index('resource "aws_iam_instance_profile"')])
        policy = block("aws_secretsmanager_secret_policy", "claw_master_account_labels")
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn("concat(local.games_mp_admin_principals, [aws_iam_role.claw_master_server[0].arn])", policy)
        grant = block("aws_iam_role_policy", "claw_master_server_account_labels")
        self.assertIn('Action   = "secretsmanager:GetSecretValue"', grant)
        self.assertIn("Resource = aws_secretsmanager_secret.claw_master_account_labels[0].arn", grant)
        self.assertNotIn('"*"', grant)


class ObservabilityTests(unittest.TestCase):
    """Logs and metrics: info level, rotated, redacted at the source, shipped by a pinned agent that
    listens on loopback only, with an IAM grant that can publish to one namespace and write one log group."""

    def test_the_agent_is_pinned_and_checked_before_it_is_installed(self):
        self.assertRegex(TF, r'claw_master_cwagent_version\s+=\s+"[0-9.]+b[0-9a-f]+"')
        self.assertRegex(TF, r'claw_master_cwagent_sha256_arm64\s+=\s+"[0-9a-f]{64}"')
        self.assertIn("amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/arm64/$CWA_VERSION/", TF)
        self.assertNotIn("/latest/", TF)
        check = 'echo "$CWA_SHA  /tmp/cwagent.deb" | sha256sum -c -'
        self.assertIn(check, TF)
        self.assertIn("dpkg -i /tmp/cwagent.deb", TF)
        self.assertLess(TF.index(check), TF.index("dpkg -i /tmp/cwagent.deb"))

    def test_otlp_stays_on_loopback_both_ends(self):
        self.assertIn('"otlp": { "http_endpoint": "127.0.0.1:4318" }', TF)
        self.assertIn("--otlp-endpoint http://127.0.0.1:4318", TF)
        self.assertNotIn("0.0.0.0:4318", TF)
        self.assertNotIn("4317", TF)
        self.assertNotIn("4318", block("aws_security_group", "claw_master_server"))

    def test_ejc3_is_the_last_profile_and_the_login_script_uses_the_same_order(self):
        # Fallback order: the first is preferred when quotas tie, so the owner's own subscription is the last resort.
        tf_order = re.search(r'claw_master_profiles = \[([^\]]*)\]', TF).group(1)
        names = re.findall(r'"([a-z0-9-]+)"', tf_order)
        self.assertEqual(names, ["claude-connor", "claude-colton", "claude-colin", "claude-thao", "claude-colby", "claude-ejc3"])
        login = (ROOT / "scripts" / "claw-master-login.sh").read_text()
        self.assertIn("profiles=(" + " ".join(names) + ")", login)

    def test_the_serve_command_is_one_command_with_every_flag_not_cut_short_by_a_doubled_backslash(self):
        # #226 wrote a line continuation as two backslashes: the shell ends the command there, so --state-dir and the open
        # listener ran as a separate command and the service died with "serve requires --listen ... and --state-dir".
        # It went unseen because the service stays idle until all three logins exist.
        wrapper = TF[TF.index("cat > /usr/local/bin/claw-master-serve"):TF.index("chmod 0755 /usr/local/bin/claw-master-serve")]
        lines = wrapper.splitlines()
        self.assertEqual([n for n, l in enumerate(lines) if l.rstrip().endswith("\\\\")], [], "a doubled backslash ends the command")
        start = next(n for n, l in enumerate(lines) if l.startswith('exec "$bin" serve'))
        logical = []
        for l in lines[start:]:
            logical.append(l.rstrip().rstrip("\\"))
            if not l.rstrip().endswith("\\"):
                break
        command = " ".join(logical)
        for flag in ("--listen ${local.claw_master_server_ip}:$port", "--open-loopback 127.0.0.1:$open",
                     "--state-dir /var/lib/claude-master/state", "--log-file /var/log/claude-master/server-$color.log",
                     "--otlp-endpoint http://127.0.0.1:4318", "--account-labels-file /etc/claude-master/account-labels",
                     "--instance claw-master-$color", "$balancer"):
            self.assertIn(flag, command)

    def test_the_wrapper_falls_back_to_the_old_binary_when_the_new_one_is_not_installed(self):
        # A convergence that could not download claw-master keeps the old file at /usr/local/bin/claude-master; the next
        # start must run that one, with the backup key under the name it reads, not fail on a missing claw-master.
        import os, tempfile
        serve = TF[TF.index("cat > /usr/local/bin/claw-master-serve <<'EOF'\n"):]
        serve = serve[serve.index("\n") + 1:serve.index("\nEOF\n")]
        serve = (serve.replace("${var.aws_region}", "us-west-1").replace("$${", "${")
                 .replace("${local.claw_master_drain_seconds}", "600").replace("${local.claw_master_server_ip}", "10.0.1.50")
                 .replace("${local.claw_master_server_port}", "8443").replace("${local.claw_master_open_port}", "8444")
                 .replace("${local.claw_master_profiles[0]}", "claude-connor"))
        serve = re.sub(r"\$\{join\(.*\)\}", "", serve)
        with tempfile.TemporaryDirectory() as d:
            bindir = os.path.join(d, "bin"); os.mkdir(bindir)
            open(os.path.join(bindir, "aws"), "w").write("#!/bin/bash\necho test-key\n")
            report = '#!/bin/bash\necho "$0|${CLAW_MASTER_BACKUP_API_KEY-unset}|${CLAUDE_MASTER_BACKUP_API_KEY-unset}"\n'
            for name in ("aws", "claw-master", "claude-master"):
                path = os.path.join(bindir, name)
                if name != "aws":
                    open(path, "w").write(report)
                os.chmod(path, 0o755)
            profiles = os.path.join(d, "profiles"); os.makedirs(os.path.join(profiles, "claude-connor", "current"))
            env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"], CLAW_MASTER_PROFILE_DIR=profiles)
            def run():
                s = serve.replace("/usr/local/bin/", bindir + "/")
                return subprocess.run(["bash", "-c", s, "serve", "legacy"], capture_output=True, text=True, env=env, timeout=30).stdout.strip()
            self.assertEqual(run(), "%s/claw-master|test-key|unset" % bindir)
            os.chmod(os.path.join(bindir, "claw-master"), 0o644)
            self.assertEqual(run(), "%s/claude-master|unset|test-key" % bindir)

    def test_the_server_always_logs_at_info_to_a_rotated_file_it_may_write(self):
        wrapper = TF[TF.index("cat > /usr/local/bin/claw-master-serve"):TF.index("chmod 0755 /usr/local/bin/claw-master-serve")]
        for flag in ("--log-level info", "--log-file /var/log/claude-master/server-$color.log", "--log-max-mb 20", "--log-keep 5",
                     "--quota-log-interval 5m", "--account-labels-file /etc/claude-master/account-labels"):
            self.assertIn(flag, wrapper)
        self.assertNotIn("--log-level debug", wrapper)
        self.assertIn("LogsDirectory=claude-master", TF)
        self.assertIn("ProtectSystem=strict", TF)

    def test_the_journal_is_capped(self):
        self.assertIn("SystemMaxUse=200M", TF)
        self.assertIn("journalctl --vacuum-size=200M", TF)

    def test_host_metric_alarms_select_exactly_the_dimensions_the_agent_publishes(self):
        # The agent adds a `host` dimension unless omit_hostname is set. The alarms select no dimensions, so the
        # series must have none; otherwise they see no data and (notBreaching) never fire.
        self.assertIn('"omit_hostname": true', TF)
        self.assertNotIn("append_dimensions", TF)
        for name in ("claw_master_server_memory", "claw_master_server_swap"):
            alarm = block("aws_cloudwatch_metric_alarm", name)
            self.assertNotIn("dimensions", alarm)
            self.assertIn("namespace           = local.claw_master_metrics_namespace", alarm)

    def test_the_agent_ships_the_exact_log_file_not_its_rotations(self):
        # Every server's log (server-blue.log, server-green.log, and server-legacy.log before Envoy), never a
        # rotation (server-blue.log.1): the pattern ends in .log.
        import fnmatch
        self.assertIn('"file_path": "/var/log/claude-master/server*.log"', TF)
        self.assertNotIn("server.log*", TF)
        self.assertNotIn("server*.log*", TF)
        for name in ("server-blue.log", "server-green.log", "server-legacy.log"):
            self.assertTrue(fnmatch.fnmatch(name, "server*.log"), name)
        for name in ("server-blue.log.1", "server-green.log.5"):
            self.assertFalse(fnmatch.fnmatch(name, "server*.log"), name)

    def test_the_agent_is_reloaded_only_when_its_config_changed_and_the_server_is_never_touched(self):
        # Compared with the copy we keep: the agent moves the file it is given, so its own path never holds it.
        self.assertIn("cmp -s /tmp/cwagent.json /etc/claude-master/cwagent.json", TF)
        record = "install -m 0644 /tmp/cwagent.json /etc/claude-master/cwagent.json"
        handed = "-c file:/tmp/cwagent.apply.json"
        self.assertIn(record, TF)
        self.assertIn(handed, TF)
        # The copy is the record of what was applied: it is written only after the agent accepted the config.
        self.assertLess(TF.index(handed), TF.index(record))
        self.assertRegex(TF, r"if /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl[^\n]*\\\n[^\n]*-s >/dev/null 2>&1; then\n\s*install -m 0644 /tmp/cwagent.json /etc/claude-master/cwagent.json")
        self.assertNotIn("etc/amazon-cloudwatch-agent.json", TF)
        script = TF[TF.index("# ---------------------------------------------------------------- cloudwatch agent"):TF.index("# ---------------------------------------------------------------- operator helpers")]
        self.assertNotIn("claude-master-server", script)
        self.assertNotRegex(script, r"systemctl (restart|stop)")

    def test_the_grant_publishes_to_one_namespace_and_writes_one_log_group(self):
        policy = block("aws_iam_role_policy", "claw_master_server_telemetry")
        self.assertRegex(policy, r'"cloudwatch:PutMetricData"')
        self.assertIn('"cloudwatch:namespace" = local.claw_master_metrics_namespace', policy)
        self.assertIn("aws_cloudwatch_log_group.claw_master_server.arn", policy)
        self.assertNotRegex(policy, r"logs:CreateLogGroup|logs:DeleteLogGroup|logs:\*|cloudwatch:\*")
        # The only wildcard resource is PutMetricData, which has no resource-level scope: the condition holds it.
        self.assertEqual(policy.count('Resource = "*"'), 1)
        self.assertLess(policy.index('Resource = "*"'), policy.index("WriteItsOwnLogGroup"))

    def test_the_metrics_namespace_is_the_one_the_dashboards_read(self):
        # Since the rename the agent publishes into ClawMaster (claw_master.* metrics); the log group keeps its name.
        self.assertRegex(TF, r'claw_master_metrics_namespace\s+=\s+"ClawMaster"')
        self.assertRegex(TF, r'claw_master_log_group\s+=\s+"/claude-master/server"')
        self.assertIn('"namespace": "${local.claw_master_metrics_namespace}"', TF)

    def test_the_log_group_is_kept_and_two_log_alarms_exist(self):
        self.assertIn("retention_in_days = 90", block("aws_cloudwatch_log_group", "claw_master_server"))
        for key in ("CredentialRejected", "NoAccountAvailable"):
            self.assertIn(key + " = {", TF)
        for message in ("profile credential rejected by Anthropic", "no inference account could be chosen"):
            self.assertIn(message, TF)
        for name in ("claw_master_server_status", "claw_master_server_memory", "claw_master_server_swap", "claw_master_server_log"):
            self.assertIn("aws_sns_topic.cost_alerts.arn", block("aws_cloudwatch_metric_alarm", name))


class DashboardTests(unittest.TestCase):
    """The dashboard and its alarms read what the proxy publishes. Every query here was checked against the live
    CloudWatch API; these tests keep them valid and cheap."""

    # The metrics the proxy emits (docs/claude-master.md in ejc3/CLIProxyAPI) and the host metrics of the agent.
    METRICS = {
        "claw_master.inference.requests", "claw_master.inference.requests.by_model",
        "claw_master.inference.requests.by_client", "claw_master.inference.errors",
        "claw_master.inference.duration", "claw_master.inference.duration.by_model",
        "claw_master.inference.ttfb", "claw_master.inference.ttfb.by_model",
        "claw_master.inference.upstream_ttfb", "claw_master.inference.duration_quantile",
        "claw_master.proxy.overhead", "claw_master.proxy.connections", "claw_master.proxy.tls_handshake_errors",
        "claw_master.quota.used_fraction", "claw_master.quota.resets_in_seconds", "claw_master.quota.rate_limited",
        "claw_master.anthropic.ratelimit", "claw_master.routing.switches", "claw_master.routing.backup_requests",
        "claw_master.auth.token_expires_in_seconds", "claw_master.auth.refresh", "claw_master.usage.polls",
        "claw_master.process.heap_bytes", "claw_master.process.goroutines",
        "mem_used_percent", "swap_used_percent", "CredentialRejected", "NoAccountAvailable",
    }
    # Names CloudWatch accepted unquoted in GROUP BY / WHERE. Anything else (window and result are reserved
    # words, which the API rejected) must be double quoted.
    SAFE = {"profile", "client_account", "client", "model", "status_class", "status", "route", "reason", "quantile", "measure"}

    def queries(self):
        found = re.findall(r'"(SELECT (?:[^"\\]|\\.)*)"', DASH)
        return [q.replace('\\"', '"').replace("${local.cm_ns}", "ClawMaster") for q in found]

    def test_there_are_queries_and_every_one_reads_the_agents_namespace_and_a_known_metric(self):
        queries = self.queries()
        self.assertGreaterEqual(len(queries), 30)
        for q in queries:
            self.assertRegex(q, r'^SELECT (SUM|AVG|MIN|MAX)\(.+\) FROM "ClawMaster(/Logs)?"')
            metric = re.match(r'SELECT \w+\("?([^")]+)"?\)', q).group(1)
            self.assertIn(metric, self.METRICS, q)

    def test_a_dimension_is_quoted_unless_it_is_known_to_be_safe(self):
        checked = 0
        for q in self.queries():
            tail = re.split(r'FROM "[^"]+"', q, maxsplit=1)[1]
            idents = []
            group = re.search(r"GROUP BY (.+)$", tail)
            if group:
                idents += [x.strip() for x in group.group(1).split(",")]
            idents += re.findall(r'(?:WHERE|AND)\s+("?\w+"?)\s*=', tail)
            for ident in idents:
                checked += 1
                self.assertTrue(ident.startswith('"') or ident in self.SAFE, "%s: dimension %s must be double quoted" % (q, ident))
        self.assertGreaterEqual(checked, 15, "the check looked at the queries' dimensions")

    def test_the_reserved_words_that_cloudwatch_rejected_are_quoted(self):
        for q in self.queries():
            self.assertNotRegex(q, r'(GROUP BY|,|WHERE)\s+(result|window)\b', q)
        self.assertIn('GROUP BY \\"result\\"', DASH)
        self.assertIn('\\"window\\"', DASH)

    def test_no_panel_names_a_dimension_list_so_a_renamed_dimension_cannot_blank_it(self):
        self.assertNotRegex(DASH, r"Name=|dimensions\s*=", "Metrics Insights queries aggregate across dimensions")

    def test_the_alarms_are_dimensionless_query_alarms_on_the_shared_topic(self):
        alarm = block_in(DASH, "aws_cloudwatch_metric_alarm", "claw_master_pool")
        self.assertIn("metric_query", alarm)
        self.assertNotIn("dimensions", alarm)
        self.assertIn("aws_sns_topic.cost_alerts.arn", alarm)
        self.assertIn('SELECT MIN(\\"claw_master.quota.used_fraction\\")', alarm)
        self.assertIn("threshold           = each.value", alarm)

    def test_the_error_alarm_judges_one_ten_minute_datapoint_not_two_five_minute_ones(self):
        # With a 300 s period and two evaluation periods, each five minutes would have to pass the threshold on
        # its own: 15 errors in each never fired a "20 errors in 10 minutes" alarm.
        alarm = block_in(DASH, "aws_cloudwatch_metric_alarm", "claw_master_pool")
        self.assertIn('evaluation_periods  = each.key == "pool_exhausted" ? 3 : 1', alarm)
        self.assertIn('period = each.key == "pool_exhausted" ? 300 : 600', alarm)
        self.assertIn("More than 20 Anthropic error responses in 10 minutes", alarm)

    def test_a_panel_titled_hours_divides_the_seconds_the_proxy_publishes(self):
        self.assertIn("divide  = 3600", DASH)
        self.assertIn('expression = "q${i}_${n}/${p.divide}"', DASH)
        panel = DASH[DASH.index('title   = "Hours until each weekly allowance resets"'):]
        panel = panel[:panel.index("},\n")]
        self.assertIn("divide", panel)
        self.assertIn("quota.resets_in_seconds", panel)

    def test_the_dashboard_exists_once_and_shows_the_alarms(self):
        self.assertEqual(DASH.count('resource "aws_cloudwatch_dashboard"'), 1)
        self.assertIn('type = "alarm"', DASH)
        self.assertIn("aws_cloudwatch_metric_alarm.claw_master_pool", DASH)


def block_in(text, kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n.*?^\}' % (kind, name), text, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


class ConvergenceTests(unittest.TestCase):
    """The instance ignores user_data, so Terraform itself must re-run the bootstrap."""

    def test_a_script_or_pin_or_resize_change_re_runs_the_bootstrap(self):
        res = block("terraform_data", "claw_master_server_converge")
        for trigger in ("aws_instance.claw_master_server[0].id", "aws_instance.claw_master_server[0].instance_type",
                        "sha256(local.claw_master_server_user_data)", 'filesha256("${path.module}/scripts/ssm-claw-master-server.sh")'):
            self.assertIn(trigger, res)
        self.assertIn("scripts/ssm-claw-master-server.sh", res)
        self.assertIn("aws_s3_object.claw_master_server_user_data", res)
        # cloudflared reads its token at start: the secret and the role's grant must exist first.
        self.assertIn("aws_secretsmanager_secret_version.claw_master_tunnel_token", res)
        self.assertIn("aws_iam_role_policy.claw_master_server_tunnel_token", res)

    def test_the_convergence_waits_for_ssm_and_cloud_init_and_fails_loudly(self):
        text = code(CONVERGE)
        self.assertIn("cloud-init status --wait", text)
        self.assertIn("PingStatus", text)
        self.assertIn('[ "$status" != Success ]', text)
        self.assertIn("exit 1", text)

    def test_the_convergence_never_restarts_the_server(self):
        self.assertNotRegex(code(CONVERGE), r"systemctl\s+(restart|stop|start)")


class TunnelTests(unittest.TestCase):
    """The Macs' path: an outbound Cloudflare tunnel to a LOOPBACK-only open listener."""

    def test_the_open_listener_is_loopback_only_and_never_in_a_security_group(self):
        # Envoy takes the tunnel's port on loopback; each server's open listener is on loopback too.
        self.assertIn('open = "127.0.0.1:${local.claw_master_open_port}"', TF)
        self.assertIn("--open-loopback 127.0.0.1:$open", TF)
        self.assertNotRegex(TF, r"--open-loopback\s+(0\.0\.0\.0|\$\{local\.claw_master_server_ip\})")
        sg = block("aws_security_group", "claw_master_server")
        self.assertNotIn("claw_master_open_port", sg)
        self.assertNotIn("claw_master_colors", sg)

    def test_the_tunnel_forwards_to_loopback_over_tcp(self):
        cfg = tblock("cloudflare_zero_trust_tunnel_cloudflared_config", "claw_master")
        self.assertIn('service  = "tcp://127.0.0.1:${local.claw_master_open_port}"', cfg)
        self.assertIn("http_status:404", cfg)  # anything else gets nothing
        self.assertIn("cloudflare_zero_trust_access_application.claw_master", cfg)  # no routing before Access

    def test_the_windows_pc_has_its_own_token_in_the_one_policy(self):
        # Revocable on its own; admitted by the same non_identity policy, so the application keeps one.
        pol = tblock("cloudflare_zero_trust_access_policy", "claw_master_macs")
        for name in ("claw_master_macs", "claw_master_windows_pc"):
            self.assertIn("cloudflare_zero_trust_access_service_token.%s.id" % name, pol)
        self.assertIn('duration   = "8760h"', tblock("cloudflare_zero_trust_access_service_token", "claw_master_windows_pc"))
        ver = tblock("aws_secretsmanager_secret_version", "claw_master_windows_pc_access")
        self.assertIn("claw_master_windows_pc.client_secret", ver)
        self.assertNotIn("claw_master_macs", ver)

    def test_only_a_service_token_may_reach_it_no_person_and_no_wildcard(self):
        pol = tblock("cloudflare_zero_trust_access_policy", "claw_master_macs")
        self.assertIn('decision         = "non_identity"', pol)
        self.assertIn("service_token", pol)
        self.assertNotRegex(pol, r"email|everyone|group|login_method")
        app = tblock("cloudflare_zero_trust_access_application", "claw_master")
        self.assertIn("domain           = local.claw_master_tunnel_hostname", app)
        self.assertEqual(app.count("id         ="), 1, "exactly one policy")
        self.assertNotIn("*", re.search(r'claw_master_tunnel_hostname\s+=\s+"([^"]+)"', TUN).group(1))

    def test_the_two_secrets_have_different_readers(self):
        conn = tblock("aws_secretsmanager_secret_policy", "claw_master_tunnel_token")
        self.assertIn("aws_iam_role.claw_master_server[0].arn", conn)
        mac = tblock("aws_secretsmanager_secret_policy", "claw_master_mac_access")
        self.assertNotIn("claw_master_server", mac)  # the server never needs the Macs' credential
        win = tblock("aws_secretsmanager_secret_policy", "claw_master_windows_pc_access")
        self.assertNotIn("claw_master_server", win)
        self.assertIn("aws_secretsmanager_secret.claw_master_windows_pc_access.arn", win)
        # fcvm-arm sets the PC up, so it may read the PC's token, and only from its own instance
        self.assertIn("[aws_iam_role.dev_server.arn]", win)
        grant = tblock("aws_iam_role_policy", "dev_server_claw_master_windows_pc")
        self.assertIn("Resource  = aws_secretsmanager_secret.claw_master_windows_pc_access.arn", grant)
        self.assertIn('"ec2:SourceInstanceARN" = aws_instance.firecracker_dev[0].arn', grant)
        self.assertNotIn("claw_master_mac_access", grant)
        self.assertNotIn("[aws_iam_role.dev_server.arn]", mac)  # the Macs' token stays administrators only
        for p in (conn, mac, win):
            self.assertIn('Effect    = "Deny"', p)
            self.assertIn("local.games_mp_admin_principals", p)

    def test_cloudflared_is_pinned_and_gets_its_token_from_the_environment(self):
        self.assertRegex(TUN, r'cloudflared_sha256_linux_arm\s+=\s+"[0-9a-f]{64}"')
        self.assertIn("sha256sum -c -", TF)
        script = TF[TF.index("claw_master_server_user_data"):TF.index('resource "aws_s3_object"')]
        self.assertIn("export TUNNEL_TOKEN", script)
        self.assertNotRegex(script, r"cloudflared[^\n]*--token")
        self.assertIn("{ set +x; } 2>/dev/null", script[script.index("cloudflared-claude-master <<"):])

    def test_a_dead_tunnel_is_never_handed_out(self):
        # A running server keeps its old configuration; the tunnel can be live with nothing behind it.
        script = TF[TF.index("claw_master_server_user_data"):TF.index('resource "aws_s3_object"')]
        self.assertIn("open listener: listening", script)
        self.assertIn("open listener: NOT listening", script)
        self.assertIn("grep -qx 'open listener: listening'", code(BUNDLE))
        self.assertLess(code(BUNDLE).index("open listener: listening"), code(BUNDLE).index("ca.pem"))
        self.assertNotRegex(code(BUNDLE), r"systemctl\s+(restart|stop|start)")  # the owner decides that

    def test_the_macs_get_one_file_and_no_certificate_or_aws_access(self):
        self.assertIn("claude-master/mac-access", BUNDLE)
        self.assertIn("chmod 0600", BUNDLE)
        self.assertIn("claw-master connect --open 127.0.0.1", BUNDLE)
        self.assertNotRegex(code(BUNDLE), r"client\.key|client-init")
        self.assertIn("cloudflared access tcp", BUNDLE)


class MacSetupTests(unittest.TestCase):
    """Setting a Mac up over its reverse tunnel: additive, pinned, token through stdin only."""

    def pin(self, name):
        return re.search(r"^%s=(\S+)$" % name, MACSETUP, re.M).group(1)

    def test_the_pinned_tag_matches_the_servers(self):
        server_tag = re.search(r'claw_master_tag\s+=\s+"([^"]+)"', TF).group(1)
        self.assertEqual(self.pin("CM_TAG"), server_tag, "bump both together")
        for name in ("CM_SHA256_DARWIN_ARM64", "CFD_SHA256_DARWIN_ARM64_TGZ", "CFD_SHA256_DARWIN_ARM64_BIN"):
            self.assertRegex(self.pin(name), r"^[0-9a-f]{64}$")
        cfd = re.search(r'cloudflared_version\s+=\s+"([^"]+)"', TUN).group(1)
        self.assertEqual(self.pin("CFD_VERSION"), cfd)

    def test_binaries_are_verified_on_the_mac_before_they_are_installed(self):
        text = code(MACSETUP)
        # claude-master, the cloudflared tarball, and the cloudflared executable inside it
        self.assertEqual(text.count("did not match its pinned sha256"), 3)
        self.assertLess(text.index("claw-master did not match"), text.index('mv -f "\\$tmp/cm"'))
        self.assertLess(text.index("cloudflared did not match"), text.index('mv -f "\\$tmp/cloudflared"'))
        self.assertLess(text.index("the cloudflared executable did not match"), text.index('mv -f "\\$tmp/cloudflared"'))

    def test_an_installed_cloudflared_is_judged_by_its_hash_never_by_what_it_reports(self):
        # It receives the long-lived Access token. A substituted executable can print any version, and an
        # unanchored version match let a prefix of the pinned version skip the check entirely.
        text = code(MACSETUP)
        self.assertNotRegex(text, r"cloudflared[^\n]*--version[^\n]*grep")
        self.assertIn('"\\$(sum "\\$HOME/.local/bin/cloudflared" 2>/dev/null || true)" != "$CFD_SHA256_DARWIN_ARM64_BIN"', text)

    def test_nothing_is_installed_until_the_server_is_serving_its_open_port(self):
        # A CA certificate exists from the server's first start, but a server still running the pre-tunnel
        # configuration listens on no open port, so a Mac set up then reports success and cannot connect.
        text = code(MACSETUP)
        gate = text.index("0/6")
        self.assertIn("claw-master-status", text[gate:text.index("1/6")])
        self.assertIn("grep -q '^open listener: listening'", text[gate:text.index("1/6")])
        self.assertIn("exit 1", text[gate:text.index("1/6")])
        for step in ("1/6", "2/6", "3/6", "5/6"):
            self.assertLess(gate, text.index(step))
        self.assertLess(gate, text.index("curl -fsSL"))
        self.assertLess(gate, text.index("TUNNEL_SERVICE_TOKEN_SECRET"))

    def test_the_token_goes_through_stdin_only(self):
        text = code(MACSETUP)
        self.assertIn("{ set +x; } 2>/dev/null", text)
        # The secret is produced by python from the variable and piped straight into the ssh `cat >`.
        seg = text[text.index("TUNNEL_SERVICE_TOKEN_SECRET"):text.index("unset SECRET")]
        self.assertIn("|\n  on_mac", seg)
        self.assertIn("cat > \\$HOME/.config/claude-master/cloudflare.env.new", seg)
        # nothing that carries the secret appears in an ssh command line
        self.assertNotRegex(text, r"ssh[^\n]*client_secret")
        self.assertNotRegex(text, r"echo[^\n]*\$SECRET")
        self.assertIn("unset SECRET", text)
        self.assertIn("chmod 600", text)

    def test_it_changes_nothing_about_the_macs_own_claude(self):
        text = code(MACSETUP)
        for forbidden in ("~/.claude", "$HOME/.claude", "settings.json", "alias claude", ".zshrc", ".zprofile", "/usr/local/bin/claude"):
            self.assertNotIn(forbidden, text)
        self.assertIn("claude-pool", text)

    def test_the_tunnel_log_rotates_and_the_token_is_not_in_the_plist(self):
        self.assertRegex(MACSETUP, r"-gt 5242880 \]")  # exactly 5 MB, not merely containing the digits
        self.assertIn('cp "\\$LOG" "\\$LOG.1" && : > "\\$LOG"', MACSETUP)
        plist = MACSETUP[MACSETUP.index("<?xml"):MACSETUP.index("</dict></plist>")]
        self.assertNotRegex(plist, r"TUNNEL_SERVICE_TOKEN|client_secret")
        self.assertIn("<key>KeepAlive</key><true/>", plist)


class WindowsSetupTests(unittest.TestCase):
    """Setting a Windows PC up from fcvm-arm: pinned, owner-only token through stdin, and the PC switched
    to the pool only once the pool answers through its tunnel."""

    def pin(self, name):
        return re.search(r"^%s=(\S+)$" % name, WINSETUP, re.M).group(1)

    def step(self, n):
        """The text of step n/7, from its echo to the next step's."""
        text = code(WINSETUP)
        start = text.index('echo "%d/7' % n)
        nxt = text.find('echo "%d/7' % (n + 1), start)
        return text[start:nxt if nxt >= 0 else len(text)]

    def test_the_pins_match_the_servers_and_the_tunnels(self):
        server_tag = re.search(r'claw_master_tag\s+=\s+"([^"]+)"', TF).group(1)
        self.assertEqual(self.pin("CM_TAG"), server_tag, "bump both together")
        cfd = re.search(r'cloudflared_version\s+=\s+"([^"]+)"', TUN).group(1)
        self.assertEqual(self.pin("CFD_VERSION"), cfd)
        for name in ("CM_SHA256_WINDOWS_AMD64", "CFD_SHA256_WINDOWS_AMD64"):
            self.assertRegex(self.pin(name), r"^[0-9a-f]{64}$")

    def test_both_binaries_are_installed_only_through_the_hash_check(self):
        s2 = self.step(2)
        fn = s2[s2.index("function Install-Pinned"):s2.index("\n}\n", s2.index("function Install-Pinned"))]
        # the downloaded file is rejected on a hash mismatch, and moved into place only after the check
        self.assertIn('if ((Get-FileHash "$dest.new").Hash -ne $sha) { Remove-Item "$dest.new"; throw', fn)
        self.assertLess(fn.index("-ne $sha"), fn.index('Move-Item -Force "$dest.new" $dest'))
        self.assertIn('claw-master-windows-amd64.exe" "$bin\\claw-master.exe" $CmSha', s2)
        self.assertIn('cloudflared-windows-amd64.exe" "$bin\\cloudflared.exe" $CfdSha', s2)
        self.assertEqual(code(WINSETUP).count("Invoke-WebRequest"), 1, "every download goes through Install-Pinned")
        self.assertNotRegex(code(WINSETUP), r"--version")

    def test_the_token_goes_through_stdin_into_a_file_checked_owner_only_first(self):
        s3 = self.step(3)
        self.assertIn("{ set +x; } 2>/dev/null", s3)
        self.assertIn("--secret-id claude-master/windows-pc-access", s3)
        self.assertIn("|\n  on_pc", s3)
        self.assertIn("unset SECRET", code(WINSETUP))
        # protected (no inherited rules), the user's one rule, checked, and only then written
        self.assertIn("SetAccessRuleProtection($true, $false)", s3)
        check = s3.index("$rules.Count -ne 1")
        self.assertIn("throw 'cloudflare.env could not be made owner-only; nothing written'", s3[check:])
        self.assertLess(check, s3.index("[Console]::In.ReadToEnd()"))
        self.assertNotRegex(code(WINSETUP), r"ssh[^\n]*client_secret")
        self.assertNotRegex(code(WINSETUP), r"echo[^\n]*\$SECRET")
        self.assertNotRegex(code(WINSETUP), r"ps_vars[^\n]*SECRET")
        self.assertIn("-EncodedCommand $enc", code(WINSETUP))

    def test_a_failed_step_stops_the_script(self):
        text = code(WINSETUP)
        self.assertIn("set -euo pipefail", text)
        # on_pc ends in a pipe; pipefail carries ssh's (so PowerShell's) exit status through it
        self.assertRegex(text, r'-EncodedCommand \$enc" \| tr -d')

    def test_the_tunnel_task_reads_the_token_into_its_environment_not_its_arguments(self):
        task = WINSETUP[WINSETUP.index("@'\n"):WINSETUP.index("'@ | Set-Content")]
        self.assertIn("SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')", task)
        self.assertNotRegex(task, r"cloudflared[^\n]*(--service-token|TUNNEL_SERVICE_TOKEN)")
        self.assertIn("-gt 5242880", task)
        self.assertIn("Start-ScheduledTask -TaskName $name", self.step(5))

    def test_the_pc_is_switched_over_only_after_the_pool_answers_through_its_tunnel(self):
        s6 = self.step(6)
        self.assertIn('-x "http://127.0.0.1:$Port"', s6)
        # the open listener is a plain-HTTP CONNECT proxy; the CA verifies the origin (api.anthropic.com), not the proxy
        self.assertIn('--cacert "$d\\ca.pem" --ssl-revoke-best-effort https://api.anthropic.com/v1/models', s6)
        self.assertNotIn("--proxy-cacert", code(WINSETUP))
        # CONNECT 200 and an HTTP status of Anthropic's own: a 5xx is the proxy's failed upstream request
        self.assertIn("if ($LASTEXITCODE -eq 0 -and $out -match '^200 [24]\\d\\d$')", s6)
        self.assertRegex(s6, r"the PC was not switched over\"\nexit 1")
        self.assertLess(code(WINSETUP).index('echo "6/7'), code(WINSETUP).index("agent install --listen"))
        self.assertNotIn("agent install --listen", s6)

    def test_agent_install_and_the_final_check_fail_the_script_when_they_fail(self):
        s7 = self.step(7)
        self.assertIn('if ($LASTEXITCODE -ne 0) { "agent install failed ($LASTEXITCODE)"; exit 1 }', s7)
        self.assertIn('''if ($LASTEXITCODE -ne 0 -or $out -notmatch '^200 [24]\\d\\d$') { "the agent did not reach the pool (curl exit $LASTEXITCODE, status $out)"; exit 1 }''', s7)
        self.assertNotIn("$ErrorActionPreference = 'Stop'", s7)  # 5.1 ends the script on native stderr
        # the agent's proxy URL (it carries the agent's token) reaches curl only through its environment
        self.assertIn("$env:HTTPS_PROXY = (Get-Content", s7)
        self.assertNotRegex(s7, r"curl\.exe[^\n]*(-x|--proxy)")
        # Schannel fails closed on the private CA's unknown revocation status; both checks pass best-effort
        self.assertIn('--cacert "$d\\ca.pem" --ssl-revoke-best-effort https://api.anthropic.com/v1/models', s7)
        self.assertNotIn("--ssl-no-revoke", code(WINSETUP))
        self.assertNotRegex(s7, r'"[^"\n]*\$env:HTTPS_PROXY')

    def test_it_parses(self):
        r = subprocess.run(["bash", "-n", str(ROOT / "scripts" / "claw-master-windows-setup.sh")], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


class IamTests(unittest.TestCase):
    def test_the_role_reads_only_its_own_bootstrap_script(self):
        policy = block("aws_iam_role_policy", "claw_master_server_bootstrap")
        self.assertIn('"${aws_s3_bucket.dev_scripts.arn}/user-data/claude-master-server.sh"', policy)
        self.assertNotIn('"*"', policy)

    def test_it_is_not_given_the_kids_box_or_dev_server_powers(self):
        role = block("aws_iam_role", "claw_master_server")
        self.assertIn("ec2.amazonaws.com", role)
        self.assertNotIn("AdministratorAccess", TF)


class ScriptTests(unittest.TestCase):
    def test_enrollment_signs_over_ssm_and_never_moves_a_key(self):
        self.assertIn("aws ssm send-command", ENROLL)
        self.assertIn("/usr/local/bin/claw-master-sign", ENROLL)
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
        self.assertIn("sudo claw-master-login", LOGIN)
        # A new login goes live through a rollout (no session is cut); no rollout when nothing was added.
        self.assertIn('if [ "$(count_logins)" -gt "$before" ]; then', LOGIN)
        self.assertIn("sudo claw-master-rollout && sudo claw-master-status", LOGIN)
        self.assertNotRegex(LOGIN, r"systemctl (restart|start)")

    def test_enrollment_as_an_account_carries_awkward_commands_and_stdin_through_ssh_and_sudo_intact(self):
        # Runs the real on_target function with ssh and sudo stubbed: ssh executes its last argument (the remote command
        # line) in a shell, sudo drops its options and runs the rest as the same user but records who it was asked for.
        import os, subprocess, tempfile
        fn = ENROLL[ENROLL.index("on_target() {"):ENROLL.index("\n}\n", ENROLL.index("on_target() {")) + 3]
        with tempfile.TemporaryDirectory() as d:
            bindir = os.path.join(d, "bin"); os.mkdir(bindir)
            open(os.path.join(bindir, "ssh"), "w").write('#!/bin/bash\nfor a; do last="$a"; done\nexec bash -c "$last"\n')
            open(os.path.join(bindir, "sudo"), "w").write(
                '#!/bin/bash\nwhile [ "${1:-}" != bash ]; do case "$1" in -u) echo "asked-for:$2" >> "$SUDO_LOG"; shift 2;; *) shift;; esac; done\nexec "$@"\n')
            for f in ("ssh", "sudo"): os.chmod(os.path.join(bindir, f), 0o755)
            log = os.path.join(d, "sudo.log")
            script = ('HOST=h; AS=colton; ' + fn + '\n'
                      'on_target "set -e; cd \\"\\$HOME\\"; printf \'%s|%s\\\\n\' \\"it\'s \\$(echo q)\\" \'a b\'"\n'
                      'printf "%s\\n" PEM-LINE-1 PEM-LINE-2 | on_target "cat"\n')
            env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"], SUDO_LOG=log)
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(out.stdout.splitlines(), ["it's q|a b", "PEM-LINE-1", "PEM-LINE-2"])
            self.assertEqual(open(log).read().split(), ["asked-for:colton", "asked-for:colton"])

    def test_nextjs_dev_installs_the_pinned_client_and_routes_only_enrolled_accounts(self):
        import os, re, subprocess, tempfile
        nx = (ROOT / "nextjs-user-data.tf").read_text()
        # one pin with the server, checksum-verified, installed under a temporary name then renamed
        self.assertIn('CM_TAG="${local.claw_master_tag}"', nx)
        self.assertIn('CM_SHA="${local.claw_master_sha256_aarch64}"', nx)
        self.assertIn('echo "$CM_SHA  $CMTMP/claw-master" | sha256sum -c --quiet -', nx)
        self.assertIn("/usr/local/bin/claw-master.new && mv -f /usr/local/bin/claw-master.new /usr/local/bin/claw-master", nx)
        self.assertIn("releases/download/$CM_TAG/claw-master-linux-arm64", nx)
        # The old name is a link to the new binary, made only once the new binary is there; it is what t-claude runs.
        self.assertIn('if [ -x /usr/local/bin/claw-master ] && [ "$(readlink /usr/local/bin/claude-master)" != claw-master ]; then', nx)
        self.assertIn("ln -sfn claw-master /usr/local/bin/claude-master.new && mv -Tf /usr/local/bin/claude-master.new /usr/local/bin/claude-master", nx)
        # the zshenv block: run it for real, twice, against a copy of the stock file
        start = nx.index('ZE=/etc/zsh/zshenv'); end = nx.index("CMZSHENV\nfi\n", start) + len("CMZSHENV\nfi\n")
        block = nx[start:end].replace("${local.claw_master_server_ip}", "203.0.113.5").replace("${local.claw_master_server_port}", "8443")
        with tempfile.TemporaryDirectory() as d:
            ze = os.path.join(d, "zshenv"); open(ze, "w").write("# stock\nexport PATH=$PATH\n")
            fake = os.path.join(d, "claude-master"); open(fake, "w").write("#!/bin/sh\n"); os.chmod(fake, 0o755)
            script = block.replace("ZE=/etc/zsh/zshenv", "ZE=" + ze).replace("[ -x /usr/local/bin/claude-master ]", "[ -x %s ]" % fake)
            for _ in range(2):
                r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 0, r.stderr)
            text = open(ze).read()
            self.assertEqual(text.count(">>> claude-master"), 1, "the block is rewritten, not repeated")
            self.assertTrue(text.startswith("# stock\nexport PATH=$PATH\n"), "the rest of the file is untouched")
            # The block is plain POSIX shell (zsh reads it unchanged), so bash is enough to run it: the CI runner has no zsh.
            def server_for(home):
                out = subprocess.run(["bash", "-c", f'HOME={home}; . {ze}; printf "%s|%s\\n" "${{TCLAUDE_INFERENCE_SERVER-unset}}" "${{TCLAUDE_CLAUDE_MASTER-unset}}"'],
                                     capture_output=True, text=True, timeout=30)
                return out.stdout.strip()
            enrolled = os.path.join(d, "enrolled"); os.makedirs(enrolled + "/.config/claude-master")
            open(enrolled + "/.config/claude-master/client.pem", "w").write("x")
            plain = os.path.join(d, "plain"); os.makedirs(plain)
            self.assertEqual(server_for(enrolled), "203.0.113.5:8443|/usr/local/bin/claude-master")
            self.assertEqual(server_for(plain), "unset|unset", "an account without a certificate is not routed")
            os.chmod(fake, 0o644)
            self.assertEqual(server_for(enrolled), "unset|unset", "an enrolled account is not routed when the client binary is not executable")

    def test_the_metal_box_gets_the_pinned_client_and_routing_through_an_ssm_association(self):
        import os, subprocess, tempfile, textwrap
        cf = (ROOT / "claw-master-client.tf").read_text()
        # the association: no key, no service, nothing restarted, one pin shared with the server
        self.assertIn('name                = "AWS-RunShellScript"', cf)
        self.assertIn('CM_TAG="${local.claw_master_tag}"', cf)
        self.assertIn('CM_SHA="${local.claw_master_sha256_aarch64}"', cf)
        self.assertIn('sha256sum -c --quiet -', cf)
        # install and rename are PART OF the if condition, so a failure of either fails the run
        self.assertIn('&& install -m 0755 "$CMTMP/claw-master" /usr/local/bin/claw-master.new \\', cf)
        self.assertIn("&& mv -f /usr/local/bin/claw-master.new /usr/local/bin/claw-master; then", cf)
        self.assertIn("releases/download/$CM_TAG/claw-master-linux-arm64", cf)
        self.assertIn("ln -sfn claw-master /usr/local/bin/claude-master.new && mv -Tf /usr/local/bin/claude-master.new /usr/local/bin/claude-master", cf)
        self.assertNotRegex(cf, r"systemctl\s+(restart|stop|kill)|tmux[- ]?(scroll)?\s+kill|kill-server|pkill")
        # render the heredoc as Terraform does (<<- strips the common indent), then run the zshenv part for real, twice
        body = cf.split("<<-CMD\n", 1)[1].split("\n  CMD\n", 1)[0]
        script = (textwrap.dedent(body) + "\n").replace("${local.claw_master_server_ip}", "203.0.113.5").replace("${local.claw_master_server_port}", "8443")
        start = script.index("ZE=/etc/zsh/zshenv"); end = script.index("CMZSHENV\nfi\n", start) + len("CMZSHENV\nfi\n")
        block = script[start:end]
        self.assertIn("\nCMZSHENV\n", block, "the heredoc terminator must be at column 0 after the indent is stripped")
        with tempfile.TemporaryDirectory() as d:
            ze = os.path.join(d, "zshenv")
            # a box that still has the block written before the file was renamed (managed by claude-master-client.tf)
            open(ze, "w").write("# stock\nexport PATH=$PATH\n# >>> claude-master (managed by claude-master-client.tf) >>>\nexport TCLAUDE_OLD=1\n# <<< claude-master <<<\n")
            fake = os.path.join(d, "claude-master"); open(fake, "w").write("#!/bin/sh\n"); os.chmod(fake, 0o755)
            for _ in range(2):
                r = subprocess.run(["bash", "-c", block.replace("ZE=/etc/zsh/zshenv", "ZE=" + ze).replace("[ -x /usr/local/bin/claude-master ]", "[ -x %s ]" % fake)], capture_output=True, text=True, timeout=30)
                self.assertEqual(r.returncode, 0, r.stderr)
            text = open(ze).read()
            self.assertEqual(text.count(">>> claude-master"), 1)
            self.assertNotIn("TCLAUDE_OLD", text, "the block from before the rename is replaced, not kept")
            self.assertTrue(text.startswith("# stock\nexport PATH=$PATH\n"))
            def server_for(home):
                return subprocess.run(["bash", "-c", f'HOME={home}; . {ze}; printf "%s|%s\\n" "${{TCLAUDE_INFERENCE_SERVER-unset}}" "${{TCLAUDE_CLAUDE_MASTER-unset}}"'],
                                      capture_output=True, text=True, timeout=30).stdout.strip()
            enrolled = os.path.join(d, "enrolled"); os.makedirs(enrolled + "/.config/claude-master")
            with open(enrolled + "/.config/claude-master/client.pem", "w") as f: f.write("x")
            plain = os.path.join(d, "plain"); os.makedirs(plain)
            self.assertEqual(server_for(enrolled), "203.0.113.5:8443|/usr/local/bin/claude-master")
            self.assertEqual(server_for(plain), "unset|unset")
            os.chmod(fake, 0o644)
            self.assertEqual(server_for(enrolled), "unset|unset", "not routed when the client binary is not executable")
        # a failed install or rename must FAIL the association (it once fell through to an echo that reported success)
        with tempfile.TemporaryDirectory() as d:
            bindir = os.path.join(d, "bin"); os.mkdir(bindir)
            payload = b"stand-in binary"
            import hashlib
            sha = hashlib.sha256(payload).hexdigest()
            # the section exits early on an architecture with no pinned build; make the test the same on every machine
            open(os.path.join(bindir, "uname"), "w").write('#!/bin/bash\n[ "${1:-}" = -m ] && { echo aarch64; exit 0; }\nexec /usr/bin/uname "$@"\n')
            os.chmod(os.path.join(bindir, "uname"), 0o755)
            open(os.path.join(bindir, "curl"), "w").write('#!/bin/bash\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && out="$2"; shift; done\nprintf %s \'' + payload.decode() + '\' > "$out"\n')
            def run_with(install_ok, mv_ok):
                for name, ok in (("install", install_ok), ("mv", mv_ok)):
                    open(os.path.join(bindir, name), "w").write("#!/bin/bash\n" + ("exit 0\n" if ok else "echo simulated-failure >&2; exit 1\n"))
                for name in ("curl", "install", "mv"): os.chmod(os.path.join(bindir, name), 0o755)
                s = script.replace('CM_SHA="203.0.113.5"', "x")  # no-op guard; the sha comes from the template below
                s = s.replace("ZE=/etc/zsh/zshenv", "ZE=" + os.path.join(d, "zshenv"))
                s = s.replace('CM_SHA="${local.claw_master_sha256_aarch64}"', 'CM_SHA="%s"' % sha).replace('CM_TAG="${local.claw_master_tag}"', 'CM_TAG="test-tag"')
                s = s.replace("/usr/local/bin/claw-master", os.path.join(d, "installed-claw-master")).replace("/usr/local/bin/claude-master", os.path.join(d, "installed-claude-master"))
                return subprocess.run(["bash", "-c", s], capture_output=True, text=True, timeout=30, env=dict(os.environ, PATH=bindir + ":" + os.environ["PATH"]))
            ok = run_with(True, True)
            self.assertEqual(ok.returncode, 0, ok.stderr); self.assertIn("installed", ok.stdout)
            for name, r in (("install fails", run_with(False, True)), ("rename fails", run_with(True, False))):
                self.assertNotEqual(r.returncode, 0, name + " must fail the association")
                self.assertIn("FAILED", r.stdout, name)
        # the whole rendered command parses as shell
        r = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_enrollment_refuses_as_without_ssh_and_a_bad_account_name(self):
        self.assertIn('[ -z "$AS" ] || [ -n "$HOST" ]', ENROLL)
        self.assertIn('^[a-z_][a-z0-9_-]{0,31}$', ENROLL)

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
        for path in ("claw-master-enroll.sh", "claw-master-login.sh", "claw-master-tunnel.sh", "claw-master-mac-bundle.sh", "claw-master-mac-setup.sh"):
            r = subprocess.run(["bash", "-n", str(ROOT / "scripts" / path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)



def envoy_config():
    return TF[TF.index("cat > /etc/envoy/envoy.yaml <<'EOF'"):TF.index("\nEOF\n", TF.index("cat > /etc/envoy/envoy.yaml"))]


def rollout():
    return TF[TF.index("cat > /usr/local/bin/claw-master-rollout"):TF.index("chmod 0755 /usr/local/bin/claw-master-rollout")]


class EnvoyTests(unittest.TestCase):
    """Envoy in front of two servers on the box, so a rollout drains instead of cutting sessions."""

    def test_envoy_is_pinned_and_checked_before_it_is_installed(self):
        self.assertRegex(TF, r'claw_master_envoy_version\s+=\s+"[0-9]+\.[0-9]+\.[0-9]+"')
        self.assertRegex(TF, r'claw_master_envoy_sha256_aarch64\s+=\s+"[0-9a-f]{64}"')
        self.assertIn("github.com/envoyproxy/envoy/releases/download/v$ENVOY_VERSION/envoy-$ENVOY_VERSION-linux-aarch_64", TF)
        check = 'echo "$ENVOY_SHA  /tmp/envoy.new" | sha256sum -c -'
        self.assertIn(check, TF)
        self.assertLess(TF.index(check), TF.index("mv -f /usr/local/bin/envoy.new /usr/local/bin/envoy"))

    def test_envoy_only_passes_tcp_through_and_its_admin_stays_on_loopback(self):
        cfg = envoy_config()
        self.assertIn("envoy.filters.network.tcp_proxy", cfg)
        for forbidden in ("http_connection_manager", "transport_socket", "tls_context", "0.0.0.0"):
            self.assertNotIn(forbidden, cfg)
        self.assertIn("admin: { address: { socket_address: { address: 127.0.0.1, port_value: 9901 } } }", cfg)
        self.assertIn("watched_directory: { path: /etc/envoy/eds }", cfg)
        self.assertIn("max_active_downstream_connections", cfg)
        unit = TF[TF.index("cat > /etc/systemd/system/envoy.service"):]
        unit = unit[:unit.index("\nEOF\n")]
        self.assertIn("User=envoy", unit)
        self.assertIn("--disable-hot-restart", unit)

    def test_the_color_ports_are_never_in_the_security_group(self):
        sg = block("aws_security_group", "claw_master_server")
        colors = re.search(r"claw_master_colors = \{(.*?)\n  \}", TF, re.S).group(1)
        for port in re.findall(r"port\s*=\s*([0-9]+)", colors):
            self.assertNotIn(port, sg)
        self.assertNotIn("claw_master_colors", sg)

    def test_a_server_may_drain_for_the_whole_drain_timeout(self):
        self.assertIn('balancer="--balanced --drain-timeout ${local.claw_master_drain_seconds}s"', TF)
        self.assertIn("TimeoutStopSec=${local.claw_master_drain_seconds + 60}", TF)
        self.assertRegex(TF, r"claw_master_drain_seconds = [0-9]+")

    def test_the_rollout_switches_only_after_the_new_server_listens_and_stops_the_old_only_after_envoy_switched(self):
        r = rollout()
        start = r.index('systemctl restart "claude-master-server@$next"')
        refused = r.index("nothing was switched")
        switch = r.index('/usr/local/bin/claw-master-envoy-endpoints "$next"')
        verified = r.index("Envoy did not report")
        record = r.index('echo "$next" > /etc/claude-master/active-color')
        stop = r.index('systemctl stop --no-block "claude-master-server@$active"')
        self.assertLess(start, refused)
        self.assertLess(refused, switch)
        self.assertLess(switch, verified)
        self.assertLess(verified, record)
        self.assertLess(record, stop)
        # Envoy is never restarted or reloaded; it is stopped in one place only: when it never came up during the
        # first rollout, so the pre-Envoy server can have its port back.
        self.assertNotRegex(r, r"systemctl (restart|reload) envoy")
        self.assertEqual(r.count("systemctl stop envoy"), 1)
        self.assertLess(r.index("Envoy did not start listening"), r.index("systemctl stop envoy"))

    def test_the_first_rollout_never_leaves_clients_without_a_listener(self):
        r = rollout()
        validate = r.index("/usr/local/bin/envoy --mode validate -c /etc/envoy/envoy.yaml")
        stop_legacy = r.index("systemctl stop claude-master-server.service")
        back = r.index("systemctl start claude-master-server.service")
        remove = r.index("rm -f /etc/systemd/system/claude-master-server.service")
        self.assertLess(validate, stop_legacy)
        self.assertLess(stop_legacy, back)
        self.assertLess(back, remove)  # the pre-Envoy unit is kept until Envoy listens

    def test_a_failed_switch_points_envoy_back_at_the_recorded_color(self):
        # If Envoy does not report the new endpoints, the record and Envoy must agree again before the rollout
        # fails, or a retry would restart the server that is taking traffic.
        r = rollout()
        failed = r[r.index('if [ "$switched" != 1 ]; then'):r.index('echo "$next" > /etc/claude-master/active-color')]
        back = failed.index('/usr/local/bin/claw-master-envoy-endpoints "$active"')
        stop = failed.index('systemctl stop "claude-master-server@$next"')
        self.assertLess(back, stop)
        self.assertIn('grep -q "^cert::$IP:$(port_of "$active")::"', failed)
        self.assertIn("exit 1", failed[stop:])

    def test_the_endpoint_files_are_replaced_by_a_rename(self):
        e = TF[TF.index("cat > /usr/local/bin/claw-master-envoy-endpoints"):TF.index("chmod 0755 /usr/local/bin/claw-master-envoy-endpoints")]
        self.assertIn("tmp=$(mktemp /etc/envoy/eds/.$1.XXXXXX)", e)
        self.assertIn('mv -f "$tmp" "/etc/envoy/eds/$1.yaml"', e)

    def test_without_a_color_the_server_is_the_one_from_before_envoy(self):
        serve = TF[TF.index("cat > /usr/local/bin/claw-master-serve"):TF.index("chmod 0755 /usr/local/bin/claw-master-serve")]
        self.assertIn("color=$${1:-legacy}", serve)
        self.assertIn('legacy) port=${local.claw_master_server_port}; open=${local.claw_master_open_port}; balancer="" ;;', serve)


class LoginDefaultsTests(unittest.TestCase):
    """A login refreshed on two boxes revokes the other copy: a server-only subscription stays off fcvm's default."""

    def test_claude_colby_is_in_the_server_default_only(self):
        script = (ROOT / "scripts" / "claw-master-login.sh").read_text()
        server = re.search(r'if \[ "\$SERVER" = 1 \]; then profiles=\(([^)]*)\)', script).group(1).split()
        other = re.search(r'else profiles=\(([^)]*)\); fi', script).group(1).split()
        self.assertIn("claude-colby", server)
        self.assertNotIn("claude-colby", other)
        self.assertEqual(set(server) - set(other), {"claude-colby"})


class SubscriptionDiscoveryTests(unittest.TestCase):
    """A subscription is added by logging it in: the server serves every login there is, the preferred ones first."""

    def serve_args(self, present):
        import os, tempfile
        serve = TF[TF.index("cat > /usr/local/bin/claw-master-serve <<'EOF'\n"):]
        serve = serve[serve.index("\n") + 1:serve.index("\nEOF\n")]
        preferred = " ".join(re.findall(r'"([a-z0-9-]+)"', re.search(r"claw_master_profiles\s*=\s*\[(.*?)\]", TF).group(1)))
        serve = (serve.replace('${join(" ", local.claw_master_profiles)}', preferred).replace("${var.aws_region}", "us-west-1")
                 .replace("$${", "${").replace("${local.claw_master_drain_seconds}", "600").replace("${local.claw_master_server_ip}", "10.0.1.50")
                 .replace("${local.claw_master_server_port}", "8443").replace("${local.claw_master_open_port}", "8444"))
        serve = re.sub(r"\$\{join\(.*\)\}", "", serve)
        with tempfile.TemporaryDirectory() as d:
            bindir = os.path.join(d, "bin"); os.mkdir(bindir)
            open(os.path.join(bindir, "aws"), "w").write("#!/bin/bash\necho\n")
            open(os.path.join(bindir, "claw-master"), "w").write('#!/bin/bash\nargs=(); while [ $# -gt 0 ] && [ "$1" != --listen ]; do args+=("$1"); shift; done; echo "${args[*]}"\n')
            for n in ("aws", "claw-master"):
                os.chmod(os.path.join(bindir, n), 0o755)
            profiles = os.path.join(d, "profiles")
            os.makedirs(profiles)
            for name in present:
                os.makedirs(os.path.join(profiles, name, "current"))
            os.makedirs(os.path.join(profiles, "claude-halfway", ".login-123"))  # a login that never finished
            env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"], CLAW_MASTER_PROFILE_DIR=profiles)
            r = subprocess.run(["bash", "-c", serve.replace("/usr/local/bin/", bindir + "/"), "serve", "legacy"], capture_output=True, text=True, env=env, timeout=30)
            return r.returncode, r.stdout.strip(), r.stderr

    def test_preferred_first_missing_skipped_new_ones_after(self):
        code, out, _ = self.serve_args(["claude-thao", "claude-connor", "claude-zed", "claude-amy"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "serve claude-connor --next-profile claude-thao --next-profile claude-amy --next-profile claude-zed")

    def test_no_login_at_all_refuses_to_start(self):
        code, out, err = self.serve_args([])
        self.assertNotEqual(code, 0)
        self.assertIn("no subscription login", err)

    def test_the_add_helpers_take_the_code_on_stdin_and_go_live_by_rollout(self):
        start = TF[TF.index("cat > /usr/local/bin/claw-master-add-start"):TF.index("chmod 0755 /usr/local/bin/claw-master-add-start")]
        finish = TF[TF.index("cat > /usr/local/bin/claw-master-add-finish"):TF.index("chmod 0755 /usr/local/bin/claw-master-add-finish")]
        for script in (start, finish):
            self.assertIn("[[ $name =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$ ]]", script)
        self.assertIn("already has a login; use a new name", start)
        self.assertIn("timeout 7200 /usr/local/bin/claw-master login", start)
        self.assertIn("IFS= read -r code", finish)            # stdin, never an argument
        self.assertNotIn("$2", finish)
        self.assertIn('[ ! -d "$profdir/$name/current" ]', finish)  # no rollout unless the login was installed
        self.assertLess(finish.index('[ ! -d "$profdir/$name/current" ]'), finish.index("/usr/local/bin/claw-master-rollout"))


class LoginCountTests(unittest.TestCase):
    """The login script rolls out after a new login; its count must be one number even when there are none."""

    def count(self, status_lines, ssh_ok=True):
        import os, tempfile
        fn = LOGIN[LOGIN.index("count_logins() {"):LOGIN.index("\n", LOGIN.index("count_logins() {"))]
        with tempfile.TemporaryDirectory() as d:
            status = os.path.join(d, "status"); open(status, "w").write("".join(l + "\n" for l in status_lines))
            ssh = os.path.join(d, "ssh")
            open(ssh, "w").write('#!/bin/bash\n%s\nsudo() { shift; cat "%s"; }\nexport -f sudo 2>/dev/null\nbash -c "sudo() { cat %s; }; ${@: -1}"\n' % ("" if ssh_ok else "exit 255", status, status))
            os.chmod(ssh, 0o755)
            env = dict(os.environ, PATH=d + ":" + os.environ["PATH"])
            return subprocess.run(["bash", "-c", fn + '\nKEY=k HOST=h; count_logins'], capture_output=True, text=True, env=env, timeout=30).stdout

    def test_one_number_with_none_some_and_no_ssh(self):
        self.assertEqual(self.count(["login claude-a: MISSING (preferred, skipped by the server)", "server: inactive (none)"]), "0\n")
        self.assertEqual(self.count(["login claude-a: present", "login claude-b: present (served after the preferred ones)"]), "2\n")
        self.assertEqual(self.count([], ssh_ok=False), "0\n")

if __name__ == "__main__":
    unittest.main(verbosity=2)

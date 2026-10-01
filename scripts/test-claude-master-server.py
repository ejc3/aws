#!/usr/bin/env python3
"""claude-master-server.tf: the shared claude-master server is private, pinned and idle until its
logins exist. Offline: reads the Terraform source and the two scripts."""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "claude-master-server.tf").read_text()
TUN = (ROOT / "claude-master-tunnel.tf").read_text()
BUNDLE = (ROOT / "scripts" / "claude-master-mac-bundle.sh").read_text()
MACSETUP = (ROOT / "scripts" / "claude-master-mac-setup.sh").read_text()
ENROLL = (ROOT / "scripts" / "claude-master-enroll.sh").read_text()
CONVERGE = (ROOT / "scripts" / "ssm-claude-master-server.sh").read_text()
LOGIN = (ROOT / "scripts" / "claude-master-login.sh").read_text()
TUNNEL = (ROOT / "scripts" / "claude-master-tunnel.sh").read_text()
DASH = (ROOT / "claude-master-dashboard.tf").read_text()


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


class ObservabilityTests(unittest.TestCase):
    """Logs and metrics: info level, rotated, redacted at the source, shipped by a pinned agent that
    listens on loopback only, with an IAM grant that can publish to one namespace and write one log group."""

    def test_the_agent_is_pinned_and_checked_before_it_is_installed(self):
        self.assertRegex(TF, r'claude_master_cwagent_version\s+=\s+"[0-9.]+b[0-9a-f]+"')
        self.assertRegex(TF, r'claude_master_cwagent_sha256_arm64\s+=\s+"[0-9a-f]{64}"')
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
        self.assertNotIn("4318", block("aws_security_group", "claude_master_server"))

    def test_the_server_always_logs_at_info_to_a_rotated_file_it_may_write(self):
        wrapper = TF[TF.index("cat > /usr/local/bin/claude-master-serve"):TF.index("chmod 0755 /usr/local/bin/claude-master-serve")]
        for flag in ("--log-level info", "--log-file /var/log/claude-master/server.log", "--log-max-mb 20", "--log-keep 5",
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
        for name in ("claude_master_server_memory", "claude_master_server_swap"):
            alarm = block("aws_cloudwatch_metric_alarm", name)
            self.assertNotIn("dimensions", alarm)
            self.assertIn("namespace           = local.claude_master_metrics_namespace", alarm)

    def test_the_agent_ships_the_exact_log_file_not_its_rotations(self):
        self.assertIn('"file_path": "/var/log/claude-master/server.log"', TF)
        self.assertNotIn("server.log*", TF)

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
        policy = block("aws_iam_role_policy", "claude_master_server_telemetry")
        self.assertRegex(policy, r'"cloudwatch:PutMetricData"')
        self.assertIn('"cloudwatch:namespace" = local.claude_master_metrics_namespace', policy)
        self.assertIn("aws_cloudwatch_log_group.claude_master_server.arn", policy)
        self.assertNotRegex(policy, r"logs:CreateLogGroup|logs:DeleteLogGroup|logs:\*|cloudwatch:\*")
        # The only wildcard resource is PutMetricData, which has no resource-level scope: the condition holds it.
        self.assertEqual(policy.count('Resource = "*"'), 1)
        self.assertLess(policy.index('Resource = "*"'), policy.index("WriteItsOwnLogGroup"))

    def test_the_log_group_is_kept_and_two_log_alarms_exist(self):
        self.assertIn("retention_in_days = 90", block("aws_cloudwatch_log_group", "claude_master_server"))
        for key in ("CredentialRejected", "NoAccountAvailable"):
            self.assertIn(key + " = {", TF)
        for message in ("profile credential rejected by Anthropic", "no inference account could be chosen"):
            self.assertIn(message, TF)
        for name in ("claude_master_server_status", "claude_master_server_memory", "claude_master_server_swap", "claude_master_server_log"):
            self.assertIn("aws_sns_topic.cost_alerts.arn", block("aws_cloudwatch_metric_alarm", name))


class DashboardTests(unittest.TestCase):
    """The dashboard and its alarms read what the proxy publishes. Every query here was checked against the live
    CloudWatch API; these tests keep them valid and cheap."""

    # The metrics the proxy emits (docs/claude-master.md in ejc3/CLIProxyAPI) and the host metrics of the agent.
    METRICS = {
        "claude_master.inference.requests", "claude_master.inference.requests.by_model",
        "claude_master.inference.requests.by_client", "claude_master.inference.errors",
        "claude_master.inference.duration", "claude_master.inference.duration.by_model",
        "claude_master.inference.ttfb", "claude_master.inference.ttfb.by_model",
        "claude_master.inference.upstream_ttfb", "claude_master.inference.duration_quantile",
        "claude_master.proxy.overhead", "claude_master.proxy.connections", "claude_master.proxy.tls_handshake_errors",
        "claude_master.quota.used_fraction", "claude_master.quota.resets_in_seconds", "claude_master.quota.rate_limited",
        "claude_master.anthropic.ratelimit", "claude_master.routing.switches", "claude_master.routing.backup_requests",
        "claude_master.auth.token_expires_in_seconds", "claude_master.auth.refresh", "claude_master.usage.polls",
        "claude_master.process.heap_bytes", "claude_master.process.goroutines",
        "mem_used_percent", "swap_used_percent", "CredentialRejected", "NoAccountAvailable",
    }
    # Names CloudWatch accepted unquoted in GROUP BY / WHERE. Anything else (window and result are reserved
    # words, which the API rejected) must be double quoted.
    SAFE = {"profile", "client_account", "client", "model", "status_class", "status", "route", "reason", "quantile", "measure"}

    def queries(self):
        found = re.findall(r'"(SELECT (?:[^"\\]|\\.)*)"', DASH)
        return [q.replace('\\"', '"').replace("${local.cm_ns}", "ClaudeMaster") for q in found]

    def test_there_are_queries_and_every_one_reads_the_agents_namespace_and_a_known_metric(self):
        queries = self.queries()
        self.assertGreaterEqual(len(queries), 30)
        for q in queries:
            self.assertRegex(q, r'^SELECT (SUM|AVG|MIN|MAX)\(.+\) FROM "ClaudeMaster(/Logs)?"')
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
        alarm = block_in(DASH, "aws_cloudwatch_metric_alarm", "claude_master_pool")
        self.assertIn("metric_query", alarm)
        self.assertNotIn("dimensions", alarm)
        self.assertIn("aws_sns_topic.cost_alerts.arn", alarm)
        self.assertIn('SELECT MIN(\\"claude_master.quota.used_fraction\\")', alarm)
        self.assertIn("threshold           = each.value", alarm)

    def test_the_dashboard_exists_once_and_shows_the_alarms(self):
        self.assertEqual(DASH.count('resource "aws_cloudwatch_dashboard"'), 1)
        self.assertIn('type = "alarm"', DASH)
        self.assertIn("aws_cloudwatch_metric_alarm.claude_master_pool", DASH)


def block_in(text, kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n.*?^\}' % (kind, name), text, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


class ConvergenceTests(unittest.TestCase):
    """The instance ignores user_data, so Terraform itself must re-run the bootstrap."""

    def test_a_script_or_pin_or_resize_change_re_runs_the_bootstrap(self):
        res = block("terraform_data", "claude_master_server_converge")
        for trigger in ("aws_instance.claude_master_server[0].id", "aws_instance.claude_master_server[0].instance_type",
                        "sha256(local.claude_master_server_user_data)", 'filesha256("${path.module}/scripts/ssm-claude-master-server.sh")'):
            self.assertIn(trigger, res)
        self.assertIn("scripts/ssm-claude-master-server.sh", res)
        self.assertIn("aws_s3_object.claude_master_server_user_data", res)
        # cloudflared reads its token at start: the secret and the role's grant must exist first.
        self.assertIn("aws_secretsmanager_secret_version.claude_master_tunnel_token", res)
        self.assertIn("aws_iam_role_policy.claude_master_server_tunnel_token", res)

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
        self.assertIn("--open-loopback 127.0.0.1:${local.claude_master_open_port}", TF)
        self.assertNotRegex(TF, r"--open-loopback\s+(0\.0\.0\.0|\$\{local\.claude_master_server_ip\})")
        sg = block("aws_security_group", "claude_master_server")
        self.assertNotIn("claude_master_open_port", sg)

    def test_the_tunnel_forwards_to_loopback_over_tcp(self):
        cfg = tblock("cloudflare_zero_trust_tunnel_cloudflared_config", "claude_master")
        self.assertIn('service  = "tcp://127.0.0.1:${local.claude_master_open_port}"', cfg)
        self.assertIn("http_status:404", cfg)  # anything else gets nothing
        self.assertIn("cloudflare_zero_trust_access_application.claude_master", cfg)  # no routing before Access

    def test_only_a_service_token_may_reach_it_no_person_and_no_wildcard(self):
        pol = tblock("cloudflare_zero_trust_access_policy", "claude_master_macs")
        self.assertIn('decision         = "non_identity"', pol)
        self.assertIn("service_token", pol)
        self.assertNotRegex(pol, r"email|everyone|group|login_method")
        app = tblock("cloudflare_zero_trust_access_application", "claude_master")
        self.assertIn("domain           = local.claude_master_tunnel_hostname", app)
        self.assertEqual(app.count("id         ="), 1, "exactly one policy")
        self.assertNotIn("*", re.search(r'claude_master_tunnel_hostname\s+=\s+"([^"]+)"', TUN).group(1))

    def test_the_two_secrets_have_different_readers(self):
        conn = tblock("aws_secretsmanager_secret_policy", "claude_master_tunnel_token")
        self.assertIn("aws_iam_role.claude_master_server[0].arn", conn)
        mac = tblock("aws_secretsmanager_secret_policy", "claude_master_mac_access")
        self.assertNotIn("claude_master_server", mac)  # the server never needs the Macs' credential
        for p in (conn, mac):
            self.assertIn('Effect    = "Deny"', p)
            self.assertIn("local.games_mp_admin_principals", p)

    def test_cloudflared_is_pinned_and_gets_its_token_from_the_environment(self):
        self.assertRegex(TUN, r'cloudflared_sha256_linux_arm\s+=\s+"[0-9a-f]{64}"')
        self.assertIn("sha256sum -c -", TF)
        script = TF[TF.index("claude_master_server_user_data"):TF.index('resource "aws_s3_object"')]
        self.assertIn("export TUNNEL_TOKEN", script)
        self.assertNotRegex(script, r"cloudflared[^\n]*--token")
        self.assertIn("{ set +x; } 2>/dev/null", script[script.index("cloudflared-claude-master <<"):])

    def test_a_dead_tunnel_is_never_handed_out(self):
        # A running server keeps its old configuration; the tunnel can be live with nothing behind it.
        script = TF[TF.index("claude_master_server_user_data"):TF.index('resource "aws_s3_object"')]
        self.assertIn("open listener: listening", script)
        self.assertIn("open listener: NOT listening", script)
        self.assertIn("grep -qx 'open listener: listening'", code(BUNDLE))
        self.assertLess(code(BUNDLE).index("open listener: listening"), code(BUNDLE).index("ca.pem"))
        self.assertNotRegex(code(BUNDLE), r"systemctl\s+(restart|stop|start)")  # the owner decides that

    def test_the_macs_get_one_file_and_no_certificate_or_aws_access(self):
        self.assertIn("claude-master/mac-access", BUNDLE)
        self.assertIn("chmod 0600", BUNDLE)
        self.assertIn("claude-master connect --open 127.0.0.1", BUNDLE)
        self.assertNotRegex(code(BUNDLE), r"client\.key|client-init")
        self.assertIn("cloudflared access tcp", BUNDLE)


class MacSetupTests(unittest.TestCase):
    """Setting a Mac up over its reverse tunnel: additive, pinned, token through stdin only."""

    def pin(self, name):
        return re.search(r"^%s=(\S+)$" % name, MACSETUP, re.M).group(1)

    def test_the_pinned_tag_matches_the_servers(self):
        server_tag = re.search(r'claude_master_tag\s+=\s+"([^"]+)"', TF).group(1)
        self.assertEqual(self.pin("CM_TAG"), server_tag, "bump both together")
        for name in ("CM_SHA256_DARWIN_ARM64", "CFD_SHA256_DARWIN_ARM64_TGZ", "CFD_SHA256_DARWIN_ARM64_BIN"):
            self.assertRegex(self.pin(name), r"^[0-9a-f]{64}$")
        cfd = re.search(r'cloudflared_version\s+=\s+"([^"]+)"', TUN).group(1)
        self.assertEqual(self.pin("CFD_VERSION"), cfd)

    def test_binaries_are_verified_on_the_mac_before_they_are_installed(self):
        text = code(MACSETUP)
        # claude-master, the cloudflared tarball, and the cloudflared executable inside it
        self.assertEqual(text.count("did not match its pinned sha256"), 3)
        self.assertLess(text.index("claude-master did not match"), text.index('mv -f "\\$tmp/cm"'))
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
        self.assertIn("claude-master-status", text[gate:text.index("1/6")])
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
        for path in ("claude-master-enroll.sh", "claude-master-login.sh", "claude-master-tunnel.sh", "claude-master-mac-bundle.sh", "claude-master-mac-setup.sh"):
            r = subprocess.run(["bash", "-n", str(ROOT / "scripts" / path)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

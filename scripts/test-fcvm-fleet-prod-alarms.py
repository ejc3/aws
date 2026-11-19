#!/usr/bin/env python3
"""Offline tests for fcvm-fleet-prod-alarms.tf and scripts/fcvm-fleet-prod-watch.py (ejc3/fcvm fleet production plan, A5):
every prod alarm exists only with the prod stack and reaches the fleet's forwarder, reads the coordinator's metrics with
Fleet=prod or a log line the coordinator writes, and the watch function counts what it should and nothing else."""
import base64
import hashlib
import importlib.util
import json
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
ALARMS = (ROOT / "fcvm-fleet-prod-alarms.tf").read_text()
FLEET = (ROOT / "fcvm-fleet.tf").read_text()
PROD = (ROOT / "fcvm-fleet-prod.tf").read_text()
FIXTURE = ROOT / "scripts/fixtures/fcvm-coordinator-prod-log-lines.txt"


def block(header, text=ALARMS):
    start = text.index(header)
    return text[start:text.index("\n}\n", start) + 2]


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")) + "\n"


def log_entry(name):
    return ALARMS.split(f'"${{local.fcvm_fleet_prod_alarm_prefix}}-{name}" = {{', 1)[1].split("\n    }", 1)[0]


def field(entry, name):
    return re.search(rf"(?m)^\s+{name}\s+= (.*)$", entry)[1]


class GateTests(unittest.TestCase):
    def test_every_resource_exists_only_with_the_prod_stack(self):
        gates = (r"count\s+= local\.fcvm_fleet_prod\n", r"count\s+= local\.fcvm_fleet_prod_broadcast\n",
                 r"for_each\s+= local\.fcvm_fleet_prod_alarm_arns\n", r"for_each\s+= var\.fcvm_fleet_prod_enabled \?",
                 r"for_each\s+= aws_cloudwatch_log_metric_filter\.fcvm_fleet_prod\n",
                 r"for_each\s+= aws_cloudwatch_event_rule\.fcvm_fleet_prod_\w+\n",
                 r"for_each\s+= \{ for k, v in local\.fcvm_fleet_prod_\w+ : k => v if var\.fcvm_fleet_prod_enabled \}\n")
        found = re.findall(r'(?m)^resource "(\w+)" "(\w+)" \{\n(.*?)\n\}\n', code(ALARMS), re.S)
        self.assertGreater(len(found), 25)
        for kind, name, body in found:
            self.assertTrue(any(re.search(g, body + "\n") for g in gates), f"{kind}.{name} is not gated")
        self.assertIn("fcvm_fleet_prod_alarm_names = var.fcvm_fleet_prod_enabled ? concat(", ALARMS)
        self.assertRegex(ALARMS, r"fcvm_fleet_prod_template_ids\s+= var\.fcvm_fleet_prod_enabled \? \[")

    def test_every_alarm_reaches_the_forwarder_and_the_forwarder_knows_it(self):
        alarms = re.findall(r'(?ms)^resource "aws_cloudwatch_metric_alarm" "(\w+)" \{\n(.*?)\n\}\n', ALARMS)
        self.assertEqual(len(alarms), 11)
        names = ALARMS.split("fcvm_fleet_prod_alarm_names = ", 1)[1].split("\n  ) : []\n", 1)[0]
        for name, body in alarms:
            self.assertRegex(body, r"alarm_actions\s+= local\.fcvm_fleet_prod_notify\n", name)
            self.assertRegex(body, r"depends_on\s+= \[aws_lambda_permission\.fcvm_fleet_prod_alarms\]", name)
            literal = re.search(r'alarm_name\s+= "\$\{local\.fcvm_fleet_prod_alarm_prefix\}-([a-z0-9-]+)"', body)
            if literal:
                self.assertIn(literal[1], names, name)
        self.assertIn('fcvm_fleet_prod_notify = [aws_lambda_function.fcvm_fleet_on_demand_notify.arn]', ALARMS)
        self.assertIn("keys(local.fcvm_fleet_prod_log_alarms)", names)
        self.assertIn("values(local.fcvm_fleet_prod_alarm_arns)", block('resource "aws_lambda_function" "fcvm_fleet_on_demand_notify"', FLEET))
        permission = block('resource "aws_lambda_permission" "fcvm_fleet_prod_alarms"')
        self.assertIn('principal      = "lambda.alarms.cloudwatch.amazonaws.com"', permission)
        self.assertIn("source_arn     = each.value", permission)


class MetricAlarmTests(unittest.TestCase):
    def test_the_service_and_dns_alarms_watch_prods_own_and_breach_on_missing_data(self):
        ecs = block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_coordinator_not_running"')
        self.assertIn("ClusterName = aws_ecs_cluster.fcvm_fleet_prod[0].name", ecs)
        self.assertIn("ServiceName = local.fcvm_fleet_prod_coordinator", ecs)
        self.assertIn('statistic           = "SampleCount"', ecs)
        dns = block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_dns_no_healthy_target"')
        self.assertIn("LoadBalancer = aws_lb.fcvm_fleet_prod_dns[0].arn_suffix", dns)
        self.assertIn("TargetGroup  = aws_lb_target_group.fcvm_fleet_prod_dns[0].arn_suffix", dns)
        for alarm in (ecs, dns):
            self.assertIn("evaluation_periods  = 5", alarm)
            self.assertIn('treat_missing_data  = "breaching"', alarm)

    def test_emf_alarms_read_fleet_prod_with_the_owners_thresholds(self):
        self.assertRegex(ALARMS, r"fcvm_fleet_prod_dimension\s+= \{ Fleet = local\.fcvm_fleet_prod_stack\.fleet \}")
        spend = block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_spend_80_percent"')
        self.assertIn('metric_name         = "SpendUsdToday"', spend)
        self.assertIn("threshold           = local.fcvm_fleet_prod_daily_spend_usd * 0.8", spend)
        self.assertIn("fcvm_fleet_prod_daily_spend_usd = 750", PROD)
        cert = block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_certificate_expiring"')
        for line in ('metric_name         = "CertDaysLeft"', 'statistic           = "Minimum"', "threshold           = 21",
                     'comparison_operator = "LessThanThreshold"', 'treat_missing_data  = "breaching"'):
            self.assertIn(line, cert)
        for name in ("spend_80_percent", "certificate_expiring"):
            self.assertIn("dimensions          = local.fcvm_fleet_prod_dimension",
                          block(f'resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_{name}"'))

    def test_the_awake_alarms_are_metric_math_over_five_minutes(self):
        cases = {
            "hosts_short": ("IF(power == 2 AND hosts < ${local.fcvm_fleet_prod_awake_hosts}, 1, 0)", "breaching",
                            {"power": ("Power", "Minimum"), "hosts": ("AwakeHosts", "Maximum")}),
            "wake_slow": ("IF(low == 1 AND high == 1, 1, 0)", "notBreaching", None),
            "broadcast_off": ("IF(power == 2 AND live == 0, 1, 0)", "notBreaching",
                              {"power": ("Power", "Minimum"), "live": ("BroadcastLive", "Maximum")}),
        }
        for name, (expression, missing, queries) in cases.items():
            alarm = block(f'resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_{name}"')
            self.assertIn(f'expression  = "{expression}"', alarm, name)
            self.assertIn(f'treat_missing_data  = "{missing}"', alarm, name)
            for line in ("evaluation_periods  = 5", "datapoints_to_alarm = 5", "threshold           = 1", "period      = 60",
                         'namespace   = "FCVM/Fleet"', "dimensions  = local.fcvm_fleet_prod_dimension"):
                self.assertIn(line, alarm, name)
            if queries:
                want = "{ " + ", ".join(f'{k} = ["{m}", "{s}"]' for k, (m, s) in queries.items()) + " }"
                self.assertIn(f"for_each = {want}", alarm, name)
        self.assertIn('for_each = { low = "Minimum", high = "Maximum" }', block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_wake_slow"'))
        self.assertIn("fcvm_fleet_prod_awake_hosts    = 2", PROD)
        self.assertIn("count               = local.fcvm_fleet_prod_broadcast",
                      block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_broadcast_off"'))

    def test_emf_names_are_the_ones_the_coordinator_emits(self):
        # ejc3/fcvm src/fleet/coord/metrics.rs at the fixture's revision.
        for metric in ("SpendUsdToday", "CertDaysLeft", "Power", "AwakeHosts", "BroadcastLive"):
            self.assertIn(f'"{metric}"', ALARMS)
        self.assertNotRegex(ALARMS, r'"(Power|AwakeHosts|BroadcastLive|CertDaysLeft|SpendUsdToday)", ?"Sum"')


class LogAlarmTests(unittest.TestCase):
    PHRASES = {
        "state-not-written": ["state not written", "the last state was not stored"],
        "state-write-lost": ["FLEET_STATE_WRITE_LOST"],
        "state-unreadable": ["FLEET_STATE_UNREADABLE"],
        "coordinator-degraded": ["listing the GPU hosts failed", "terminating GPU hosts failed", "launching GPU hosts failed",
                                 "EC2 refused a GPU host launch", "asking a lost GPU host launch again failed",
                                 "text check failed"],
        "certificate-renewal-failed": ["certificate renewal failed", "the renewed certificate was not stored",
                                       "the certificate secret could not be read"],
        "agent-refused": ["fleet-agent-refused"],
        "turnstile-siteverify-errors": ["turnstile-siteverify-error"],
        "forced-sleep": ["fleet asleep", "budget"],
        "on-demand-host-launches": ["wake: host launch accepted", "on-demand"],
        "coordinator-restarts": ["coordinator serving /live"],
    }

    def test_patterns_are_strings_the_coordinator_writes_at_the_pinned_revision(self):
        revision, source = FIXTURE.read_text().split("\n", 1)
        self.assertRegex(revision, r"^[0-9a-f]{40}$")
        for name, want in self.PHRASES.items():
            pattern = field(log_entry(name), "pattern")
            self.assertEqual(re.findall(r'\\"([^"\\]+)\\"', pattern), want, name)
            for phrase in want:
                if name == "forced-sleep" and phrase == "budget":
                    continue
                self.assertIn('"' + phrase, source, f"{name}: {phrase!r} is missing from ejc3/fcvm at {revision}")
        # The two markers are tracing fields, so they are logged as marker="<value>" at these sites.
        for site in ("agents.rs:138:                marker = AGENT_REFUSED_MARKER,",
                     "live.rs:267:                marker = super::agents::AGENT_REFUSED_MARKER,",
                     "live.rs:864:                            marker = SITEVERIFY_ERROR_MARKER,"):
            self.assertIn(site, source)
        # "budget" is the value of the fleet asleep line's reason field (spend.rs: BUDGET), and no other field there.
        self.assertIn("mod.rs:328:                reason = ?f.power.reason,", source)
        self.assertIn("mod.rs:330:                \"fleet asleep\"", source)

    def test_alternatives_and_conjunctions_are_written_as_intended(self):
        for name, want in self.PHRASES.items():
            pattern = field(log_entry(name), "pattern")
            if name in ("forced-sleep", "on-demand-host-launches"):
                self.assertFalse(pattern.startswith('"?'), name)
                self.assertNotIn("?", pattern, name)  # both terms must appear
            elif len(want) > 1:
                self.assertEqual(pattern.count("?"), len(want), name)

    def test_thresholds_and_metric_names(self):
        want = {"turnstile-siteverify-errors": ("300", "5"), "coordinator-restarts": ("600", "2"),
                "on-demand-host-launches": ("60", "1"), "agent-refused": ("300", "1"), "forced-sleep": ("300", "1"),
                "state-write-lost": ("300", "1"), "state-unreadable": ("300", "1")}
        for name, (period, threshold) in want.items():
            entry = log_entry(name)
            self.assertEqual(field(entry, "period"), period, name)
            self.assertEqual(field(entry, "threshold"), threshold, name)
        metrics = re.findall(r'(?m)^      metric = "(\w+)"$', ALARMS)
        self.assertEqual(len(metrics), len(self.PHRASES) + 1)  # and the tunnel
        self.assertEqual(len(set(metrics)), len(metrics))
        for metric in metrics:
            self.assertTrue(metric.startswith("Prod"), metric)
        f = block('resource "aws_cloudwatch_log_metric_filter" "fcvm_fleet_prod"')
        self.assertIn("log_group_name = aws_cloudwatch_log_group.fcvm_fleet_prod_coordinator[0].name", f)
        alarm = block('resource "aws_cloudwatch_metric_alarm" "fcvm_fleet_prod_logs"')
        self.assertIn('treat_missing_data  = "notBreaching"', alarm)
        self.assertIn("threshold           = local.fcvm_fleet_prod_log_alarms[each.key].threshold", alarm)


class ChangeRuleTests(unittest.TestCase):
    def test_release_role_names_match_plan_a4(self):
        self.assertIn('"ecs.amazonaws.com" = ["arn:aws:iam::${local.fcvm_fleet_account_id}:role/fcvm-coordinator-release-prod"]', ALARMS)
        self.assertIn('"ec2.amazonaws.com" = ["arn:aws:iam::${local.fcvm_fleet_account_id}:role/fcvm-fleet-ami-prod-release"]', ALARMS)
        self.assertIn('assumed-role/fcvm-coordinator-release-prod/', ALARMS)
        self.assertIn('assumed-role/fcvm-fleet-ami-prod-release/', ALARMS)
        self.assertIn('fcvm_fleet_prod_worker_release_token_name = "fcvm-prod-workers-deploy"', ALARMS)

    def test_template_changes_are_forwarded_from_us_east_1_to_the_home_bus(self):
        east1 = block('resource "aws_cloudwatch_event_target" "fcvm_fleet_prod_template_changes_east1"')
        self.assertIn("provider = aws.fcvm_fleet_east1", east1)
        self.assertIn('arn      = "arn:aws:events:${local.fcvm_fleet_home_region}:${local.fcvm_fleet_account_id}:event-bus/default"', east1)
        self.assertIn("local.fcvm_fleet_prod_change_patterns.templates.pattern",
                      block('resource "aws_cloudwatch_event_rule" "fcvm_fleet_prod_template_changes_east1"'))
        target = block('resource "aws_cloudwatch_event_target" "fcvm_fleet_prod_changes"')
        self.assertIn("arn        = aws_lambda_function.fcvm_fleet_prod_watch[0].arn", target)
        for want in ('eventName         = ["UpdateService"]', 'eventName    = ["ModifyLaunchTemplate", "CreateLaunchTemplateVersion"]'):
            self.assertIn(want, ALARMS)

    def test_the_watch_role_can_put_fleet_metrics_and_read_one_secret_only(self):
        policy = block('resource "aws_iam_role_policy" "fcvm_fleet_prod_watch"')
        self.assertEqual(re.findall(r'Action\s+= (.+)', policy),
                         ['["logs:CreateLogStream", "logs:PutLogEvents"]', '"cloudwatch:PutMetricData"', '"secretsmanager:GetSecretValue"'])
        self.assertIn('"cloudwatch:namespace" = "FCVM/Fleet"', policy)
        token = block('resource "cloudflare_account_token" "fcvm_fleet_prod_audit_read"')
        self.assertEqual(re.findall(r"id = (local\.\w+)", token), ["local.account_settings_read"])


# ------------------------------------------------------------------------------------------------ the function
class Watch(unittest.TestCase):
    ENV = {
        "FLEET": "prod", "PAGE_URL": "", "LIVE_URL": "wss://fleet-api.example.test/live", "ORIGIN": "https://example.test",
        "CF_TOKEN_SECRET": "audit", "CF_ACCOUNT_ID": "0123456789abcdef", "WORKER": "fcvm-fleet",
        "RELEASE_TOKEN_NAME": "fcvm-prod-workers-deploy", "WINDOW_MINUTES": "20",
        "PROD_SERVICE": "fcvm-fleet-prod-coordinator", "PROD_TEMPLATE_IDS": json.dumps(["lt-0prod"]),
        "PROD_TEMPLATE_PREFIX": "fcvm-fleet-prod-",
        "RELEASE_ROLES": json.dumps({"ecs.amazonaws.com": ["arn:aws:iam::000000000001:role/fcvm-coordinator-release-prod"],
                                     "ec2.amazonaws.com": ["arn:aws:iam::000000000001:role/fcvm-fleet-ami-prod-release"]}),
    }

    def setUp(self):
        self.sdk = mock.Mock()
        spec = importlib.util.spec_from_file_location("prod_watch", ROOT / "scripts/fcvm-fleet-prod-watch.py")
        self.watch = importlib.util.module_from_spec(spec)
        with mock.patch.dict("sys.modules", {"boto3": self.sdk}):
            spec.loader.exec_module(self.watch)
        env = mock.patch.dict("os.environ", self.ENV)
        env.start()
        self.addCleanup(env.stop)
        self.watch.print = lambda *a, **k: None

    def metrics(self):
        return [(c.kwargs["MetricData"][0]["MetricName"], c.kwargs["MetricData"][0]["Value"],
                 c.kwargs["MetricData"][0]["Dimensions"], c.kwargs["Namespace"])
                for c in self.sdk.client.return_value.put_metric_data.call_args_list]


def audit(uri, method="POST", token="fcvm-prod-workers-deploy", context="api_token", action="create", **extra):
    entry = {"action": {"type": action, "result": "success", "time": "2026-11-19T12:00:00Z"},
             "actor": {"context": context, "token_name": token, "token_id": "t1", "type": "account"},
             "raw": {"method": method, "uri": uri}, "resource": {"product": "workers", "id": "x"}}
    entry.update(extra)
    return entry


A = "/client/v4/accounts/0123456789abcdef"


class WorkerAuditTests(Watch):
    FOREIGN = [
        audit(f"{A}/workers/scripts/fcvm-fleet", "PUT", token="workers-deploy-terraform"),            # the staging token
        audit(f"{A}/workers/scripts/fcvm-fleet/versions", token=None, context="dash"),                # the dashboard
        audit(f"{A}/workers/scripts/fcvm-fleet/deployments?force=true", token="other"),               # versions deploy
        audit(f"{A}/workers/scripts/fcvm-fleet/secrets", "PUT", token="fcvm-prod-workers-deploy", context="api_key"),
        audit(f"{A}/workers/services/fcvm-fleet/environments/production", "PATCH", token="other"),
        audit(f"{A}/workers/scripts/fcvm-fleet/settings", "", token="other", action="update"),        # no method
        audit("", "PUT", token="other", resource={"product": "Workers", "id": "fcvm-fleet"}),          # no URI
    ]
    NOT_COUNTED = [
        audit(f"{A}/workers/scripts/fcvm-fleet/deployments"),                                         # the release token
        audit(f"{A}/workers/scripts/fcvm-fleet", "PUT"),
        audit(f"{A}/workers/scripts/fcvm-fleet/deployments", "GET", token="other", action="view"),     # a read
        audit(f"{A}/workers/scripts/fcvm-fleet/deployments", "", token="other", action="view"),        # a view, no method
        audit(f"{A}/workers/scripts/fcvm-fleet-stage", "PUT", token="workers-deploy-terraform"),       # staging's Worker
        audit(f"{A}/workers/scripts/fcvm-fleet-stage/deployments", token="other"),
        audit(f"{A}/workers/scripts/other/content", "PUT", token="other"),
        audit(f"{A}/members", token="other", resource={"product": "members", "id": "fcvm-fleet"}),
    ]

    def test_writes_to_the_prod_worker_by_anything_but_the_release_token_are_counted(self):
        for entry in self.FOREIGN:
            self.assertEqual(self.watch.foreign_worker_changes([entry], "fcvm-fleet", "fcvm-prod-workers-deploy"), [entry],
                             entry["raw"])
        for entry in self.NOT_COUNTED:
            self.assertEqual(self.watch.foreign_worker_changes([entry], "fcvm-fleet", "fcvm-prod-workers-deploy"), [],
                             entry["raw"])

    def pages(self, *pages):
        bodies = iter(pages)
        self.requests = []

        class Response:
            def __init__(self, body):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps(self.body).encode()

        def opener(request, timeout):
            self.requests.append(request)
            return Response(next(bodies))
        return opener

    def test_a_poll_reads_every_page_of_the_window_and_puts_the_count(self):
        self.sdk.client.return_value.get_secret_value.return_value = {"SecretString": "secret-token\n"}
        opener = self.pages({"success": True, "result": self.FOREIGN[:2] + self.NOT_COUNTED[:3], "result_info": {"cursor": "c2"}},
                            {"success": True, "result": [self.FOREIGN[2]], "result_info": {}})
        now = datetime(2026, 11, 19, 12, 30, tzinfo=timezone.utc)
        self.assertEqual(self.watch.worker_deploys(now, opener), {"entries": 6, "outside_release": 3})
        self.assertEqual(self.metrics(), [("WorkerChangesOutsideRelease", 3, [{"Name": "Fleet", "Value": "prod"}], "FCVM/Fleet")])
        first, second = (r.full_url for r in self.requests)
        self.assertIn("/accounts/0123456789abcdef/logs/audit?since=2026-11-19T12%3A10%3A00Z&before=2026-11-19T12%3A30%3A00Z", first)
        self.assertNotIn("cursor", first)
        self.assertIn("cursor=c2", second)
        self.assertEqual(self.requests[0].get_header("Authorization"), "Bearer secret-token")

    def test_a_failed_or_partial_read_puts_no_metric(self):
        self.sdk.client.return_value.get_secret_value.return_value = {"SecretString": "t"}
        with self.assertRaisesRegex(RuntimeError, "audit log read failed"):
            self.watch.worker_deploys(None, self.pages({"success": False, "errors": [{"code": 10000}]}))
        endless = self.pages(*[{"success": True, "result": [], "result_info": {"cursor": "more"}}] * 30)
        with self.assertRaisesRegex(RuntimeError, "more than 20 pages"):
            self.watch.worker_deploys(None, endless)
        self.assertEqual(self.metrics(), [])


def trail(source, name, params, role=None, kind="AssumedRole", user=None):
    identity = {"type": kind, "arn": user or f"arn:aws:sts::000000000001:assumed-role/{role}/session"}
    if role:
        identity["sessionContext"] = {"sessionIssuer": {"arn": f"arn:aws:iam::000000000001:role/{role}"}}
    return {"detail-type": "AWS API Call via CloudTrail", "source": "aws." + source.split(".")[0],
            "detail": {"eventSource": source, "eventName": name, "requestParameters": params, "userIdentity": identity}}


class ChangeTests(Watch):
    SERVICE = {"cluster": "fcvm-fleet-prod", "service": "fcvm-fleet-prod-coordinator", "taskDefinition": "x"}
    TEMPLATE = {"CreateLaunchTemplateVersionRequest": {"LaunchTemplateId": "lt-0prod", "LaunchTemplateData": {}}}

    def outside(self, event):
        self.sdk.reset_mock()
        result = self.watch.handler(event, None)["outside_release"]
        self.assertEqual(len(self.metrics()), result)
        return result

    def test_prod_changes_by_anything_but_their_release_role_are_counted(self):
        counted = [
            trail("ecs.amazonaws.com", "UpdateService", self.SERVICE, role="OrganizationAccountAccessRole"),
            trail("ecs.amazonaws.com", "UpdateService", {"service": "arn:aws:ecs:us-east-2:000000000001:service/fcvm-fleet-prod/fcvm-fleet-prod-coordinator"}, role="fcvm-coordinator-release"),
            # The image release role may change templates, not the service, and the reverse.
            trail("ecs.amazonaws.com", "UpdateService", self.SERVICE, role="fcvm-fleet-ami-prod-release"),
            trail("ec2.amazonaws.com", "CreateLaunchTemplateVersion", self.TEMPLATE, role="fcvm-coordinator-release-prod"),
            trail("ec2.amazonaws.com", "ModifyLaunchTemplate", {"ModifyLaunchTemplateRequest": {"LaunchTemplateName": "fcvm-fleet-prod-host-x86_64"}}, role="fcvm-fleet-ami-pipeline"),
            trail("ec2.amazonaws.com", "ModifyLaunchTemplate", {"ModifyLaunchTemplateRequest": {"SetDefaultVersion": "3"}}, role="someone"),
            trail("ec2.amazonaws.com", "CreateLaunchTemplateVersion", self.TEMPLATE, kind="IAMUser", user="arn:aws:iam::000000000001:user/u"),
            trail("ec2.amazonaws.com", "CreateLaunchTemplateVersion", self.TEMPLATE, kind="Root", user="arn:aws:iam::000000000001:root"),
        ]
        for event in counted:
            self.assertEqual(self.outside(event), 1, event["detail"])
        self.assertEqual(self.metrics()[0][0], "ProdChangesOutsideRelease")

    def test_release_role_calls_and_staging_changes_are_not_counted(self):
        quiet = [
            trail("ecs.amazonaws.com", "UpdateService", self.SERVICE, role="fcvm-coordinator-release-prod"),
            trail("ec2.amazonaws.com", "CreateLaunchTemplateVersion", self.TEMPLATE, role="fcvm-fleet-ami-prod-release"),
            trail("ecs.amazonaws.com", "UpdateService", {"service": "fcvm-fleet-coordinator"}, role="OrganizationAccountAccessRole"),
            trail("ecs.amazonaws.com", "UpdateService", {"service": "fcvm-fleet-prod-coordinator-old"}, role="x"),
            trail("ec2.amazonaws.com", "ModifyLaunchTemplate", {"ModifyLaunchTemplateRequest": {"LaunchTemplateId": "lt-0stage"}}, role="fcvm-fleet-ami-pipeline"),
            trail("ec2.amazonaws.com", "ModifyLaunchTemplate", {"ModifyLaunchTemplateRequest": {"LaunchTemplateName": "fcvm-fleet-tile-x86_64"}}, role="x"),
        ]
        for event in quiet:
            self.assertEqual(self.outside(event), 0, event["detail"])

    def test_an_unknown_event_raises(self):
        with self.assertRaises(ValueError):
            self.watch.handler({"source": "aws.events"}, None)


class FakeSocket:
    """A TLS socket stand-in that answers the upgrade with the right accept and then `frames`."""

    def __init__(self, frames, status="101 Switching Protocols", accept=True):
        self.sent, self.frames, self.status, self.accept, self.buffer, self.closed = b"", frames, status, accept, b"", False

    def sendall(self, data):
        self.sent += data
        if not self.buffer and b"Sec-WebSocket-Key: " in data:
            key = data.split(b"Sec-WebSocket-Key: ", 1)[1].split(b"\r\n", 1)[0].decode()
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.buffer = (f"HTTP/1.1 {self.status}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           f"Sec-WebSocket-Accept: {accept if self.accept else 'wrong'}\r\n\r\n").encode() + self.frames

    def recv(self, n):
        out, self.buffer = self.buffer[:n], self.buffer[n:]
        return out

    def close(self):
        self.closed = True


def text_frame(message):
    data = message.encode()
    if len(data) < 126:
        return bytes([0x81, len(data)]) + data
    return bytes([0x81, 126]) + len(data).to_bytes(2, "big") + data


class SyntheticTests(Watch):
    def run_with(self, sock):
        self.sdk.reset_mock()
        return self.watch.synthetic(connect=lambda host, port: sock)

    def test_a_hello_asking_for_turnstile_passes(self):
        sock = FakeSocket(text_frame(json.dumps({"type": "hello", "viewer": "v-1", "turnstile": "site-key"})))
        self.assertEqual(self.run_with(sock), {"ok": True, "failures": []})
        self.assertEqual(self.metrics(), [("SyntheticOk", 1, [{"Name": "Fleet", "Value": "prod"}], "FCVM/Fleet")])
        self.assertIn(b"GET /live HTTP/1.1\r\nHost: fleet-api.example.test\r\n", sock.sent)
        self.assertIn(b"Origin: https://example.test\r\n", sock.sent)
        self.assertTrue(sock.closed)
        # A long hello takes the 16-bit length.
        long = json.dumps({"type": "hello", "viewer": "v-" + "1" * 300, "turnstile": "k"})
        self.assertTrue(self.run_with(FakeSocket(text_frame(long)))["ok"])

    def test_every_broken_answer_fails(self):
        cases = {
            "no turnstile": FakeSocket(text_frame(json.dumps({"type": "hello", "viewer": "v-1", "turnstile": None}))),
            "not a hello": FakeSocket(text_frame(json.dumps({"type": "snapshot"}))),
            "refused": FakeSocket(b"", status="403 Forbidden"),
            "bad accept": FakeSocket(text_frame(json.dumps({"type": "hello", "turnstile": "k"})), accept=False),
            "binary": FakeSocket(bytes([0x82, 2]) + b"{}"),
            "closed": FakeSocket(b""),
            "not json": FakeSocket(text_frame("hello")),
        }
        for name, sock in cases.items():
            result = self.run_with(sock)
            self.assertFalse(result["ok"], name)
            self.assertEqual(self.metrics(), [("SyntheticOk", 0, [{"Name": "Fleet", "Value": "prod"}], "FCVM/Fleet")], name)

    def test_the_page_is_checked_only_once_it_is_public(self):
        self.assertIn('PAGE_URL = local.fcvm_fleet_prod_page_public ? "${local.fcvm_fleet_prod_site_origin}/" : ""', ALARMS)
        self.assertRegex(ALARMS, r"fcvm_fleet_prod_page_public\s+= false\n")
        hello = text_frame(json.dumps({"type": "hello", "turnstile": "k"}))
        with mock.patch.object(self.watch, "check_page", return_value="https://example.test/ answered 522") as page:
            self.assertTrue(self.run_with(FakeSocket(hello))["ok"])
            page.assert_not_called()
            with mock.patch.dict("os.environ", {"PAGE_URL": "https://example.test/"}):
                self.assertEqual(self.run_with(FakeSocket(hello))["failures"], ["https://example.test/ answered 522"])


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

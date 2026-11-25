#!/usr/bin/env python3
"""scripts/alert-telegram.py and alert-telegram.tf: alarms reach Telegram as one readable line, recoveries only for a
sent alarm, flapping alarms are muted, the function is inert until its secret has a value, and neither the token nor
the chat id reaches a log. Offline: fake Secrets Manager, DynamoDB and HTTP."""
import contextlib
import datetime
import importlib.util
import io
import json
import re
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("alert_telegram", ROOT / "scripts" / "alert-telegram.py")
at = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(at)
TF = (ROOT / "alert-telegram.tf").read_text()

TOKEN = "123456789:AAE-testtoken-SECRET"
CHAT = "987654321"
MAIN = "111111111111"
NOW = 1_800_000_000.0
UTC = datetime.timezone.utc


def iso(t):
    return datetime.datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


class ClientError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class Secrets:
    def __init__(self, value=None, missing=False):
        self.value, self.missing = value, missing

    def get_secret_value(self, SecretId):
        if self.missing:
            raise ClientError("ResourceNotFoundException")
        return {"SecretString": self.value}


class Table:
    def __init__(self):
        self.items = {}

    def get_item(self, Key):
        item = self.items.get(Key["alarm"])
        return {"Item": json.loads(json.dumps(item))} if item else {}

    def put_item(self, Item):
        self.items[Item["alarm"]] = json.loads(json.dumps(Item))

    def scan(self, **kwargs):
        return {"Items": [json.loads(json.dumps(i)) for i in self.items.values()]}


class CloudWatch:
    """describe_alarms answering from a dict of name -> (state, seconds since its last change)."""

    def __init__(self, now):
        self.now, self.alarms, self.asked = now, {}, []

    def describe_alarms(self, AlarmNames):
        self.asked.extend(AlarmNames)
        found = [{"AlarmName": n, "StateValue": self.alarms[n][0],
                  "StateUpdatedTimestamp": datetime.datetime.fromtimestamp(self.now() - self.alarms[n][1], UTC)}
                 for n in AlarmNames if n in self.alarms]
        return {"MetricAlarms": found, "CompositeAlarms": []}


class Response:
    def __init__(self):
        pass

    def read(self):
        return b'{"ok":true}'

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Telegram:
    """Records each sendMessage; fails the first N calls with the given status."""

    def __init__(self, fail=()):
        self.sent, self.calls, self.fail = [], [], list(fail)

    def __call__(self, request, timeout=None):
        self.calls.append((request.full_url, timeout))
        if self.fail:
            status, body = self.fail.pop(0)
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(body))
        payload = json.loads(request.data)
        self.sent.append(payload)
        return Response()


def alarm(name, state, t, desc="The thing broke. More detail here.", account=MAIN, region="us-west-1"):
    message = {"AlarmName": name, "AlarmDescription": desc, "NewStateValue": state, "AWSAccountId": account,
               "StateChangeTime": iso(t), "AlarmArn": f"arn:aws:cloudwatch:{region}:{account}:alarm:{name}"}
    return {"Sns": {"Message": json.dumps(message), "Subject": f"{state}: \"{name}\"", "Timestamp": iso(t)}}


class AlertTelegram(unittest.TestCase):
    def setUp(self):
        self.table, self.telegram, self.slept = Table(), Telegram(), []
        at.secrets_client = lambda: Secrets(json.dumps({"bot_token": TOKEN, "chat_id": CHAT}))
        at.state_table = lambda: self.table
        at.urlopen = self.telegram
        at.sleep = self.slept.append
        at.MAIN_ACCOUNT, at.HOME_REGION = MAIN, "us-west-1"
        at.ACCOUNT_NAMES = {MAIN: "main", "222222222222": "dev-staging"}
        self.clock = NOW
        self.cw = CloudWatch(lambda: self.clock)
        at.cloudwatch = lambda region: self.cw
        real_time = at.time.time
        at.time.time = lambda: self.clock
        self.addCleanup(setattr, at.time, "time", real_time)

    def run_records(self, *records):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = at.lambda_handler({"Records": list(records)})
        self.log = out.getvalue()
        return result

    def texts(self):
        return [m["text"] for m in self.telegram.sent]

    def test_alarm_sends_one_readable_line_as_plain_text(self):
        self.run_records(alarm("claude-master-NoAccountAvailable", "ALARM", NOW,
                               desc="No subscription could take a request: every one is out. Clients are refused."))
        self.assertEqual(self.texts(), ["🔴 claude-master-NoAccountAvailable: No subscription could take a request: "
                                        "every one is out."])
        [message] = self.telegram.sent
        self.assertEqual(message["chat_id"], CHAT)
        self.assertTrue(message["disable_web_page_preview"])
        self.assertNotIn("parse_mode", message)
        self.assertEqual(self.telegram.calls[0][1], 10)

    def test_recovery_follows_a_sent_alarm_with_its_duration(self):
        self.run_records(alarm("x", "ALARM", NOW))
        self.run_records(alarm("x", "OK", NOW + 23 * 60))
        self.assertEqual(self.texts()[-1], "✅ x recovered after 23 min")
        # A second OK, with no ALARM between, says nothing.
        self.run_records(alarm("x", "OK", NOW + 30 * 60))
        self.assertEqual(len(self.texts()), 2)

    def test_ok_without_a_sent_alarm_and_insufficient_data_are_silent(self):
        result = self.run_records(alarm("y", "OK", NOW), alarm("y", "INSUFFICIENT_DATA", NOW + 60))
        self.assertEqual(self.texts(), [])
        self.assertEqual(result["silent"], 2)

    def test_a_flapping_alarm_is_muted_once_then_silent_until_the_mute_ends(self):
        t = NOW
        for _ in range(3):  # three ALARM/OK cycles: each sent
            self.run_records(alarm("flappy", "ALARM", t), alarm("flappy", "OK", t + 60))
            t += 600
        self.assertEqual(len(self.texts()), 6)
        self.run_records(alarm("flappy", "ALARM", t))  # the 4th within 6h
        self.assertRegex(self.texts()[-1], r"^🔁 flappy is flapping \(4 times in 6h\); muted until \d\d:\d\d UTC$")
        for _ in range(20):  # a hundred-a-day flapper: nothing at all while muted
            t += 600
            self.run_records(alarm("flappy", "OK", t), alarm("flappy", "ALARM", t + 60))
        self.assertEqual(len(self.texts()), 7)
        # Once the mute ends, an alarm that has calmed down is reported normally again.
        t = NOW + 3000 + 6 * 3600 + 7 * 3600
        self.run_records(alarm("flappy", "ALARM", t))
        self.assertEqual(self.texts()[-1], "🔴 flappy: The thing broke.")

    def test_a_flapper_still_flapping_after_its_mute_is_muted_again_with_one_line(self):
        t = NOW
        for _ in range(4):
            self.run_records(alarm("f", "ALARM", t))
            t += 60
        self.assertIn("flapping", self.texts()[-1])
        t = NOW + 6 * 3600 + 900  # the mute (from the 4th, at NOW + 180) has ended; still firing often
        self.run_records(alarm("f", "ALARM", t - 300), alarm("f", "ALARM", t - 200), alarm("f", "ALARM", t - 100),
                         alarm("f", "ALARM", t))
        flaps = [x for x in self.texts() if "flapping" in x]
        self.assertEqual(len(flaps), 2)

    def test_other_accounts_and_regions_are_named(self):
        self.run_records(alarm("a", "ALARM", NOW, account="222222222222"),
                         alarm("b", "ALARM", NOW, region="us-east-2"))
        self.assertEqual(self.texts(), ["🔴 a (dev-staging): The thing broke.", "🔴 b (us-east-2): The thing broke."])

    def test_an_alarm_without_a_description_uses_its_name(self):
        self.run_records(alarm("bare", "ALARM", NOW, desc=None))
        self.assertEqual(self.texts(), ["🔴 bare: bare"])

    def test_other_messages_are_subject_and_a_short_readable_body(self):
        self.run_records({"Sns": {"Subject": "AWS Budgets: daily  over", "Message": "line one\n\n   line two " + "x" * 900,
                                  "Timestamp": iso(NOW)}},
                         {"Sns": {"Subject": None, "Message": json.dumps({"summary": "box rebooted", "id": 3}),
                                  "Timestamp": iso(NOW)}})
        first, second = self.texts()
        self.assertTrue(first.startswith("ℹ️ AWS Budgets: daily over\nline one line two xxx"))
        self.assertLessEqual(len(first.split("\n", 1)[1]), at.BODY_CHARS)
        self.assertTrue(first.endswith("…"))
        self.assertEqual(second, "ℹ️ Alert\nbox rebooted")

    def test_not_configured_is_inert_and_succeeds(self):
        for secrets in (Secrets(missing=True), Secrets(json.dumps({"bot_token": TOKEN})), Secrets("not json")):
            with self.subTest(secrets=secrets.value):
                at.secrets_client = lambda s=secrets: s
                result = self.run_records(alarm("z", "ALARM", NOW))
                self.assertEqual(result, {"configured": False})
                self.assertIn("not configured", self.log)
                self.assertEqual(self.telegram.calls, [])
                self.assertEqual(self.table.items, {})

    def test_a_429_is_retried_once_after_its_retry_after(self):
        self.telegram.fail = [(429, b'{"ok":false,"parameters":{"retry_after":7}}')]
        self.run_records(alarm("r", "ALARM", NOW))
        self.assertEqual(self.slept, [7.0])
        self.assertEqual(len(self.texts()), 1)

    def test_a_send_that_keeps_failing_raises_without_the_token_and_keeps_no_state(self):
        self.telegram.fail = [(502, b""), (502, b"")]
        with self.assertRaises(RuntimeError) as raised:
            self.run_records(alarm("q", "ALARM", NOW))
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertEqual(self.table.items, {})  # nothing recorded, so the retry sends it

    def test_neither_the_token_nor_the_chat_id_nor_a_message_is_logged(self):
        self.telegram.fail = [(429, b'{"parameters":{"retry_after":1}}')]
        self.run_records(alarm("secret-name", "ALARM", NOW, desc="Sensitive detail."), alarm("secret-name", "OK", NOW + 60))
        for leak in (TOKEN, CHAT, "Sensitive detail", "secret-name"):
            self.assertNotIn(leak, self.log)
        self.assertIn('"outcome": "done"', self.log)


    def sweep(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = at.lambda_handler({"source": "aws.events", "detail-type": "Scheduled Event"})
        self.log = out.getvalue()
        return result

    def mute(self, name):
        t = NOW
        for _ in range(4):
            self.run_records(alarm(name, "ALARM", t))
            t += 60
        self.assertIn("flapping", self.texts()[-1])

    def test_the_sweep_reports_an_alarm_still_in_alarm_after_its_mute_once(self):
        self.mute("stuck")
        self.clock = NOW + 6 * 3600 + 1200  # the mute (until NOW + 180 + 6h) has ended
        self.cw.alarms["stuck"] = ("ALARM", 3 * 3600)
        self.assertEqual(self.sweep()["still in alarm"], 1)
        self.assertEqual(self.texts()[-1], "🔴 stuck is still in ALARM after its mute")
        self.sweep()  # cleared: a second sweep says nothing
        self.assertEqual(sum("still in ALARM" in t for t in self.texts()), 1)
        # Its recovery is reported, since its ALARM now counts as sent.
        self.run_records(alarm("stuck", "OK", self.clock + 600))
        self.assertTrue(self.texts()[-1].startswith("✅ stuck recovered after 10 min"))

    def test_the_sweep_clears_an_alarm_back_in_ok_and_waits_on_one_still_changing(self):
        self.mute("calm")
        self.mute("busy")
        self.clock = NOW + 6 * 3600 + 1200
        self.cw.alarms["calm"] = ("OK", 600)
        self.cw.alarms["busy"] = ("ALARM", 60)  # flipped a minute ago: not settled
        before = len(self.texts())
        result = self.sweep()
        self.assertEqual((result.get("cleared"), result.get("unsettled")), (1, 1))
        self.assertEqual(len(self.texts()), before)
        key = lambda n: f"{MAIN}/arn:aws:cloudwatch:us-west-1:{MAIN}:alarm:{n}"
        self.assertNotIn("muted_until", self.table.items[key("calm")])
        self.assertIn("muted_until", self.table.items[key("busy")])

    def test_an_alarm_sent_after_its_mute_is_not_reported_again_by_the_sweep(self):
        self.mute("back")
        self.clock = NOW + 6 * 3600 + 1200
        self.run_records(alarm("back", "OK", NOW + 6 * 3600 + 400))
        self.run_records(alarm("back", "ALARM", NOW + 6 * 3600 + 600))  # comes back just after the mute: sent as usual
        self.assertTrue(self.texts()[-1].startswith("🔴 back"))
        self.cw.alarms["back"] = ("ALARM", 3 * 3600)
        before = len(self.texts())
        self.sweep()
        self.assertEqual(len(self.texts()), before)  # no second 🔴 for the same alarm

    def test_the_sweep_leaves_a_mute_that_has_not_ended(self):
        self.mute("m")
        self.clock = NOW + 3600
        self.cw.alarms["m"] = ("ALARM", 3 * 3600)
        self.assertEqual(self.sweep(), {"configured": True})
        self.assertEqual(self.cw.asked, [])

    def test_an_alarm_retried_after_its_ok_and_a_duplicate_are_dropped(self):
        self.run_records(alarm("late", "OK", NOW + 60))  # the OK arrives first
        result = self.run_records(alarm("late", "ALARM", NOW))  # then the retried, older ALARM
        self.assertEqual(result.get("stale"), 1)
        self.assertEqual(self.texts(), [])
        self.run_records(alarm("dup", "ALARM", NOW))
        result = self.run_records(alarm("dup", "ALARM", NOW))
        self.assertEqual(result.get("stale"), 1)
        self.assertEqual(len(self.texts()), 1)
        item = self.table.items[f"{MAIN}/arn:aws:cloudwatch:us-west-1:{MAIN}:alarm:dup"]
        self.assertEqual(len(item["fired"]), 1)  # a duplicate never counts toward flapping

    def test_a_token_with_trailing_whitespace_is_trimmed_and_a_malformed_one_is_not_configured(self):
        at.secrets_client = lambda: Secrets(json.dumps({"bot_token": TOKEN + " \n", "chat_id": " " + CHAT + "\n"}))
        self.run_records(alarm("w", "ALARM", NOW))
        url = self.telegram.calls[0][0]
        self.assertEqual(url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(self.telegram.sent[0]["chat_id"], CHAT)
        for value in ({"bot_token": "12 34:abc", "chat_id": CHAT}, {"bot_token": TOKEN, "chat_id": "@me"}):
            with self.subTest(value=value):
                at.secrets_client = lambda v=value: Secrets(json.dumps(v))
                self.assertEqual(self.run_records(alarm("w2", "ALARM", NOW)), {"configured": False})

    def test_an_unexpected_error_carrying_the_url_never_carries_the_token_out(self):
        import http.client

        def broken(request, timeout=None):
            raise http.client.InvalidURL("URL can't contain control characters. " + request.full_url)

        at.urlopen = broken
        with self.assertRaises(RuntimeError) as raised:
            self.run_records(alarm("u", "ALARM", NOW))
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertIsNone(raised.exception.__cause__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_eventbridge_alarm_events_from_the_watchdog_relay_are_read_as_alarms(self):
        event = {"version": "0", "detail-type": "CloudWatch Alarm State Change", "source": "aws.cloudwatch",
                 "account": MAIN, "region": "us-east-2", "time": iso(NOW),
                 "resources": [f"arn:aws:cloudwatch:us-east-2:{MAIN}:alarm:parallel-watchdog-ohio-errors"],
                 "detail": {"alarmName": "parallel-watchdog-ohio-errors",
                            "state": {"value": "ALARM", "timestamp": iso(NOW)},
                            "configuration": {"description": "The Ohio watchdog failed. Boxes may not be reaped."}}}
        self.run_records({"Sns": {"Message": json.dumps(event), "Timestamp": iso(NOW)}})
        self.assertEqual(self.texts(), ["🔴 parallel-watchdog-ohio-errors (us-east-2): The Ohio watchdog failed."])

    def test_the_security_digest_and_access_audit_send_their_subject_alone(self):
        self.run_records(
            {"Sns": {"Subject": "Security alerts: 3 Inspector, 1 GuardDuty", "Message": "finding arn:aws:... on i-0abc",
                     "Timestamp": iso(NOW)}},
            {"Sns": {"Subject": "Security alert: GuardDuty: Recon", "Message": "the resource detail", "Timestamp": iso(NOW)}},
            {"Sns": {"Subject": "Access audit: jumpbox: credentials opened by python3", "Message": json.dumps({"path": "/x"}),
                     "Timestamp": iso(NOW)}})
        self.assertEqual(self.texts(), ["ℹ️ Security alerts: 3 Inspector, 1 GuardDuty", "ℹ️ Security alert: GuardDuty: Recon",
                                        "ℹ️ Access audit: jumpbox: credentials opened by python3"])


    def test_cloudtrails_access_audit_notice_sends_its_first_sentence_only(self):
        body = json.dumps("ACCESS AUDIT: registered principal used. arn:aws:iam::123456789012:user/someone (key AKIAEXAMPLE) "
                          "called iam CreateUser from 203.0.113.9 in us-west-1 at 2026-11-25. Result: none. User agent: curl.")
        self.run_records({"Sns": {"Message": body, "Timestamp": iso(NOW)}})
        self.assertEqual(self.texts(), ["ℹ️ Access audit: registered principal used"])
        for leak in ("AKIAEXAMPLE", "203.0.113.9", "someone", "curl"):
            self.assertNotIn(leak, self.texts()[0])


class Terraform(unittest.TestCase):
    def test_the_role_is_the_secret_the_table_and_its_own_logs_only(self):
        policy = TF[TF.index('resource "aws_iam_role_policy" "alert_telegram"'):]
        policy = policy[:policy.index('resource "aws_cloudwatch_log_group"')]
        self.assertEqual(sorted(re.findall(r'Sid\s*=\s*"(\w+)"', policy)),
                         ["KeepItsOwnState", "ReadAlarmStates", "ReadTheBotSecret", "WriteItsOwnLogs"])
        self.assertIn('Resource = "arn:aws:cloudwatch:*:${data.aws_caller_identity.current.account_id}:alarm:*"', policy)
        self.assertNotIn('"*"', policy)
        self.assertIn('["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Scan"]', policy)
        self.assertNotRegex(TF, r'resource "aws_iam_role_policy_attachment"')  # no managed policy on top

    def test_the_secret_admits_administration_and_this_role_alone_and_has_no_value(self):
        self.assertIn('concat(local.games_mp_admin_principals, [aws_iam_role.alert_telegram.arn])', TF)
        self.assertNotIn("aws_secretsmanager_secret_version", TF)

    def test_it_listens_to_the_alert_topic_only_and_its_errors_still_email(self):
        self.assertIn('topic_arn = aws_sns_topic.cost_alerts.arn', TF)
        self.assertIn('source_arn    = aws_sns_topic.cost_alerts.arn', TF)
        self.assertIn('principal     = "sns.amazonaws.com"', TF)
        self.assertIn('alarm_actions       = [aws_sns_topic.cost_alerts.arn]', TF)
        self.assertIn("reserved_concurrent_executions = 1", TF)
        self.assertIn('schedule_expression = "rate(15 minutes)"', TF)
        self.assertIn('source_arn    = aws_cloudwatch_event_rule.alert_telegram_sweep.arn', TF)


if __name__ == "__main__":
    unittest.main(buffer=True)

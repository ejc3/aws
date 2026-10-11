"""Offline tests for the host access audit agent and its Terraform boundaries.

No AWS calls and no root: AWS requests go to fakes, files go to a temporary directory.
Run: python3 -S -B scripts/test-access-audit.py
The live fanotify path runs separately as root: scripts/test-access-audit-live.py
"""
import datetime
import importlib.util
import json
import os
from pathlib import Path
import plistlib
import pwd
import re
import struct
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("access_audit", ROOT / "scripts/access-audit.py")
agent = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agent)
TERRAFORM = (ROOT / "access-audit.tf").read_text()
USER = pwd.getpwuid(os.getuid()).pw_name


def block(kind, name, category="resource"):
    if kind == "data":
        category, kind, name = "data", name, category
    match = re.search(r'^' + category + ' "' + kind + '" "' + name + r'" \{\n(.*?)^\}', TERRAFORM, re.M | re.S)
    assert match, (kind, name)
    return re.sub(r"[ \t]+=[ \t]+", " = ", match.group(1))


def bundle(directory, files=None):
    return {"version": 1, "host": "example-host", "region": "us-west-1",
            "topic_arn": "arn:aws:sns:us-west-1:111111111111:alerts", "log_group": "/security/access-audit",
            "namespace": "AccessAudit",
            "service_name": "hostmetricsd", "launchd_label": "local.hostmetricsd",
            "reporter": {"access_key_id": "AKIAREPORTER", "secret_access_key": "reporter-secret"},
            "principal": {"access_key_id": "AKIAPRINCIPAL", "secret_access_key": "principal-secret"},
            "files": files if files is not None else [
                {"path": directory + "/a/b/creds", "owner": USER,
                 "content": "id={{principal_access_key_id}}\nsecret={{principal_secret_access_key}}\n"},
                {"path": directory + "/notes", "owner": USER, "content": "{{random_hex:8}}", "watch": False}]}


class FakeClient:
    def __init__(self, fail=0):
        self.calls = []
        self.fail = fail

    def _record(self, *call):
        if self.fail:
            self.fail -= 1
            raise RuntimeError("transient")
        self.calls.append(call)

    def put_metrics(self, namespace, metrics):
        self._record("metrics", namespace, metrics)

    def put_log(self, group, stream, message):
        self._record("log", group, stream, json.loads(message))

    def publish(self, topic, subject, message):
        self._record("publish", topic, subject, json.loads(message))


class SigningTest(unittest.TestCase):
    def test_matches_the_aws_documented_sigv4_example(self):
        # https://docs.aws.amazon.com/IAM/latest/UserGuide/create-signed-request.html (IAM ListUsers example)
        headers = agent.sign_request(
            "GET", "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
            {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}, b"",
            {"access_key_id": "AKIDEXAMPLE", "secret_access_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"},
            "us-east-1", "iam", datetime.datetime(2015, 8, 30, 12, 36, tzinfo=datetime.timezone.utc))
        self.assertEqual(headers["authorization"],
                         "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
                         "SignedHeaders=content-type;host;x-amz-date, "
                         "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7")

    def test_session_token_is_signed(self):
        headers = agent.sign_request("POST", "https://sns.us-west-1.amazonaws.com/", {}, b"x",
                                     {"access_key_id": "A", "secret_access_key": "B", "session_token": "T"},
                                     "us-west-1", "sns", datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
        self.assertEqual(headers["x-amz-security-token"], "T")
        self.assertIn("SignedHeaders=host;x-amz-date;x-amz-security-token,", headers["authorization"])

    def test_client_builds_each_api_request(self):
        sent = []
        client = agent.AwsClient({"access_key_id": "A", "secret_access_key": "B"}, "us-west-1",
                                 post=lambda url, headers, body: sent.append((url, headers, body)) or (200, b"{}"))
        client.put_metrics("AccessAudit", [("FileRead", {}), ("FileRead", {"Host": "h"})])
        client.put_log("/g", "h", "m")
        client.publish("arn:t", "s", "m")
        self.assertEqual([url for url, _, _ in sent], ["https://monitoring.us-west-1.amazonaws.com/",
                                                       "https://logs.us-west-1.amazonaws.com/",
                                                       "https://sns.us-west-1.amazonaws.com/"])
        metrics = dict(p.split("=", 1) for p in sent[0][2].decode().split("&"))
        self.assertEqual(metrics["MetricData.member.2.Dimensions.member.1.Value"], "h")
        self.assertNotIn("MetricData.member.1.Dimensions.member.1.Name", metrics)
        self.assertEqual(sent[1][1]["x-amz-target"], "Logs_20140328.PutLogEvents")
        self.assertEqual(json.loads(sent[1][2])["logStreamName"], "h")

    def test_non_200_is_an_error(self):
        client = agent.AwsClient({"access_key_id": "A", "secret_access_key": "B"}, "us-west-1",
                                 post=lambda *_: (403, b"denied"))
        with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            client.publish("arn:t", "s", "m")


class BundleTest(unittest.TestCase):
    def test_templates(self):
        self.assertEqual(agent.render("{{a}}-{{a}}", {"a": "x"}), "x-x")
        self.assertRegex(agent.render("{{random_hex:4}}", {}), r"^[0-9a-f]{8}$")
        with self.assertRaisesRegex(ValueError, "unknown template"):
            agent.render("{{nope}}", {})

    def test_validation_rejects_unsafe_entries(self):
        good = bundle("/tmp/x")
        agent.validate_bundle(good)
        for mutate in (lambda b: b.update(version=2),
                       lambda b: b.update(host="Bad Host"),
                       lambda b: b["files"][0].update(path="relative/path"),
                       lambda b: b["files"][0].update(path="/tmp/x/../etc/passwd"),
                       lambda b: b["files"].append(dict(b["files"][0])),
                       lambda b: b["files"][0].update(mode="777"),
                       lambda b: b["files"][0].update(content="x" * (agent.MAX_FILE_BYTES + 1)),
                       lambda b: b["reporter"].pop("secret_access_key"),
                       lambda b: b.update(service_name="Bad-Name"),
                       lambda b: b.update(launchd_label="com.apple.evil"),
                       lambda b: b.update(files=[])):
            candidate = json.loads(json.dumps(good))
            mutate(candidate)
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                agent.validate_bundle(candidate)


class PlaceFilesTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()

    def test_places_renders_ages_and_records(self):
        state = agent.place_files(bundle(self.directory), {}, chown=False)
        creds = self.directory + "/a/b/creds"
        with open(creds) as handle:
            self.assertEqual(handle.read(), "id=AKIAPRINCIPAL\nsecret=principal-secret\n")
        self.assertEqual(os.stat(creds).st_mode & 0o777, 0o600)
        self.assertLess(os.stat(creds).st_mtime, time.time() - 119 * 86400)
        self.assertEqual(state["dirs"], [self.directory + "/a", self.directory + "/a/b"])
        self.assertTrue(state["files"][creds]["watch"])
        self.assertFalse(state["files"][self.directory + "/notes"]["watch"])

    def test_never_overwrites_a_file_it_did_not_place(self):
        with open(self.directory + "/notes", "w") as handle:
            handle.write("the owner's real data")
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
            agent.place_files(bundle(self.directory), {}, chown=False)
        with open(self.directory + "/notes") as handle:
            self.assertEqual(handle.read(), "the owner's real data")
        self.assertFalse(os.path.exists(self.directory + "/a"))

    def test_reinstall_replaces_its_own_files_but_not_changed_ones(self):
        state = agent.place_files(bundle(self.directory), {}, chown=False)
        agent.place_files(bundle(self.directory), state, chown=False)
        with open(self.directory + "/notes", "a") as handle:
            handle.write("edited by someone")
        with self.assertRaisesRegex(RuntimeError, "refusing to overwrite"):
            agent.place_files(bundle(self.directory), state, chown=False)

    def test_dropped_entries_are_removed_only_when_unchanged(self):
        state = agent.place_files(bundle(self.directory), {}, chown=False)
        only_creds = bundle(self.directory)
        only_creds["files"] = only_creds["files"][:1]
        agent.place_files(only_creds, state, chown=False)
        self.assertFalse(os.path.exists(self.directory + "/notes"))

    def test_ssh_key_generated_once_per_install(self):
        calls = []
        files = [{"path": self.directory + "/k", "owner": USER, "content": "{{ssh_private_key}}"},
                 {"path": self.directory + "/k.pub", "owner": USER, "content": "{{ssh_public_key}}", "watch": False}]
        agent.place_files(bundle(self.directory, files), {}, chown=False,
                          keygen=lambda: calls.append(1) or ("PRIVATE", "PUBLIC"))
        self.assertEqual(calls, [1])
        self.assertEqual(Path(self.directory + "/k").read_text(), "PRIVATE")


class EsloggerScannerTest(unittest.TestCase):
    PATH = "/Users/example/.config/deploy/prod.env"

    def event(self, path, exe="/usr/bin/cat", pid=4242):
        return json.dumps({"event_type": 10, "process": {"executable": {"path": exe}, "ppid": 1,
                           "audit_token": {"pid": pid, "ruid": os.getuid()}, "signing_id": "com.apple.cat"},
                           "event": {"open": {"file": {"path": path}, "fflag": 1}}}).replace("/", "\\/").encode() + b"\n"

    def scanner(self):
        reports = []
        return agent.EsloggerScanner([self.PATH], reports.append, ["/usr/bin/mdworker"], describe=lambda pid: {"cmdline": "cat"}), reports

    def test_escaped_slashes_and_lines_split_across_chunks(self):
        scanner, reports = self.scanner()
        stream = self.event("/tmp/other") + self.event(self.PATH) + self.event(self.PATH + ".bak")
        for index in range(0, len(stream), 7):
            scanner.feed(stream[index:index + 7])
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["path"], self.PATH)
        self.assertEqual(reports[0]["exe"], "/usr/bin/cat")
        self.assertEqual(reports[0]["pid"], 4242)
        self.assertEqual(reports[0]["cmdline"], "cat")

    def test_ignored_executables_same_basename_elsewhere_and_garbage(self):
        scanner, reports = self.scanner()
        scanner.feed(self.event(self.PATH, exe="/usr/bin/mdworker") + self.event("/elsewhere/prod.env")
                     + b"{not json prod.env\n" + self.event(self.PATH, pid=os.getpid()))
        self.assertEqual(reports, [])

    def test_unterminated_line_is_bounded(self):
        scanner, _ = self.scanner()
        scanner.feed(b"x" * (agent.ESLOGGER_MAX_LINE + 1))
        self.assertEqual(scanner.buffer, b"")


class EsloggerFifoTest(unittest.TestCase):
    def setUp(self):
        self.fifo = tempfile.mkdtemp() + "/open.fifo"
        os.mkfifo(self.fifo, 0o600)
        self.now = [0.0]
        self.reports = []
        self.sensor = agent.EsloggerSensor([EsloggerScannerTest.PATH], self.reports.append, [], fifo=self.fifo,
                                           silence=100, clock=lambda: self.now[0])
        self.sensor.scanner.describe = lambda pid: {}
        self.reader = os.open(self.fifo, os.O_RDONLY | os.O_NONBLOCK)
        self.addCleanup(os.close, self.reader)

    def test_reads_events_reports_silence_once_and_recovers(self):
        writer = os.open(self.fifo, os.O_WRONLY)
        os.write(writer, EsloggerScannerTest.event(None, EsloggerScannerTest.PATH))
        self.sensor.poll_once(self.reader, 1)
        self.assertEqual([r["kind"] for r in self.reports], ["file-read"])
        self.assertTrue(self.sensor.reported_recently(EsloggerScannerTest.PATH))
        os.close(writer)
        self.now[0] += 101
        for _ in range(3):
            self.sensor.poll_once(self.reader, 0)
        self.assertEqual([r.get("reason") for r in self.reports], [None, "sensor silent"])
        writer = os.open(self.fifo, os.O_WRONLY)
        os.write(writer, EsloggerScannerTest.event(None, "/unrelated"))
        self.sensor.poll_once(self.reader, 1)
        self.assertFalse(self.sensor.silent)
        os.close(writer)

    def test_refuses_a_regular_file(self):
        path = tempfile.mkdtemp() + "/regular"
        Path(path).write_text("")
        with self.assertRaisesRegex(OSError, "not a FIFO"):
            agent.EsloggerSensor([], print, [], fifo=path)

    def test_poll_fallback_is_suppressed_when_eslogger_already_reported(self):
        mac = agent.MacSensor.__new__(agent.MacSensor)
        mac.eslogger, mac.emit, emitted = self.sensor, None, []
        mac.emit = emitted.append
        self.sensor.last_report[EsloggerScannerTest.PATH] = self.now[0]
        original, agent.time.sleep = agent.time.sleep, lambda _: None
        try:
            mac.fallback({"kind": "file-read", "path": EsloggerScannerTest.PATH})
            mac.fallback({"kind": "file-read", "path": "/other"})
        finally:
            agent.time.sleep = original
        self.assertEqual([r["path"] for r in emitted], ["/other"])

    def test_launchd_plists_are_valid_and_helper_writes_the_fifo(self):
        layout = agent.Layout("hostmetricsd")
        es = plistlib.loads(agent.launchd_plist(layout.helper_label, ["/usr/bin/eslogger", "open"], layout.fifo, layout.helper_log).encode())
        self.assertEqual(es["ProgramArguments"], ["/usr/bin/eslogger", "open"])
        self.assertEqual(es["StandardOutPath"], layout.fifo)
        self.assertTrue(es["KeepAlive"])
        main = plistlib.loads(agent.launchd_plist(layout.label, ["/usr/bin/python3", "-I", "a&b", "run"], None, layout.log).encode())
        self.assertNotIn("StandardOutPath", main)
        self.assertEqual(main["ProgramArguments"][2], "a&b")


class LayoutTest(unittest.TestCase):
    def test_all_host_paths_derive_from_one_private_name(self):
        layout = agent.Layout("widgetd")
        self.assertEqual(layout.config, "/etc/widgetd/config.json")
        self.assertEqual(layout.agent, "/usr/local/libexec/widgetd/widgetd")
        self.assertEqual(layout.unit, "/etc/systemd/system/widgetd.service")
        self.assertEqual(layout.label, "local.widgetd")
        self.assertEqual(layout.helper_label, "local.widgetd.helper")
        self.assertEqual(layout.fifo, "/var/db/widgetd/events")

    def test_name_and_label_are_validated(self):
        for bad in ("Widget", "a", "x" * 40, "has space", "sh;rm"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                agent.Layout(bad)
        with self.assertRaises(ValueError):
            agent.Layout("widgetd", "com.apple.widgetd")  # never impersonate a system label
        self.assertEqual(agent.Layout("widgetd", "local.custom.name").label, "local.custom.name")

    def test_default_name_does_not_reveal_purpose(self):
        for word in ("audit", "honey", "decoy", "canary", "trap", "security", "trip"):
            self.assertNotIn(word, agent.DEFAULT_SERVICE_NAME)


class ReporterTest(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.client = FakeClient()
        self.config = {"host": "example-host", "namespace": "AccessAudit", "log_group": "/g", "topic_arn": "arn:t"}
        self.reporter = agent.Reporter(self.config, self.client, clock=lambda: self.now[0])

    def drain(self):
        while not self.reporter.queue.empty():
            self.reporter.deliver(self.reporter.queue.get())

    def test_file_read_goes_to_logs_metrics_and_email(self):
        self.reporter.submit({"kind": "file-read", "path": "/p", "pid": 1, "exe": "/usr/bin/cat"})
        self.drain()
        kinds = [call[0] for call in self.client.calls]
        self.assertEqual(kinds, ["log", "metrics", "publish"])
        self.assertEqual(self.client.calls[1][2], [("FileRead", {}), ("FileRead", {"Host": "example-host"})])
        self.assertEqual(self.client.calls[2][2], "Access audit: example-host: p opened by cat")

    def test_heartbeat_is_metric_only(self):
        self.reporter.submit({"kind": "heartbeat"})
        self.drain()
        self.assertEqual(self.client.calls, [("metrics", "AccessAudit", [("Heartbeat", {"Host": "example-host"})])])

    def test_same_reader_is_deduplicated_for_a_minute(self):
        for _ in range(3):
            self.reporter.submit({"kind": "file-read", "path": "/p", "pid": 1})
        self.assertEqual(self.reporter.queue.qsize(), 1)
        self.now[0] += agent.DEDUPE_SECONDS + 1
        self.reporter.submit({"kind": "file-read", "path": "/p", "pid": 1})
        self.assertEqual(self.reporter.queue.qsize(), 2)

    def test_full_queue_drops_and_reports_the_count(self):
        for pid in range(agent.MAX_QUEUE + 5):
            self.reporter.submit({"kind": "file-read", "path": "/p", "pid": pid})
        self.assertEqual(self.reporter.dropped, 5)
        self.reporter.deliver(self.reporter.queue.get())
        self.assertEqual(self.client.calls[0][3]["dropped_before"], 5)

    def test_email_is_rate_limited_but_logs_and_metrics_are_not(self):
        for pid in range(agent.SNS_WINDOW_LIMIT + 3):
            self.reporter.deliver({"kind": "file-read", "path": "/p", "pid": pid, "host": "example-host"})
        kinds = [call[0] for call in self.client.calls]
        self.assertEqual(kinds.count("publish"), agent.SNS_WINDOW_LIMIT)
        self.assertEqual(kinds.count("log"), agent.SNS_WINDOW_LIMIT + 3)
        self.now[0] += agent.SNS_WINDOW_SECONDS + 1
        self.reporter.deliver({"kind": "tamper", "reason": "x", "host": "example-host"})
        self.assertEqual(self.client.calls[-1][3]["emails_suppressed_before"], 3)

    def test_retry_survives_transient_errors(self):
        self.client.fail = 2
        self.assertTrue(agent.Reporter.retry(lambda: self.client.publish("t", "s", "{}"), sleep=lambda _: None))
        self.assertFalse(agent.Reporter.retry(lambda: 1 / 0, attempts=2, sleep=lambda _: None))

    def test_subject_is_ascii_and_bounded(self):
        subject = agent.subject_for({"kind": "file-read", "host": "h", "path": "/x/" + "é" * 200, "exe": None})
        self.assertLessEqual(len(subject), 100)
        self.assertTrue(all(32 <= ord(ch) < 127 for ch in subject))


class SensorTest(unittest.TestCase):
    def test_atime_poll_detects_a_read_and_rearms(self):
        directory = tempfile.mkdtemp()
        path = directory + "/f"
        Path(path).write_text("x")
        reports = []
        sensor = agent.PollSensor([path], reports.append)
        mtime = os.stat(path).st_mtime_ns
        sensor.check()
        self.assertEqual(reports, [])
        Path(path).read_text()
        sensor.check()
        self.assertEqual([r["path"] for r in reports], [path])
        self.assertEqual(os.stat(path).st_mtime_ns, mtime)
        sensor.check()
        self.assertEqual(len(reports), 1)

    def test_fanotify_overflow_and_short_records(self):
        reports = []
        sensor = agent.FanotifySensor.__new__(agent.FanotifySensor)
        sensor.paths, sensor.emit, sensor.ignore = set(), reports.append, set()
        sensor.parse(agent.FAN_EVENT.pack(agent.FAN_EVENT.size, 3, 0, agent.FAN_EVENT.size, agent.FAN_Q_OVERFLOW, -1, 0)
                     + struct.pack("=I", 2) + b"\0" * 20)
        self.assertEqual([r["reason"] for r in reports], ["sensor queue overflow"])

    def test_fanotify_always_answers_allow_even_when_describe_fails(self):
        sensor = agent.FanotifySensor.__new__(agent.FanotifySensor)
        answered, reports = [], []
        sensor.paths, sensor.emit, sensor.ignore = {"/p"}, reports.append, set()
        sensor.respond = answered.append
        sensor.describe = lambda fd, pid, own: 1 / 0
        event = agent.FAN_EVENT.pack(agent.FAN_EVENT.size, 3, 0, agent.FAN_EVENT.size, agent.FAN_OPEN_PERM, 7, 99)
        sensor.parse(event + event)
        self.assertEqual(answered, [7, 7])
        self.assertEqual(reports, [])

    def test_integrity_reports_each_change_once(self):
        directory = tempfile.mkdtemp()
        path = directory + "/f"
        Path(path).write_text("x")
        reports, remarked = [], []
        integrity = agent.Integrity({path: agent.fingerprint(path)}, reports.append, remarked.append)
        integrity.check()
        self.assertEqual(reports, [])
        Path(path).write_text("changed")
        integrity.check()
        integrity.check()
        os.unlink(path)
        integrity.check()
        Path(path + ".new").write_text("x")
        os.rename(path + ".new", path)
        integrity.check()
        reasons = [r["reason"] for r in reports]
        self.assertEqual(reasons[:2], ["registered path modified", "registered path missing"])
        # Recreation is reported whether or not the filesystem reused the inode number.
        self.assertIn(reasons[2], ("registered path replaced", "registered path modified"))
        self.assertEqual(len(reasons), 3)
        self.assertEqual(remarked, [path, path])


class HandlerHookTest(unittest.TestCase):
    def make(self, safe=True, run=None):
        self.emitted = []
        return agent.HandlerRunner("/etc/x/event_handler", self.emitted.append,
                                   run=run or (lambda payload: b""), is_safe=lambda path: safe)

    def test_absent_or_unsafe_handler_is_never_run(self):
        ran = []
        runner = self.make(safe=False, run=lambda payload: ran.append(payload) or b"")
        runner.offer({"kind": "file-read", "path": "/p"})
        self.assertTrue(runner.queue.empty())
        self.assertEqual(ran, [])

    def test_only_trigger_kinds_are_offered(self):
        runner = self.make()
        runner.offer({"kind": "heartbeat"})
        runner.offer({"kind": "test"})
        self.assertTrue(runner.queue.empty())
        runner.offer({"kind": "file-read", "path": "/p"})
        runner.offer({"kind": "tamper", "reason": "x"})
        self.assertEqual(runner.queue.qsize(), 2)

    def test_handler_receives_event_json_and_its_report_is_delivered(self):
        seen = {}
        def run(payload):
            seen["event"] = json.loads(payload)
            return json.dumps({"kind": "handler", "summary": "acted", "detail": "z"}).encode()
        runner = self.make(run=run)
        runner.handle({"kind": "file-read", "path": "/p", "pid": 7})
        self.assertEqual(seen["event"]["path"], "/p")
        self.assertEqual([r["kind"] for r in self.emitted], ["handler"])
        self.assertEqual(self.emitted[0]["summary"], "acted")

    def test_no_output_delivers_nothing_and_bad_output_is_reported(self):
        self.make(run=lambda p: b"").handle({"kind": "file-read"})
        self.assertEqual(self.emitted, [])
        runner = self.make(run=lambda p: b"not json")
        runner.handle({"kind": "tamper", "reason": "x"})
        self.assertEqual(self.emitted[0]["reason"], "event handler returned non-JSON")

    def test_handler_crash_is_reported_not_fatal(self):
        runner = self.make(run=lambda p: (_ for _ in ()).throw(OSError("boom")))
        runner.handle({"kind": "file-read"})
        self.assertEqual(self.emitted[0]["reason"], "event handler failed")

    def test_unknown_kind_from_handler_is_dropped(self):
        runner = self.make(run=lambda p: json.dumps({"kind": "arbitrary"}).encode())
        runner.handle({"kind": "file-read"})
        self.assertEqual(self.emitted, [])

    def test_safety_predicate_requires_root_owned_non_writable(self):
        import tempfile, pathlib
        path = tempfile.mkdtemp() + "/h"
        pathlib.Path(path).write_text("x")
        os.chmod(path, 0o755)
        self.assertEqual(agent.handler_is_safe(path), os.stat(path).st_uid == 0)
        os.chmod(path, 0o777)
        self.assertFalse(agent.handler_is_safe(path))
        self.assertFalse(agent.handler_is_safe(path + "-missing"))


class TerraformBoundaryTest(unittest.TestCase):
    def test_no_private_layout_in_the_public_repository(self):
        for text in (TERRAFORM, (ROOT / "scripts/access-audit.py").read_text()):
            for word in ("honey", "decoy", "canary", "bait", "bitcoin", "wallet", "AKIA"):
                self.assertNotIn(word.lower(), text.lower())

    def test_everything_is_off_until_the_private_config_exists(self):
        self.assertRegex(TERRAFORM, r'variable "enable_access_audit" \{[^}]*default\s*=\s*false')
        self.assertRegex(block("data", "aws_secretsmanager_secret_version", "access_audit_config"),
                         r"count\s*=\s*var\.enable_access_audit \? 1 : 0")

    def test_registered_principals_can_do_nothing(self):
        policy = block("aws_iam_user_policy", "access_audit_principal")
        self.assertIn('Effect = "Deny"', policy)
        self.assertIn('Action = "*"', policy)
        self.assertIn('Resource = "*"', policy)

    def test_reporter_is_scoped_to_its_own_stream_namespace_and_topic(self):
        policy = block("aws_iam_user_policy", "access_audit_reporter")
        self.assertIn('"logs:PutLogEvents"', policy)
        self.assertIn(':log-stream:${each.key}"', policy)
        self.assertIn('"cloudwatch:namespace" = local.access_audit_namespace', policy)
        self.assertIn("aws_sns_topic.cost_alerts.arn", policy)
        self.assertEqual(policy.count('Resource = "*"'), 1)
        self.assertRegex(policy, r'"cloudwatch:PutMetricData"\]\s+Resource = "\*"\s+Condition')

    def test_principal_use_is_forwarded_from_every_enabled_region(self):
        rule = block("aws_cloudwatch_event_rule", "access_audit_forward")
        self.assertIn("local.security_delivery_regions", rule)
        self.assertIn("region = each.key", rule)
        self.assertIn('state = "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"', rule)
        self.assertIn("length(local.access_audit_forward_pattern) <= 2048", rule)
        self.assertIn('state = "ENABLED_WITH_ALL_CLOUDTRAIL_MANAGEMENT_EVENTS"',
                      block("aws_cloudwatch_event_rule", "access_audit_notify"))

    def test_alarms_page_the_operator(self):
        for name in ("access_audit_principal_use", "access_audit_file_read", "access_audit_tamper",
                     "access_audit_heartbeat", "access_audit_delivery"):
            with self.subTest(alarm=name):
                self.assertIn("alarm_actions = [aws_sns_topic.cost_alerts.arn]",
                              block("aws_cloudwatch_metric_alarm", name))
        self.assertIn('treat_missing_data = "breaching"', block("aws_cloudwatch_metric_alarm", "access_audit_heartbeat"))


if __name__ == "__main__":
    unittest.main()

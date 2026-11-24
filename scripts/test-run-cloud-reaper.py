#!/usr/bin/env python3
"""scripts/run-cloud-reaper.py and run-cloud-reaper.tf: run.cloud sessions left running are deleted, persistent and
keep-tagged ones never are, and the key never reaches a log. Offline: a fake run.cloud API, fake AWS clients and the
Terraform source."""
import contextlib
import datetime
import importlib.util
import io
import json
import os
import re
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("run_cloud_reaper", ROOT / "scripts" / "run-cloud-reaper.py")
rcr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rcr)
TF = (ROOT / "run-cloud-reaper.tf").read_text()
AI_TF = (ROOT / "dev-ai-services.tf").read_text()

KEY = "rc_live_TESTKEY_0123456789abcdef"
NOW = 1_800_000_000.0
UTC = datetime.timezone.utc


def iso(t):
    return datetime.datetime.fromtimestamp(t, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def sim(sid, age_min, inactivity=120, hard_min=30, status="active", tags=None, drop=()):
    """A simulator session as GET /run-cloud/ios returns it (the fields seen live on 2026-11-24)."""
    created = NOW - age_min * 60
    s = {"id": sid, "status": status, "createdAt": iso(created),
         "expiresAt": None if hard_min is None else iso(created + hard_min * 60),
         "inactivityTimeoutSeconds": inactivity, "inactivityCountdownSeconds": 10,
         "tags": tags or {}, "displayName": "agent-" + sid, "url": "https://example.invalid/stream", "codec": "auto"}
    for k in drop:
        s.pop(k)
    return s


def sandbox(sid, run_min, idle_min=None, idle_pause=300, timeout=1800, state="running", tags=None, created_min=None,
            last_min=None, drop=()):
    """A sandbox on the wire (snake_case, as the SDK documents). run_min: minutes since it last started or resumed."""
    s = {"id": sid, "state": state, "name": "sb-" + sid, "idle_pause_seconds": idle_pause, "timeout_seconds": timeout,
         "created_at": iso(NOW - (created_min if created_min is not None else run_min) * 60),
         "state_changed_at": iso(NOW - run_min * 60), "tags": tags or {}}
    if idle_min is not None:
        s["last_active_at"] = iso(NOW - idle_min * 60)
    if last_min is not None:
        s["last_active_at"] = iso(NOW - last_min * 60)
    for k in drop:
        s.pop(k)
    return s


class FakeRunCloud:
    """The run.cloud REST API as the SDK uses it: GET /run-cloud/{ios,android} (a list of active sessions),
    GET /run-cloud/sandboxes and /run-cloud/boxes ({items, nextCursor}), DELETE on each. Errors are {"error": "..."}."""

    def __init__(self, ios=(), android=(), sandboxes=(), boxes=(), fail_delete=(), fail_list=(), gone=(),
                 echo_key=False, sandbox_pages=None, delete_answer=None):
        self.lists = {"ios": list(ios), "android": list(android)}
        self.sandboxes, self.boxes = list(sandboxes), list(boxes)
        self.fail_delete, self.fail_list, self.gone = set(fail_delete), set(fail_list), set(gone)
        self.echo_key, self.sandbox_pages, self.delete_answer = echo_key, sandbox_pages, delete_answer
        self.calls, self.headers = [], []

    def deleted(self):
        return [p for m, p in self.calls if m == "DELETE"]

    def _err(self, status, message):
        if self.echo_key:
            message += " (token %s)" % KEY
        return status, json.dumps({"error": message}).encode()

    def __call__(self, method, url, headers, timeout):
        assert url.startswith("https://api.run.cloud/"), url
        parsed = urllib.parse.urlsplit(url)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        self.calls.append((method, path + ("?" + parsed.query if parsed.query else "")))
        self.headers.append(headers)
        if headers.get("Authorization") != "Bearer " + KEY:
            return 401, b'{"error":"Unauthorized"}'
        if headers.get("User-Agent", "").startswith("Python-urllib"):
            return 403, b"error code: 1010"          # Cloudflare in front of api.run.cloud
        parts = path.strip("/").split("/")           # ["run-cloud", kind, id?]
        kind = parts[1]
        if method == "GET":
            if kind in self.fail_list:
                return self._err(503, "temporarily unavailable")
            if kind in self.lists:
                return 200, json.dumps(self.lists[kind]).encode()
            if kind == "boxes":
                return 200, json.dumps({"items": self.boxes}).encode()
            if kind == "sandboxes":
                if self.sandbox_pages is not None:
                    cursor = (query.get("cursor") or [""])[0]
                    return 200, json.dumps(self.sandbox_pages[cursor]).encode()
                return 200, json.dumps({"items": self.sandboxes, "nextCursor": None}).encode()
        if method == "DELETE":
            sid = urllib.parse.unquote(parts[2])
            if sid in self.fail_delete:
                return self._err(500, "could not release")
            if sid in self.gone:
                return self._err(404, "iOS simulator session not found")
            if self.delete_answer is not None:
                return self.delete_answer
            return 200, json.dumps({"id": sid, "status": "released"}).encode()
        return 404, b'{"error":"not found"}'


class FakeSM:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else json.dumps({"RUN_CLOUD_API_KEY": KEY})
        self.asked = []

    def get_secret_value(self, SecretId):
        self.asked.append(SecretId)
        return {"SecretString": self.payload}


class FakeCW:
    def __init__(self):
        self.puts = []

    def put_metric_data(self, Namespace, MetricData):
        self.puts.append((Namespace, MetricData))

    def metric(self, name):
        return next((d["Value"] for _, data in self.puts for d in data if d["MetricName"] == name), None)


def run(api, dry_run=False, sm=None):
    """(result or None, the exception or None, stdout, cw)."""
    out, cw = io.StringIO(), FakeCW()
    result = error = None
    with contextlib.redirect_stdout(out):
        try:
            result = rcr.lambda_handler({"dry_run": dry_run}, None, transport=api, sm=sm or FakeSM(), cw=cw, now=NOW)
        except Exception as exc:
            error = exc
    return result, error, out.getvalue(), cw


def lines(stdout):
    return [json.loads(x) for x in stdout.splitlines() if x.strip()]


def by_id(stdout):
    return {r["id"]: r for r in lines(stdout) if r.get("id")}


class SimulatorRuleTests(unittest.TestCase):
    def test_no_timeouts_is_deleted_once_past_the_grace_period(self):
        api = FakeRunCloud(ios=[sim("old", 6, inactivity=None, hard_min=None), sim("new", 3, inactivity=None, hard_min=None)])
        result, error, out, cw = run(api)
        self.assertIsNone(error)
        self.assertEqual(api.deleted(), ["/run-cloud/ios/old"])
        rec = by_id(out)
        self.assertEqual(rec["old"]["decision"], "delete")
        self.assertIn("no inactivity timeout and no hard timeout", rec["old"]["reason"])
        self.assertEqual(rec["new"]["decision"], "keep")
        self.assertIn("grace period", rec["new"]["reason"])

    def test_zero_inactivity_counts_as_none(self):
        api = FakeRunCloud(ios=[sim("z", 6, inactivity=0, hard_min=None)])
        run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/ios/z"])

    def test_one_timeout_within_its_cap_is_enough(self):
        api = FakeRunCloud(ios=[sim("hard-only", 20, inactivity=None, hard_min=30), sim("idle-only", 20, inactivity=120, hard_min=None)])
        run(api)
        self.assertEqual(api.deleted(), [], "each is bounded by the timeout it has, and by the age rule")

    def test_an_inactivity_timeout_over_the_cap_is_deleted(self):
        api = FakeRunCloud(android=[sim("long-idle", 6, inactivity=11 * 60, hard_min=30)])
        _, _, out, _ = run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/android/long-idle"])
        self.assertIn("inactivity timeout 11 min is over the 10-minute cap", by_id(out)["long-idle"]["reason"])

    def test_a_hard_timeout_over_the_cap_is_deleted_and_exactly_the_cap_is_not(self):
        api = FakeRunCloud(ios=[sim("long", 6, hard_min=61), sim("exact", 6, hard_min=60)])
        _, _, out, _ = run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/ios/long"])
        self.assertIn("hard timeout 61 min is over the 60-minute cap", by_id(out)["long"]["reason"])

    def test_the_recommended_timeouts_are_left_alone_until_the_age_cap(self):
        api = FakeRunCloud(ios=[sim("good", 45, inactivity=120, hard_min=50)])
        _, _, out, _ = run(api)
        self.assertEqual(api.deleted(), [])
        self.assertEqual(by_id(out)["good"]["decision"], "keep")

    def test_older_than_max_age_is_deleted_whatever_its_timeouts(self):
        api = FakeRunCloud(ios=[sim("forgotten", 61, inactivity=60, hard_min=50), sim("young", 59, inactivity=60, hard_min=50)])
        _, _, out, _ = run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/ios/forgotten"])
        self.assertIn("older than 60 minutes", by_id(out)["forgotten"]["reason"])

    def test_simulators_report_no_idleness_so_the_line_says_null(self):
        api = FakeRunCloud(ios=[sim("s", 20)])
        _, _, out, _ = run(api)
        rec = by_id(out)["s"]
        self.assertIsNone(rec["idle_minutes"])
        self.assertEqual(rec["age_minutes"], 20.0)

    def test_a_released_session_in_the_list_is_not_deleted(self):
        api = FakeRunCloud(ios=[sim("r", 90, status="released")])
        run(api)
        self.assertEqual(api.deleted(), [])

    def test_keep_true_is_never_deleted_by_any_rule(self):
        api = FakeRunCloud(ios=[sim("k", 600, inactivity=None, hard_min=None, tags={"keep": "true"}),
                                sim("K", 600, tags={"keep": "TRUE"}), sim("other-tag", 600, tags={"keep": "no"})])
        _, _, out, _ = run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/ios/other-tag"])
        self.assertIn("keep=true", by_id(out)["k"]["reason"])

    def test_a_session_whose_timeouts_the_api_does_not_state_is_an_error_not_a_deletion(self):
        # A renamed field must not read as "no timeout" and delete every session after five minutes.
        api = FakeRunCloud(ios=[sim("renamed", 20, drop=("inactivityTimeoutSeconds", "expiresAt")),
                                sim("renamed-old", 61, drop=("inactivityTimeoutSeconds", "expiresAt"))])
        _, error, out, _ = run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/ios/renamed-old"], "the age rule needs only createdAt")
        self.assertEqual(by_id(out)["renamed"]["decision"], "error")
        self.assertIsInstance(error, RuntimeError, "so the Errors alarm says the API changed")

    def test_no_created_at_is_an_error(self):
        api = FakeRunCloud(ios=[sim("nodate", 99, drop=("createdAt",))])
        _, error, out, _ = run(api)
        self.assertEqual(api.deleted(), [])
        self.assertEqual(by_id(out)["nodate"]["decision"], "error")
        self.assertIsNotNone(error)


class SandboxRuleTests(unittest.TestCase):
    def test_an_idle_sandbox_is_deleted_and_a_busy_one_is_not(self):
        api = FakeRunCloud(sandboxes=[sandbox("idle", 20, idle_min=11), sandbox("busy", 20, idle_min=1)])
        _, _, out, _ = run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/sandboxes/idle"])
        rec = by_id(out)
        self.assertIn("idle for 11 minutes", rec["idle"]["reason"])
        self.assertEqual(rec["idle"]["idle_minutes"], 11.0)
        self.assertEqual(rec["busy"]["decision"], "keep")

    def test_idleness_counts_from_the_resume_not_from_before_the_pause(self):
        # Last active two days ago, paused, resumed three minutes ago: idle three minutes, not two days.
        api = FakeRunCloud(sandboxes=[sandbox("resumed", 3, created_min=3000, last_min=2900)])
        _, _, out, _ = run(api)
        self.assertEqual(api.deleted(), [])
        self.assertEqual(by_id(out)["resumed"]["idle_minutes"], 3.0)
        self.assertEqual(by_id(out)["resumed"]["age_minutes"], 3.0, "age is the current run")

    def test_a_run_older_than_max_age_is_deleted(self):
        api = FakeRunCloud(sandboxes=[sandbox("long-run", 61, idle_min=0)])
        _, _, out, _ = run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/sandboxes/long-run"])
        self.assertIn("running for more than 60 minutes", by_id(out)["long-run"]["reason"])

    def test_limits_over_the_caps_are_deleted_after_the_grace_period(self):
        api = FakeRunCloud(sandboxes=[sandbox("big-lifetime", 6, idle_min=0, timeout=7200),
                                      sandbox("big-pause", 6, idle_min=0, idle_pause=1800),
                                      sandbox("young", 2, idle_min=0, timeout=7200)])
        _, _, out, _ = run(api)
        self.assertEqual(sorted(api.deleted()), ["/run-cloud/sandboxes/big-lifetime", "/run-cloud/sandboxes/big-pause"])
        self.assertIn("grace period", by_id(out)["young"]["reason"])

    def test_persistent_sandboxes_and_boxes_are_never_deleted_and_are_reported(self):
        api = FakeRunCloud(
            sandboxes=[sandbox("no-pause", 3000, idle_min=2000, idle_pause=0, timeout=0),
                       sandbox("no-lifetime", 3000, idle_min=2000, timeout=0),
                       sandbox("pause-zero", 3000, idle_min=2000, idle_pause=0),
                       sandbox("boxed", 3000, idle_min=2000)],
            boxes=[{"id": "box-1", "sandboxId": "boxed", "name": "site", "status": "ready"}])
        result, error, out, _ = run(api)
        self.assertIsNone(error)
        self.assertEqual(api.deleted(), [])
        rec = by_id(out)
        for sid in ("no-pause", "no-lifetime", "pause-zero", "boxed"):
            self.assertEqual(rec[sid]["decision"], "keep", sid)
            self.assertIn("persistent", rec[sid]["reason"], sid)
        self.assertEqual(rec["box-1"]["kind"], "box")
        summary = [r for r in lines(out) if r.get("summary")][0]
        self.assertEqual(sorted(summary["persistent"]), ["box box-1", "sandbox boxed", "sandbox no-lifetime",
                                                         "sandbox no-pause", "sandbox pause-zero"])

    def test_a_paused_or_stopped_sandbox_is_left_alone_and_a_destroyed_one_is_not_logged(self):
        api = FakeRunCloud(sandboxes=[sandbox("p", 3000, idle_min=2000, state="paused"),
                                      sandbox("s", 3000, idle_min=2000, state="stopped"),
                                      sandbox("d", 3000, idle_min=2000, state="destroyed")])
        _, _, out, _ = run(api)
        self.assertEqual(api.deleted(), [])
        rec = by_id(out)
        self.assertIn("not running", rec["p"]["reason"])
        self.assertNotIn("d", rec)

    def test_keep_true_sandbox_is_never_deleted(self):
        api = FakeRunCloud(sandboxes=[sandbox("k", 600, idle_min=500, tags={"keep": "true"})])
        run(api)
        self.assertEqual(api.deleted(), [])

    def test_a_sandbox_that_does_not_say_whether_it_is_persistent_is_an_error_never_deleted(self):
        api = FakeRunCloud(sandboxes=[sandbox("vague", 600, idle_min=500, drop=("idle_pause_seconds",))])
        _, error, out, _ = run(api)
        self.assertEqual(api.deleted(), [])
        self.assertEqual(by_id(out)["vague"]["decision"], "error")
        self.assertIsNotNone(error)

    def test_camel_case_fields_are_read_too(self):
        s = {"id": "camel", "state": "running", "idlePauseSeconds": 300, "timeoutSeconds": 1800,
             "createdAt": iso(NOW - 20 * 60), "lastActiveAt": iso(NOW - 15 * 60)}
        api = FakeRunCloud(sandboxes=[s])
        run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/sandboxes/camel"])

    def test_every_page_of_sandboxes_is_read(self):
        pages = {"": {"items": [sandbox("a", 61)], "nextCursor": "c1"},
                 "c1": {"items": [sandbox("b", 61)], "nextCursor": None}}
        api = FakeRunCloud(sandbox_pages=pages)
        run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/sandboxes/a", "/run-cloud/sandboxes/b"])
        self.assertIn(("GET", "/run-cloud/sandboxes?cursor=c1"), api.calls)

    def test_a_repeated_cursor_is_an_error_not_a_loop(self):
        api = FakeRunCloud(sandbox_pages={"": {"items": [], "nextCursor": "c1"}, "c1": {"items": [], "nextCursor": "c1"}})
        _, error, _, _ = run(api)
        self.assertIn("repeated a pagination cursor", str(error))

    def test_no_sandbox_is_judged_when_the_boxes_cannot_be_listed(self):
        # Without the box list, a sandbox behind a public hostname cannot be told from any other.
        api = FakeRunCloud(sandboxes=[sandbox("old", 600, idle_min=500)], ios=[sim("s", 90)], fail_list={"boxes"})
        _, error, _, _ = run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/ios/s"], "simulators are still handled")
        self.assertIsNotNone(error)


class RunTests(unittest.TestCase):
    def test_dry_run_deletes_nothing_publishes_nothing_and_reports_the_decisions(self):
        api = FakeRunCloud(ios=[sim("s", 90)], sandboxes=[sandbox("b", 90)])
        result, error, out, cw = run(api, dry_run=True)
        self.assertIsNone(error)
        self.assertEqual(api.deleted(), [])
        self.assertEqual([m for m, _ in api.calls], ["GET"] * len(api.calls))
        self.assertEqual(cw.puts, [])
        self.assertEqual({r["id"]: r["action"] for r in result["results"]}, {"s": "would-delete", "b": "would-delete"})
        self.assertTrue(all(r["dry_run"] for r in lines(out)))
        self.assertEqual(result["summary"]["would_delete"], 2)

    def test_a_failed_delete_does_not_stop_the_rest_and_fails_the_run_at_the_end(self):
        api = FakeRunCloud(ios=[sim("bad", 90), sim("ok1", 90)], android=[sim("ok2", 90)], sandboxes=[sandbox("ok3", 90)],
                           fail_delete={"bad"})
        result, error, out, cw = run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/ios/bad", "/run-cloud/ios/ok1", "/run-cloud/android/ok2",
                                         "/run-cloud/sandboxes/ok3"])
        self.assertIsInstance(error, RuntimeError)
        self.assertIn("delete ios bad", str(error))
        self.assertEqual(by_id(out)["bad"]["action"], "delete-failed")
        self.assertEqual(cw.metric("SessionsReaped"), 3, "the metrics are still published")
        self.assertIn('"summary": true', out, "the summary is still written")

    def test_a_session_already_gone_is_not_an_error(self):
        api = FakeRunCloud(ios=[sim("gone", 90)], gone={"gone"})
        _, error, out, cw = run(api)
        self.assertIsNone(error)
        self.assertEqual(by_id(out)["gone"]["action"], "already-gone")
        self.assertEqual(cw.metric("SessionsReaped"), 0)

    def test_a_delete_answered_with_no_body_or_plain_text_is_deleted(self):
        for answer in ((204, b""), (200, b"OK")):
            api = FakeRunCloud(sandboxes=[sandbox("b", 90)], delete_answer=answer)
            _, error, out, cw = run(api)
            self.assertIsNone(error, answer)
            self.assertEqual(by_id(out)["b"]["action"], "deleted", answer)
            self.assertEqual(cw.metric("SessionsReaped"), 1, answer)

    def test_a_session_without_an_id_is_an_error_and_nothing_is_deleted_for_it(self):
        nameless = sim("x", 90)
        del nameless["id"]
        api = FakeRunCloud(ios=[nameless, sim("y", 90)])
        _, error, out, _ = run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/ios/y"])
        self.assertIn("no id", str(error))

    def test_a_list_that_fails_does_not_stop_the_others(self):
        api = FakeRunCloud(ios=[sim("i", 90)], android=[sim("a", 90)], fail_list={"ios"})
        _, error, out, cw = run(api)
        self.assertEqual(api.deleted(), ["/run-cloud/android/a"])
        self.assertIn("list ios", str(error))
        self.assertIsNone(cw.metric("ActiveSessions"), "an incomplete count is not published")
        self.assertEqual(cw.metric("SessionsReaped"), 1)

    def test_metrics_in_the_run_cloud_namespace(self):
        api = FakeRunCloud(ios=[sim("old", 90), sim("fine", 10)], sandboxes=[sandbox("run", 10, idle_min=1),
                                                                             sandbox("paused", 10, state="paused")])
        _, error, _, cw = run(api)
        self.assertIsNone(error)
        self.assertEqual([ns for ns, _ in cw.puts], ["RunCloud"])
        self.assertEqual(cw.metric("SessionsReaped"), 1)
        self.assertEqual(cw.metric("ActiveSessions"), 3, "active simulators and running sandboxes, before deleting")

    def test_one_structured_line_per_session_seen(self):
        api = FakeRunCloud(ios=[sim("a", 10)], android=[sim("b", 90)], sandboxes=[sandbox("c", 10, idle_min=2)])
        _, _, out, _ = run(api)
        recs = [r for r in lines(out) if r.get("id")]
        self.assertEqual(sorted(r["id"] for r in recs), ["a", "b", "c"])
        for r in recs:
            for k in ("id", "kind", "age_minutes", "idle_minutes", "decision", "reason", "action"):
                self.assertIn(k, r)
        self.assertEqual({r["id"]: r["kind"] for r in recs}, {"a": "ios", "b": "android", "c": "sandbox"})
        self.assertEqual(sum(1 for r in lines(out) if r.get("summary")), 1)

    def test_the_numbers_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {"IDLE_MINUTES": "3", "MAX_AGE_MINUTES": "20", "GRACE_MINUTES": "1"}):
            api = FakeRunCloud(ios=[sim("s", 21, inactivity=60, hard_min=15), sim("t", 2, inactivity=240, hard_min=15)],
                               sandboxes=[sandbox("b", 10, idle_min=4)])
            _, _, out, _ = run(api)
        self.assertEqual(sorted(api.deleted()), ["/run-cloud/ios/s", "/run-cloud/ios/t", "/run-cloud/sandboxes/b"])
        self.assertEqual([r for r in lines(out) if r.get("summary")][0]["limits_minutes"], {"idle": 3, "max_age": 20, "grace": 1})

    def test_the_defaults_are_ten_sixty_and_five(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(rcr.limits_from_env(), rcr.Limits(600, 3600, 300))

    def test_the_key_is_read_from_the_secret_on_every_run_and_sent_as_a_bearer_token(self):
        sm = FakeSM()
        api = FakeRunCloud(ios=[sim("s", 10)])
        run(api, sm=sm)
        run(api, sm=sm)
        self.assertEqual(sm.asked, ["run-cloud/api-key", "run-cloud/api-key"])
        self.assertTrue(all(h["Authorization"] == "Bearer " + KEY for h in api.headers))

    def test_the_user_agent_is_not_pythons_default(self):
        # Cloudflare in front of api.run.cloud answers Python-urllib/3.x with 403.
        api = FakeRunCloud(ios=[sim("s", 90)])
        _, error, _, _ = run(api)
        self.assertIsNone(error)
        self.assertTrue(all(h["User-Agent"] == "run-cloud-reaper/1" for h in api.headers))

    def test_a_secret_without_the_key_fails_without_quoting_it(self):
        _, error, out, _ = run(FakeRunCloud(), sm=FakeSM(payload="not json " + KEY))
        self.assertIsInstance(error, RuntimeError)
        self.assertNotIn(KEY, str(error) + out)
        _, error, _, _ = run(FakeRunCloud(), sm=FakeSM(payload=json.dumps({"OTHER": KEY})))
        self.assertNotIn(KEY, str(error))


class SecretHygieneTests(unittest.TestCase):
    def test_the_key_never_appears_in_logs_errors_or_the_result_even_when_the_api_echoes_it(self):
        api = FakeRunCloud(ios=[sim("bad", 90), sim("ok", 10)], fail_delete={"bad"}, fail_list={"android"}, echo_key=True)
        result, error, out, _ = run(api)
        self.assertIsNotNone(error)
        self.assertIn("[redacted]", out, "the echoed key was there to be redacted")
        self.assertNotIn(KEY, out)
        self.assertNotIn(KEY, str(error))
        self.assertNotIn(KEY, json.dumps(result))
        _, _, out, _ = run(api, dry_run=True)
        result, _, _, _ = run(api, dry_run=True)
        self.assertNotIn(KEY, out + json.dumps(result))

    def test_nothing_is_printed_except_through_the_redacting_emitter(self):
        src = (ROOT / "scripts" / "run-cloud-reaper.py").read_text()
        self.assertEqual(len(re.findall(r"\bprint\(", src)), 1)
        self.assertIn("print(redact(json.dumps(record", src)


def block(text, kind, name):
    m = re.search(r'^(?:resource|data) "%s" "%s" \{\n.*?^\}' % (kind, name), text, re.S | re.M)
    assert m, "%s.%s missing" % (kind, name)
    return m.group()


class TerraformTests(unittest.TestCase):
    def test_it_runs_every_five_minutes_in_the_home_region(self):
        rule = block(TF, "aws_cloudwatch_event_rule", "run_cloud_reaper")
        self.assertIn('schedule_expression = "rate(5 minutes)"', rule)
        fn = block(TF, "aws_lambda_function", "run_cloud_reaper")
        self.assertIn('function_name    = "run-cloud-reaper"', fn)
        self.assertIn('runtime          = "python3.12"', fn)
        self.assertNotIn("provider", fn, "the default provider: us-west-1, where the secret is")
        self.assertIn('content  = file("${path.module}/scripts/run-cloud-reaper.py")', TF)
        self.assertIn("maximum_retry_attempts = 0", TF)

    def test_the_role_can_read_one_secret_write_its_own_logs_and_publish_its_own_namespace_and_nothing_else(self):
        role = block(TF, "aws_iam_role_policy", "run_cloud_reaper")
        actions = re.findall(r"[a-z]+:[A-Za-z*]+", " ".join(re.findall(r"Action\s*=\s*(\[[^\]]*\]|\"[^\"]*\")", role)))
        self.assertEqual(sorted(actions), ["cloudwatch:PutMetricData", "logs:CreateLogStream", "logs:PutLogEvents",
                                           "secretsmanager:GetSecretValue"])
        self.assertEqual(len(re.findall(r"Sid\s*=", role)), 3)
        self.assertRegex(role, r'Action   = "secretsmanager:GetSecretValue"\n\s*Resource = aws_secretsmanager_secret\.run_cloud_api_key\.arn\n')
        self.assertIn('Resource = "${aws_cloudwatch_log_group.run_cloud_reaper.arn}:*"', role)
        self.assertIn('Condition = { StringEquals = { "cloudwatch:namespace" = local.run_cloud_reaper_namespace } }', role)
        self.assertIn('run_cloud_reaper_namespace = "RunCloud"', TF)
        self.assertNotIn("Deny", role)

    def test_no_managed_policy_or_other_inline_policy_is_attached_to_the_role(self):
        for path in ROOT.glob("*.tf"):
            text = path.read_text()
            self.assertNotRegex(text, r"role\s*=\s*aws_iam_role\.run_cloud_reaper\.name",
                                "%s attaches something to the reaper's role" % path.name)
            self.assertEqual(len(re.findall(r"role\s*=\s*aws_iam_role\.run_cloud_reaper\.id", text)),
                             1 if path.name == "run-cloud-reaper.tf" else 0, path.name)
        self.assertNotIn("policy_arn", TF, "no managed policy (AWSLambdaBasicExecutionRole writes to every log group)")

    def test_its_log_group_exists_first_and_is_kept_thirty_days(self):
        group = block(TF, "aws_cloudwatch_log_group", "run_cloud_reaper")
        self.assertIn('name              = "/aws/lambda/run-cloud-reaper"', group)
        self.assertIn("retention_in_days = 30", group)
        fn = block(TF, "aws_lambda_function", "run_cloud_reaper")
        for dep in ("aws_cloudwatch_log_group.run_cloud_reaper", "aws_iam_role_policy.run_cloud_reaper",
                    "aws_secretsmanager_secret_policy.run_cloud_api_key"):
            self.assertIn(dep, fn.split("depends_on")[1])

    def test_the_secret_policy_lists_the_reaper_role_for_this_secret_only(self):
        policy = block(AI_TF, "aws_secretsmanager_secret_policy", "run_cloud_api_key")
        self.assertIn('Effect    = "Deny"', policy)
        self.assertIn('"aws:PrincipalArn" = concat(local.games_mp_admin_principals, local.run_cloud_api_key_readers)', policy)
        self.assertIn("run_cloud_api_key_readers = concat(local.elevenlabs_readers, [aws_iam_role.run_cloud_reaper.arn])", AI_TF)
        self.assertIn("elevenlabs_readers = [aws_iam_role.dev_server.arn, aws_iam_role.nextjs_dev.arn]", AI_TF,
                      "ElevenLabs keeps its two readers")
        # No other secret, here or anywhere, names the reaper.
        self.assertEqual(AI_TF.count("run_cloud_api_key_readers"), 2)
        self.assertEqual(AI_TF.count("aws_iam_role.run_cloud_reaper"), 1)
        for path in ROOT.glob("*.tf"):
            if path.name not in ("run-cloud-reaper.tf", "dev-ai-services.tf"):
                self.assertNotIn("run_cloud_reaper", path.read_text(), path.name)

    def test_the_numbers_are_variables_with_defaults_wired_into_the_environment(self):
        for var, default, env in (("run_cloud_reaper_idle_minutes", 10, "IDLE_MINUTES"),
                                  ("run_cloud_reaper_max_age_minutes", 60, "MAX_AGE_MINUTES"),
                                  ("run_cloud_reaper_grace_minutes", 5, "GRACE_MINUTES")):
            self.assertRegex(TF, r'variable "%s" \{[^}]*default     = %d\n' % (var, default))
            self.assertRegex(TF, r"%s\s*= tostring\(var\.%s\)" % (env, var))
        self.assertIn('API_URL          = "https://api.run.cloud"', TF)
        self.assertIn("SECRET_ID        = aws_secretsmanager_secret.run_cloud_api_key.name", TF)

    def test_both_alarms_go_to_the_alert_topic_and_silence_is_an_alarm(self):
        errors = block(TF, "aws_cloudwatch_metric_alarm", "run_cloud_reaper_errors")
        self.assertIn('metric_name         = "Errors"', errors)
        self.assertIn("aws_sns_topic.cost_alerts.arn", errors)
        silent = block(TF, "aws_cloudwatch_metric_alarm", "run_cloud_reaper_not_running")
        self.assertIn('metric_name         = "Invocations"', silent)
        self.assertIn("period              = 900", silent)
        self.assertIn('treat_missing_data  = "breaching"', silent)
        self.assertIn("aws_sns_topic.cost_alerts.arn", silent)


class DocsTests(unittest.TestCase):
    def test_agents_md_and_readme_say_what_it_does_and_its_defaults(self):
        for name in ("AGENTS.md", "README.md"):
            text = (ROOT / name).read_text()
            self.assertIn("run-cloud-reaper", text, name)
            for phrase in ("keep=true", "10 minutes", "60 minutes", "5 minutes", "persistent"):
                self.assertIn(phrase, text[text.index("run-cloud-reaper") - 1500:text.index("run-cloud-reaper") + 1500], (name, phrase))


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

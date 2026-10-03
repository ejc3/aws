#!/usr/bin/env python3
"""Offline checks for imagine/scale.py, the function that wakes and sleeps the imagine backend.

It is the only thing that changes the ECS service's desired count, and nothing runs it
before it is deployed: a mistake here either leaves Fargate tasks running for nobody or
stops a backend people are using. This imports the real file and drives it with a fake ECS
client and, for the status request, a real HTTP server on localhost. No AWS credentials,
no boto3.

Run from the repo root:  python3 -S -B scripts/test-imagine-scale.py
"""
import http.server
import importlib.util
import json
import os
import re
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOW = 1_800_000_000
ARN = "arn:aws:ecs:us-west-1:928413605543:service/imagine/imagine"


def load_scale(**env):
    for key in ("CLUSTER", "SERVICE", "AWAKE_COUNT", "IDLE_SEC", "GRACE_SEC", "TOUCH_SEC", "STATUS_URL",
                "STATUS_TIMEOUT_SEC", "SNS_TOPIC_ARN"):
        os.environ.pop(key, None)
    os.environ["STATUS_URL"] = "https://imagine.example/api/status"
    os.environ["SNS_TOPIC_ARN"] = "arn:aws:sns:us-west-1:928413605543:cost-alerts"
    os.environ.update(env)
    spec = importlib.util.spec_from_file_location("scale", ROOT / "imagine" / "scale.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeECS:
    def __init__(self, desired=0, running=0, tags=None, status="ACTIVE", missing=False):
        self.desired, self.running, self.status, self.missing = desired, running, status, missing
        self.tags = dict(tags or {})
        self.calls = []

    def describe_services(self, cluster, services, include):
        assert (cluster, services, include) == ("imagine", ["imagine"], ["TAGS"]), (cluster, services, include)
        if self.missing:
            return {"services": [], "failures": [{"arn": ARN, "reason": "MISSING"}]}
        return {"failures": [], "services": [{
            "serviceArn": ARN, "status": self.status,
            "desiredCount": self.desired, "runningCount": self.running,
            "tags": [{"key": k, "value": v} for k, v in self.tags.items()],
        }]}

    def update_service(self, cluster, service, **changes):
        assert (cluster, service) == ("imagine", "imagine"), (cluster, service)
        self.calls.append(("update_service", changes))
        if "desiredCount" in changes:
            self.desired = changes["desiredCount"]

    def tag_resource(self, resourceArn, tags):
        assert resourceArn == ARN, resourceArn
        for tag in tags:
            self.calls.append(("tag", tag["key"], tag["value"]))
            self.tags[tag["key"]] = tag["value"]

    def updates(self):
        return [c[1] for c in self.calls if c[0] == "update_service"]

    def tagged(self):
        return [c[1:] for c in self.calls if c[0] == "tag"]


class FakeSNS:
    def __init__(self):
        self.published = []

    def publish(self, TopicArn, Subject, Message):
        self.published.append((TopicArn, Subject, Message))


WANTED, UNREACHABLE = "imagine:wanted-at", "imagine:unreachable-since"


class Case(unittest.TestCase):
    def setup(self, backend="unset", **ecs):
        """Load scale.py with a fake service (`ecs`: FakeECS's arguments). `backend` is what
        the backend answers when asked for its status: a dict, or None for no usable answer;
        left unset, asking it at all fails the test."""
        self.scale = load_scale()
        self.ecs, self.sns = FakeECS(**ecs), FakeSNS()
        self.scale._clients.update(ecs=self.ecs, sns=self.sns)
        self.asked = 0

        def fetch():
            self.asked += 1
            if backend == "unset":
                raise AssertionError("the backend was asked for its status")
            return backend

        self.scale.fetch_status = fetch
        return self.scale


class WakeTest(Case):
    def test_asleep_starts_the_awake_count_on_a_new_deployment(self):
        scale = self.setup(desired=0)
        self.assertEqual(scale.wake(NOW), {"state": "waking", "desired": 2, "running": 0})
        self.assertEqual(self.ecs.updates(), [{"desiredCount": 2, "forceNewDeployment": True}])

    def test_asleep_records_the_wake_and_clears_unreachable_before_starting(self):
        scale = self.setup(desired=0, tags={WANTED: str(NOW - 9000), UNREACHABLE: str(NOW - 8000)})
        scale.wake(NOW)
        self.assertEqual(self.ecs.calls, [
            ("tag", WANTED, str(NOW)),
            ("tag", UNREACHABLE, "0"),
            ("update_service", {"desiredCount": 2, "forceNewDeployment": True}),
        ])

    def test_awake_with_a_running_task_changes_no_count(self):
        scale = self.setup(desired=2, running=2, tags={WANTED: str(NOW - 5)})
        self.assertEqual(scale.wake(NOW), {"state": "awake", "desired": 2, "running": 2})
        self.assertEqual(self.ecs.calls, [])

    def test_awake_with_one_of_two_running_is_awake(self):
        scale = self.setup(desired=2, running=1, tags={WANTED: str(NOW)})
        self.assertEqual(scale.wake(NOW)["state"], "awake")

    def test_started_but_nothing_running_yet_is_waking(self):
        scale = self.setup(desired=2, running=0, tags={WANTED: str(NOW - 5)})
        self.assertEqual(scale.wake(NOW), {"state": "waking", "desired": 2, "running": 0})
        self.assertEqual(self.ecs.updates(), [])

    def test_awake_rerecords_the_wake_only_once_it_is_touch_sec_old(self):
        scale = self.setup(desired=2, running=2, tags={WANTED: str(NOW - 59)})
        scale.wake(NOW)
        self.assertEqual(self.ecs.tagged(), [])
        scale.wake(NOW + 1)
        self.assertEqual(self.ecs.tagged(), [(WANTED, str(NOW + 1))])
        self.assertEqual(self.ecs.updates(), [])

    def test_awake_with_no_wake_recorded_records_one(self):
        for tags in ({}, {WANTED: "soon"}, {WANTED: "-5"}, {WANTED: ""}):
            scale = self.setup(desired=2, running=2, tags=tags)
            scale.wake(NOW)
            self.assertEqual(self.ecs.tagged(), [(WANTED, str(NOW))], tags)

    def test_a_service_that_is_missing_or_not_active_raises_and_changes_nothing(self):
        for ecs in ({"missing": True}, {"status": "DRAINING"}, {"status": "INACTIVE"}):
            scale = self.setup(**ecs)
            for action in (scale.wake, scale.sweep, scale.deploy):
                with self.assertRaises(RuntimeError):
                    action(NOW)
            self.assertEqual(self.ecs.calls, [], ecs)


class SweepTest(Case):
    IDLE = {"sockets": 0, "idle_ms": 300_000}

    def test_asleep_does_nothing_and_does_not_ask_the_backend(self):
        scale = self.setup(desired=0)
        self.assertEqual(scale.sweep(NOW), {"did": "nothing", "reason": "asleep"})
        self.assertEqual((self.ecs.calls, self.asked), ([], 0))

    def test_idle_for_idle_sec_with_no_recent_wake_sleeps(self):
        scale = self.setup(backend=self.IDLE, desired=2, running=2, tags={WANTED: str(NOW - 300)})
        self.assertEqual(scale.sweep(NOW), {"did": "sleep", "reason": "idle"})
        self.assertEqual(self.ecs.updates(), [{"desiredCount": 0}])
        self.assertEqual(self.sns.published, [])

    def test_open_sockets_keep_it_awake_however_old_the_wake(self):
        scale = self.setup(backend={"sockets": 1, "idle_ms": 0}, desired=2, running=2, tags={WANTED: "1"})
        self.assertEqual(scale.sweep(NOW)["reason"], "in use")
        self.assertEqual(self.ecs.calls, [])

    def test_idle_for_less_than_idle_sec_stays_awake(self):
        scale = self.setup(backend={"sockets": 0, "idle_ms": 299_999}, desired=2, running=2, tags={WANTED: "1"})
        self.assertEqual(scale.sweep(NOW)["reason"], "in use")
        self.assertEqual(self.ecs.updates(), [])

    def test_a_wake_within_idle_sec_stays_awake_even_if_the_backend_is_idle(self):
        # Someone asked for it 299 s ago and has not connected yet (a cold start, a slow page).
        scale = self.setup(backend={"sockets": 0, "idle_ms": 9_000_000}, desired=2, running=2,
                           tags={WANTED: str(NOW - 299)})
        self.assertEqual(scale.sweep(NOW)["reason"], "in use")
        self.assertEqual(self.ecs.updates(), [])

    def test_no_answer_is_recorded_once_and_tolerated_for_grace_sec(self):
        scale = self.setup(backend=None, desired=2, running=0, tags={WANTED: str(NOW)})
        self.assertEqual(scale.sweep(NOW), {"did": "nothing", "reason": "unreachable", "for_sec": 0})
        self.assertEqual(self.ecs.calls, [("tag", UNREACHABLE, str(NOW))])

        self.assertEqual(scale.sweep(NOW + 599), {"did": "nothing", "reason": "unreachable", "for_sec": 599})
        self.assertEqual(self.ecs.calls, [("tag", UNREACHABLE, str(NOW))])
        self.assertEqual(self.sns.published, [])

    def test_no_answer_for_grace_sec_sleeps_and_reports_it(self):
        scale = self.setup(backend=None, desired=2, running=1,
                           tags={WANTED: str(NOW), UNREACHABLE: str(NOW - 600)})
        self.assertEqual(scale.sweep(NOW), {"did": "sleep", "reason": "unreachable"})
        self.assertEqual(self.ecs.updates(), [{"desiredCount": 0}])
        (topic, subject, message), = self.sns.published
        self.assertEqual(topic, "arn:aws:sns:us-west-1:928413605543:cost-alerts")
        self.assertIn("not answering", subject)
        self.assertIn("600 s", message)

    def test_a_recent_wake_does_not_keep_a_backend_that_never_answers(self):
        # Visitors keep asking for a backend that is broken; each ask is a fresh wake.
        scale = self.setup(backend=None, desired=2, running=0,
                           tags={WANTED: str(NOW), UNREACHABLE: str(NOW - 601)})
        self.assertEqual(scale.sweep(NOW)["did"], "sleep")

    def test_an_answer_clears_unreachable_once(self):
        scale = self.setup(backend={"sockets": 3, "idle_ms": 0}, desired=2, running=2,
                           tags={WANTED: str(NOW), UNREACHABLE: str(NOW - 500)})
        scale.sweep(NOW)
        self.assertEqual(self.ecs.calls, [("tag", UNREACHABLE, "0")])
        scale.sweep(NOW + 60)
        self.assertEqual(self.ecs.calls, [("tag", UNREACHABLE, "0")])

    def test_one_missed_answer_after_a_long_healthy_run_does_not_sleep(self):
        scale = self.setup(backend=None, desired=2, running=2, tags={WANTED: str(NOW - 86_400), UNREACHABLE: "0"})
        self.assertEqual(scale.sweep(NOW)["did"], "nothing")
        self.assertEqual(self.ecs.updates(), [])

    def test_a_wake_after_an_unreachable_stop_starts_the_grace_period_again(self):
        scale = self.setup(backend=None, desired=2, running=0,
                           tags={WANTED: str(NOW - 700), UNREACHABLE: str(NOW - 700)})
        self.assertEqual(scale.sweep(NOW)["did"], "sleep")
        scale.wake(NOW + 5)
        self.assertEqual(scale.sweep(NOW + 60), {"did": "nothing", "reason": "unreachable", "for_sec": 0})
        self.assertEqual(self.ecs.desired, 2)


class DeployTest(Case):
    def test_asleep_does_nothing(self):
        scale = self.setup(desired=0)
        self.assertEqual(scale.deploy(NOW), {"did": "nothing", "reason": "asleep"})
        self.assertEqual(self.ecs.calls, [])

    def test_awake_rolls_without_touching_the_count(self):
        scale = self.setup(desired=2, running=2)
        self.assertEqual(scale.deploy(NOW), {"did": "rolled"})
        self.assertEqual(self.ecs.updates(), [{"forceNewDeployment": True}])


class HandlerTest(Case):
    def test_dispatches_by_action(self):
        scale = self.setup(desired=0)
        self.assertEqual(scale.lambda_handler({"action": "wake"}, None)["state"], "waking")
        self.assertEqual(scale.lambda_handler({"action": "deploy"}, None), {"did": "rolled"})

    def test_anything_else_raises_and_changes_nothing(self):
        scale = self.setup(desired=0)
        for event in ({}, {"action": "sleep"}, {"action": None}, {"action": ["wake"]}, None, "wake", []):
            with self.assertRaises(ValueError):
                scale.lambda_handler(event, None)
        self.assertEqual(self.ecs.calls, [])

    def test_the_count_is_only_ever_set_to_zero_or_the_awake_count(self):
        # Whatever the event carries beside the action, there is no path to another count.
        scale = self.setup(backend={"sockets": 0, "idle_ms": 10**9}, desired=0)
        events = [{"action": a, "desiredCount": 50, "count": 50, "AWAKE_COUNT": 50}
                  for a in ("wake", "sweep", "deploy", "wake", "sweep", "wake")]
        for event in events:
            scale.lambda_handler(event, None)
        counts = {u["desiredCount"] for u in self.ecs.updates() if "desiredCount" in u}
        self.assertTrue(counts and counts <= {0, 2}, counts)


class StatusServer:
    """A real HTTP server on localhost answering GET with whatever the test sets."""

    def __init__(self):
        outer = self
        self.code, self.body = 200, b"{}"

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(outer.code)
                self.send_header("Content-Length", str(len(outer.body)))
                self.end_headers()
                self.wfile.write(outer.body)

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/api/status" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FetchStatusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = StatusServer()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def fetch(self, body, code=200):
        self.server.code = code
        self.server.body = body if isinstance(body, bytes) else json.dumps(body).encode()
        return load_scale(STATUS_URL=self.server.url).fetch_status()

    def test_reads_sockets_and_idle_ms(self):
        document = {"node": "imagine@10.0.70.5", "nodes": ["imagine@10.0.70.5"], "sockets": 3,
                    "idle_ms": 0, "documents": 2}
        self.assertEqual(self.fetch(document), {"sockets": 3, "idle_ms": 0})

    def test_anything_but_two_non_negative_integers_is_no_answer(self):
        for body in (
            {"sockets": 0}, {"idle_ms": 5}, {"sockets": "0", "idle_ms": 5}, {"sockets": 0, "idle_ms": "5"},
            {"sockets": -1, "idle_ms": 5}, {"sockets": 0, "idle_ms": -5}, {"sockets": 0.0, "idle_ms": 5},
            {"sockets": True, "idle_ms": 5}, {"sockets": None, "idle_ms": 5}, [0, 5], "ok", 7, None,
            b"", b"ok", b"{", b"<html>502 Bad Gateway</html>",
        ):
            self.assertIsNone(self.fetch(body), body)

    def test_an_error_status_is_no_answer_even_with_a_valid_body(self):
        for code in (404, 500, 502, 503):
            self.assertIsNone(self.fetch({"sockets": 0, "idle_ms": 999_999}, code), code)

    def test_nothing_listening_is_no_answer(self):
        self.assertIsNone(load_scale(STATUS_URL="http://127.0.0.1:1/api/status").fetch_status())

    def test_only_https_is_asked_apart_from_this_test_s_own_server(self):
        for url in ("", "http://imagine.example/api/status", "file:///etc/passwd", "ftp://x/y"):
            self.assertIsNone(load_scale(STATUS_URL=url).fetch_status(), url)

    def test_an_answer_larger_than_any_status_is_not_read_whole(self):
        padded = json.dumps({"sockets": 0, "idle_ms": 1, "pad": "x" * 100_000}).encode()
        self.assertIsNone(self.fetch(padded))


class WiringTest(unittest.TestCase):
    """What imagine.tf must say for the function above to be what runs."""

    tf = (ROOT / "imagine.tf").read_text()

    def block(self, header):
        """A top-level block's text, from its header to its closing brace."""
        start = self.tf.index(header)
        return self.tf[start:self.tf.index("\n}\n", start)]

    def test_the_function_is_built_from_this_file_and_runs_one_at_a_time(self):
        self.assertRegex(self.tf, r'source_file\s+= "\$\{path\.module\}/imagine/scale\.py"')
        function = self.block('resource "aws_lambda_function" "imagine_scale"')
        self.assertRegex(function, r'handler\s+= "scale\.lambda_handler"')
        self.assertRegex(function, r"reserved_concurrent_executions\s+= 1\n")

    def test_terraform_never_sets_the_count_back(self):
        service = self.block('resource "aws_ecs_service" "imagine"')
        self.assertRegex(service, r"desired_count\s+= 0\n")
        # The count is the function's, and so are the tags it keeps its state in.
        self.assertRegex(service, r"ignore_changes\s+= \[desired_count, tags, tags_all\]")

    def test_the_function_can_reach_only_the_one_service(self):
        policy = self.block('resource "aws_iam_role_policy" "imagine_scale"')
        self.assertEqual(policy.count("ecs:"), 4)
        self.assertRegex(policy, r'Action\s+= \["ecs:DescribeServices", "ecs:UpdateService", "ecs:TagResource", '
                                 r'"ecs:ListTagsForResource"\]\s+Resource\s+= aws_ecs_service\.imagine\.id')

    def test_the_callers_can_only_invoke_the_function(self):
        for role in ("imagine_waker", "imagine_scale_scheduler"):
            policy = self.block('resource "aws_iam_role_policy" "%s"' % role)
            self.assertEqual(re.findall(r'Action\s+= (.*)', policy), ['"lambda:InvokeFunction"'], role)
        deploy = self.block('resource "aws_iam_role_policy" "imagine_deploy"')
        self.assertNotIn("ecs:", deploy)
        self.assertNotIn("iam:", deploy)

    def test_only_production_deployments_and_the_main_branch_are_trusted(self):
        waker = self.block('resource "aws_iam_role" "imagine_waker"')
        self.assertIn("project:${local.imagine_vercel_project}:environment:production", waker)
        self.assertNotIn("StringLike", waker)
        deploy = self.block('resource "aws_iam_role" "imagine_deploy"')
        # The form GitHub issues for this repository: owner and repository by ID and name.
        self.assertIn('"repo:ejc3@1694850/imagine@1403409773:ref:refs/heads/main"', deploy)
        self.assertNotIn('"repo:ejc3/imagine:', deploy)
        self.assertNotIn("StringLike", deploy)

    def test_every_setting_the_function_reads_is_set(self):
        source = (ROOT / "imagine" / "scale.py").read_text()
        read = set(re.findall(r'os\.environ\.get\("([A-Z_]+)"', source))
        function = self.block('resource "aws_lambda_function" "imagine_scale"')
        set_ = set(re.findall(r"^\s+([A-Z_]+)\s+=", function, re.M))
        # STATUS_TIMEOUT_SEC keeps its default.
        self.assertEqual(read - {"STATUS_TIMEOUT_SEC"}, set_)


if __name__ == "__main__":
    unittest.main(verbosity=1)

#!/usr/bin/env python3
"""Offline checks for games-multiplayer/sweeper.py, the match-engine backstop.

The sweeper is the one thing that stops a hung Fargate engine from billing for a month,
and nothing runs it before it is deployed. This imports the real file and drives it with
a fake ECS client: no AWS credentials, no network, no boto3.

Run from the repo root:  python3 -S -B scripts/test-games-mp-sweeper.py
"""
import datetime
import importlib.util
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime.datetime(2026, 9, 26, 20, 0, 0, tzinfo=datetime.timezone.utc)
CLUSTER_ARN_PREFIX = "arn:aws:ecs:us-west-1:928413605543:task/games/"


def load_sweeper():
    for key in ("CLUSTER", "GRACE_SEC", "DEFAULT_LIMIT_SEC", "MAX_HARDCAP_SEC", "SNS_TOPIC_ARN", "ROUTER_FAMILY",
                "ENGINE_CEILING", "METRIC_NAMESPACE"):
        os.environ.pop(key, None)
    os.environ["SNS_TOPIC_ARN"] = "arn:aws:sns:us-west-1:928413605543:cost-alerts"
    spec = importlib.util.spec_from_file_location("sweeper", ROOT / "games-multiplayer" / "sweeper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


TD_PREFIX = "arn:aws:ecs:us-west-1:928413605543:task-definition/"


def task(n, age_sec, tags=None, group="family:games-mptest", status="RUNNING",
         family="games-mptest", started_by=None):
    t = {
        "taskArn": CLUSTER_ARN_PREFIX + "t%03d" % n,
        "taskDefinitionArn": TD_PREFIX + "%s:7" % family,
        "group": group,
        "lastStatus": status,
        "createdAt": NOW - datetime.timedelta(seconds=age_sec),
        "tags": [{"key": k, "value": v} for k, v in (tags or {}).items()],
    }
    if started_by:
        t["startedBy"] = started_by
    return t


class FakeECS:
    def __init__(self, tasks, stop_error=None, page_size=100, fail_stop=()):
        self.fail_stop = set(fail_stop)   # task ids (tNNN) whose StopTask fails
        self.tasks = {t["taskArn"]: t for t in tasks}
        self.stopped = []
        self.stop_error = stop_error
        self.page_size = page_size
        self.calls = []

    def list_tasks(self, cluster, desiredStatus, maxResults, nextToken=None):
        self.calls.append(("list_tasks", cluster, desiredStatus))
        arns = sorted(self.tasks)
        start = int(nextToken or 0)
        page = arns[start : start + self.page_size]
        out = {"taskArns": page}
        if start + self.page_size < len(arns):
            out["nextToken"] = str(start + self.page_size)
        return out

    def describe_tasks(self, cluster, tasks, include):
        assert include == ["TAGS"], include
        assert len(tasks) <= 100, len(tasks)
        self.calls.append(("describe_tasks", cluster, len(tasks)))
        return {"tasks": [self.tasks[a] for a in tasks]}

    def stop_task(self, cluster, task, reason):
        assert len(reason) <= 255
        if self.stop_error:
            raise self.stop_error
        if task.rsplit("/", 1)[1] in self.fail_stop:
            raise RuntimeError("StopTask failed")
        self.stopped.append((cluster, task, reason))


class FakeCloudWatch:
    def __init__(self, error=None):
        self.put, self.error = [], error

    def put_metric_data(self, Namespace, MetricData):
        if self.error:
            raise self.error
        self.put.append((Namespace, {m["MetricName"]: m["Value"] for m in MetricData}))


class FakeSNS:
    def __init__(self):
        self.published = []

    def publish(self, TopicArn, Subject, Message):
        self.published.append((TopicArn, Subject, Message))


class SweeperTests(unittest.TestCase):
    def setUp(self):
        self.sw = load_sweeper()
        self.sns = FakeSNS()
        self.cw = FakeCloudWatch()

    def run_with(self, tasks, **kw):
        ecs = FakeECS(tasks, **kw)
        self.sw._clients.update({"ecs": ecs, "sns": self.sns, "cloudwatch": self.cw})
        return ecs, self.sw.sweep(now=NOW)

    def stopped_ids(self, ecs):
        return sorted(t.rsplit("/", 1)[1] for _, t, _ in ecs.stopped)

    def test_hardcap_plus_ten_minutes(self):
        ecs, _ = self.run_with([
            task(1, 3600 + 600, {"match": "m1", "hardcap": "3600"}),       # exactly at limit: keep
            task(2, 3600 + 601, {"match": "m2", "hardcap": "3600"}),       # one second over: stop
            task(3, 300, {"match": "m3", "hardcap": "60"}),                # 60+600 > 300: keep
            task(4, 661, {"match": "m4", "hardcap": "60"}),                # stop
        ])
        self.assertEqual(self.stopped_ids(ecs), ["t002", "t004"])
        self.assertTrue(all(c == "games" for c, _, _ in ecs.stopped))

    def test_missing_or_bad_hardcap_means_two_hours(self):
        ecs, _ = self.run_with([
            task(1, 7200, {"match": "m1"}),
            task(2, 7201, {"match": "m2"}),
            task(3, 7201, {"match": "m3", "hardcap": "soon"}),
            task(4, 7201, {"match": "m4", "hardcap": "-5"}),
            task(5, 7201, {"match": "m5", "hardcap": "0"}),
            task(6, 7199, {"match": "m6", "hardcap": ""}),
        ])
        self.assertEqual(self.stopped_ids(ecs), ["t002", "t003", "t004", "t005"])

    def test_hardcap_is_clamped(self):
        # A launcher cannot buy a task a year of runtime with a huge tag.
        ecs, _ = self.run_with([
            task(1, 14400 + 601, {"match": "m1", "hardcap": str(365 * 86400)}),
            task(2, 14400 + 599, {"match": "m2", "hardcap": str(365 * 86400)}),
        ])
        self.assertEqual(self.stopped_ids(ecs), ["t001"])

    def test_router_family_tasks_are_never_touched(self):
        ecs, results = self.run_with([
            task(1, 90 * 86400, {}, group="service:mp-router", family="games-mp-router"),
            task(2, 90 * 86400, {"match": "x", "hardcap": "1"}, group="service:mp-router",
                 family="games-mp-router"),
        ])
        self.assertEqual(ecs.stopped, [])
        self.assertEqual(results, [])

    def test_launcher_spoofable_fields_never_exempt_an_engine(self):
        # RunTask callers set group, startedBy and tags. An engine that claims to be the
        # router through any of them is still swept (Codex review, 1784331).
        ecs, _ = self.run_with([
            task(1, 7201, {}, group="service:mp-router"),
            task(2, 7201, {"match": "m2"}, group="service:mp-router",
                 started_by="ecs-svc/1234567890123456789"),
            task(3, 7201, {"games-role": "router", "aws:ecs:serviceName": "mp-router"}),
            task(4, 7201, {"match": "m4"}, group="family:games-mp-router"),
        ])
        self.assertEqual(self.stopped_ids(ecs), ["t001", "t002", "t003", "t004"])

    def test_family_is_matched_exactly(self):
        # A game id that merely starts with the router's name is an engine.
        ecs, _ = self.run_with([
            task(1, 7201, {}, family="games-mp-router2"),
            task(2, 7201, {}, family="games-mp-route"),
        ])
        self.assertEqual(self.stopped_ids(ecs), ["t001", "t002"])
        self.assertEqual(self.sw.task_family({}), "")

    def test_untagged_standalone_task_still_gets_the_default_limit(self):
        # A launcher bug that drops the tags must not make a task immortal.
        ecs, _ = self.run_with([task(1, 7201, {}, group="family:games-mptest")])
        self.assertEqual(self.stopped_ids(ecs), ["t001"])

    def test_pending_tasks_are_aged_and_stopping_tasks_skipped(self):
        ecs, _ = self.run_with([
            task(1, 9000, {"match": "m1"}, status="PENDING"),
            task(2, 9000, {"match": "m2"}, status="PROVISIONING"),
            task(3, 9000, {"match": "m3"}, status="STOPPING"),
            task(4, 9000, {"match": "m4"}, status="DEPROVISIONING"),
        ])
        self.assertEqual(self.stopped_ids(ecs), ["t001", "t002"])
        self.assertTrue(all(c[2] == "RUNNING" for c in ecs.calls if c[0] == "list_tasks"))

    def test_pages_and_describe_batches(self):
        tasks = [task(i, 9000, {"match": "m%d" % i}) for i in range(250)]
        ecs, results = self.run_with(tasks, page_size=100)
        self.assertEqual(len(ecs.stopped), 250)
        self.assertEqual(len(results), 250)
        self.assertEqual([c[2] for c in ecs.calls if c[0] == "describe_tasks"], [100, 100, 50])

    def test_failed_stop_alerts_and_keeps_going(self):
        ecs, results = self.run_with(
            [task(1, 9000, {"match": "m1", "game": "mptest"}), task(2, 9000, {"match": "m2"})],
            stop_error=RuntimeError("AccessDenied"),
        )
        self.assertEqual([r["action"] for r in results], ["stop_failed", "stop_failed"])
        self.assertEqual(len(self.sns.published), 2)
        self.assertIn("m1", self.sns.published[0][2])

    def test_routine_stop_does_not_page(self):
        self.run_with([task(1, 9000, {"match": "m1"})])
        self.assertEqual(self.sns.published, [])

    def test_ceiling_stops_the_newest_engines_and_never_the_router(self):
        self.sw.ENGINE_CEILING = 3
        tasks = [task(n, 1000 - n * 10, {"match": "m%d" % n, "hardcap": "3600"}) for n in range(1, 7)]
        tasks.append(task(99, 5, family="games-mp-router", group="service:mp-router"))   # newest of all
        ecs, results = self.run_with(tasks)
        # Six engines, ceiling 3: the three newest (t004, t005, t006) go; the router is not counted.
        self.assertEqual(self.stopped_ids(ecs), ["t004", "t005", "t006"])
        self.assertTrue(all("ceiling 3" in r for _, _, r in ecs.stopped))
        self.assertEqual([s for _, s, _ in self.sns.published], ["games-mp-sweeper: engine ceiling reached"])
        self.assertEqual(self.cw.put[-1], ("GamesMultiplayer", {"RunningEngines": 3, "EnginesStopped": 3}))

    def test_a_ceiling_of_zero_stops_every_engine_and_never_the_router(self):
        self.sw.ENGINE_CEILING = 0
        tasks = [task(n, 1000 - n * 10, {"match": "m%d" % n}) for n in range(1, 4)]
        tasks.append(task(99, 5, family="games-mp-router", group="service:mp-router"))
        ecs, _ = self.run_with(tasks)
        self.assertEqual(self.stopped_ids(ecs), ["t001", "t002", "t003"])

    def test_a_failed_age_stop_still_counts_toward_the_ceiling(self):
        # Ceiling 3: one over-age engine whose stop fails is still running, so with three healthy
        # engines there are four, and the newest healthy one must go.
        self.sw.ENGINE_CEILING = 3
        ecs, _ = self.run_with([task(1, 9000, {}), task(2, 300, {}), task(3, 200, {}), task(4, 100, {})],
                               fail_stop={"t001"})
        self.assertIn("t004", self.stopped_ids(ecs))

    def test_at_or_below_the_ceiling_nothing_is_stopped(self):
        self.sw.ENGINE_CEILING = 3
        ecs, _ = self.run_with([task(n, 100, {"match": "m%d" % n}) for n in range(1, 4)])
        self.assertEqual(ecs.stopped, [])
        self.assertEqual(self.sns.published, [])

    def test_age_stops_count_before_the_ceiling(self):
        # An over-age engine stopped for age no longer counts toward the ceiling.
        self.sw.ENGINE_CEILING = 2
        ecs, _ = self.run_with([task(1, 9000, {}), task(2, 100, {}), task(3, 50, {})])
        self.assertEqual(self.stopped_ids(ecs), ["t001"])

    def test_running_engines_is_published_every_run_and_a_metrics_outage_does_not_fail_it(self):
        self.run_with([task(1, 100, {"match": "a"})])
        self.assertEqual(self.cw.put[-1], ("GamesMultiplayer", {"RunningEngines": 1, "EnginesStopped": 0}))
        self.cw.error = RuntimeError("Throttling")
        ecs, results = self.run_with([task(1, 9000, {})])
        self.assertEqual(self.stopped_ids(ecs), ["t001"], "the sweep still acts when metrics fail")

    def test_handler_summary(self):
        self.sw._clients.update({"ecs": FakeECS([task(1, 10, {"match": "a"}), task(2, 9000, {})]), "sns": self.sns,
                                 "cloudwatch": self.cw})
        orig = self.sw.sweep
        self.sw.sweep = lambda: orig(now=NOW)
        try:
            self.assertEqual(self.sw.lambda_handler({}, None), {"engines": 2, "stopped": 1})
        finally:
            self.sw.sweep = orig


class TerraformWiringTests(unittest.TestCase):
    """The Terraform must deploy the file tested above, with the limits tested above."""

    def setUp(self):
        self.tf = (ROOT / "games-multiplayer.tf").read_text()

    def test_lambda_packages_this_file(self):
        self.assertIn('source_file = "${path.module}/games-multiplayer/sweeper.py"', self.tf)
        self.assertRegex(self.tf, r'handler\s*=\s*"sweeper\.lambda_handler"')

    def test_env_limits_match_the_documented_rule(self):
        for key, value in [("GRACE_SEC", "600"), ("DEFAULT_LIMIT_SEC", "7200"), ("MAX_HARDCAP_SEC", "14400")]:
            self.assertRegex(self.tf, r'%s\s*=\s*"%s"' % (key, value))

    def launch_statement(self, sid):
        """One statement of the launch function's policy (the only RunTask grant), by braces."""
        policy = re.search(r'^resource "aws_iam_role_policy" "games_mp_launch" \{\n.*?^\}', self.tf,
                           re.S | re.M).group()
        at = re.search(r'Sid\s*=\s*"%s"' % sid, policy)
        begin, depth = policy.rindex("{", 0, at.start()), 0
        for i in range(begin, len(policy)):
            depth += {"{": 1, "}": -1}.get(policy[i], 0)
            if depth == 0:
                return policy[begin:i + 1]

    def test_exempt_family_is_the_one_the_launch_function_cannot_run(self):
        self.assertRegex(self.tf, r'mp_router_family\s*=\s*"games-mp-router"')
        self.assertRegex(self.tf, r'ROUTER_FAMILY\s*=\s*local\.mp_router_family')
        self.assertRegex(self.tf, r'family\s*=\s*local\.mp_router_family')
        deny = self.launch_statement("NeverRunTheRouter")
        self.assertRegex(deny, r'Effect\s*=\s*"Deny"')
        self.assertIn("task-definition/${local.mp_router_family}:*", deny)

    def test_sim_versions_and_engine_tags_start_like_a_docker_tag(self):
        # The engine image tag is <simVersion>-<sha12>; a Docker tag cannot begin with "." or "-".
        import re as _re
        for name in ("games_mp_sim_versions", "mp_engine_image_tags"):
            var = self.tf.split('variable "%s"' % name, 1)[1].split("\n}\n", 1)[0]
            pattern = _re.search(r'regex\("(\^[^"]+)"', var).group(1).replace("\\\\", "\\")
            for bad in (".v2", "-v2"):
                value = bad if name == "games_mp_sim_versions" else bad + "-0123456789ab"
                self.assertIsNone(_re.match(pattern, value), (name, value))
            ok = "v2" if name == "games_mp_sim_versions" else "v2-0123456789ab"
            self.assertIsNotNone(_re.match(pattern, ok), name)

    def test_a_missing_engine_count_alarms(self):
        # The sweeper swallows PutMetricData failures; the metric going missing must alarm.
        alarm = self.tf.split('resource "aws_cloudwatch_metric_alarm" "games_mp_engine_count_missing"', 1)[1].split("\n}\n", 1)[0]
        self.assertIn('metric_name         = "RunningEngines"', alarm)
        self.assertIn('treat_missing_data  = "breaching"', alarm)
        self.assertIn('statistic           = "SampleCount"', alarm)

    def test_the_engine_ceiling_must_be_a_whole_number(self):
        # Both Lambdas int() it at start-up: "10.5" would fail every launch and every sweep.
        var = self.tf.split('variable "games_mp_engine_ceiling"', 1)[1].split("\n}\n", 1)[0]
        self.assertIn("floor(var.games_mp_engine_ceiling) == var.games_mp_engine_ceiling", var)

    def test_schedule_is_every_minute_with_a_ceiling_above_the_lobby_caps(self):
        self.assertIn('schedule_expression = "rate(1 minute)"', self.tf)
        self.assertIn('ENGINE_CEILING   = tostring(var.games_mp_engine_ceiling)', self.tf)
        caps = re.search(r'games_mp_lobby_max_active = \{ production = (\d+), preview = (\d+) \}', self.tf)
        ceiling = re.search(r'variable "games_mp_engine_ceiling" \{.*?default\s*=\s*(\d+)', self.tf, re.S)
        self.assertGreater(int(ceiling.group(1)), int(caps.group(1)) + int(caps.group(2)),
                           "the AWS ceiling must sit above what the lobby itself allows")

    def test_only_the_launch_function_may_run_or_stop_engines_besides_the_sweeper(self):
        # The lobby's roles may only invoke games-mp-launch (scripts/test-games-mp-launch.py).
        for path in ROOT.glob("*.tf"):
            text = path.read_text()
            for action in ("ecs:RunTask", "ecs:StopTask"):
                for m in re.finditer(r'"%s"' % action, text):
                    owner = re.findall(r'^resource "aws_iam_role_policy" "(\w+)"', text[:m.start()], re.M)
                    self.assertIn((path.name, owner[-1] if owner else None),
                                  {("games-multiplayer.tf", "games_mp_launch"), ("games-multiplayer.tf", "games_mp_sweeper")},
                                  "%s granted outside the launch function and the sweeper" % action)

    def test_router_is_denied_and_carries_the_denied_tag(self):
        block = self.launch_statement("NeverStopTheRouter")
        self.assertRegex(block, r'Effect\s*=\s*"Deny"')
        self.assertIn('"aws:ResourceTag/games-role" = "router"', block)
        service = re.search(r'resource "aws_ecs_service" "games_mp_router" \{.*?\n\}', self.tf, re.S).group()
        self.assertRegex(service, r'propagate_tags\s*=\s*"SERVICE"')
        self.assertIn('"games-role" = "router"', service)

    def test_router_accepts_a_list_of_envs(self):
        self.assertIn('{ name = "MP_ENVS", value = join(",", var.mp_router_envs) }', self.tf)
        self.assertRegex(self.tf, r'default\s*=\s*\["production", "preview"\]')


if __name__ == "__main__":
    unittest.main(verbosity=2)

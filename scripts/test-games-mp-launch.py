#!/usr/bin/env python3
"""Offline checks for games-multiplayer/launch.py, the only way a match engine starts.

The launch function is the admission control for Fargate match engines: the lobby's roles
can only invoke it, and it builds every RunTask itself and refuses at the engine ceiling.
This imports the real file and drives it with fake ECS clients (no AWS credentials, no
network, no boto3), then pins the Terraform that deploys it and the launcher roles that
may call it.

Run from the repo root:  python3 -S -B scripts/test-games-mp-launch.py
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GAMES = (ROOT / "games-multiplayer.tf").read_text()
BRINGUP = (ROOT / "games-multiplayer-bringup.tf").read_text()

ACCOUNT = "928413605543"
FUNCTION = "arn:aws:lambda:us-west-1:%s:function:games-mp-launch" % ACCOUNT
TD = "arn:aws:ecs:us-west-1:%s:task-definition/games-mptest:7" % ACCOUNT
ROUTER_TD = "arn:aws:ecs:us-west-1:%s:task-definition/games-mp-router:3" % ACCOUNT
TASK = "arn:aws:ecs:us-west-1:%s:task/games/" % ACCOUNT
PREVIEW_API = "https://colton-games-abc123xyz-coltons-projects-7f9a4e8b.vercel.app"

ENV = {
    "CLUSTER": "games",
    "SUBNETS": "subnet-0aaaaaaaaaaaaaaaa,subnet-0bbbbbbbbbbbbbbbb",
    "SECURITY_GROUP": "sg-0ccccccccccccccccc",
    "TASK_DEFINITIONS": json.dumps({"mptest": TD}),
    "ROUTER_FAMILY": "games-mp-router",
    "ENGINE_CEILING": "6",
    # The regexes exactly as Terraform's jsonencode writes them (see TerraformTests).
    "ENVIRONMENTS": json.dumps({
        "production": {"ceiling": 6, "api_base": r"^https://cc-games\.app$", "bypass": False},
        "preview": {"ceiling": 2,
                    "api_base": r"^https://colton-games-[a-z0-9-]+-coltons-projects-7f9a4e8b\.vercel\.app$",
                    "bypass": True},
    }),
    "MIN_HARDCAP_SEC": "60",
    "MAX_HARDCAP_SEC": "14400",
    "SETTLE_SEC": "120",
}


def load_launch():
    for key, value in ENV.items():
        os.environ[key] = value
    spec = importlib.util.spec_from_file_location("launch", ROOT / "games-multiplayer" / "launch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Context:
    def __init__(self, qualifier="production", arn=None):
        self.invoked_function_arn = arn or ("%s:%s" % (FUNCTION, qualifier) if qualifier else FUNCTION)


def match(n):
    return "00000000-0000-4000-8000-%012d" % n


def engine(n, env="production", family="games-mptest", status="RUNNING", desired="RUNNING", tags=None,
           group=None):
    t = {
        "taskArn": TASK + "t%03d" % n,
        "taskDefinitionArn": TD.replace("games-mptest", family),
        "lastStatus": status,
        "desiredStatus": desired,
        "startedBy": match(n),
        "tags": [{"key": k, "value": v} for k, v in (tags if tags is not None else
                                                     {"env": env, "match": match(n)}).items()],
    }
    if group:
        t["group"] = group
    return t


class FakeECS:
    """ECS with a switch for eventual consistency: `visible=False` hides launched tasks."""

    def __init__(self, tasks=(), visible=True, listed=None):
        self.tasks = {t["taskArn"]: t for t in tasks}
        # visible: DescribeTasks knows launched tasks; listed: ListTasks returns them.
        self.visible = visible
        self.listed = visible if listed is None else listed
        self.run, self.stopped, self.calls = [], [], []
        self.run_reply = None

    def list_tasks(self, cluster, maxResults, desiredStatus=None, startedBy=None, nextToken=None):
        assert cluster == "games"
        self.calls.append("list_tasks")
        arns = sorted(a for a, t in self.tasks.items()
                      if (self.listed or not t.get("_launched"))
                      and (desiredStatus is None or t["desiredStatus"] == desiredStatus)
                      and (startedBy is None or t.get("startedBy") == startedBy))
        return {"taskArns": arns}

    def describe_tasks(self, cluster, tasks, include):
        assert cluster == "games" and include == ["TAGS"] and len(tasks) <= 100
        self.calls.append("describe_tasks")
        return {"tasks": [self.tasks[a] for a in tasks
                          if a in self.tasks and (self.visible or not self.tasks[a].get("_launched"))]}

    def run_task(self, **kwargs):
        self.calls.append("run_task")
        self.run.append(kwargs)
        if self.run_reply is not None:
            return self.run_reply
        arn = TASK + "new%03d" % len(self.run)
        tags = {t["key"]: t["value"] for t in kwargs["tags"]}
        self.tasks[arn] = {"taskArn": arn, "taskDefinitionArn": kwargs["taskDefinition"], "lastStatus": "PROVISIONING",
                           "desiredStatus": "RUNNING", "startedBy": kwargs["startedBy"], "_launched": True,
                           "tags": [{"key": k, "value": v} for k, v in tags.items()]}
        return {"tasks": [{"taskArn": arn}], "failures": []}

    def stop_task(self, cluster, task, reason):
        assert cluster == "games" and len(reason) <= 255
        self.calls.append("stop_task")
        self.stopped.append(task)


def start_event(n=1, **over):
    event = {"action": "start", "matchId": match(n), "game": "mptest", "secret": "s" * 43,
             "hardCapSec": 1800, "apiBase": "https://cc-games.app"}
    event.update(over)
    return event


def preview_event(n=1, **over):
    return start_event(n, **{"apiBase": PREVIEW_API, "apiBypass": "B" * 32, **over})


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.lf = load_launch()
        self.lf._recent.clear()
        self.log = io.StringIO()   # the handler's one line per call

    def invoke(self, ecs, event, qualifier="production", **ctx):
        self.lf._clients["ecs"] = ecs
        with contextlib.redirect_stdout(self.log):
            return self.lf.lambda_handler(event, Context(qualifier, **ctx))

    # -- what a launch is ------------------------------------------------------------------

    def test_launches_the_pinned_task_definition_in_the_engine_network(self):
        ecs = FakeECS()
        reply = self.invoke(ecs, start_event())
        self.assertEqual(reply["ok"], True)
        self.assertEqual(reply["taskArn"], TASK + "new001")
        [call] = ecs.run
        self.assertEqual(call["cluster"], "games")
        self.assertEqual(call["taskDefinition"], TD, "the exact family:revision from Terraform")
        self.assertEqual(call["launchType"], "FARGATE")
        self.assertEqual(call["count"], 1)
        self.assertEqual(call["clientToken"], match(1))
        self.assertEqual(call["startedBy"], match(1))
        self.assertEqual(call["networkConfiguration"], {"awsvpcConfiguration": {
            "subnets": ["subnet-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb"],
            "securityGroups": ["sg-0ccccccccccccccccc"], "assignPublicIp": "ENABLED"}})
        self.assertEqual(call["overrides"], {"containerOverrides": [{"name": "engine", "environment": [
            {"name": "MATCH_ID", "value": match(1)},
            {"name": "MATCH_SECRET", "value": "s" * 43},
            {"name": "MP_API", "value": "https://cc-games.app"},
            {"name": "GAME_ID", "value": "mptest"},
            {"name": "MP_ENV", "value": "production"},
            {"name": "PORT", "value": "8080"},
        ]}]})
        self.assertEqual(call["tags"], [{"key": "game", "value": "mptest"}, {"key": "match", "value": match(1)},
                                        {"key": "env", "value": "production"}, {"key": "hardcap", "value": "1800"}])
        # Nothing else: no command, role, size, capacity provider or propagation setting.
        self.assertEqual(set(call), {"cluster", "taskDefinition", "launchType", "count", "clientToken", "startedBy",
                                     "networkConfiguration", "overrides", "tags"})

    def test_preview_engines_are_preview_whatever_the_request_says(self):
        ecs = FakeECS()
        self.assertTrue(self.invoke(ecs, preview_event(), "preview")["ok"])
        call = ecs.run[0]
        env = {e["name"]: e["value"] for e in call["overrides"]["containerOverrides"][0]["environment"]}
        self.assertEqual(env["MP_ENV"], "preview")
        self.assertEqual(env["MP_API"], PREVIEW_API)
        self.assertEqual(env["MP_API_BYPASS"], "B" * 32)
        self.assertIn({"key": "env", "value": "preview"}, call["tags"])

    # -- caller-supplied overrides and tags ------------------------------------------------

    def test_caller_supplied_overrides_tags_and_settings_launch_nothing(self):
        for key, value in [
            ("overrides", {"containerOverrides": [{"name": "engine", "command": ["sh", "-c", "curl evil"]}]}),
            ("tags", [{"key": "games-role", "value": "router"}]),
            ("taskDefinition", ROUTER_TD),
            ("group", "service:mp-router"),
            ("env", "production"),
            ("cpu", "16384"),
            ("taskRoleArn", "arn:aws:iam::%s:role/admin" % ACCOUNT),
            ("networkConfiguration", {"awsvpcConfiguration": {"subnets": ["subnet-0dddddddddddddddd"]}}),
            ("cluster", "other"),
        ]:
            with self.subTest(key=key):
                ecs = FakeECS()
                reply = self.invoke(ecs, start_event(**{key: value}))
                self.assertEqual(reply, {"ok": False, "error": "bad-request", "field": "unknown-field"})
                self.assertEqual(ecs.calls, [], "refused before any ECS call")

    def test_bad_input_is_refused_without_touching_ecs(self):
        cases = [
            ("matchId", start_event(matchId="../../x")),
            ("matchId", start_event(matchId="0000000a-0000-4000-8000-00000000000B")),
            ("matchId", start_event(matchId=match(1) + "\n")),
            ("matchId", start_event(matchId=None)),
            ("game", start_event(game="mp-router")),
            ("game", start_event(game=["mptest"])),
            ("secret", start_event(secret="short")),
            ("secret", start_event(secret="s" * 42 + "\n")),
            ("hardCapSec", start_event(hardCapSec=True)),
            ("hardCapSec", start_event(hardCapSec=1800.0)),
            ("hardCapSec", start_event(hardCapSec="1800")),
            ("hardCapSec", start_event(hardCapSec=59)),
            ("hardCapSec", start_event(hardCapSec=14401)),
            ("apiBase", start_event(apiBase="https://cc-games.app.evil.example")),
            ("apiBase", start_event(apiBase="http://cc-games.app")),
            ("apiBase", start_event(apiBase=PREVIEW_API)),       # production calls back production only
            ("apiBypass", start_event(apiBypass="B" * 32)),      # production never passes a bypass
        ]
        for field, event in cases:
            with self.subTest(field=field, value=event.get(field)):
                self.lf._recent.clear()
                ecs = FakeECS()
                self.assertEqual(self.invoke(ecs, event), {"ok": False, "error": "bad-request", "field": field})
                self.assertEqual(ecs.calls, [])
        for field, event in [
            ("apiBase", preview_event(apiBase="https://cc-games.app")),
            ("apiBase", preview_event(apiBase="https://evil-abc-coltons-projects-7f9a4e8b.vercel.app")),
            ("apiBase", preview_event(apiBase="https://colton-games-a.b-coltons-projects-7f9a4e8b.vercel.app")),
            ("apiBypass", preview_event(apiBypass="has space in it ok")),
        ]:
            with self.subTest(preview=field, value=event.get(field)):
                self.lf._recent.clear()
                ecs = FakeECS()
                self.assertEqual(self.invoke(ecs, event, "preview"),
                                 {"ok": False, "error": "bad-request", "field": field})
                self.assertEqual(ecs.calls, [])
        for event in (None, [], "start", {"action": "launch", "matchId": match(1)}):
            with self.subTest(event=event):
                ecs = FakeECS()
                self.assertEqual(self.invoke(ecs, event)["error"], "bad-request")
                self.assertEqual(ecs.calls, [])

    def test_only_an_environment_alias_may_launch(self):
        for ctx in ({"qualifier": None}, {"qualifier": "$LATEST"}, {"qualifier": "12"},
                    {"qualifier": "development"}, {"qualifier": "production", "arn": "production"}):
            with self.subTest(**ctx):
                ecs = FakeECS()
                self.assertEqual(self.invoke(ecs, start_event(), **ctx), {"ok": False, "error": "forbidden"})
                self.assertEqual(ecs.calls, [])

    # -- admission ---------------------------------------------------------------------------

    def test_refuses_at_the_engine_ceiling(self):
        ecs = FakeECS([engine(n) for n in range(1, 7)])            # ceiling 6
        self.assertEqual(self.invoke(ecs, start_event(50)), {"ok": False, "error": "capacity"})
        self.assertEqual(ecs.run, [])
        ecs = FakeECS([engine(n) for n in range(1, 6)])            # one below
        self.assertTrue(self.invoke(ecs, start_event(50))["ok"])
        self.assertEqual(self.invoke(ecs, start_event(51)), {"ok": False, "error": "capacity"})
        self.assertEqual(len(ecs.run), 1)

    def test_the_ceiling_counts_every_environment_together(self):
        # Production has room of its own (4 of 6), but 6 engines run in total.
        ecs = FakeECS([engine(n) for n in range(1, 5)] + [engine(5, env="preview"), engine(6, tags={})])
        self.assertEqual(self.invoke(ecs, start_event(50)), {"ok": False, "error": "capacity"})
        self.assertEqual(ecs.run, [])

    def test_preview_has_its_own_smaller_ceiling_and_cannot_starve_production(self):
        ecs = FakeECS([engine(1, env="preview"), engine(2, env="preview")])   # preview ceiling 2
        self.assertEqual(self.invoke(ecs, preview_event(50), "preview"), {"ok": False, "error": "capacity"})
        self.assertTrue(self.invoke(ecs, start_event(51))["ok"], "production still launches")
        self.assertEqual(len(ecs.run), 1)

    def test_every_non_router_task_counts_whatever_its_tags_or_group(self):
        tasks = [
            engine(1, tags={}),                                            # untagged engine
            engine(2, tags={"games-role": "router"}, group="service:mp-router"),
            engine(3, family="games-mp-router2"),
            engine(4, env="preview"),
            engine(5),
            engine(90, family="games-mp-router"), engine(91, family="games-mp-router"),   # not engines
            engine(92, status="STOPPING"), engine(93, status="DEPROVISIONING"),         # on their way out
        ]
        total, in_env = self.lf.count_engines(FakeECS(tasks), "production", now=0)
        self.assertEqual((total, in_env), (5, 2))

    def test_launches_ecs_cannot_see_yet_still_count(self):
        # ECS is eventually consistent: a task started a moment ago may not be listed.
        ecs = FakeECS([engine(n) for n in range(1, 5)], visible=False)
        self.assertTrue(self.invoke(ecs, start_event(50))["ok"])
        self.assertTrue(self.invoke(ecs, start_event(51))["ok"])
        self.assertEqual(self.invoke(ecs, start_event(52)), {"ok": False, "error": "capacity"})
        self.assertEqual(len(ecs.run), 2)

    def test_a_just_launched_task_ecs_reports_stopped_frees_its_slot(self):
        ecs = FakeECS([engine(n) for n in range(1, 6)])
        self.assertTrue(self.invoke(ecs, start_event(50))["ok"])
        self.assertEqual(self.invoke(ecs, start_event(51))["error"], "capacity")
        ecs.tasks[TASK + "new001"].update(lastStatus="STOPPED", desiredStatus="STOPPED")
        self.assertTrue(self.invoke(ecs, start_event(51))["ok"])

    def test_the_settle_window_expires(self):
        self.lf._recent[match(9)] = (TASK + "gone", "production", -1000.0)
        self.assertEqual(self.lf.count_engines(FakeECS(), "production", now=0), (0, 0))
        self.assertEqual(self.lf._recent, {})

    def test_a_retried_start_returns_the_same_task(self):
        ecs = FakeECS()
        first = self.invoke(ecs, start_event(7))
        again = self.invoke(ecs, start_event(7))
        self.assertEqual(again["taskArn"], first["taskArn"])
        self.assertEqual(len(ecs.run), 1)

    def test_runtask_that_starts_nothing_is_an_error(self):
        ecs = FakeECS()
        ecs.run_reply = {"tasks": [], "failures": [{"reason": "RESOURCE:ENI"}]}
        with self.assertRaisesRegex(RuntimeError, "RESOURCE:ENI"):
            self.invoke(ecs, start_event())
        self.assertEqual(self.lf._recent, {})

    def test_the_secret_and_bypass_never_reach_the_log(self):
        self.invoke(FakeECS(), preview_event(secret="SECRET" + "x" * 40, apiBypass="BYPASS" + "y" * 26), "preview")
        self.invoke(FakeECS(), {"action": "start", "matchId": "SECRETmatch", "SECRETkey": 1})
        self.assertNotIn("SECRET", self.log.getvalue())
        self.assertNotIn("BYPASS", self.log.getvalue())
        self.assertIn(match(1), self.log.getvalue())

    # -- stop -------------------------------------------------------------------------------

    def test_stop_stops_only_this_environments_engine_for_the_match(self):
        prod = engine(1)
        prev = engine(2, tags={"env": "preview", "match": match(1)})    # same match id, other environment
        prev["startedBy"] = match(1)
        router = engine(3, family="games-mp-router", tags={"env": "preview", "match": match(1)})
        router["startedBy"] = match(1)
        wrong_match = engine(4, env="preview")
        wrong_match["startedBy"] = match(1)           # startedBy says match 1, tag says match 4
        ecs = FakeECS([prod, prev, router, wrong_match])
        self.assertEqual(self.invoke(ecs, {"action": "stop", "matchId": match(1)}, "preview"),
                         {"ok": True, "stopped": 1})
        self.assertEqual(ecs.stopped, [prev["taskArn"]])
        ecs.stopped.clear()
        self.assertEqual(self.invoke(ecs, {"action": "stop", "matchId": match(1)}), {"ok": True, "stopped": 1})
        self.assertEqual(ecs.stopped, [prod["taskArn"]])

    def test_stop_finds_a_task_ecs_cannot_list_yet(self):
        ecs = FakeECS(listed=False)          # DescribeTasks knows it, ListTasks does not yet
        self.invoke(ecs, start_event(3))
        self.assertEqual(self.invoke(ecs, {"action": "stop", "matchId": match(3)}), {"ok": True, "stopped": 1})
        self.assertEqual(ecs.stopped, [TASK + "new001"])

    def test_stop_takes_only_a_match_id(self):
        ecs = FakeECS([engine(1)])
        for event in ({"action": "stop", "matchId": match(1), "task": engine(1)["taskArn"]},
                      {"action": "stop", "matchId": "all"}):
            self.assertEqual(self.invoke(ecs, event)["error"], "bad-request")
        self.assertEqual(ecs.calls, [])


def tf_block(text, kind, name):
    m = re.search(r'^resource "%s" "%s" \{\n.*?^\}' % (re.escape(kind), re.escape(name)), text, re.S | re.M)
    if m is None:
        raise AssertionError("missing %s.%s" % (kind, name))
    return m.group()


def statement(policy, sid):
    """The `{ ... }` statement holding Sid = "<sid>", by brace matching."""
    at = re.search(r'Sid\s*=\s*"%s"' % re.escape(sid), policy)
    if at is None:
        raise AssertionError("no statement %s" % sid)
    begin = policy.rindex("{", 0, at.start())
    depth = 0
    for i in range(begin, len(policy)):
        depth += {"{": 1, "}": -1}.get(policy[i], 0)
        if depth == 0:
            return policy[begin:i + 1]
    raise AssertionError("unbalanced statement %s" % sid)


class TerraformTests(unittest.TestCase):
    """The Terraform must deploy the file tested above and leave the lobby only this path."""

    def setUp(self):
        self.fn = tf_block(GAMES, "aws_lambda_function", "games_mp_launch")
        self.launch_policy = tf_block(GAMES, "aws_iam_role_policy", "games_mp_launch")
        self.launcher_policy = tf_block(GAMES, "aws_iam_role_policy", "games_mp_launcher")

    def test_the_function_packages_this_file_serialized_with_aliases(self):
        self.assertIn('source_file = "${path.module}/games-multiplayer/launch.py"', GAMES)
        self.assertRegex(self.fn, r'handler\s*=\s*"launch\.lambda_handler"')
        self.assertRegex(self.fn, r"reserved_concurrent_executions = 1\n")
        self.assertRegex(self.fn, r"publish\s*=\s*true")
        alias = tf_block(GAMES, "aws_lambda_alias", "games_mp_launch")
        self.assertIn("for_each = local.games_mp_launch_environments", alias)
        self.assertIn("function_version = aws_lambda_function.games_mp_launch.version", alias)

    def test_the_function_gets_the_settings_the_tests_assume(self):
        for line in ('TASK_DEFINITIONS = jsonencode({ for id, td in aws_ecs_task_definition.games_engine : id => td.arn })',
                     "ENGINE_CEILING   = tostring(var.games_mp_engine_ceiling)",
                     "ENVIRONMENTS     = jsonencode(local.games_mp_launch_environments)",
                     "SECURITY_GROUP   = aws_security_group.games_engine.id",
                     "ROUTER_FAMILY    = local.mp_router_family",
                     'MAX_HARDCAP_SEC = "14400"'):
            self.assertIn(line, self.fn)
        envs = re.search(r"games_mp_launch_environments = \{.*?\n  \}\n", GAMES, re.S).group()
        self.assertEqual(sorted(re.findall(r"^    (\w+) = \{", envs, re.M)), ["preview", "production"])
        self.assertIn(r'api_base = "^https://cc-games\\.app$"', envs)
        self.assertIn(r'api_base = "^https://${local.vercel_project_name}-[a-z0-9-]+-${local.vercel_team_slug}\\.vercel\\.app$"',
                      envs)
        self.assertIn("ceiling = min(8, var.games_mp_engine_ceiling)", envs)
        self.assertEqual(re.findall(r"bypass\s*=\s*(\w+)", envs), ["false", "true"])
        # The test's ENVIRONMENTS is what those HCL strings render to.
        rendered = json.loads(ENV["ENVIRONMENTS"])
        self.assertEqual(rendered["production"]["api_base"], r"^https://cc-games\.app$")
        self.assertEqual(rendered["preview"]["api_base"],
                         r"^https://colton-games-[a-z0-9-]+-coltons-projects-7f9a4e8b\.vercel\.app$")
        self.assertIn('vercel_team_slug    = "coltons-projects-7f9a4e8b"', GAMES)
        self.assertIn('vercel_project_name = "colton-games"', GAMES)

    def test_launch_role_runs_only_the_pinned_revisions_and_passes_only_engine_roles(self):
        run = statement(self.launch_policy, "RunPinnedEngineTaskDefinitions")
        self.assertIn("Resource  = [for td in aws_ecs_task_definition.games_engine : td.arn]", run)
        self.assertIn('ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn }', run)
        self.assertNotIn("*", run.split("Resource", 1)[1].split("\n", 1)[0])
        deny = statement(self.launch_policy, "NeverRunTheRouter")
        self.assertRegex(deny, r'Effect\s*=\s*"Deny"')
        self.assertIn("task-definition/${local.mp_router_family}:*", deny)
        passrole = statement(self.launch_policy, "PassOnlyEngineRoles")
        self.assertIn("Resource = [aws_iam_role.games_engine_task.arn, aws_iam_role.games_engine_execution.arn]",
                      passrole)
        self.assertIn('"iam:PassedToService" = "ecs-tasks.amazonaws.com"', passrole)
        self.assertIn('"ecs:CreateAction" = "RunTask"', statement(self.launch_policy, "TagTasksAtLaunch"))
        stop = statement(self.launch_policy, "StopOnlyMatchEngines")
        self.assertIn('"aws:ResourceTag/match" = "false"', stop)
        never = statement(self.launch_policy, "NeverStopTheRouter")
        self.assertIn('"aws:ResourceTag/games-role" = "router"', never)

    def test_launcher_roles_can_only_invoke_their_own_alias(self):
        self.assertIn("for_each = local.games_mp_launcher_role_names", self.launcher_policy)
        self.assertEqual(re.findall(r'Action\s*=\s*("[^"]*"|\[[^\]]*\])', self.launcher_policy),
                         ['"lambda:InvokeFunction"'])
        self.assertIn("Resource = aws_lambda_alias.games_mp_launch[each.key].arn", self.launcher_policy)
        for forbidden in ("ecs:", "iam:", "ec2:", "RunTask", "PassRole", '"*"', "games_mp_launch.arn"):
            self.assertNotIn(forbidden, self.launcher_policy)
        # Nothing else may attach permissions to the launcher roles.
        attached = re.findall(r'role\s*=\s*aws_iam_role\.games_mp_launcher[\[.]', GAMES + BRINGUP)
        self.assertEqual(len(attached), 1, attached)
        self.assertNotRegex(GAMES + BRINGUP, r'aws_iam_role_policy_attachment" "games_mp_launcher')

    def test_each_launcher_role_trusts_exactly_one_environment(self):
        names = re.search(r"games_mp_launcher_role_names = \{.*?\n  \}", GAMES, re.S).group()
        self.assertIn('production = "games-mp-launcher"', names)
        self.assertIn('preview    = "games-mp-launcher-preview"', names)
        role = tf_block(GAMES, "aws_iam_role", "games_mp_launcher")
        self.assertIn('"${local.vercel_oidc_host}:sub" = "owner:${local.vercel_team_slug}:project:'
                      '${local.vercel_project_name}:environment:${each.key}"', role)
        self.assertIn("StringEquals", role)
        self.assertNotIn("StringLike", role)
        self.assertNotIn("development", role.split("assume_role_policy", 1)[1])
        # The production role and its policy keep their state addresses (moved, not recreated).
        for kind in ("aws_iam_role", "aws_iam_role_policy"):
            self.assertIn("from = %s.games_mp_launcher\n  to   = %s.games_mp_launcher[\"production\"]" % (kind, kind),
                          GAMES)

    def test_vercel_gets_each_environment_its_own_role_and_alias_and_no_ecs_settings(self):
        env = BRINGUP[BRINGUP.index("games_mp_vercel_shared_config = {"):BRINGUP.index("games_mp_vercel_secret_values = {")]
        for e in ("production", "preview"):
            self.assertRegex(env, r'"MP_LAUNCH_ROLE_ARN/%s"\s+= \{ targets = \["%s"\], sensitive = false, '
                                  r'value = aws_iam_role\.games_mp_launcher\["%s"\]\.arn \}' % (e, e, e))
            self.assertRegex(env, r'"MP_LAUNCH_FUNCTION/%s"\s+= \{ targets = \["%s"\], sensitive = false, '
                                  r'value = aws_lambda_alias\.games_mp_launch\["%s"\]\.arn \}' % (e, e, e))
        for gone in ("MP_ROLE_ARN", "AWS_ROLE_ARN", "MP_CLUSTER", "MP_SUBNETS", "MP_ENGINE_SG"):
            self.assertNotRegex(env, r"\b%s\s*=" % gone)
            self.assertNotIn('"%s' % gone, env)


if __name__ == "__main__":
    unittest.main(verbosity=2)

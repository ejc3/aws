#!/usr/bin/env python3
"""Offline checks for games-multiplayer/launch.py, the only way a match engine starts.

The launch function is the admission control for Fargate match engines: the lobby's roles
can only invoke it, and it builds every RunTask itself and refuses at the engine ceiling.
This imports the real file and drives it with fake ECS and DynamoDB clients (no AWS
credentials, no network, no boto3), then pins the Terraform that deploys it and the launcher
roles that may call it.

Run from the repo root:  python3 -S -B scripts/test-games-mp-launch.py
"""
import base64
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
TD = "arn:aws:ecs:us-west-1:%s:task-definition/games-mptest:7" % ACCOUNT
ROUTER_TD = "arn:aws:ecs:us-west-1:%s:task-definition/games-mp-router:3" % ACCOUNT
TASK = "arn:aws:ecs:us-west-1:%s:task/games/" % ACCOUNT
# Games are dynamic: every game's images are in its channel's one engine repository, tagged
# `<game>_<simVersion>-<sha12>`. mptest's revisions from before that run from its own old
# repository (REPO, PREVIEW_REPO), which stays launchable (LEGACY_REPOSITORIES).
ENGINES = "%s.dkr.ecr.us-west-1.amazonaws.com/games/engines" % ACCOUNT
PREVIEW_ENGINES = "%s.dkr.ecr.us-west-1.amazonaws.com/games-preview/engines" % ACCOUNT
REPO = "%s.dkr.ecr.us-west-1.amazonaws.com/games/mptest-engine" % ACCOUNT
PREVIEW_REPO = "%s.dkr.ecr.us-west-1.amazonaws.com/games-preview/mptest-engine" % ACCOUNT
CURRENT_SIM = "mptest-1"
# A preview lobby's commit (VERCEL_GIT_COMMIT_SHA) and the revision games-mp-release registered for it.
COMMIT = "c0ffee" + "0123456789abcdef0123456789abcdef01"
PREVIEW_TD = "arn:aws:ecs:us-west-1:%s:task-definition/games-preview-mptest:5" % ACCOUNT
PREVIEW_API = "https://colton-games-abc123xyz-coltons-projects-7f9a4e8b.vercel.app"


def spki(fill):
    """A 44-byte Ed25519 SubjectPublicKeyInfo (RFC 8410) around a stand-in 32-byte key."""
    return base64.b64encode(bytes.fromhex("302a300506032b6570032100") + bytes([fill]) * 32).decode()


# What Terraform writes: each environment's own Ed25519 public keys (games_mp_token_public_keys_by_env).
PROD_KEYS = "production:production-kid2:%s,production:production-kid1:%s" % (spki(1), spki(2))
PREVIEW_KEYS = "preview:preview-kid1:%s" % spki(3)
# A PKCS#8 Ed25519 private key's DER is 48 bytes: this prefix plus the 32-byte seed.
PRIVATE_DER = base64.b64encode(bytes.fromhex("302e020100300506032b657004220420") + b"\x07" * 32).decode()

# What Terraform puts in every engine revision's container environment (games_engine).
VERIFIER_ENTRY = {"name": "MP_TOKEN_VERIFIER", "value": "ed25519-v2"}


def legacy(image):
    """A revision registered before engines verified tokens: the same image shape, no marker."""
    return [{"name": "engine", "image": image, "environment": [{"name": "GAME_ID", "value": "mptest"}]}]


ENV = {
    "CLUSTER": "games",
    "SUBNETS": "subnet-0aaaaaaaaaaaaaaaa,subnet-0bbbbbbbbbbbbbbbb",
    "SECURITY_GROUP": "sg-0ccccccccccccccccc",
    "RELEASES_TABLE": "games-mp-releases",
    "ROUTER_FAMILY": "games-mp-router",
    "MIN_HARDCAP_SEC": "60",
    "MAX_HARDCAP_SEC": "14400",
    "SETTLE_SEC": "120",
    "STOP_ATTEMPTS": "3",
    "STOP_RETRY_SEC": "0",   # the tests do not sleep
}

# Each environment is its own function (games-mp-launch-<env>) with its own settings. Ceilings
# scaled down from Terraform's 22 + 8: a total of 6, production 4 and preview 2. The regexes are
# exactly what Terraform writes (see TerraformTests).
FUNCTIONS = {
    "production": {"LAUNCH_ENV": "production", "ENV_CEILING": "4",
                   "API_BASE": r"^https://cc-games\.app$", "ALLOW_BYPASS": "false",
                   "TOKEN_PUBLIC_KEYS": PROD_KEYS,
                   "ENGINE_FAMILY_PREFIX": "games-", "ENGINE_REPOSITORY": ENGINES,
                   "LEGACY_REPOSITORIES": json.dumps({"mptest": REPO})},
    "preview": {"LAUNCH_ENV": "preview", "ENV_CEILING": "2",
                "API_BASE": r"^https://colton-games-[a-z0-9-]+-coltons-projects-7f9a4e8b\.vercel\.app$",
                "ALLOW_BYPASS": "true", "TOKEN_PUBLIC_KEYS": PREVIEW_KEYS,
                "ENGINE_FAMILY_PREFIX": "games-preview-", "ENGINE_REPOSITORY": PREVIEW_ENGINES,
                "LEGACY_REPOSITORIES": json.dumps({"mptest": PREVIEW_REPO})},
}


def load_launch(env="production"):
    """A fresh copy of launch.py configured as that environment's function (its own globals and
    its own _recent, as two Lambda functions have)."""
    for key, value in dict(ENV, **FUNCTIONS[env]).items():
        os.environ[key] = value
    spec = importlib.util.spec_from_file_location("launch_" + env, ROOT / "games-multiplayer" / "launch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Context:
    """Lambda's context. The function reads nothing from it: its environment is its own setting."""
    invoked_function_arn = "arn:aws:lambda:us-west-1:%s:function:games-mp-launch-production" % ACCOUNT


def match(n):
    return "00000000-0000-4000-8000-%012d" % n


def revision(n, family="games-mptest"):
    return "arn:aws:ecs:us-west-1:%s:task-definition/%s:%d" % (ACCOUNT, family, n)


def sha(n):
    return "%012x" % (0xabc000000000 + n)


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
    """ECS with a switch for eventual consistency: `visible=False` hides launched tasks.

    `revisions` are the ACTIVE task definition revisions, ARN -> engine image (or a list of
    container definitions, for a revision that is not a one-container engine).
    """

    PAGE = 2   # small, so paging is exercised

    def __init__(self, tasks=(), visible=True, listed=None, revisions=None):
        self.tasks = {t["taskArn"]: t for t in tasks}
        self.revisions = dict(revisions or {})
        # visible: DescribeTasks knows launched tasks; listed: ListTasks returns them.
        self.visible = visible
        self.listed = visible if listed is None else listed
        self.run, self.stopped, self.calls, self.tokens = [], [], [], {}
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

    def list_task_definitions(self, familyPrefix, status, sort, maxResults, nextToken=None):
        assert status == "ACTIVE" and sort == "DESC" and maxResults == 100
        self.calls.append("list_task_definitions")
        # ECS matches familyPrefix as a prefix: games-mptest also lists games-mptest2.
        arns = sorted((a for a in self.revisions if a.split("/", 1)[1].startswith(familyPrefix)),
                      key=lambda a: (a.split("/", 1)[1].rsplit(":", 1)[0], -int(a.rsplit(":", 1)[1])))
        start = int(nextToken or 0)
        page = {"taskDefinitionArns": arns[start:start + self.PAGE]}
        if start + self.PAGE < len(arns):
            page["nextToken"] = str(start + self.PAGE)
        return page

    def describe_task_definition(self, taskDefinition):
        self.calls.append("describe_task_definition")
        # The current production and preview releases' revisions, unless a test lists its own.
        default = {TD: "%s:%s-%s" % (REPO, CURRENT_SIM, sha(7)),
                   PREVIEW_TD: "%s:%s-%s" % (PREVIEW_REPO, CURRENT_SIM, COMMIT[:12])}
        image = self.revisions.get(taskDefinition, default.get(taskDefinition))
        if image is None:
            raise KeyError(taskDefinition)
        # A plain image is an engine revision Terraform registered: one container, marked as
        # verifying join tokens. A list is the container definitions exactly (a legacy revision
        # has no marker).
        containers = image if isinstance(image, list) else [
            {"name": "engine", "image": image, "environment": [VERIFIER_ENTRY]}]
        return {"taskDefinition": {"taskDefinitionArn": taskDefinition, "status": "ACTIVE",
                                   "containerDefinitions": containers}}

    def run_task(self, **kwargs):
        self.calls.append("run_task")
        self.run.append(kwargs)
        if self.run_reply is not None:
            return self.run_reply
        # ECS's clientToken contract: the same token with the same request returns that token's
        # original task as it is now (terminal, if it died); with a different request, a conflict.
        token = kwargs.get("clientToken")
        if token in self.tokens:
            original, arn = self.tokens[token]
            if original != kwargs:
                raise Conflict([arn])
            return {"tasks": [dict(self.tasks[arn])], "failures": []}
        arn = TASK + "new%03d" % len(self.run)
        self.tokens[token] = (dict(kwargs), arn)
        tags = {t["key"]: t["value"] for t in kwargs["tags"]}
        self.tasks[arn] = {"taskArn": arn, "taskDefinitionArn": kwargs["taskDefinition"], "lastStatus": "PROVISIONING",
                           "desiredStatus": "RUNNING", "startedBy": kwargs["startedBy"], "_launched": True,
                           "tags": [{"key": k, "value": v} for k, v in tags.items()]}
        return {"tasks": [{"taskArn": arn}], "failures": []}

    # StopTask on a task ECS does not know yet: how many such calls fail with not-found first
    # (None: every one does), and the error code they fail with.
    stop_unseen_failures = 0
    stop_unseen_code = "InvalidParameterException"

    def stop_task(self, cluster, task, reason):
        assert cluster == "games" and len(reason) <= 255
        self.calls.append("stop_task")
        unseen = task not in self.tasks or (not self.visible and self.tasks[task].get("_launched"))
        if unseen and (self.stop_unseen_failures is None or self.stop_unseen_failures > 0):
            if self.stop_unseen_failures is not None:
                self.stop_unseen_failures -= 1
            raise AwsError(self.stop_unseen_code)
        self.stopped.append(task)
        if task in self.tasks:
            self.tasks[task].update(desiredStatus="STOPPED")


class FakeDynamo:
    """The releases table as games-mp-release writes it (plain values in, typed JSON out)."""

    def __init__(self, items=None):
        self.items = dict(items if items is not None else RELEASED)
        self.reads = []

    def get_item(self, TableName, Key, ConsistentRead):
        assert TableName == "games-mp-releases" and ConsistentRead is True
        key = Key["id"]["S"]
        self.reads.append(key)
        item = self.items.get(key)
        return {"Item": typed(dict(item, id=key))["M"]} if item is not None else {}


def typed(value):
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, dict):
        return {"M": {k: typed(v) for k, v in value.items()}}
    raise TypeError(value)


def release(td, sim=CURRENT_SIM, **more):
    return dict({"games": {"mptest": {"taskDefinition": td, "simVersion": sim}}}, **more)


# What games-mp-release has written: production's current release and one preview commit's.
RELEASED = {
    "current#main": release(TD, commit="d" * 40, seq=7),
    "build#preview#%s" % COMMIT: release(PREVIEW_TD, status="released", commit=COMMIT),
}


class AwsError(Exception):
    """botocore's ClientError, as far as the function reads it."""
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class Conflict(Exception):
    """botocore's ClientError for ECS ConflictException (a clientToken reused with other params);
    ECS names the task(s) already tied to the token in resourceIds."""
    def __init__(self, arns):
        super().__init__("ConflictException")
        self.response = {"Error": {"Code": "ConflictException"}, "resourceIds": list(arns)}


def start_event(n=1, **over):
    event = {"action": "start", "matchId": match(n), "game": "mptest", "simVersion": CURRENT_SIM,
             "secret": "s" * 43, "hardCapSec": 1800, "apiBase": "https://cc-games.app"}
    event.update(over)
    return event


def preview_event(n=1, **over):
    return start_event(n, **{"apiBase": PREVIEW_API, "apiBypass": "B" * 32, "commit": COMMIT, **over})


class LaunchTests(unittest.TestCase):
    def setUp(self):
        # The two functions: production (self.lf) and preview (self.pf).
        self.fns = {env: load_launch(env) for env in FUNCTIONS}
        self.lf, self.pf = self.fns["production"], self.fns["preview"]
        self.log = io.StringIO()   # the handler's one line per call
        self.ddb = FakeDynamo()

    def invoke(self, ecs, event, env="production"):
        fn = self.fns[env]
        fn._clients["ecs"] = ecs
        fn._clients["dynamodb"] = self.ddb
        with contextlib.redirect_stdout(self.log):
            return fn.lambda_handler(event, Context())

    # -- what a launch is ------------------------------------------------------------------

    def test_launches_the_current_release_in_the_engine_network(self):
        ecs = FakeECS()
        reply = self.invoke(ecs, start_event())
        self.assertEqual(reply["ok"], True)
        self.assertEqual(reply["taskArn"], TASK + "new001")
        [call] = ecs.run
        self.check_run_task(call, TD)

    def check_run_task(self, call, task_definition):
        """Everything but the task definition is the same whatever the simVersion."""
        self.assertEqual(call["cluster"], "games")
        self.assertEqual(call["taskDefinition"], task_definition)
        self.assertEqual(call["launchType"], "FARGATE")
        self.assertEqual(call["count"], 1)
        self.assertEqual(call["clientToken"], "production-" + match(1))
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
            {"name": "MP_TOKEN_PUBLIC_KEYS", "value": PROD_KEYS},
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
        self.assertEqual(env["MP_TOKEN_PUBLIC_KEYS"], PREVIEW_KEYS, "preview's keys, never production's")
        self.assertIn({"key": "env", "value": "preview"}, call["tags"])

    # -- join-token keys -------------------------------------------------------------------

    def test_engines_get_only_their_own_environments_public_keys(self):
        bad_values = {
            "a private key": "production:production-kid1:%s" % PRIVATE_DER,
            "a raw HMAC key": "production:kid1:%s" % base64.b64encode(b"\x05" * 32).decode(),
            "the retired MP_TOKEN_KEYS shape": "kid1:%s" % base64.b64encode(b"\x05" * 32).decode(),
            "another environment's key": "%s,%s" % (PROD_KEYS, PREVIEW_KEYS),
            "a non-Ed25519 SPKI": "production:k:%s" % base64.b64encode(
                bytes.fromhex("302a300506032b6571032100") + b"\x01" * 32).decode(),
            "nothing": "",
            "an empty entry": PROD_KEYS + ",",
        }
        for why, value in bad_values.items():
            with self.subTest(why):
                self.lf.TOKEN_PUBLIC_KEYS = value
                ecs = FakeECS()
                with self.assertRaises(RuntimeError) as err:
                    self.invoke(ecs, start_event())
                self.assertEqual(ecs.run, [], "nothing launched")
                self.assertNotIn(PRIVATE_DER, str(err.exception))
        # Each function checks against its own environment: production's keys in the preview
        # function launch nothing either.
        self.pf.TOKEN_PUBLIC_KEYS = PROD_KEYS
        ecs = FakeECS()
        with self.assertRaises(RuntimeError):
            self.invoke(ecs, preview_event(), "preview")
        self.assertEqual(ecs.run, [])
        # A function deployed without the setting refuses too.
        os.environ.pop("TOKEN_PUBLIC_KEYS", None)
        bare = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location("launch_bare", ROOT / "games-multiplayer" / "launch.py"))
        for key, value in dict(ENV, **FUNCTIONS["production"]).items():
            if key != "TOKEN_PUBLIC_KEYS":
                os.environ[key] = value
        bare.__spec__.loader.exec_module(bare)
        self.assertEqual(bare.TOKEN_PUBLIC_KEYS, "")
        bare._clients["ecs"] = ecs = FakeECS()
        bare._clients["dynamodb"] = FakeDynamo()
        with self.assertRaises(RuntimeError), contextlib.redirect_stdout(self.log):
            bare.lambda_handler(start_event(), Context())
        self.assertEqual(ecs.run, [])

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
            ("simVersion", start_event(simVersion=CURRENT_SIM + "\n")),
            ("simVersion", start_event(simVersion="")),
            ("simVersion", start_event(simVersion="m" * 65)),
            ("simVersion", start_event(simVersion="mptest 1")),
            ("simVersion", start_event(simVersion="../mptest-1")),
            ("simVersion", start_event(simVersion="mptest-1:latest")),
            ("simVersion", start_event(simVersion="mptest-1@sha256")),
            ("simVersion", start_event(simVersion="mptést-1")),
            ("simVersion", start_event(simVersion=None)),
            ("simVersion", start_event(simVersion=1)),
            ("simVersion", start_event(simVersion=[CURRENT_SIM])),
            ("simVersion", {k: v for k, v in start_event().items() if k != "simVersion"}),
            ("hardCapSec", start_event(hardCapSec=True)),
            ("hardCapSec", start_event(hardCapSec=1800.0)),
            ("hardCapSec", start_event(hardCapSec="1800")),
            ("hardCapSec", start_event(hardCapSec=59)),
            ("hardCapSec", start_event(hardCapSec=14401)),
            ("commit", start_event(commit="C0FFEE" + COMMIT[6:])),
            ("commit", start_event(commit=COMMIT[:12])),
            ("commit", start_event(commit=COMMIT + "\n")),
            ("commit", start_event(commit=1)),
            ("apiBase", start_event(apiBase="https://cc-games.app.evil.example")),
            ("apiBase", start_event(apiBase="http://cc-games.app")),
            ("apiBase", start_event(apiBase=PREVIEW_API)),       # production calls back production only
            ("apiBypass", start_event(apiBypass="B" * 32)),      # production never passes a bypass
        ]
        for field, event in cases:
            with self.subTest(field=field, value=event.get(field)):
                ecs = FakeECS()
                self.assertEqual(self.invoke(ecs, event), {"ok": False, "error": "bad-request", "field": field})
                self.assertEqual(ecs.calls, [])
        for field, event in [
            ("apiBase", preview_event(apiBase="https://cc-games.app")),
            ("apiBase", preview_event(apiBase="https://evil-abc-coltons-projects-7f9a4e8b.vercel.app")),
            ("apiBase", preview_event(apiBase="https://colton-games-a.b-coltons-projects-7f9a4e8b.vercel.app")),
            ("apiBypass", preview_event(apiBypass="has space in it ok")),
            # A preview runs exactly its own commit's engine, so it must say which.
            ("commit", {k: v for k, v in preview_event().items() if k != "commit"}),
            ("commit", preview_event(commit=None)),
            ("commit", preview_event(commit="main")),
        ]:
            with self.subTest(preview=field, value=event.get(field)):
                ecs = FakeECS()
                self.assertEqual(self.invoke(ecs, event, "preview"),
                                 {"ok": False, "error": "bad-request", "field": field})
                self.assertEqual(ecs.calls, [])
        for event in (None, [], "start", {"action": "launch", "matchId": match(1)}):
            with self.subTest(event=event):
                ecs = FakeECS()
                self.assertEqual(self.invoke(ecs, event)["error"], "bad-request")
                self.assertEqual(ecs.calls, [])

    def test_a_function_deployed_without_its_environment_refuses_everything(self):
        for setting, value in (("LAUNCH_ENV", ""), ("LAUNCH_ENV", "development"), ("LAUNCH_ENV", "Production"),
                               ("API_BASE", "")):
            with self.subTest(**{setting: value}):
                self.lf = self.fns["production"] = load_launch("production")
                setattr(self.lf, setting, value)
                for event in (start_event(), {"action": "stop", "matchId": match(1)}):
                    ecs = FakeECS([engine(1)])
                    self.assertEqual(self.invoke(ecs, event), {"ok": False, "error": "forbidden"})
                    self.assertEqual(ecs.calls, [])

    def test_the_environment_is_the_functions_whatever_the_caller_sends(self):
        # Lambda's context (the invoked ARN, any qualifier) and the request are the caller's;
        # neither can make the preview function launch a production engine.
        ecs = FakeECS()
        self.pf._clients["ecs"] = ecs
        self.pf._clients["dynamodb"] = self.ddb
        ctx = Context()
        ctx.invoked_function_arn = "arn:aws:lambda:us-west-1:%s:function:games-mp-launch-production:production" % ACCOUNT
        with contextlib.redirect_stdout(self.log):
            self.assertTrue(self.pf.lambda_handler(preview_event(), ctx)["ok"])
            self.assertEqual(self.pf.lambda_handler(preview_event(2, env="production"), ctx)["field"], "unknown-field")
        self.assertIn({"key": "env", "value": "preview"}, ecs.run[0]["tags"])
        self.assertEqual(len(ecs.run), 1)

    # -- which revision: production -----------------------------------------------------------

    def test_production_launches_the_current_release_reading_its_marker_once(self):
        ecs = FakeECS()
        self.assertTrue(self.invoke(ecs, start_event())["ok"])
        self.assertEqual(ecs.run[0]["taskDefinition"], TD, "current#main's exact family:revision")
        self.assertEqual(self.ddb.reads, ["current#main"], "read on every launch, nothing else")
        self.assertNotIn("list_task_definitions", ecs.calls)
        self.assertEqual(ecs.calls.count("describe_task_definition"), 1, "its marker, read once")
        # A release moves current#main: the very next launch runs the new revision.
        self.ddb.items["current#main"] = release(revision(8))
        ecs.revisions[revision(8)] = "%s:%s-%s" % (REPO, CURRENT_SIM, sha(8))
        self.assertEqual(self.invoke(ecs, start_event(2))["taskDefinition"], revision(8))
        self.assertTrue(self.invoke(ecs, start_event(3))["ok"])
        self.assertEqual(ecs.calls.count("describe_task_definition"), 2, "each revision read once")

    def test_production_ignores_a_commit(self):
        # Only a preview names its commit; production runs main's release whatever it says.
        ecs = FakeECS()
        reply = self.invoke(ecs, start_event(commit=COMMIT))
        self.assertEqual(reply["taskDefinition"], TD, reply)
        self.assertNotIn("build#preview#" + COMMIT, self.ddb.reads)

    def test_a_revision_without_the_token_verifier_marker_is_never_launched(self):
        old = "%s:mptest-0-%s" % (REPO, sha(4))
        for why, entry in [("no environment", [{"name": "engine", "image": old}]),
                           ("no marker", legacy(old)),
                           ("another value", [{"name": "engine", "image": old, "environment": [
                               {"name": "MP_TOKEN_VERIFIER", "value": "hmac-v1"}]}]),
                           ("marker on the wrong key", [{"name": "engine", "image": old, "environment": [
                               {"name": "MP_TOKEN_VERIFIERS", "value": "ed25519-v2"}]}])]:
            with self.subTest(why):
                self.lf._images.clear()
                self.ddb.items["sim#mptest#mptest-0"] = {"taskDefinition": revision(4)}
                ecs = FakeECS(revisions={revision(4): entry})
                self.assertEqual(self.invoke(ecs, start_event(simVersion="mptest-0")),
                                 {"ok": False, "error": "unknown-sim-version"})
                self.assertEqual(ecs.run, [])
        self.lf._images.clear()
        ecs = FakeECS(revisions={revision(4): old})
        self.assertEqual(self.invoke(ecs, start_event(simVersion="mptest-0"))["taskDefinition"], revision(4))

    def test_a_current_revision_without_the_marker_launches_nothing(self):
        ecs = FakeECS(revisions={TD: legacy("%s:%s-%s" % (REPO, CURRENT_SIM, sha(7)))})
        self.assertEqual(self.invoke(ecs, start_event()), {"ok": False, "error": "unknown-sim-version"})
        self.assertEqual(ecs.run, [])

    def test_an_older_sim_version_launches_the_newest_main_release_for_it(self):
        self.ddb.items["sim#mptest#mptest-0"] = {"taskDefinition": revision(4), "seq": 3}
        ecs = FakeECS(revisions={revision(4): "%s:mptest-0-%s" % (REPO, sha(4))})
        reply = self.invoke(ecs, start_event(simVersion="mptest-0"))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(reply["taskDefinition"], revision(4))
        [call] = ecs.run
        self.check_run_task(call, revision(4))
        self.assertEqual(self.ddb.reads, ["current#main", "sim#mptest#mptest-0"])

    def test_a_release_item_naming_anything_else_is_never_launched(self):
        # Whatever the table says, production runs only games-mptest revisions of its own
        # repository's <simVersion>-<12 hex> image with the marker.
        cases = {
            "a preview revision": (PREVIEW_TD, None),
            "another family": (revision(30, "games-mptest2"), "%s:mptest-0-%s" % (REPO, sha(30))),
            "the router's family": (revision(40, "games-mp-router"), "%s:mptest-0-%s" % (REPO, sha(40))),
            "another account": (revision(2).replace(ACCOUNT, "111111111111"), "%s:mptest-0-%s" % (REPO, sha(2))),
            "not a revision": (revision(2).rsplit(":", 1)[0] + ":latest", "%s:mptest-0-%s" % (REPO, sha(2))),
            "the preview repository": (revision(10), "%s:mptest-0-%s" % (PREVIEW_REPO, sha(10))),
            "another repository": (revision(11), "111111111111.dkr.ecr.us-west-1.amazonaws.com/games/mptest-engine:mptest-0-%s" % sha(11)),
            "a longer repository": (revision(14), "%sx:mptest-0-%s" % (REPO, sha(14))),
            "another simVersion": (revision(15), "%s:mptest-05-%s" % (REPO, sha(15))),
            "not <sim>-<12 hex>": (revision(16), "%s:mptest-0-%s" % (REPO, sha(16).upper())),
            "a digest": (revision(19), "%s@sha256:%s" % (REPO, "a" * 64)),
            "a sidecar": (revision(20), [{"name": "engine", "image": "%s:mptest-0-%s" % (REPO, sha(20)),
                                          "environment": [VERIFIER_ENTRY]},
                                         {"name": "sidecar", "image": "docker.io/evil/x"}]),
            "not named engine": (revision(21), [{"name": "other", "image": "%s:mptest-0-%s" % (REPO, sha(21)),
                                                 "environment": [VERIFIER_ENTRY]}]),
        }
        for n, (why, (td, image)) in enumerate(cases.items(), 1):
            with self.subTest(why):
                self.lf._images.clear()
                self.ddb.items["sim#mptest#mptest-0"] = {"taskDefinition": td}
                ecs = FakeECS(revisions={td: image} if image else {})
                self.assertEqual(self.invoke(ecs, start_event(n, simVersion="mptest-0")),
                                 {"ok": False, "error": "unknown-sim-version"})
                self.assertEqual(ecs.run, [])

    def test_an_unknown_sim_version_or_no_release_launches_nothing(self):
        for version in ("mptest-9", "mptest", "mptest-0-" + sha(3)):
            with self.subTest(version=version):
                ecs = FakeECS()
                self.assertEqual(self.invoke(ecs, start_event(simVersion=version)),
                                 {"ok": False, "error": "unknown-sim-version"})
                self.assertEqual(ecs.run, [])
        # Before the first release (a platform built from nothing), nothing launches.
        self.ddb.items.pop("current#main")
        ecs = FakeECS()
        self.assertEqual(self.invoke(ecs, start_event()), {"ok": False, "error": "unknown-sim-version"})
        self.assertEqual(ecs.run, [])

    # -- dynamic games ------------------------------------------------------------------------

    def test_a_new_game_launches_from_the_shared_engine_repository(self):
        # No Terraform entry: a release registered games-starfall-arena running games/engines.
        td = revision(3, "games-starfall-arena")
        self.ddb.items["current#main"] = {"games": {
            "mptest": {"taskDefinition": TD, "simVersion": CURRENT_SIM},
            "starfall-arena": {"taskDefinition": td, "simVersion": "arena-4"}}, "commit": "e" * 40, "seq": 8}
        ecs = FakeECS(revisions={td: "%s:starfall-arena_arena-4-%s" % (ENGINES, sha(3))})
        reply = self.invoke(ecs, start_event(game="starfall-arena", simVersion="arena-4"))
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(ecs.run[0]["taskDefinition"], td)
        # mptest (released before games were dynamic) still launches from its old repository.
        self.assertTrue(self.invoke(FakeECS(), start_event(2))["ok"])

    def test_a_game_no_release_has_is_refused_cleanly(self):
        for env, event in (("production", start_event(game="starfall", simVersion="hl-1")),
                           ("preview", preview_event(game="starfall", simVersion="hl-1"))):
            with self.subTest(env=env):
                ecs = FakeECS()
                self.assertEqual(self.invoke(ecs, event, env), {"ok": False, "error": "unknown-sim-version"})
                self.assertEqual(ecs.run, [])
                self.assertNotIn("describe_task_definition", ecs.calls)

    def test_game_ids_that_could_name_another_family_are_refused_before_any_read(self):
        # mp-router would be the router's family; preview-* another game's preview family; the
        # rest are not game ids at all.
        for game in ("mp-router", "engines", "preview-mptest", "MPtest", "a", "-x", "x-", "x_y", "x.y",
                     "a" * 33, "", None, 7):
            with self.subTest(game=game):
                ecs = FakeECS()
                reads = len(self.ddb.reads)
                self.assertEqual(self.invoke(ecs, start_event(game=game)),
                                 {"ok": False, "error": "bad-request", "field": "game"})
                self.assertEqual((ecs.run, ecs.calls, len(self.ddb.reads)), ([], [], reads))

    def test_a_revision_must_run_its_own_games_image(self):
        # The release item names games-starfall's revision; its image must be games/engines:
        # starfall_<sim>-<12 hex>, never another game's, a legacy repository it never had, or
        # the preview repository.
        td = revision(4, "games-starfall")
        cases = {
            "another game's image": "%s:mptest_hl-1-%s" % (ENGINES, sha(4)),
            "no game prefix": "%s:hl-1-%s" % (ENGINES, sha(4)),
            "a legacy repository it never had": "%s:hl-1-%s" % (REPO.replace("mptest", "starfall"), sha(4)),
            "the preview engines": "%s:starfall_hl-1-%s" % (PREVIEW_ENGINES, sha(4)),
            "another game's family": None,
        }
        for why, image in cases.items():
            with self.subTest(why):
                self.lf._images.clear()
                arn = revision(4, "games-mptest") if image is None else td
                self.ddb.items["current#main"] = {"games": {"starfall": {"taskDefinition": arn, "simVersion": "hl-1"}},
                                                  "commit": "e" * 40, "seq": 9}
                ecs = FakeECS(revisions={arn: image or "%s:starfall_hl-1-%s" % (ENGINES, sha(4))})
                self.assertEqual(self.invoke(ecs, start_event(game="starfall", simVersion="hl-1")),
                                 {"ok": False, "error": "unknown-sim-version"})
                self.assertEqual(ecs.run, [])

    def test_preview_runs_a_new_game_only_from_its_commit_in_the_preview_repository(self):
        td = revision(6, "games-preview-starfall")
        self.ddb.items["build#preview#" + COMMIT] = {"status": "released", "commit": COMMIT, "games": {
            "starfall": {"taskDefinition": td, "simVersion": "hl-1"}}}
        ok = FakeECS(revisions={td: "%s:starfall_hl-1-%s" % (PREVIEW_ENGINES, COMMIT[:12])})
        self.assertEqual(self.invoke(ok, preview_event(game="starfall", simVersion="hl-1"), "preview")["taskDefinition"], td)
        for image in ("%s:starfall_hl-1-%s" % (ENGINES, COMMIT[:12]),          # production's repository
                      "%s:starfall_hl-1-%s" % (PREVIEW_ENGINES, sha(6))):      # another commit
            with self.subTest(image=image):
                self.pf._images.clear()
                ecs = FakeECS(revisions={td: image})
                self.assertEqual(self.invoke(ecs, preview_event(2, game="starfall", simVersion="hl-1"), "preview"),
                                 {"ok": False, "error": "unknown-sim-version"})

    # -- which revision: preview --------------------------------------------------------------

    def test_preview_launches_exactly_its_commits_revision(self):
        ecs = FakeECS()
        reply = self.invoke(ecs, preview_event(), "preview")
        self.assertEqual(reply["taskDefinition"], PREVIEW_TD, reply)
        self.assertEqual(self.ddb.reads, ["build#preview#" + COMMIT], "never production's items")

    def test_preview_waits_for_its_commits_build(self):
        for status, error in (("starting", "engine-building"), ("building", "engine-building"),
                              ("failed", "engine-build-failed"), (None, "engine-building")):
            with self.subTest(status=status):
                if status is None:
                    self.ddb.items.pop("build#preview#" + COMMIT, None)
                else:
                    self.ddb.items["build#preview#" + COMMIT] = {"status": status}
                ecs = FakeECS()
                self.assertEqual(self.invoke(ecs, preview_event(), "preview"), {"ok": False, "error": error})
                self.assertNotIn("describe_task_definition", ecs.calls)
                self.assertEqual(ecs.run, [])

    def test_preview_never_launches_another_commits_or_a_production_image(self):
        cases = {
            "another commit's image": (revision(6, "games-preview-mptest"),
                                       "%s:%s-%s" % (PREVIEW_REPO, CURRENT_SIM, sha(6))),
            "a production revision": (TD, None),
            "the production repository": (revision(7, "games-preview-mptest"),
                                          "%s:%s-%s" % (REPO, CURRENT_SIM, COMMIT[:12])),
        }
        for why, (td, image) in cases.items():
            with self.subTest(why):
                self.pf._images.clear()
                self.ddb.items["build#preview#" + COMMIT] = release(td, status="released")
                ecs = FakeECS(revisions={td: image} if image else {})
                self.assertEqual(self.invoke(ecs, preview_event(), "preview"),
                                 {"ok": False, "error": "unknown-sim-version"})
                self.assertEqual(ecs.run, [])
        # And another simVersion than the one its commit built.
        self.ddb.items["build#preview#" + COMMIT] = release(PREVIEW_TD, status="released")
        ecs = FakeECS()
        self.assertEqual(self.invoke(ecs, preview_event(simVersion="mptest-0"), "preview"),
                         {"ok": False, "error": "unknown-sim-version"})

    # -- admission ---------------------------------------------------------------------------

    def test_refuses_at_the_environments_ceiling(self):
        ecs = FakeECS([engine(n) for n in range(1, 5)])            # production ceiling 4
        self.assertEqual(self.invoke(ecs, start_event(50)), {"ok": False, "error": "capacity"})
        self.assertEqual(ecs.run, [])
        ecs = FakeECS([engine(n) for n in range(1, 4)])            # one below
        self.assertTrue(self.invoke(ecs, start_event(50))["ok"])
        self.assertEqual(self.invoke(ecs, start_event(51)), {"ok": False, "error": "capacity"})
        self.assertEqual(len(ecs.run), 1)
        ecs = FakeECS([engine(1, env="preview"), engine(2, env="preview")])   # preview ceiling 2
        self.assertEqual(self.invoke(ecs, preview_event(50), "preview"), {"ok": False, "error": "capacity"})
        self.assertEqual(ecs.run, [])

    def test_a_ceiling_of_zero_refuses_every_launch(self):
        # Terraform's split of a total of 0 is 0 and 0 (TerraformTests).
        for env, event in (("production", start_event(50)), ("preview", preview_event(50))):
            self.fns[env].ENV_CEILING = 0
            ecs = FakeECS([])
            self.assertEqual(self.invoke(ecs, event, env), {"ok": False, "error": "capacity"})
            self.assertEqual(ecs.run, [])

    def test_a_warm_retry_forgets_a_cached_task_that_died_and_launches_again(self):
        ecs = FakeECS([])
        first = self.invoke(ecs, start_event(50))
        self.assertTrue(first["ok"])
        for t in ecs.tasks.values():
            t["lastStatus"] = "STOPPED"; t["desiredStatus"] = "STOPPED"
        out = self.invoke(ecs, start_event(50))
        self.assertIsNone(out.get("repeat"), "a stopped task is not the match's engine")
        self.assertNotEqual(out["taskArn"], first["taskArn"], "ECS returned the dead task for the reused token")
        self.assertEqual(ecs.tasks[out["taskArn"]]["desiredStatus"], "RUNNING")
        self.assertNotEqual(ecs.run[-1]["clientToken"], "production-" + match(50), "the replacement needs a token of its own")

    def test_a_cold_retry_with_changed_parameters_never_starts_a_second_engine(self):
        # The original engine is live but ECS shows it nowhere yet; the retry's parameters differ,
        # so RunTask raises a conflict naming it. That is the match's engine, not a dead one.
        ecs = FakeECS([], visible=False)
        first = self.invoke(ecs, start_event(50))
        self.lf._recent.clear()   # a fresh execution environment
        out = self.invoke(ecs, start_event(50, hardCapSec=900))
        self.assertEqual(out["taskArn"], first["taskArn"])
        self.assertEqual(len(ecs.tokens), 1, "no second engine under a fresh token")

    def test_tokens_are_scoped_to_the_environment(self):
        ecs = FakeECS([])
        self.invoke(ecs, preview_event(50), "preview")
        self.invoke(ecs, start_event(50))
        self.assertEqual([c["clientToken"] for c in ecs.run], ["preview-" + match(50), "production-" + match(50)])
        self.assertEqual(len({c["clientToken"] for c in ecs.run}), 2, "a preview launch must not hold production's token")

    def test_a_conflict_never_adopts_another_environments_engine(self):
        # Suppose the production token were somehow tied to a live PREVIEW engine: refuse it.
        foreign = engine(50, env="preview")
        ecs = FakeECS([foreign])
        ecs.tokens["production-" + match(50)] = ({"other": "request"}, foreign["taskArn"])
        with self.assertRaises(RuntimeError):
            self.invoke(ecs, start_event(50))

    def test_a_lost_replacement_response_never_starts_a_second_replacement(self):
        # Engine 1 dies; a warm retry replaces it (engine 2). Engine 2's reply is lost and the
        # next retry lands in a fresh environment where ECS shows neither task yet.
        ecs = FakeECS([])
        first = self.invoke(ecs, start_event(50))
        for t in ecs.tasks.values():
            t["lastStatus"] = "STOPPED"; t["desiredStatus"] = "STOPPED"
        replacement = self.invoke(ecs, start_event(50))
        self.lf._recent.clear()
        ecs.visible = False
        retry = self.invoke(ecs, start_event(50))
        self.assertEqual(retry["taskArn"], replacement["taskArn"])
        live = [a for a, t in ecs.tasks.items() if t["desiredStatus"] != "STOPPED"]
        self.assertEqual(live, [replacement["taskArn"]], "exactly one live engine for the match")

    def test_each_dead_generation_gets_its_own_deterministic_token(self):
        ecs = FakeECS([])
        seen = []
        for _ in range(3):
            out = self.invoke(ecs, start_event(50))
            seen.append(out["taskArn"])
            for t in ecs.tasks.values():
                t["lastStatus"] = "STOPPED"; t["desiredStatus"] = "STOPPED"
        self.assertEqual(len(set(seen)), 3, "each replacement is a new engine")
        tokens = [c["clientToken"] for c in ecs.run]
        self.assertEqual(len(tokens), len(set(tokens)) + 3, "each retry re-sends earlier tokens, then one new one")

    def test_a_replacement_with_changed_parameters_survives_the_token_conflict(self):
        ecs = FakeECS([])
        first = self.invoke(ecs, start_event(50))
        for t in ecs.tasks.values():
            t["lastStatus"] = "STOPPED"; t["desiredStatus"] = "STOPPED"
        out = self.invoke(ecs, start_event(50, hardCapSec=900))   # a different request, same token
        self.assertTrue(out["ok"])
        self.assertNotEqual(out["taskArn"], first["taskArn"])

    def test_a_warm_retry_trusts_a_cached_task_ecs_does_not_show_yet(self):
        ecs = FakeECS([], visible=False)
        self.assertTrue(self.invoke(ecs, start_event(50))["ok"])
        self.assertTrue(self.invoke(ecs, start_event(50)).get("repeat"))
        self.assertEqual(len(ecs.run), 1)

    def test_a_retry_after_a_cold_start_returns_the_running_engine_even_at_the_ceiling(self):
        # The match's engine (t050) started, the response was lost, and the retry lands in a
        # fresh execution environment with the ceiling (4) full, that engine included.
        ecs = FakeECS([engine(n) for n in range(1, 4)] + [engine(50)])
        out = self.invoke(ecs, start_event(50))
        self.assertEqual((out["ok"], out["taskArn"], out.get("repeat")), (True, TASK + "t050", True))
        self.assertEqual(ecs.run, [])

    def test_a_retry_never_adopts_another_environments_or_a_stopping_engine(self):
        for other in (engine(50, env="preview"), engine(50, status="STOPPING"), engine(50, tags={})):
            self.lf._recent.clear()   # each case is its own cold start
            ecs = FakeECS([other])
            self.assertTrue(self.invoke(ecs, start_event(50)).get("repeat") is None)
            self.assertEqual(len(ecs.run), 1)

    def test_each_environment_counts_its_own_engines_and_untagged_ones_only(self):
        # Production: 2 of its own + 1 untagged of 4; the preview engines are preview's share.
        ecs = FakeECS([engine(1), engine(2), engine(3, tags={}), engine(4, env="preview"), engine(5, env="preview")])
        self.assertTrue(self.invoke(ecs, start_event(50))["ok"], "preview's engines never take production's room")
        self.assertEqual(self.invoke(ecs, start_event(51)), {"ok": False, "error": "capacity"})
        # Preview (2): its own two fill it, whatever production runs.
        self.assertEqual(self.invoke(ecs, preview_event(52), "preview"), {"ok": False, "error": "capacity"})
        self.assertEqual(len(ecs.run), 1)

    def test_an_engine_without_an_env_tag_counts_against_every_environment(self):
        # Two engines whose tags could not be read fill preview (2) and take half of production (4).
        ecs = FakeECS([engine(1, tags={}), engine(2, tags={})])
        self.assertEqual(self.invoke(ecs, preview_event(50), "preview"), {"ok": False, "error": "capacity"})
        self.assertEqual(ecs.run, [])
        self.assertEqual(self.lf.count_engines(ecs, "production", now=0), (2, 2))

    def test_the_shares_bound_the_total_with_no_shared_lock(self):
        # Both functions launch to their own ceilings against the same cluster; together they
        # never pass the total (4 + 2), however the calls interleave.
        ecs = FakeECS([])
        for n in range(10):
            self.invoke(ecs, start_event(100 + n))
            self.invoke(ecs, preview_event(200 + n), "preview")
        self.assertEqual(len(ecs.run), 6)
        self.assertEqual(sorted({t["key"]: t["value"] for t in c["tags"]}["env"] for c in ecs.run),
                         ["preview"] * 2 + ["production"] * 4)

    def test_preview_has_its_own_smaller_ceiling_and_cannot_starve_production(self):
        ecs = FakeECS([engine(1, env="preview"), engine(2, env="preview")])   # preview ceiling 2
        self.assertEqual(self.invoke(ecs, preview_event(50), "preview"), {"ok": False, "error": "capacity"})
        self.assertTrue(self.invoke(ecs, start_event(51))["ok"], "production still launches")
        self.assertEqual(len(ecs.run), 1)

    def test_the_two_functions_share_no_state(self):
        # Separate functions, separate execution environments: preview's launches and caches are
        # never production's, so one cannot hold or clear the other's bookkeeping.
        ecs = FakeECS([], visible=False)
        self.invoke(ecs, preview_event(50), "preview")
        self.assertEqual(set(self.pf._recent), {match(50)})
        self.assertEqual(self.lf._recent, {})
        self.assertEqual(self.lf.count_engines(ecs, "production", now=0), (0, 0),
                         "preview's unlisted launch is preview's to count")

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
        # Production's own: 3 and 5, plus 1 and 2, whose env tag is missing (fail closed).
        self.assertEqual((total, in_env), (5, 4))

    def test_launches_ecs_cannot_see_yet_still_count(self):
        # ECS is eventually consistent: a task started a moment ago may not be listed.
        ecs = FakeECS([engine(n) for n in range(1, 3)], visible=False)
        self.assertTrue(self.invoke(ecs, start_event(50))["ok"])
        self.assertTrue(self.invoke(ecs, start_event(51))["ok"])
        self.assertEqual(self.invoke(ecs, start_event(52)), {"ok": False, "error": "capacity"})
        self.assertEqual(len(ecs.run), 2)

    def test_a_just_launched_task_ecs_reports_stopped_frees_its_slot(self):
        ecs = FakeECS([engine(n) for n in range(1, 4)])
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

    def test_stop_right_after_a_launch_stops_the_task_ecs_cannot_show_yet(self):
        # ECS lists and describes the new task nowhere yet, but StopTask on its ARN works.
        ecs = FakeECS(visible=False)
        arn = self.invoke(ecs, start_event(3))["taskArn"]
        self.assertEqual(self.invoke(ecs, {"action": "stop", "matchId": match(3)}), {"ok": True, "stopped": 1})
        self.assertEqual(ecs.stopped, [arn])
        self.assertEqual(self.lf._recent, {}, "stopped: no longer counted")

    def test_stop_retries_while_ecs_cannot_find_the_new_task(self):
        for code in ("InvalidParameterException", "AccessDeniedException"):
            with self.subTest(code=code):
                self.lf._recent.clear()
                ecs = FakeECS(visible=False)
                ecs.stop_unseen_failures, ecs.stop_unseen_code = 2, code
                arn = self.invoke(ecs, start_event(3))["taskArn"]
                self.assertEqual(self.invoke(ecs, {"action": "stop", "matchId": match(3)}), {"ok": True, "stopped": 1})
                self.assertEqual((ecs.stopped, ecs.calls.count("stop_task")), ([arn], 3))

    def test_a_stop_that_cannot_reach_the_new_task_yet_says_so_and_a_retry_stops_it(self):
        ecs = FakeECS(visible=False)
        ecs.stop_unseen_failures = None       # ECS cannot find it at all yet
        arn = self.invoke(ecs, start_event(3))["taskArn"]
        self.assertEqual(self.invoke(ecs, {"action": "stop", "matchId": match(3)}),
                         {"ok": False, "error": "not-yet-visible"})
        self.assertEqual(ecs.calls.count("stop_task"), 3, "STOP_ATTEMPTS, then give up for this call")
        self.assertEqual(ecs.stopped, [])
        self.assertIn(match(3), self.lf._recent, "still counted, and still known to the next stop")
        ecs.visible = ecs.listed = True        # the lobby's retry, once ECS catches up
        self.assertEqual(self.invoke(ecs, {"action": "stop", "matchId": match(3)}), {"ok": True, "stopped": 1})
        self.assertEqual(ecs.stopped, [arn])

    def test_a_stop_error_other_than_not_found_is_raised(self):
        ecs = FakeECS(visible=False)
        ecs.stop_unseen_failures, ecs.stop_unseen_code = 1, "ThrottlingException"
        self.invoke(ecs, start_event(3))
        with self.assertRaises(AwsError):
            self.invoke(ecs, {"action": "stop", "matchId": match(3)})

    def test_stop_never_stops_an_unseen_task_it_did_not_launch_for_this_match_recently(self):
        ecs = FakeECS(visible=False)
        # Another match's launch, and one too old for ECS still to be catching up on.
        self.invoke(ecs, start_event(4))
        self.lf._recent[match(5)] = (TASK + "old", "production", -1000.0)
        for n in (3, 5):
            self.assertEqual(self.invoke(ecs, {"action": "stop", "matchId": match(n)}), {"ok": True, "stopped": 0})
        self.assertEqual(ecs.calls.count("stop_task"), 0)
        # And the preview function knows nothing of production's launch of match 4.
        self.assertEqual(self.invoke(ecs, {"action": "stop", "matchId": match(4)}, "preview"), {"ok": True, "stopped": 0})
        self.assertEqual(ecs.stopped, [])

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

    def test_one_function_per_environment_each_serialized_on_its_own(self):
        self.assertIn('source_file = "${path.module}/games-multiplayer/launch.py"', GAMES)
        self.assertRegex(self.fn, r'handler\s*=\s*"launch\.lambda_handler"')
        self.assertIn("for_each = local.games_mp_launch_environments\n", self.fn)
        self.assertRegex(self.fn, r'function_name\s*=\s*"games-mp-launch-\$\{each\.key\}"')
        # Its own concurrency slot per environment: Preview keeping its function busy never
        # throttles production's.
        self.assertRegex(self.fn, r"reserved_concurrent_executions = 1\n")
        self.assertIn("log_group  = aws_cloudwatch_log_group.games_mp_launch[each.key].name", self.fn)
        # No versions or aliases: nothing but the function's own configuration picks the environment.
        self.assertNotRegex(self.fn, r"publish\s*=\s*true")
        self.assertNotIn('resource "aws_lambda_alias"', GAMES)
        self.assertNotIn("aws_lambda_alias", BRINGUP)

    def test_async_invocations_are_never_retried_or_left_queued(self):
        cfg = tf_block(GAMES, "aws_lambda_function_event_invoke_config", "games_mp_launch")
        self.assertIn("for_each = local.games_mp_launch_environments\n", cfg)
        self.assertIn("function_name                = aws_lambda_function.games_mp_launch[each.key].function_name", cfg)
        self.assertIn("maximum_retry_attempts       = 0\n", cfg)
        self.assertIn("maximum_event_age_in_seconds = 60\n", cfg)

    def test_the_ceiling_is_split_so_the_shares_sum_to_the_total(self):
        envs = re.search(r"games_mp_launch_environments = \{.*?\n  \}\n", GAMES, re.S).group()
        self.assertIn("games_mp_preview_ceiling = min(8, var.games_mp_engine_ceiling)\n", GAMES)
        self.assertIn("ceiling  = var.games_mp_engine_ceiling - local.games_mp_preview_ceiling\n", envs)
        self.assertIn("ceiling = local.games_mp_preview_ceiling\n", envs)
        self.assertEqual(len(re.findall(r"^\s+ceiling\s*=", envs, re.M)), 2)
        # min(8, t) <= t for every allowed total (0..200), so neither share goes negative:
        # 30 -> 22 + 8, 8 -> 0 + 8, 0 -> 0 + 0 (the kill switch stops both).
        # The lobby's caps fit each share at the default ceiling, and Terraform warns if not.
        caps = re.search(r"games_mp_lobby_max_active = \{ production = (\d+), preview = (\d+) \}", GAMES)
        default = int(re.search(r'variable "games_mp_engine_ceiling" \{.*?default\s*=\s*(\d+)', GAMES, re.S).group(1))
        self.assertLessEqual(int(caps.group(1)), default - min(8, default))
        self.assertLessEqual(int(caps.group(2)), min(8, default))
        check = re.search(r'check "games_mp_lobby_caps_fit_the_launch_ceilings" \{.*?\n\}\n', GAMES, re.S).group()
        self.assertIn("cap <= local.games_mp_launch_environments[env].ceiling", check)

    def test_the_function_gets_the_settings_the_tests_assume(self):
        for line in ("RELEASES_TABLE = aws_dynamodb_table.games_mp_releases.name",
                     "ENGINE_FAMILY_PREFIX = local.mp_engine_channels[each.value.channel].family_prefix",
                     "ENGINE_REPOSITORY    = aws_ecr_repository.games_mp[local.mp_engine_channels[each.value.channel].repository].repository_url",
                     'LEGACY_REPOSITORIES = jsonencode({ for id in local.mp_legacy_engine_games : id =>',
                     'aws_ecr_repository.games_mp["${local.mp_engine_channels[each.value.channel].repository_prefix}${id}-engine"].repository_url',
                     "LAUNCH_ENV   = each.key",
                     "ENV_CEILING  = tostring(each.value.ceiling)",
                     "API_BASE     = each.value.api_base",
                     "ALLOW_BYPASS = tostring(each.value.bypass)",
                     "SECURITY_GROUP   = aws_security_group.games_engine.id",
                     # The engines' own subnets (games-rt, no I/O-box peer route; games-engine ACL),
                     # never the dev fleet's subnet_a/subnet_b.
                     'SUBNETS          = join(",", [for s in local.mp_engine_subnets : s.id])',
                     "ROUTER_FAMILY    = local.mp_router_family",
                     "MAX_HARDCAP_SEC = tostring(local.mp_router_drain_sec)"):
            # Compared with runs of spaces collapsed: terraform fmt realigns the `=` column.
            self.assertIn(" ".join(line.split()), " ".join(self.fn.split()))
        # Production runs main's families and repositories, preview its own, never the other's.
        channels = re.search(r"mp_engine_channels = \{.*?\n  \}\n", GAMES, re.S).group()
        self.assertIn('main    = { family_prefix = "games-", repository_prefix = "games/", repository = "games/engines" }', channels)
        self.assertIn('preview = { family_prefix = "games-preview-", repository_prefix = "games-preview/", repository = "games-preview/engines" }', channels)
        self.assertNotIn("mp_games", GAMES, "no list of games in Terraform")
        self.assertEqual(re.findall(r'channel\s*=\s*"(\w+)"', envs_block := re.search(
            r"games_mp_launch_environments = \{.*?\n  \}\n", GAMES, re.S).group()), ["main", "preview"])
        # No match outlives a draining router (the target group's delay is the same local).
        self.assertIn("mp_router_drain_sec = 3600", GAMES)
        self.assertIn("deregistration_delay = local.mp_router_drain_sec", GAMES)
        self.assertIn("mp_engine_subnets = values(aws_subnet.games_engine)", GAMES)
        self.assertNotRegex(self.fn, r"aws_subnet\.subnet_[ab]|dev_fleet_subnets")
        envs = re.search(r"games_mp_launch_environments = \{.*?\n  \}\n", GAMES, re.S).group()
        self.assertEqual(sorted(re.findall(r"^    (\w+) = \{", envs, re.M)), ["preview", "production"])
        self.assertIn(r'api_base = "^https://cc-games\\.app$"', envs)
        self.assertIn(r'api_base = "^https://${local.vercel_project_name}-[a-z0-9-]+-${local.vercel_team_slug}\\.vercel\\.app$"',
                      envs)
        # Each environment's engines get that environment's public keys, and nothing else.
        self.assertEqual(re.findall(r"token_public_keys = (.*)", envs), [
            'local.games_mp_token_public_keys_by_env["production"]',
            'local.games_mp_token_public_keys_by_env["preview"]'])
        self.assertIn("TOKEN_PUBLIC_KEYS = each.value.token_public_keys", self.fn)
        by_env = re.search(r"^  games_mp_token_public_keys_by_env = \{.*?\n  \}\n", BRINGUP, re.S | re.M).group()
        self.assertIn('for env in local.games_mp_token_envs : env => join(",", [', by_env)
        self.assertIn('"${env}:${env}-${kid}:${local.games_mp_token_public_der["${env}-${kid}"]}"', by_env)
        self.assertNotRegex(by_env, r"private|signing")
        # The functions start launching new engine revisions only after the router rollout.
        self.assertRegex(self.fn, r"depends_on = \[[^\]]*terraform_data\.games_mp_healthy[,\]]")
        self.assertEqual(re.findall(r"bypass\s*=\s*(\w+)", envs), ["false", "true"])
        # The test's FUNCTIONS settings are what those HCL values render to (tostring(bool) is
        # "true"/"false"; HCL's "\\." is the regex's \.).
        self.assertEqual([FUNCTIONS[e]["ALLOW_BYPASS"] for e in ("production", "preview")], ["false", "true"])
        self.assertEqual(FUNCTIONS["production"]["API_BASE"], r"^https://cc-games\.app$")
        self.assertEqual(FUNCTIONS["preview"]["API_BASE"],
                         r"^https://colton-games-[a-z0-9-]+-coltons-projects-7f9a4e8b\.vercel\.app$")
        for gone in ("ENGINE_CEILING", "ENVIRONMENTS"):
            self.assertNotRegex(self.fn, r"\b%s\s*=" % gone)
        self.assertIn('vercel_team_slug    = "coltons-projects-7f9a4e8b"', GAMES)
        self.assertIn('vercel_project_name = "colton-games"', GAMES)


    def test_every_engine_revision_carries_the_token_verifier_marker(self):
        # games-mp-release registers every engine revision now; Terraform registers none.
        release = (ROOT / "games-multiplayer" / "release.py").read_text()
        self.assertIn('TOKEN_VERIFIER = {"name": "MP_TOKEN_VERIFIER", "value": "ed25519-v2"}', release)
        self.assertNotIn('resource "aws_ecs_task_definition" "games_engine"', GAMES)
        self.assertRegex(GAMES, r'removed \{\n  from = aws_ecs_task_definition\.games_engine\n\n  lifecycle \{\n    destroy = false')
        # The same pair launch.py requires, and the one the fake ECS gives released revisions.
        lf = load_launch()
        self.assertEqual(lf.TOKEN_VERIFIER, ("MP_TOKEN_VERIFIER", "ed25519-v2"))
        self.assertEqual((VERIFIER_ENTRY["name"], VERIFIER_ENTRY["value"]), lf.TOKEN_VERIFIER)

    def test_the_launch_role_uses_the_standard_lambda_trust(self):
        # Lambda supplies no aws:SourceAccount when it assumes an execution role: a condition on
        # it leaves the function unable to run, and every launch with it.
        role = GAMES.split('resource "aws_iam_role" "games_mp_launch"', 1)[1].split("\n}\n", 1)[0]
        self.assertIn('Principal = { Service = "lambda.amazonaws.com" }', role)
        self.assertNotIn("Condition", role)

    def test_the_launch_role_can_read_task_tags(self):
        # DescribeTasks include=TAGS returns no tags without ListTagsForResource.
        # Its own statement, with no ecs:cluster condition (that key does not apply to it).
        stmt = GAMES.split('Sid      = "ReadTaskTagsInThisCluster"', 1)[1].split("},", 1)[0]
        self.assertIn('Action   = "ecs:ListTagsForResource"', stmt)
        self.assertNotIn("ecs:cluster", stmt)

    def test_the_sim_version_shapes_agree(self):
        # The release accepts a subset of what the function and the lobby accept: the same
        # characters, but starting like a Docker tag (not "." or "-").
        release = (ROOT / "games-multiplayer" / "release.py").read_text()
        self.assertIn('SIM_VERSION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")', release)
        self.assertEqual(self.lf_sim_shape(), "[A-Za-z0-9._-]{1,64}")

    def lf_sim_shape(self):
        return re.search(r'SIM_VERSION = re\.compile\(r"([^"]+)"\)',
                         (ROOT / "games-multiplayer" / "launch.py").read_text()).group(1)

    def test_launch_role_runs_only_engine_family_revisions_and_passes_only_engine_roles(self):
        self.assertIn("for_each = local.games_mp_launch_environments", self.launch_policy)
        run = statement(self.launch_policy, "RunOwnEngineFamilyRevisions")
        resource = re.search(r"Resource\s*=\s*(.*)", run).group(1)
        # Games are dynamic, so the Allow is this environment's family PREFIX (any game) ...
        self.assertEqual(resource, '"arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:'
                                   'task-definition/${local.mp_engine_channels[each.value.channel].family_prefix}*:*"')
        self.assertRegex(run, r'Action\s*=\s*"ecs:RunTask"')
        self.assertIn('ArnEquals = { "ecs:cluster" = aws_ecs_cluster.games.arn }', run)
        # ... and production's prefix games-* would cover the preview families and the router's,
        # so both are denied (Deny wins over any Allow).
        self.assertEqual(len(re.findall(r'"ecs:RunTask"', self.launch_policy)), 3,
                         "the Allow, the preview-families Deny and the router Deny")
        preview_deny = statement(self.launch_policy, "NeverRunPreviewEngines")
        self.assertRegex(preview_deny, r'Effect\s*=\s*"Deny"')
        self.assertIn("task-definition/${local.mp_engine_channels.preview.family_prefix}*:*", preview_deny)
        self.assertIn('each.value.channel == "main" ? [{\n        Sid      = "NeverRunPreviewEngines"', self.launch_policy)
        read = statement(self.launch_policy, "ReadEngineTaskDefinitions")
        self.assertIn('Action   = "ecs:DescribeTaskDefinition"', read)
        self.assertRegex(read, r'Effect\s*=\s*"Allow"')
        self.assertRegex(read, r'Resource\s*=\s*"\*"')
        # Never every task definition: always an engine family prefix.
        self.assertNotIn("task-definition/*", self.launch_policy)
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
        # Its own release items only, read-only.
        items = statement(self.launch_policy, "ReadOwnReleaseItems")
        self.assertIn('Action   = "dynamodb:GetItem"', items)
        self.assertIn('"dynamodb:LeadingKeys" = each.key == "production" ? ["current#main", "sim#*"] : ["build#preview#*"]',
                      items)

    def test_launcher_roles_can_only_invoke_their_own_environments_function(self):
        self.assertIn("for_each = local.games_mp_launcher_role_names", self.launcher_policy)
        self.assertEqual(re.findall(r'Action\s*=\s*("[^"]*"|\[[^\]]*\])', self.launcher_policy),
                         ['"lambda:InvokeFunction"'])
        self.assertIn("Resource = aws_lambda_function.games_mp_launch[each.key].arn\n", self.launcher_policy)
        for forbidden in ("ecs:", "iam:", "ec2:", "RunTask", "PassRole", '"*"', ":*", "games_mp_launch.arn",
                          "qualified_arn", '["production"]', '["preview"]'):
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

    def test_vercel_gets_each_environment_its_own_role_and_function_and_no_ecs_settings(self):
        env = BRINGUP[BRINGUP.index("games_mp_vercel_shared_config = {"):BRINGUP.index("games_mp_vercel_secret_values = {")]
        for e in ("production", "preview"):
            self.assertRegex(env, r'"MP_LAUNCH_ROLE_ARN/%s"\s+= \{ targets = \["%s"\], sensitive = false, '
                                  r'value = aws_iam_role\.games_mp_launcher\["%s"\]\.arn \}' % (e, e, e))
            self.assertRegex(env, r'"MP_LAUNCH_FUNCTION/%s"\s+= \{ targets = \["%s"\], sensitive = false, '
                                  r'value = aws_lambda_function\.games_mp_launch\["%s"\]\.arn \}' % (e, e, e))
        for gone in ("MP_ROLE_ARN", "AWS_ROLE_ARN", "MP_CLUSTER", "MP_SUBNETS", "MP_ENGINE_SG"):
            self.assertNotRegex(env, r"\b%s\s*=" % gone)
            self.assertNotIn('"%s' % gone, env)


if __name__ == "__main__":
    unittest.main(verbosity=2)

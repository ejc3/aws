"""games-mp-launch: the only way a match engine starts, and the engine ceiling's admission check.

The colton-games lobby (Vercel) used to call ecs:RunTask itself. IAM cannot restrict a
RunTask's container overrides, and the sweeper only trims engines above the ceiling once a
minute, so any deployment holding the launcher's credentials, including every Preview build,
could run arbitrary commands in an engine image and burst to the account's Fargate quota
between sweeps. Now the launcher roles may only invoke this function, and this function builds
every RunTask itself:

- the cluster, the subnets, the security group and the public-IP setting are fixed here, and
  the task definition is always a revision of the game's own family, chosen by simVersion
  (SIM VERSIONS AND COMMITS below);
- the container gets only the environment below, from validated fields; nothing the caller
  sends is passed through as an override, a tag, a command, a role or a size;
- it refuses to launch when its environment's running engines are at its ceiling.

CALLER ENVIRONMENT. There is one function per Vercel environment, `games-mp-launch-production`
and `games-mp-launch-preview` (games-multiplayer.tf), each deployed with its own LAUNCH_ENV and
settings, and each environment's launcher role may invoke only its own. So the function the
caller could reach decides the engine's MP_ENV, the `env` tag, the allowed callback URL, whether
a Vercel protection-bypass secret may be passed, and the ceiling; nothing in the request can.

ADMISSION IS SERIALIZED PER ENVIRONMENT. Each function has reserved concurrency 1
(games-multiplayer.tf), so two invocations of one environment never run at once and its
count-then-launch cannot interleave: the repo's usual fix for "concurrent callers all read the
same count" (runner webhook, GITHUB-RUNNERS.md). The two functions do not share that slot, so a
Preview build keeping its own function busy never delays a production launch. They need no
shared lock either: each enforces only its own environment's ceiling on engines tagged with its
environment, and Terraform splits the total so the two ceilings sum to it (preview min(8,
total), production the rest). Untagged engines count against both. A DynamoDB conditional
counter would also need a decrement on every task stop (an EventBridge rule and a second
function) to stay true; ECS already knows what is running.

ECS reads are eventually consistent: a task started a moment ago may not be listed yet. So
the function also counts every task it launched in the last SETTLE_SEC seconds (_recent,
kept in memory; a burst keeps the single execution environment warm) and asks ECS about
those by ARN, dropping them only once ECS says they are stopping. What that cannot cover is a
fresh execution environment's first seconds, when tasks the previous environment launched
just before it was recycled may not be listed yet: a bounded overshoot of a few engines at
most, which the sweeper (games-multiplayer/sweeper.py) stops within a minute.

What is counted: every task in the cluster whose task definition family is not the
router's, and that is not being stopped (the sweeper's rule), and whose `env` tag is this
function's environment or missing. Reading the tag is safe because only these functions (and
administrators) can RunTask an engine or tag a task; the launcher roles can do neither.

STOP AFTER A FRESH LAUNCH. The same eventual consistency hides a just-started task from
ListTasks and DescribeTasks, so a stop right after a launch can find nothing. For the task this
execution environment launched for that match (_recent), stop() then calls StopTask on the ARN
directly, retrying briefly while ECS does not know it yet, and if it still cannot, answers
{"ok": false, "error": "not-yet-visible"} so the lobby retries rather than believing it stopped.
A stop in a fresh execution environment has no such record and can still find nothing; that
engine runs until it exits or the sweeper's hard cap stops it.

SIM VERSIONS AND COMMITS. Engine revisions are registered by games-mp-release (release.py), never
by hand or by Terraform, and it records which is which in the releases table (RELEASES_TABLE):
  - production launches the current main release (`current#main`) for the game's current
    simVersion, and for an older simVersion (clients still on an older site) the newest main
    revision released for it (`sim#<game>#<simVersion>`);
  - preview launches exactly the revision built from the lobby's own commit (the request's
    `commit`, the deployment's VERCEL_GIT_COMMIT_SHA; item `build#preview#<commit>`), and
    answers `engine-building` until that commit's images are built and released, or
    `engine-build-failed` if its build failed. Production ignores `commit`: a preview's code
    never reaches a production engine.
Whatever the table says, the revision must be of this function's own family for the game
(GAMES: `games-<game>` on production, `games-preview-<game>` on preview; IAM lets each function
run only its own families) and run exactly `<its repository>:<simVersion>-<12 hex>` (a preview's:
its commit's 12 hex), or nothing is launched (`unknown-sim-version`).

TOKEN VERIFIER. Engines built before engines verified join tokens themselves trust the
router's X-MP-* identity headers, so launching one would let a compromised router claim any of
its seats. Every engine revision (games-mp-release registers them; Terraform did before) carries
the container environment entry MP_TOKEN_VERIFIER=ed25519-v2 (TOKEN_VERIFIER), and the function
launches no revision without it (read once per revision with DescribeTaskDefinition, then
cached: a revision's container definitions never change).

JOIN-TOKEN KEYS. Every engine verifies players' join tokens itself (colton-games mp-kit), so it
gets MP_TOKEN_PUBLIC_KEYS: the Ed25519 public keys of this function's environment only
(TOKEN_PUBLIC_KEYS, from Terraform), checked here to be exactly that before
any launch: `<env>:<kid>:<base64 of a 44-byte Ed25519 SubjectPublicKeyInfo>` entries, every one
for this environment. A private key, a raw HMAC key or another environment's key is never
passed on (the function refuses to launch with a RuntimeError instead).

No dependency beyond boto3 (bundled in the Lambda runtime). Tested offline by
scripts/test-games-mp-launch.py against fake ECS clients.
"""

import base64
import json
import os
import re
import hashlib
import time

CLUSTER = os.environ.get("CLUSTER", "games")
SUBNETS = [s for s in os.environ.get("SUBNETS", "").split(",") if s]
SECURITY_GROUP = os.environ.get("SECURITY_GROUP", "")
# Game id -> {"family": this environment's task definition family for it, "repository": the ECR
# repository URL its images must come from}. Production: games-<game> and games/<game>-engine;
# preview: games-preview-<game> and games-preview/<game>-engine (Terraform).
GAMES = json.loads(os.environ.get("GAMES", "{}"))
# games-mp-release's table: which revision is current, per simVersion, and per preview commit.
RELEASES_TABLE = os.environ.get("RELEASES_TABLE", "")
ROUTER_FAMILY = os.environ.get("ROUTER_FAMILY", "games-mp-router")
# This function's environment (production / preview) and its settings. No default: a function
# deployed without them refuses every call.
LAUNCH_ENV = os.environ.get("LAUNCH_ENV", "")
# Most engines of this environment running at once. Terraform splits the total engine ceiling
# between the environments' functions, so the ceilings sum to it and 0 stops every launch.
ENV_CEILING = int(os.environ.get("ENV_CEILING", "0"))
# The callback URL this environment's engines may use (a regex, fullmatched).
API_BASE = os.environ.get("API_BASE", "")
# Whether a Vercel protection-bypass secret may be passed (previews only).
ALLOW_BYPASS = os.environ.get("ALLOW_BYPASS", "false") == "true"
# This environment's Ed25519 join-token public keys, handed to every engine it starts (checked
# by engine_token_keys before each launch).
TOKEN_PUBLIC_KEYS = os.environ.get("TOKEN_PUBLIC_KEYS", "")
MIN_HARDCAP_SEC = int(os.environ.get("MIN_HARDCAP_SEC", "60"))
MAX_HARDCAP_SEC = int(os.environ.get("MAX_HARDCAP_SEC", "14400"))
SETTLE_SEC = int(os.environ.get("SETTLE_SEC", "120"))
# StopTask on a task ECS does not know yet: attempts, and the pause before each retry.
STOP_ATTEMPTS = int(os.environ.get("STOP_ATTEMPTS", "3"))
STOP_RETRY_SEC = float(os.environ.get("STOP_RETRY_SEC", "0.5"))

ENGINE_CONTAINER = "engine"
ENGINE_PORT = "8080"

# RFC 8410: SEQUENCE { SEQUENCE { OID 1.3.101.112 (Ed25519) } BIT STRING (32-byte key) }.
_ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
_TOKEN_PUBLIC_KEY = re.compile(r"([a-z][a-z0-9-]{0,31}):([A-Za-z0-9_-]{1,32}):([A-Za-z0-9+/]{59}=)")

# The container environment entry marking an engine revision whose kit verifies join tokens
# itself (games-multiplayer.tf, aws_ecs_task_definition.games_engine). No marker, no launch.
TOKEN_VERIFIER = ("MP_TOKEN_VERIFIER", "ed25519-v2")

_STOPPING = {"DEACTIVATING", "STOPPING", "DEPROVISIONING", "STOPPED", "DELETED"}
# What StopTask answers for a task ECS does not know yet: not found (InvalidParameterException,
# "The referenced task was not found"), or AccessDenied because IAM cannot read the `match` tag
# its StopOnlyMatchEngines grant requires off a task ECS cannot find.
_NOT_YET_VISIBLE = {"InvalidParameterException", "ClientException", "AccessDeniedException"}

# Always fullmatch: `$` would also accept a trailing newline.
MATCH_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
# The lobby's per-match secret: base64url of 32 random bytes (43 characters).
SECRET = re.compile(r"[A-Za-z0-9_-]{32,128}")
# A preview lobby's commit (VERCEL_GIT_COMMIT_SHA): a full lowercase sha.
COMMIT = re.compile(r"[0-9a-f]{40}")
# The lobby's simVersion shape (colton-games lib/multiplayer/games.ts SIM_VERSION, and the SQL).
SIM_VERSION = re.compile(r"[A-Za-z0-9._-]{1,64}")
# Vercel's Protection Bypass for Automation secret (Terraform generates 32 alphanumerics).
BYPASS = re.compile(r"[A-Za-z0-9_-]{16,128}")

START_FIELDS = {"action", "matchId", "game", "simVersion", "secret", "hardCapSec", "apiBase", "apiBypass",
                "commit"}
STOP_FIELDS = {"action", "matchId"}

# Match id -> (task ARN, environment, time.monotonic() at launch). See ECS reads above.
_recent = {}
# Revision ARN -> its engine image, or None when it is not a one-container engine revision.
_images = {}
_clients = {}


def _client(name):
    # Lazily created so the offline test can install fakes before the first call.
    if name not in _clients:
        import boto3

        _clients[name] = boto3.client(name)
    return _clients[name]


class Refused(Exception):
    """A request the function answers with {"ok": false}; never an AWS failure."""

    def __init__(self, error, field=None):
        super().__init__(error)
        self.error, self.field = error, field


def caller_environment():
    """This function's own environment, or None when it was deployed without a usable one."""
    if LAUNCH_ENV in ("production", "preview") and API_BASE:
        return LAUNCH_ENV
    return None


def _match_id(event):
    value = event.get("matchId")
    if not isinstance(value, str) or not MATCH_ID.fullmatch(value):
        raise Refused("bad-request", "matchId")
    return value


def validate_start(event, env):
    """The launch request, from exactly the fields this function understands."""
    if not isinstance(event, dict):
        raise Refused("bad-request")
    if set(event) - START_FIELDS:
        # overrides, tags, taskDefinition, env, cpu...: refused, not silently dropped, so a
        # caller never believes one took effect. (The key is not echoed back or logged.)
        raise Refused("bad-request", "unknown-field")
    match = _match_id(event)
    game = event.get("game")
    if not isinstance(game, str) or game not in GAMES:
        raise Refused("bad-request", "game")
    sim = event.get("simVersion")
    if not isinstance(sim, str) or not SIM_VERSION.fullmatch(sim):
        raise Refused("bad-request", "simVersion")
    secret = event.get("secret")
    if not isinstance(secret, str) or not SECRET.fullmatch(secret):
        raise Refused("bad-request", "secret")
    cap = event.get("hardCapSec")
    if isinstance(cap, bool) or not isinstance(cap, int) or not MIN_HARDCAP_SEC <= cap <= MAX_HARDCAP_SEC:
        raise Refused("bad-request", "hardCapSec")
    api = event.get("apiBase")
    if not isinstance(api, str) or not re.fullmatch(API_BASE, api):
        raise Refused("bad-request", "apiBase")
    bypass = event.get("apiBypass")
    if bypass is not None and (not ALLOW_BYPASS or not isinstance(bypass, str) or not BYPASS.fullmatch(bypass)):
        raise Refused("bad-request", "apiBypass")
    commit = event.get("commit")
    if commit is not None and (not isinstance(commit, str) or not COMMIT.fullmatch(commit)):
        raise Refused("bad-request", "commit")
    if env == "preview" and commit is None:
        # A preview runs exactly its own commit's engine; without it there is nothing to pick.
        raise Refused("bad-request", "commit")
    return {"matchId": match, "game": game, "simVersion": sim, "secret": secret, "hardCapSec": cap,
            "apiBase": api, "apiBypass": bypass, "commit": commit if env == "preview" else None}


def _engine_image(ecs, arn):
    """The revision's engine image, or None unless it is a one-container engine that verifies
    join tokens itself (TOKEN_VERIFIER in its environment)."""
    if arn not in _images:
        td = ecs.describe_task_definition(taskDefinition=arn).get("taskDefinition") or {}
        containers = td.get("containerDefinitions") or []
        ok = (td.get("taskDefinitionArn") == arn and len(containers) == 1
              and containers[0].get("name") == ENGINE_CONTAINER
              and TOKEN_VERIFIER in [(e.get("name"), e.get("value"))
                                     for e in containers[0].get("environment") or [] if isinstance(e, dict)])
        _images[arn] = containers[0].get("image") if ok else None
    return _images[arn]


def _release_item(key):
    """The releases table's item `key` as plain values, or None."""
    out = _client("dynamodb").get_item(TableName=RELEASES_TABLE, Key={"id": {"S": key}}, ConsistentRead=True)
    item = out.get("Item")
    return _plain(item) if item else None


def _plain(value):
    """DynamoDB's typed JSON ({"S": ...}, {"M": ...}) as plain Python values."""
    if isinstance(value, dict) and len(value) == 1:
        (kind, inner), = value.items()
        if kind == "S":
            return inner
        if kind == "N":
            return int(inner) if inner.lstrip("-").isdigit() else float(inner)
        if kind == "BOOL":
            return inner
        if kind == "NULL":
            return None
        if kind == "M":
            return {k: _plain(v) for k, v in inner.items()}
        if kind == "L":
            return [_plain(v) for v in inner]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value


def _released(item, game):
    """(taskDefinition, simVersion) of `game` in a release item, or (None, None)."""
    games = item.get("games") if isinstance(item, dict) else None
    entry = games.get(game) if isinstance(games, dict) else None
    if not isinstance(entry, dict):
        return None, None
    td, sim = entry.get("taskDefinition"), entry.get("simVersion")
    return (td, sim) if isinstance(td, str) and isinstance(sim, str) else (None, None)


def task_definition_for(ecs, env, req):
    """The revision to launch. See SIM VERSIONS AND COMMITS above."""
    game, sim = req["game"], req["simVersion"]
    if env == "preview":
        build = _release_item("build#preview#%s" % req["commit"])
        status = build.get("status") if build else None
        if status == "failed":
            raise Refused("engine-build-failed")
        if status != "released":
            raise Refused("engine-building")
        arn, released_sim = _released(build, game)
        suffix = req["commit"][:12]
    else:
        current = _release_item("current#main")
        arn, released_sim = _released(current or {}, game)
        if released_sim != sim:
            older = _release_item("sim#%s#%s" % (game, sim))
            arn = older.get("taskDefinition") if older else None
            released_sim = sim if isinstance(arn, str) else None
        suffix = None
    if arn is None or released_sim != sim or not _runs(ecs, game, arn, sim, suffix):
        raise Refused("unknown-sim-version")
    return arn


def _runs(ecs, game, arn, sim, suffix):
    """Whether `arn` is a revision of this function's family for `game` whose engine image is
    exactly <its repository>:<sim>-<12 hex> (the given 12 hex, if any) and verifies tokens."""
    family, repository = GAMES[game]["family"], GAMES[game]["repository"]
    # This account and region are the game's repository's: <account>.dkr.ecr.<region>.amazonaws.com/...
    where = re.match(r"([0-9]{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/", repository)
    revision = arn.rsplit(":", 1)[1] if ":" in arn else ""
    if not where or arn != "arn:aws:ecs:%s:%s:task-definition/%s:%s" % (where.group(2), where.group(1), family, revision) \
            or not (revision.isdigit() and revision.isascii()):
        return False
    image = _engine_image(ecs, arn)
    tail = re.escape(suffix) if suffix else "[0-9a-f]{12}"
    return isinstance(image, str) and re.fullmatch(
        re.escape(repository) + ":" + re.escape(sim) + "-" + tail, image) is not None


def engine_token_keys(env):
    """MP_TOKEN_PUBLIC_KEYS for an engine of `env`: that environment's Ed25519 public keys only."""
    value = TOKEN_PUBLIC_KEYS
    entries = value.split(",") if value else []
    if not entries:
        raise RuntimeError("no token public keys for %s" % env)
    for entry in entries:
        m = _TOKEN_PUBLIC_KEY.fullmatch(entry)
        der = base64.b64decode(m.group(3), validate=True) if m else b""
        if not m or m.group(1) != env or len(der) != 44 or not der.startswith(_ED25519_SPKI_PREFIX):
            # Never echoes the value: it is meant to be public, but a misplaced private key is not.
            raise RuntimeError("token_public_keys for %s: an entry is not one of its Ed25519 public keys" % env)
    return value


def run_task_input(env, req, task_definition):
    """The one RunTask this function ever sends."""
    environment = [
        {"name": "MATCH_ID", "value": req["matchId"]},
        {"name": "MATCH_SECRET", "value": req["secret"]},
        {"name": "MP_API", "value": req["apiBase"]},
        {"name": "GAME_ID", "value": req["game"]},
        {"name": "MP_ENV", "value": env},
        {"name": "MP_TOKEN_PUBLIC_KEYS", "value": engine_token_keys(env)},
        {"name": "PORT", "value": ENGINE_PORT},
    ]
    if req.get("apiBypass"):
        environment.append({"name": "MP_API_BYPASS", "value": req["apiBypass"]})
    return {
        "cluster": CLUSTER,
        "taskDefinition": task_definition,
        "launchType": "FARGATE",
        "count": 1,
        # A retried launch (same match) returns the same task instead of starting a second.
        # Scoped to the environment: a preview launch can never hold, or collide with, the
        # token of a production match (match ids are visible to both lobbies).
        "clientToken": "%s-%s" % (env, req["matchId"]),
        # stop finds the match's task with ListTasks startedBy.
        "startedBy": req["matchId"],
        "networkConfiguration": {"awsvpcConfiguration": {
            "subnets": SUBNETS,
            "securityGroups": [SECURITY_GROUP],
            # Outbound only (image pull, callbacks): the security group admits just the router.
            "assignPublicIp": "ENABLED",
        }},
        "overrides": {"containerOverrides": [{"name": ENGINE_CONTAINER, "environment": environment}]},
        "tags": [
            {"key": "game", "value": req["game"]},
            {"key": "match", "value": req["matchId"]},
            {"key": "env", "value": env},
            # The sweeper stops a task at hardcap + 10 min.
            {"key": "hardcap", "value": str(req["hardCapSec"])},
        ],
    }


def task_family(task):
    # arn:aws:ecs:<region>:<account>:task-definition/<family>:<revision>
    arn = str(task.get("taskDefinitionArn", ""))
    return arn.rsplit("/", 1)[-1].rsplit(":", 1)[0] if "/" in arn else ""


def _tag(task, key):
    for t in task.get("tags", []):
        if t.get("key") == key:
            return t.get("value")
    return None


def _alive_engine(task):
    return (task_family(task) != ROUTER_FAMILY
            and task.get("lastStatus") not in _STOPPING
            and task.get("desiredStatus") != "STOPPED")


def _listed(ecs, **filters):
    arns, token = [], None
    while True:
        kwargs = dict(cluster=CLUSTER, maxResults=100, **filters)
        if token:
            kwargs["nextToken"] = token
        page = ecs.list_tasks(**kwargs)
        arns.extend(page.get("taskArns", []))
        token = page.get("nextToken")
        if not token:
            return arns


def _describe(ecs, arns):
    """ARN -> task for every ARN ECS knows; ones it does not know yet are simply absent."""
    found = {}
    for i in range(0, len(arns), 100):
        out = ecs.describe_tasks(cluster=CLUSTER, tasks=arns[i:i + 100], include=["TAGS"])
        for task in out.get("tasks", []):
            found[task["taskArn"]] = task
    return found


def count_engines(ecs, env, now):
    """(engines running in total, engines running for env), counting just-launched ones.

    Only the second is admission; the total is reported for the log and the reply."""
    for match, (_, _, at) in list(_recent.items()):
        if now - at > SETTLE_SEC:
            del _recent[match]
    listed = _listed(ecs, desiredStatus="RUNNING")
    recent_arns = [arn for arn, _, _ in _recent.values() if arn not in listed]
    tasks = _describe(ecs, listed + recent_arns)
    alive = {arn: _tag(task, "env") for arn, task in tasks.items() if _alive_engine(task)}
    for arn, launched_env, _ in _recent.values():
        # Not visible to ECS yet: count it as running until ECS says otherwise.
        if arn not in tasks:
            alive[arn] = launched_env
    # An engine without a readable env tag counts against EVERY environment's ceiling: missing
    # tags (a permission slip, or a task started around this function) must not free up room.
    return len(alive), sum(1 for e in alive.values() if e == env or e is None)


def start(env, event):
    req = validate_start(event, env)
    now = time.monotonic()
    ecs = _client("ecs")
    known = _recent.get(req["matchId"])
    if known and known[1] == env:
        # A warm retry: trust the cached task only while it is alive, or too new for ECS to
        # show yet. One that failed or was stopped is forgotten, and the match launches again.
        task = _describe(ecs, [known[0]]).get(known[0])
        if (task is None and now - known[2] <= SETTLE_SEC) or (task is not None and _alive_engine(task)):
            return {"ok": True, "taskArn": known[0], "repeat": True}
        del _recent[req["matchId"]]
    # A retry after a lost response, possibly in a fresh execution environment (_recent empty):
    # the match's engine may already run. Find it by startedBy before admission, or the retry
    # would count its own engine against the ceiling (refused as capacity), or collide with
    # RunTask's clientToken if the launch parameters changed since.
    existing = _existing_engine(ecs, env, req["matchId"])
    if existing:
        _recent[req["matchId"]] = (existing, env, now)
        return {"ok": True, "taskArn": existing, "repeat": True}
    task_definition = task_definition_for(ecs, env, req)
    total, in_env = count_engines(ecs, env, now)
    # This environment's own ceiling only: the other environment's engines never take its room,
    # and Terraform's split keeps the sum at the total ceiling (see ADMISSION above).
    if in_env >= ENV_CEILING:
        raise Refused("capacity")
    out = _run_task(ecs, run_task_input(env, req, task_definition), env, req["matchId"])
    tasks = out.get("tasks") or []
    if not tasks or not tasks[0].get("taskArn"):
        reasons = ", ".join(str(f.get("reason")) for f in out.get("failures") or []) or "no reason given"
        raise RuntimeError("RunTask started no task: %s" % reasons)
    arn = tasks[0]["taskArn"]
    _recent[req["matchId"]] = (arn, env, now)
    return {"ok": True, "taskArn": arn, "taskDefinition": task_definition, "engines": total + 1}


# How many engines of one match may die and be replaced within one launch call.
MAX_GENERATIONS = 5


def _run_task(ecs, params, env, match):
    """RunTask idempotently across retries, through every replacement of a dead engine.

    The first launch uses clientToken <env>-<match id>. ECS answers a reused token with that
    token's ORIGINAL task, or, if the parameters changed, with ConflictException naming it. A
    live original (or one ECS cannot show yet: a cold start can see neither the listing nor the
    task) IS the match's engine. A confirmed-terminal one is replaced under a token DERIVED from
    it, <env>-<match id>-<12 hex of its ARN>: a retry after the replacement's own response was
    lost derives the same token and so resolves to the same replacement, never a second one."""
    token = "%s-%s" % (env, match)
    for _ in range(MAX_GENERATIONS):
        state, found = _launch_or_resolve(ecs, dict(params, clientToken=token), env, match)
        if state == "live":
            return found
        token = "%s-%s-%s" % (env, match, hashlib.sha256(found.encode()).hexdigest()[:12])
    raise RuntimeError("match %s/%s: %d engines in a row were already dead" % (env, match, MAX_GENERATIONS))


def _launch_or_resolve(ecs, params, env, match):
    """("live", RunTask-shaped reply) for the token's live task, or ("dead", its terminal ARN)."""
    try:
        out = ecs.run_task(**params)
        task = (out.get("tasks") or [{}])[0]
        if not _terminal(task):
            return "live", out
        return "dead", task["taskArn"]
    except Exception as error:  # botocore ClientError; the code is what matters
        response = getattr(error, "response", None) or {}
        if response.get("Error", {}).get("Code") != "ConflictException":
            raise
        originals = [a for a in response.get("resourceIds") or [] if isinstance(a, str)]
        if not originals:
            raise  # cannot prove the original engine is gone: do not start another
        seen = _describe(ecs, originals)
        for arn in originals:
            if arn not in seen:
                return "live", {"tasks": [{"taskArn": arn}], "failures": []}   # ours, not listed yet
            task = seen[arn]
            if _terminal(task):
                continue
            if _tag(task, "env") != env or _tag(task, "match") != match:
                # Never adopt another environment's or match's engine as this one's.
                raise RuntimeError("clientToken for %s/%s is tied to a foreign task %s" % (env, match, arn))
            return "live", {"tasks": [{"taskArn": arn}], "failures": []}
        return "dead", sorted(originals)[-1]


def _terminal(task):
    return task.get("desiredStatus") == "STOPPED" or task.get("lastStatus") in _STOPPING


def _existing_engine(ecs, env, match):
    """The ARN of this environment's live engine for `match`, if one runs."""
    for arn, task in _describe(ecs, _listed(ecs, startedBy=match)).items():
        if _alive_engine(task) and _tag(task, "env") == env and _tag(task, "match") == match:
            return arn
    return None


def stop(env, event):
    """Stops this environment's engine for one match; never another environment's, never the router."""
    if not isinstance(event, dict) or set(event) - STOP_FIELDS:
        raise Refused("bad-request")
    match = _match_id(event)
    ecs = _client("ecs")
    arns = _listed(ecs, startedBy=match)
    known = _recent.get(match)
    # Only a launch recent enough that ECS may not show it yet; an older one ECS no longer
    # shows is long gone, and is just forgotten below.
    known = known if known and known[1] == env and time.monotonic() - known[2] <= SETTLE_SEC else None
    if known and known[0] not in arns:
        arns.append(known[0])
    seen = _describe(ecs, arns)
    stopped = 0
    for arn, task in seen.items():
        if not _alive_engine(task) or _tag(task, "env") != env or _tag(task, "match") != match:
            continue
        ecs.stop_task(cluster=CLUSTER, task=arn, reason=_stop_reason(match))
        stopped += 1
    if known and known[0] not in seen:
        # The engine this environment launched for the match a moment ago, which ECS lists and
        # describes nowhere yet (see STOP AFTER A FRESH LAUNCH). It is ours: _recent holds only
        # tasks this function started or found for this environment and match.
        if not _stop_unseen(ecs, known[0], match):
            return {"ok": False, "error": "not-yet-visible"}
        stopped += 1
    if stopped:
        _recent.pop(match, None)
    return {"ok": True, "stopped": stopped}


def _stop_reason(match):
    return "games-mp-launch: match %s over" % match


def _stop_unseen(ecs, arn, match):
    """StopTask on a task ECS does not show yet; False while ECS still cannot find it."""
    for attempt in range(STOP_ATTEMPTS):
        if attempt:
            time.sleep(STOP_RETRY_SEC * attempt)
        try:
            ecs.stop_task(cluster=CLUSTER, task=arn, reason=_stop_reason(match))
            return True
        except Exception as error:  # botocore ClientError; the code is what matters
            code = (getattr(error, "response", None) or {}).get("Error", {}).get("Code")
            if code not in _NOT_YET_VISIBLE:
                raise
    return False


def lambda_handler(event, context):
    env = caller_environment()
    action = event.get("action") if isinstance(event, dict) else None
    match = event.get("matchId") if isinstance(event, dict) else None
    match = match if isinstance(match, str) and MATCH_ID.fullmatch(match) else None
    try:
        if env is None:
            raise Refused("forbidden")
        if action == "start":
            reply = start(env, event)
        elif action == "stop":
            reply = stop(env, event)
        else:
            raise Refused("bad-request", "action")
    except Refused as r:
        reply = {"ok": False, "error": r.error}
        if r.field:
            reply["field"] = r.field
    # One line per call, never the secret or the bypass: env, action, match, outcome.
    print(json.dumps({"env": env, "action": action if action in ("start", "stop") else None,
                      "match": match, "reply": reply}))
    return reply

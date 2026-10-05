"""games-mp-release: turns a finished multiplayer image build into a release.

Invoked by EventBridge for every CodeBuild state change of the three games projects
(games-multiplayer-deploy.tf), and by an administrator to roll production back:

  aws lambda invoke --function-name games-mp-release \\
    --cli-binary-format raw-in-base64-out --payload '{"action":"promote","commit":"<40 hex>"}' out.json

WHICH CHANNEL is decided by the project that built it, never by anything the build wrote:
  games-mp-images          main    router + engines in games/*; runs main's code, which is
                                   production's code by definition (main deploys production)
  games-mp-images-preview  preview engines in games-preview/*; runs any open pull request's code,
                                   so everything it reports is checked, and none of it can
                                   reach production: its role cannot push to games/*, and its
                                   revisions are registered only in the games-preview-<game>
                                   families, which the production launch function cannot run
  games-mp-migrate         main    the Supabase migrations of a main commit (the mp ones, then
                                   every other file: the site's), then promote it

A MAIN BUILD (SUCCEEDED):
  1. registers one engine task definition revision in `games-<game>` for EACH GAME THE COMMIT
     DEFINES (its scripts/mp-images.mjs; no list of games lives in Terraform), running
     `games/engines:<game>_<simVersion>-<sha12>`, the image the build pushed. Everything but the
     image and the size is fixed by ENGINE_TEMPLATE (roles, log group, network mode, arm64, port,
     MP_TOKEN_VERIFIER); the size is the commit's own (mp-engine.json), checked against Fargate's
     sizes and Terraform's maximums (MAX_CPU, MAX_MEMORY), and the game id against the rules
     that keep its family off the router's and off the preview families;
  2. if the commit's mp schema revision or its site migrations (the other files under
     supabase/migrations, reported as one value) are not the database's (`schema#main`),
     starts games-mp-migrate on the same source and stops here; that build's success, with the
     database at exactly this commit's migrations of both kinds, continues at 3;
  3. PROMOTES: `current#main` becomes this commit's revisions, unless a newer main commit (a
     higher poller sequence number) is already current, and `sim#<game>#<simVersion>` points
     at them (production launches older simVersions from these);
  4. if the router's inputs (the files its Dockerfile copies) changed since the router was last
     rolled, retags `games/mp-router:live` to this commit's router image and forces a new
     deployment of the mp-router service, then waits until the new tasks are healthy. The old
     tasks drain at the ALB for up to an hour (deregistration delay 3600 s, and no match lasts
     longer), so live matches keep their connections. A failed rollout puts `live` back and
     alerts.
Running engines never change: a release only decides what the NEXT launch runs, and no
revision is ever deregistered.

A PREVIEW BUILD (SUCCEEDED) registers revisions in `games-preview-<game>` and marks
`build#preview#<commit>` released with them; games-mp-launch-preview launches exactly those
for a lobby built from that commit.

A FAILED build (any project) marks its record failed. Main and migration failures, and any
error here, are published to SNS (cost-alerts); preview failures are not (any pushed branch can
fail), the preview lobby answers `engine-build-failed` instead.

No dependency beyond boto3. Offline test: scripts/test-games-mp-deploy.py.
"""

import json
import os
import re
import time

TABLE = os.environ.get("RELEASES_TABLE", "")
PROJECTS = {
    os.environ.get("MAIN_PROJECT", "games-mp-images"): "main",
    os.environ.get("PREVIEW_PROJECT", "games-mp-images-preview"): "preview",
}
MIGRATE_PROJECT = os.environ.get("MIGRATE_PROJECT", "games-mp-migrate")
BUCKET = os.environ.get("BUCKET", "")
# The one shape of every engine revision: {"executionRoleArn", "taskRoleArn", "logGroup",
# "region", "maxCpu", "maxMemory", "main"/"preview": {"familyPrefix", "repository",
# "repositoryUrl"}, "legacy": {game: {"main"/"preview": {"repository", "repositoryUrl"}}}}.
# `legacy` is the per-game repositories from before games were dynamic (mptest): a build made
# by the previous driver reports those, and is released from them.
ENGINE_TEMPLATE = json.loads(os.environ.get("ENGINE_TEMPLATE", "{}"))
# Shared with bringup.py and launch.py (scripts/test-games-mp-deploy.py checks they agree).
GAME_ID = re.compile(r"[a-z][a-z0-9-]{0,30}[a-z0-9]")
RESERVED_GAME_IDS = {"mp-router", "engines"}
DEFAULT_ENGINE_SIZE = (2048, 4096)
FARGATE_MEMORY = {
    256: (512, 1024, 2048),
    512: tuple(range(1024, 4097, 1024)),
    1024: tuple(range(2048, 8193, 1024)),
    2048: tuple(range(4096, 16385, 1024)),
    4096: tuple(range(8192, 30721, 1024)),
    8192: tuple(range(16384, 61441, 4096)),
    16384: tuple(range(32768, 122881, 8192)),
}


def valid_game_id(game):
    return (isinstance(game, str) and GAME_ID.fullmatch(game) is not None
            and not game.startswith("preview-") and game not in RESERVED_GAME_IDS)


def valid_engine_size(cpu, memory):
    return (type(cpu) is int and type(memory) is int and cpu <= ENGINE_TEMPLATE["maxCpu"]
            and memory <= ENGINE_TEMPLATE["maxMemory"] and memory in FARGATE_MEMORY.get(cpu, ()))
ROUTER_REPOSITORY = os.environ.get("ROUTER_REPOSITORY", "games/mp-router")
ROUTER_LIVE_TAG = os.environ.get("ROUTER_LIVE_TAG", "live")
CLUSTER = os.environ.get("CLUSTER", "games")
ROUTER_SERVICE = os.environ.get("ROUTER_SERVICE", "mp-router")
TARGET_GROUP_ARN = os.environ.get("TARGET_GROUP_ARN", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
ROLL_TIMEOUT_SEC = int(os.environ.get("ROLL_TIMEOUT_SEC", "600"))
POLL_SEC = float(os.environ.get("POLL_SEC", "15"))
ENGINE_PORT = 8080
TOKEN_VERIFIER = {"name": "MP_TOKEN_VERIFIER", "value": "ed25519-v2"}

COMMIT = re.compile(r"[0-9a-f]{40}")
SIM_VERSION = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,63}")
HASH = re.compile(r"[0-9a-f]{64}")
# A set of site migrations as bringup.py site_state reports it, and one of their file names.
SITE_STATE = re.compile(r"[0-9]{1,6}:[0-9a-f]{64}")
SITE_FILE = re.compile(r"[0-9]{14}_[a-z0-9_]+\.sql")

# Seams the offline test replaces.
SLEEP = time.sleep
CLOCK = time.monotonic
_clients = {}


def _client(name):
    if name not in _clients:
        import boto3

        _clients[name] = boto3.client(name)
    return _clients[name]


class ReleaseError(Exception):
    """A release that must not go ahead; published to SNS and raised (a Lambda error)."""


def log(**fields):
    print(json.dumps(fields, sort_keys=True, default=str))


def alert(subject, message):
    if SNS_TOPIC_ARN:
        _client("sns").publish(TopicArn=SNS_TOPIC_ARN, Subject=subject[:100], Message=message)


# --------------------------------------------------------------------------------------
# The releases table (plain values in, DynamoDB's typed JSON out)
# --------------------------------------------------------------------------------------


def _typed(value):
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, int):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, dict):
        return {"M": {k: _typed(v) for k, v in value.items()}}
    if isinstance(value, list):
        return {"L": [_typed(v) for v in value]}
    if value is None:
        return {"NULL": True}
    raise TypeError(type(value))


def _plain(value):
    (kind, inner), = value.items()
    if kind == "S":
        return inner
    if kind == "N":
        return int(inner)
    if kind == "BOOL":
        return inner
    if kind == "NULL":
        return None
    if kind == "M":
        return {k: _plain(v) for k, v in inner.items()}
    if kind == "L":
        return [_plain(v) for v in inner]
    raise TypeError(kind)


def get(key):
    item = _client("dynamodb").get_item(TableName=TABLE, Key={"id": {"S": key}}, ConsistentRead=True).get("Item")
    return {k: _plain(v) for k, v in item.items()} if item else None


def update(key, fields, condition=None, values=None):
    """SET each field; returns the item's OLD values, or None when `condition` failed."""
    names = {"#f%d" % i: k for i, k in enumerate(fields)}
    vals = {":v%d" % i: _typed(v) for i, v in enumerate(fields.values())}
    vals.update({k: _typed(v) for k, v in (values or {}).items()})
    kwargs = dict(TableName=TABLE, Key={"id": {"S": key}},
                  UpdateExpression="SET " + ", ".join("#f%d = :v%d" % (i, i) for i in range(len(fields))),
                  ExpressionAttributeNames=names, ExpressionAttributeValues=vals, ReturnValues="ALL_OLD")
    if condition:
        kwargs["ConditionExpression"] = condition
    try:
        out = _client("dynamodb").update_item(**kwargs)
    except Exception as error:
        if (getattr(error, "response", None) or {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return {k: _plain(v) for k, v in (out.get("Attributes") or {}).items()}


# --------------------------------------------------------------------------------------
# Builds
# --------------------------------------------------------------------------------------


def build_info(build_id):
    """(project, commit, exported variables) of a build, read from CodeBuild itself."""
    builds = _client("codebuild").batch_get_builds(ids=[build_id]).get("builds") or []
    if not builds:
        raise ReleaseError("CodeBuild has no build %s" % build_id)
    b = builds[0]
    env = {v["name"]: v.get("value") for v in (b.get("environment") or {}).get("environmentVariables") or []}
    commit = env.get("GAMES_MP_COMMIT")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise ReleaseError("build %s has no GAMES_MP_COMMIT" % build_id)
    exported = {v["name"]: v.get("value") for v in b.get("exportedEnvironmentVariables") or []}
    return b.get("projectName"), commit, exported


def built_images(channel, commit, exported):
    """{game: {"tag", "simVersion", "digest", "repositoryUrl", "cpu", "memory"}} for every engine
    the build reports, each checked here and against ECR.

    A preview build runs untrusted code, so its report is only a hint: every game id must be
    valid, every size a Fargate size within the maximums, and every image must exist in the
    channel's engine repository as `<game>_<simVersion>-<its commit's 12 hex>`."""
    where = ENGINE_TEMPLATE[channel]
    raw = exported.get("GAMES_MP_ENGINES")
    # A previous-driver build under this buildspec sets only GAMES_MP_IMAGES, and CodeBuild may
    # report the unset GAMES_MP_ENGINES as empty.
    if not raw:
        return legacy_built_images(channel, commit, exported)
    try:
        engines = json.loads(raw)
    except ValueError:
        raise ReleaseError("GAMES_MP_ENGINES is not JSON") from None
    if not isinstance(engines, dict) or not engines:
        raise ReleaseError("the build reports no engine")
    out = {}
    for game, spec in sorted(engines.items()):
        if not valid_game_id(game):
            raise ReleaseError("%s: %r is not a valid game id" % (channel, game))
        if not (isinstance(spec, list) and len(spec) == 3 and isinstance(spec[0], str)
                and SIM_VERSION.fullmatch(spec[0])):
            raise ReleaseError("%s: bad engine report for %s: %r" % (channel, game, spec))
        sim, cpu, memory = spec
        if not valid_engine_size(cpu, memory):
            raise ReleaseError("%s: %s asks for cpu %r / memory %r, not a Fargate size within %s / %s"
                               % (channel, game, cpu, memory, ENGINE_TEMPLATE["maxCpu"], ENGINE_TEMPLATE["maxMemory"]))
        tag = "%s_%s-%s" % (game, sim, commit[:12])
        out[game] = {"tag": tag, "simVersion": sim, "digest": image_digest(where["repository"], tag),
                     "repositoryUrl": where["repositoryUrl"], "cpu": cpu, "memory": memory}
    return out


def legacy_built_images(channel, commit, exported):
    """A build by the driver from before games were dynamic (in flight while Terraform switched
    over): GAMES_MP_IMAGES {"<prefix><game>-engine": "<simVersion>-<sha12>"}, released only for
    the games that had their own repositories, at the old default size."""
    try:
        images = json.loads(exported.get("GAMES_MP_IMAGES") or "")
    except ValueError:
        raise ReleaseError("the build exported neither GAMES_MP_ENGINES nor GAMES_MP_IMAGES") from None
    if not isinstance(images, dict):
        raise ReleaseError("GAMES_MP_IMAGES is not an object")
    out = {}
    for game, repos in sorted((ENGINE_TEMPLATE.get("legacy") or {}).items()):
        where = repos[channel]
        tag = images.get(where["repository"])
        if tag is None:
            continue
        m = re.fullmatch(r"(.+)-([0-9a-f]{12})", tag) if isinstance(tag, str) else None
        if not m or m.group(2) != commit[:12] or not SIM_VERSION.fullmatch(m.group(1)):
            raise ReleaseError("%s: no image for %s at %s (got %r)" % (channel, game, commit[:12], tag))
        out[game] = {"tag": tag, "simVersion": m.group(1), "digest": image_digest(where["repository"], tag),
                     "repositoryUrl": where["repositoryUrl"], "cpu": DEFAULT_ENGINE_SIZE[0],
                     "memory": DEFAULT_ENGINE_SIZE[1]}
    if not out:
        raise ReleaseError("the build reports no engine")
    return out


def image_digest(repo, tag):
    try:
        found = _client("ecr").describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])
    except Exception as error:
        if (getattr(error, "response", None) or {}).get("Error", {}).get("Code") == "ImageNotFoundException":
            raise ReleaseError("%s:%s is not in ECR" % (repo, tag)) from None
        raise
    return found["imageDetails"][0]["imageDigest"]


def register(channel, commit, game, image):
    """One engine revision for `game` running `<image repositoryUrl>:<image tag>` at the image's
    size; returns its ARN. Only the image and the size come from the build (checked by
    built_images); everything else is ENGINE_TEMPLATE's."""
    t = ENGINE_TEMPLATE
    if not valid_game_id(game) or not valid_engine_size(image["cpu"], image["memory"]):
        raise ReleaseError("refusing to register %r at %r / %r" % (game, image.get("cpu"), image.get("memory")))
    tag = image["tag"]
    out = _client("ecs").register_task_definition(
        family=t[channel]["familyPrefix"] + game,
        requiresCompatibilities=["FARGATE"],
        networkMode="awsvpc",
        cpu=str(image["cpu"]),
        memory=str(image["memory"]),
        executionRoleArn=t["executionRoleArn"],
        taskRoleArn=t["taskRoleArn"],
        runtimePlatform={"operatingSystemFamily": "LINUX", "cpuArchitecture": "ARM64"},
        containerDefinitions=[{
            "name": "engine",
            "image": "%s:%s" % (image["repositoryUrl"], tag),
            "essential": True,
            "portMappings": [{"containerPort": ENGINE_PORT, "protocol": "tcp"}],
            "environment": [
                {"name": "GAME_ID", "value": game},
                {"name": "PORT", "value": str(ENGINE_PORT)},
                {"name": "MP_ENV", "value": "production" if channel == "main" else "preview"},
                # games-mp-launch launches no revision without it (launch.py TOKEN VERIFIER).
                TOKEN_VERIFIER,
            ],
            "logConfiguration": {"logDriver": "awslogs", "options": {
                "awslogs-group": t["logGroup"], "awslogs-region": t["region"],
                "awslogs-stream-prefix": game if channel == "main" else "preview-" + game}},
            # Room to post a result on SIGTERM (StopTask, the sweeper) before SIGKILL.
            "stopTimeout": 30,
        }],
        tags=[{"key": "Project", "value": "games-multiplayer"}, {"key": "channel", "value": channel},
              {"key": "commit", "value": commit}, {"key": "ImageTag", "value": tag}],
    )
    return out["taskDefinition"]["taskDefinitionArn"]


def register_all(channel, commit, record, images):
    """The build record's revisions, registering those it does not have yet (idempotent)."""
    games = dict(record.get("games") or {})
    for game, image in images.items():
        have = games.get(game) or {}
        if have.get("tag") == image["tag"] and have.get("taskDefinition"):
            continue
        games[game] = {"taskDefinition": register(channel, commit, game, image), "simVersion": image["simVersion"],
                       "tag": image["tag"], "digest": image["digest"], "cpu": image["cpu"], "memory": image["memory"]}
    return games


# --------------------------------------------------------------------------------------
# Events
# --------------------------------------------------------------------------------------


def on_images_built(channel, commit, exported):
    key = "build#%s#%s" % (channel, commit)
    record = get(key)
    if record is None:
        raise ReleaseError("%s has no record; the poller did not start this build" % key)
    games = register_all(channel, commit, record, built_images(channel, commit, exported))
    if channel == "preview":
        update(key, {"status": "released", "games": games})
        log(event="preview-released", commit=commit, games=games)
        return {"released": "preview", "commit": commit}
    router_inputs = exported.get("GAMES_MP_ROUTER_INPUTS") or ""
    schema = exported.get("GAMES_MP_SCHEMA_REVISION") or ""
    if not HASH.fullmatch(router_inputs) or not schema.isdigit():
        raise ReleaseError("main build of %s exported no router inputs or schema revision" % commit)
    # The commit's site migrations as one value (bringup.py site_state). A source archive
    # packaged before the driver reported them carries none: it is released as before.
    site = exported.get("GAMES_SITE_MIGRATIONS") or ""
    if site and not SITE_STATE.fullmatch(site):
        raise ReleaseError("main build of %s exported malformed site migrations" % commit)
    if not site:
        log(event="site-migrations-not-reported", commit=commit)
    router_tag = commit[:12]
    router_digest = image_digest(ROUTER_REPOSITORY, router_tag)
    built = {"status": "built", "games": games, "routerTag": router_tag, "routerDigest": router_digest,
             "routerInputs": router_inputs, "schemaRevision": int(schema)}
    if site:
        built["siteMigrations"] = site
    update(key, built)
    db = get("schema#main")
    if db is None or db.get("revision") != int(schema) or (site and db.get("site") != site):
        start_migration(commit)
        update(key, {"status": "migrating"})
        return {"migrating": commit}
    return promote(commit)


def start_migration(commit):
    build = _client("codebuild").start_build(
        projectName=MIGRATE_PROJECT,
        sourceLocationOverride="%s/sources/main/%s.zip" % (BUCKET, commit),
        environmentVariablesOverride=[{"name": "GAMES_MP_COMMIT", "value": commit, "type": "PLAINTEXT"}],
    )["build"]["id"]
    log(event="migration-started", commit=commit, build=build)


def on_migrated(commit, exported):
    revision = exported.get("GAMES_MP_DB_REVISION") or ""
    if not revision.isdigit():
        raise ReleaseError("games-mp-migrate for %s exported no GAMES_MP_DB_REVISION" % commit)
    site = exported.get("GAMES_SITE_DB_MIGRATIONS") or ""
    newest = exported.get("GAMES_SITE_DB_NEWEST") or ""
    if (site and not SITE_STATE.fullmatch(site)) or (newest and not SITE_FILE.fullmatch(newest)):
        raise ReleaseError("games-mp-migrate for %s exported malformed site migrations" % commit)
    database = {"revision": int(revision), "commit": commit}
    if site:
        # siteNewest is for whoever looks: the newest file the database has applied.
        database.update(site=site, siteNewest=newest or "none")
    update("schema#main", database)
    record = get("build#main#%s" % commit) or {}
    if record.get("schemaRevision") != int(revision):
        raise ReleaseError("database is at mp revision %s after migrating %s, which needs %s"
                           % (revision, commit, record.get("schemaRevision")))
    if record.get("siteMigrations") and record["siteMigrations"] != site:
        raise ReleaseError("database's site migrations are %s after migrating %s, which has %s"
                           % (site or "not reported", commit, record["siteMigrations"]))
    return promote(commit)


def promote(commit, manual=False):
    """Makes a built main commit production's current release, rolling the router first if its
    inputs changed. A newer current release (higher sequence number) wins, except for an
    administrator's explicit promote (a rollback), which takes a fresh sequence number.

    The router goes first: if its rollout fails, nothing is promoted, so new matches never run
    this commit's engines behind the previous router. (One function runs at a time, reserved
    concurrency 1, so nothing moves current#main between the check and the write; the write is
    conditional anyway.)"""
    record = get("build#main#%s" % commit)
    if not record or not record.get("games") or record.get("status") not in ("built", "migrating", "released", "superseded"):
        raise ReleaseError("main commit %s has no built release to promote" % commit)
    seq = record["seq"]
    if manual:
        seq = int(_client("dynamodb").update_item(
            TableName=TABLE, Key={"id": {"S": "seq#main"}}, UpdateExpression="ADD seq :one",
            ExpressionAttributeValues={":one": {"N": "1"}}, ReturnValues="UPDATED_NEW")["Attributes"]["seq"]["N"])
    existing = get("current#main") or {}
    if isinstance(existing.get("seq"), int) and existing["seq"] >= seq:
        return _superseded(commit, seq)
    rolled = roll_router_if_changed(record)
    current = {"commit": commit, "seq": seq, "games": {g: {"taskDefinition": v["taskDefinition"],
                                                             "simVersion": v["simVersion"]}
                                                         for g, v in record["games"].items()}}
    old = update("current#main", current, condition="attribute_not_exists(seq) OR seq < :seq",
                 values={":seq": seq})
    if old is None:
        return _superseded(commit, seq)
    for game, v in current["games"].items():
        update("sim#%s#%s" % (game, v["simVersion"]),
               {"taskDefinition": v["taskDefinition"], "commit": commit, "seq": seq},
               condition="attribute_not_exists(seq) OR seq < :seq", values={":seq": seq})
    log(event="promoted", commit=commit, seq=seq, previous=old.get("commit"), games=current["games"])
    update("build#main#%s" % commit, {"status": "released"})
    return {"promoted": commit, "router": rolled}


def _superseded(commit, seq):
    log(event="superseded", commit=commit, seq=seq)
    update("build#main#%s" % commit, {"status": "superseded"})
    return {"superseded": commit}


# --------------------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------------------


def service_exists():
    svc = _client("ecs").describe_services(cluster=CLUSTER, services=[ROUTER_SERVICE]).get("services") or []
    return bool(svc) and svc[0].get("status") == "ACTIVE"


def roll_router_if_changed(record):
    """Rolls the router to this commit's image when its inputs differ from the last rolled
    one's. `live` and the running tasks are each checked on their own, so a retry after a
    failure half-way (live moved, the rollout never ran) still deploys; a failed rollout puts
    `live` back on the last router that rolled successfully."""
    last = get("router#main") or {}
    if last.get("inputs") == record["routerInputs"]:
        log(event="router-unchanged", inputs=record["routerInputs"])
        return False
    ecr = _client("ecr")
    live = ecr.batch_get_image(repositoryName=ROUTER_REPOSITORY, imageIds=[{"imageTag": ROUTER_LIVE_TAG}],
                               acceptedMediaTypes=_MEDIA_TYPES).get("images") or []
    previous = live[0] if live else None
    if previous is None or previous["imageId"]["imageDigest"] != record["routerDigest"]:
        _retag(record["routerTag"])
    if not service_exists():
        # A platform built from nothing: the apply creates the service on `live` next.
        log(event="router-live-before-service", tag=record["routerTag"])
    elif deployed_digest() != record["routerDigest"]:
        try:
            roll()
        except ReleaseError:
            good = last.get("tag")
            if good:
                _retag(good)
            elif previous is not None and previous["imageId"]["imageDigest"] != record["routerDigest"]:
                ecr.put_image(repositoryName=ROUTER_REPOSITORY, imageTag=ROUTER_LIVE_TAG,
                              imageManifest=previous["imageManifest"],
                              imageManifestMediaType=previous.get("imageManifestMediaType"))
            raise
    update("router#main", {"inputs": record["routerInputs"], "tag": record["routerTag"],
                           "digest": record["routerDigest"]})
    log(event="router-live", tag=record["routerTag"])
    return True


_MEDIA_TYPES = ["application/vnd.docker.distribution.manifest.v2+json",
                "application/vnd.docker.distribution.manifest.list.v2+json",
                "application/vnd.oci.image.manifest.v1+json",
                "application/vnd.oci.image.index.v1+json"]


def _retag(tag):
    ecr = _client("ecr")
    img = ecr.batch_get_image(repositoryName=ROUTER_REPOSITORY, imageIds=[{"imageTag": tag}],
                              acceptedMediaTypes=_MEDIA_TYPES)["images"][0]
    try:
        ecr.put_image(repositoryName=ROUTER_REPOSITORY, imageTag=ROUTER_LIVE_TAG,
                      imageManifest=img["imageManifest"],
                      imageManifestMediaType=img.get("imageManifestMediaType"))
    except Exception as error:
        # Already pointing there (a retried event).
        if (getattr(error, "response", None) or {}).get("Error", {}).get("Code") != "ImageAlreadyExistsException":
            raise


def deployed_digest():
    """The router image digest every running mp-router task runs, or None if they differ."""
    ecs = _client("ecs")
    arns = ecs.list_tasks(cluster=CLUSTER, serviceName=ROUTER_SERVICE, desiredStatus="RUNNING").get("taskArns") or []
    if not arns:
        return None
    digests = set()
    for task in ecs.describe_tasks(cluster=CLUSTER, tasks=arns).get("tasks") or []:
        for c in task.get("containers") or []:
            digests.add(c.get("imageDigest"))
    return digests.pop() if len(digests) == 1 else None


def roll():
    """Forces a new mp-router deployment (it resolves `live` again) and waits until its tasks
    run and are healthy in the target group. The old tasks then drain for up to an hour."""
    ecs = _client("ecs")
    service = ecs.update_service(cluster=CLUSTER, service=ROUTER_SERVICE, forceNewDeployment=True)["service"]
    ours = next(d["id"] for d in service["deployments"] if d["status"] == "PRIMARY")
    log(event="router-rolling", deployment=ours)
    deadline = CLOCK() + ROLL_TIMEOUT_SEC
    while True:
        state = rollout_state(ours)
        if state == "done":
            log(event="router-rolled", deployment=ours)
            return
        if state != "wait":
            raise ReleaseError("mp-router rollout %s: %s" % (ours, state))
        if CLOCK() > deadline:
            raise ReleaseError("mp-router rollout %s not healthy after %ds" % (ours, ROLL_TIMEOUT_SEC))
        SLEEP(POLL_SEC)


def rollout_state(deployment_id):
    """'done' once the deployment runs its desired count and every one of its tasks is a healthy
    target; 'wait'; or why it failed."""
    ecs = _client("ecs")
    svc = ecs.describe_services(cluster=CLUSTER, services=[ROUTER_SERVICE])["services"][0]
    dep = next((d for d in svc.get("deployments") or [] if d["id"] == deployment_id), None)
    if dep is None:
        return "deployment replaced (rolled back or superseded)"
    if dep.get("rolloutState") == "FAILED":
        return "FAILED: %s" % (dep.get("rolloutStateReason") or "see the service events")
    if dep["status"] != "PRIMARY":
        return "no longer PRIMARY (rolled back)"
    want = dep.get("desiredCount", 0)
    if want < 1 or dep.get("runningCount", 0) < want:
        return "wait"
    arns = ecs.list_tasks(cluster=CLUSTER, startedBy=deployment_id, desiredStatus="RUNNING").get("taskArns") or []
    tasks = ecs.describe_tasks(cluster=CLUSTER, tasks=arns).get("tasks") if arns else []
    ips = set()
    for task in tasks or []:
        for att in task.get("attachments") or []:
            for d in att.get("details") or []:
                if d.get("name") == "privateIPv4Address":
                    ips.add(d["value"])
    if len(ips) < want:
        return "wait"
    health = _client("elbv2").describe_target_health(TargetGroupArn=TARGET_GROUP_ARN)
    healthy = {t["Target"]["Id"] for t in health.get("TargetHealthDescriptions") or []
               if t["TargetHealth"]["State"] == "healthy"}
    return "done" if ips <= healthy else "wait"


# --------------------------------------------------------------------------------------
# Handler
# --------------------------------------------------------------------------------------


def on_build_event(detail):
    build_id = detail.get("build-id") or ""
    status = detail.get("build-status")
    project, commit, exported = build_info(build_id)
    if project == MIGRATE_PROJECT:
        channel = "migrate"
    else:
        channel = PROJECTS.get(project)
    if channel is None:
        raise ReleaseError("event for an unknown project %r" % project)
    if status != "SUCCEEDED":
        key = "build#%s#%s" % ("main" if channel == "migrate" else channel, commit)
        update(key, {"status": "failed", "failedBuild": build_id}, condition="attribute_exists(id)")
        log(event="build-failed", channel=channel, commit=commit, build=build_id, status=status)
        if channel != "preview":
            alert("games-mp: %s build %s for %s" % (channel, status, commit[:12]),
                  "CodeBuild %s ended %s for CoderColton/colton-games %s. Production keeps its current "
                  "release. Logs: /aws/codebuild/%s" % (build_id, status, commit, project))
        return {"failed": commit}
    if channel == "migrate":
        return on_migrated(commit, exported)
    return on_images_built(channel, commit, exported)


def lambda_handler(event, context):
    try:
        if isinstance(event, dict) and event.get("source") == "aws.codebuild":
            return on_build_event(event.get("detail") or {})
        if isinstance(event, dict) and event.get("action") == "promote":
            commit = event.get("commit")
            if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
                raise ReleaseError("promote needs a full 40-hex commit")
            return promote(commit, manual=True)
        raise ReleaseError("unrecognised event")
    except Exception as error:
        log(event="release-error", error=str(error)[:500])
        alert("games-mp: release failed", "games-mp-release failed: %s" % str(error)[:2000])
        raise

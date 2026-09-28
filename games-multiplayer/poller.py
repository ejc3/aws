"""games-mp-poller: starts the multiplayer image builds for new colton-games commits.

Runs every minute (EventBridge Scheduler, games-multiplayer-deploy.tf). AWS pulls from GitHub;
GitHub holds no AWS credential and cannot start anything here. Each run:

  1. reads the head of every branch of the repo (never a fork's: a fork's branches are not the
     repo's), with Colton's read-only token (secret games/colton-games-read, Contents: read).
     Every branch, not only those with an open pull request: Vercel builds a preview for every
     pushed branch, so every preview lobby then has its engine, and listing pull requests would
     need a wider token (Pull requests: read);
  2. for each commit it has not seen, claims `build#<channel>#<commit>` in the releases table
     (a conditional put, so a commit is built once), downloads the commit's zipball, repacks it
     for CodeBuild with the Terraform-shipped driver (bringup.py) and the pinned Supabase CA
     added under .games-mp/, uploads it to s3://<bucket>/sources/<channel>/<commit>.zip, and
     starts that channel's CodeBuild project:
       main     -> games-mp-images          router and engines, pushed to games/*;
                   a sequence number from `seq#main` orders main releases, so a slow build of
                   an older commit can never replace a newer production release
       preview  -> games-mp-images-preview  engines only, pushed to games-preview/*; its role
                   cannot push to a production repository. At most MAX_PREVIEW_BUILDS run at
                   once and MAX_STARTS start per run, which bounds what pushing many branches
                   can cost

games-mp-release (release.py) takes over when a build ends. A commit is tried once; a failed
build is recorded (`status: failed`) and not retried until the branch gets a new commit. Preview
records expire after PREVIEW_TTL_DAYS, and an expired record of a branch still there is simply
built again, which keeps its engine image in ECR (whose preview rule expires images later than
that).

A commit that is the head of main AND of another branch is built in both channels: the preview
image and revision never serve production, and the production ones never serve a preview.

No dependency beyond boto3 (bundled in the Lambda runtime) and bringup.py (packaged next to
this file). Offline test: scripts/test-games-mp-deploy.py.
"""

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request

import bringup

REPO = os.environ.get("REPO", "CoderColton/colton-games")
TOKEN_SECRET = os.environ.get("TOKEN_SECRET", "games/colton-games-read")
TABLE = os.environ.get("RELEASES_TABLE", "")
BUCKET = os.environ.get("BUCKET", "")
PROJECTS = {"main": os.environ.get("MAIN_PROJECT", ""), "preview": os.environ.get("PREVIEW_PROJECT", "")}
# New commits started per run, per channel: a burst of pushes is spread over a few minutes
# instead of queueing many builds at once. main has one head, so this only limits previews.
MAX_STARTS = int(os.environ.get("MAX_STARTS", "2"))
MAX_PREVIEW_BUILDS = int(os.environ.get("MAX_PREVIEW_BUILDS", "3"))
PREVIEW_TTL_DAYS = int(os.environ.get("PREVIEW_TTL_DAYS", "30"))
# A claim whose build never started (the run died between the claim and StartBuild) is retried
# after this long.
STALE_CLAIM_SEC = int(os.environ.get("STALE_CLAIM_SEC", "900"))
GITHUB_API = "https://api.github.com"
HERE = os.path.dirname(os.path.abspath(__file__))

COMMIT = re.compile(r"[0-9a-f]{40}")

# Seams the offline test replaces.
URLOPEN = urllib.request.urlopen
CLOCK = time.time
_clients = {}
_token = {"value": None, "at": 0.0}


def _client(name):
    if name not in _clients:
        import boto3

        _clients[name] = boto3.client(name)
    return _clients[name]


def log(**fields):
    print(json.dumps(fields, sort_keys=True))


def github_token():
    # Cached for five minutes in a warm environment: a rotated token is picked up soon after.
    if _token["value"] is None or CLOCK() - _token["at"] > 300:
        value = _client("secretsmanager").get_secret_value(SecretId=TOKEN_SECRET)["SecretString"].strip()
        if not value:
            raise RuntimeError("secret %s is empty" % TOKEN_SECRET)
        _token.update(value=value, at=CLOCK())
    return _token["value"]


def github(path, accept="application/vnd.github+json", raw=False, timeout=30):
    req = urllib.request.Request(GITHUB_API + path, headers={
        "Authorization": "Bearer " + github_token(), "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "games-mp-poller"})
    try:
        with URLOPEN(req, timeout=timeout) as resp:
            data = resp.read()
    except urllib.error.HTTPError as e:
        # The status says what is wrong (401: the token expired or was revoked); never the token.
        raise RuntimeError("GitHub %s -> HTTP %s" % (path.split("?")[0], e.code)) from None
    return data if raw else json.loads(data)


def heads():
    """[(channel, commit, branch)]: main's head first, then every other branch's head."""
    main, others, page = None, [], 1
    while True:
        branches = github("/repos/%s/branches?per_page=100&page=%d" % (REPO, page))
        for b in branches:
            name, sha = b.get("name"), (b.get("commit") or {}).get("sha")
            if not isinstance(name, str) or not isinstance(sha, str) or not COMMIT.fullmatch(sha):
                continue
            if name == "main":
                main = sha
            else:
                others.append(("preview", sha, name))
        if len(branches) < 100:
            break
        page += 1
    if main is None:
        raise RuntimeError("GitHub listed no main branch")
    return [("main", main, "main")] + others


def preview_builds_running():
    """How many preview builds are in progress (the newest 100 are enough to see them all)."""
    cb = _client("codebuild")
    ids = cb.list_builds_for_project(projectName=PROJECTS["preview"], sortOrder="DESCENDING").get("ids") or []
    if not ids:
        return 0
    return sum(1 for b in cb.batch_get_builds(ids=ids[:100]).get("builds") or [] if b.get("buildStatus") == "IN_PROGRESS")


def _get(key):
    item = _client("dynamodb").get_item(TableName=TABLE, Key={"id": {"S": key}}, ConsistentRead=True).get("Item")
    return item


def next_seq():
    out = _client("dynamodb").update_item(
        TableName=TABLE, Key={"id": {"S": "seq#main"}}, UpdateExpression="ADD seq :one",
        ExpressionAttributeValues={":one": {"N": "1"}}, ReturnValues="UPDATED_NEW")
    return int(out["Attributes"]["seq"]["N"])


def claim(channel, commit, detail):
    """Claims the commit's build, or returns None when it is already claimed (or built)."""
    key = "build#%s#%s" % (channel, commit)
    now = int(CLOCK())
    item = {"id": {"S": key}, "channel": {"S": channel}, "commit": {"S": commit},
            "status": {"S": "starting"}, "claimedAt": {"N": str(now)}, "source": {"S": detail}}
    if channel == "preview":
        item["expiresAt"] = {"N": str(now + PREVIEW_TTL_DAYS * 86400)}
    # A claim left `starting` by a run that died before StartBuild is taken over when stale.
    condition = "attribute_not_exists(id) OR (#s = :starting AND claimedAt < :stale)"
    values = {":starting": {"S": "starting"}, ":stale": {"N": str(now - STALE_CLAIM_SEC)}}
    if channel == "main":
        item["seq"] = {"N": str(next_seq())}
    try:
        _client("dynamodb").put_item(TableName=TABLE, Item=item, ConditionExpression=condition,
                                     ExpressionAttributeNames={"#s": "status"},
                                     ExpressionAttributeValues=values)
    except Exception as error:
        if (getattr(error, "response", None) or {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return None
        raise
    return key


def source_zip(commit):
    """The commit as CodeBuild's source: the repo at the zip root plus .games-mp/ (our driver and
    the pinned Supabase CA, never anything of the repo's own under that path)."""
    raw = github("/repos/%s/zipball/%s" % (REPO, commit), raw=True, timeout=120)
    with open(os.path.join(HERE, "bringup.py"), "rb") as f:
        driver = f.read()
    with open(os.path.join(HERE, "supabase-root-2021-ca.crt"), "rb") as f:
        ca = f.read()
    return bringup.repack_zipball(raw, commit, driver, extra={bringup.CA_PATH: ca})


def start(channel, commit, key):
    data = source_zip(commit)
    s3_key = "sources/%s/%s.zip" % (channel, commit)
    with tempfile.TemporaryFile() as f:
        f.write(data)
        f.seek(0)
        _client("s3").upload_fileobj(f, BUCKET, s3_key)
    build = _client("codebuild").start_build(
        projectName=PROJECTS[channel],
        sourceLocationOverride="%s/%s" % (BUCKET, s3_key),
        environmentVariablesOverride=[{"name": "GAMES_MP_COMMIT", "value": commit, "type": "PLAINTEXT"}],
    )["build"]["id"]
    _client("dynamodb").update_item(
        TableName=TABLE, Key={"id": {"S": key}}, UpdateExpression="SET #s = :building, buildId = :b",
        ConditionExpression="#s = :starting",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":building": {"S": "building"}, ":b": {"S": build},
                                   ":starting": {"S": "starting"}})
    return build


def lambda_handler(event, context):
    started, seen = [], 0
    budget = {"main": MAX_STARTS, "preview": MAX_STARTS}
    running = None
    for channel, commit, detail in heads():
        seen += 1
        if budget[channel] <= 0:
            continue
        existing = _get("build#%s#%s" % (channel, commit))
        if existing and existing.get("status", {}).get("S") != "starting":
            continue
        if channel == "preview":
            running = preview_builds_running() if running is None else running
            if running >= MAX_PREVIEW_BUILDS:
                budget["preview"] = 0
                continue
            running += 1
        key = claim(channel, commit, detail)
        if key is None:
            continue
        budget[channel] -= 1
        try:
            build = start(channel, commit, key)
        except Exception:
            # Leave nothing half-claimed: the next run tries this commit again.
            _client("dynamodb").delete_item(TableName=TABLE, Key={"id": {"S": key}},
                                            ConditionExpression="#s = :starting",
                                            ExpressionAttributeNames={"#s": "status"},
                                            ExpressionAttributeValues={":starting": {"S": "starting"}})
            raise
        started.append({"channel": channel, "commit": commit, "from": detail, "build": build})
    log(heads=seen, started=started)
    return {"heads": seen, "started": started}

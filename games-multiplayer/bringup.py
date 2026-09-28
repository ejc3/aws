#!/usr/bin/env python3
"""games-mp steps: the ones `terraform apply` runs on the jumpbox, and the two CodeBuild runs.

Most steps are subcommands called from a terraform_data local-exec in
games-multiplayer-bringup.tf. Every step is idempotent: it checks the live state first and
only changes what is missing, then verifies. A failed check exits non-zero, which fails the
apply loudly.

    preflight         (terraform plan, data "external") read-only: prove every later step can
                      run, or stop the plan before anything changes
    vercel-oidc       make sure the Vercel project issues OIDC tokens in Team issuer mode
    preview-supabase  copy the Supabase URL and server key from Production to Preview
    sync-db-url       copy the Supabase integration's database URL into games/mp-db-url, the one
                      secret games-mp-migrate reads
    router-live       once: give games/mp-router the `live` tag the router task definition runs
    releases-bootstrap  once: production's current release before the first automatic one
    wait-healthy      wait for a healthy router deployment and a 200 `ok` from /healthz

and inside CodeBuild, from the source zip games-mp-poller made (it adds this file at
.games-mp/bringup.py, see games-multiplayer/poller.py):

    codebuild-images   (games-mp-images, games-mp-images-preview) build and push one commit's
                       images, only the tags that are missing, and export what was built
    codebuild-migrate  (games-mp-migrate) apply a main commit's mp migrations over verify-full TLS

SECRETS NEVER LEAVE MEMORY. Tokens and database credentials are read from Secrets Manager or
the Vercel API into this process, sent only in HTTP headers, request bodies or a child's
environment (never argv, which every local user can read in /proc/<pid>/cmdline), and never
printed. Python standard library only, plus the `aws`, `psql` (migrate), `node` and `docker`
(codebuild-images) executables.

Offline tests: scripts/test-games-mp-bringup.py.
"""

import argparse
import base64
import datetime
import glob
import hashlib
import io
import json
import os
import re
import shlex
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

# Seams the offline tests replace.
RUN = subprocess.run
URLOPEN = urllib.request.urlopen
SLEEP = time.sleep
CLOCK = time.monotonic

VERCEL_API = "https://api.vercel.com"
GITHUB_API = "https://api.github.com"
DRIVER_PATH = ".games-mp/bringup.py"  # where games-mp-poller puts this file inside the source zip
CA_PATH = ".games-mp/supabase-root-2021-ca.crt"  # and the pinned Supabase CA
EXPORTS_PATH = ".games-mp/exports.sh"  # what a CodeBuild run exports (buildspec sources it)
# SHA-256 of the DER of "Supabase Root 2021 CA", the root every Supabase Postgres endpoint
# (direct and pooler) chains to. Checked against Supabase's published copy
# (supabase-downloads .../prod/ssl/prod-ca-2021.crt) and the chain served on 2026-09-27.
SUPABASE_ROOT_SHA256 = "807025ad50d4ed219d2c9c7d299c004f824eb00cf7f65afef607d07b72e6cafa"


class StepError(Exception):
    """A check failed. The message is printed and the apply fails."""


# The preflight speaks Terraform's external-program protocol on stdout, so it logs to stderr.
LOG_STREAM = None


def log(msg):
    print("[games-mp] %s" % msg, file=LOG_STREAM or sys.stdout, flush=True)


# --------------------------------------------------------------------------------------
# Small wrappers
# --------------------------------------------------------------------------------------


def aws(*args, input_text=None, parse=True, check=True):
    """Runs the AWS CLI. Returns parsed JSON (or text), or None when check=False and it failed."""
    proc = RUN(
        ["aws", *args, "--output", "json" if parse else "text"],
        input=input_text,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if not check:
            return None
        # AWS CLI errors name the call and the reason; they never echo secret values.
        raise StepError("aws %s failed: %s" % (" ".join(args[:2]), proc.stderr.strip()[-500:]))
    out = proc.stdout.strip()
    if not parse:
        return out
    return json.loads(out) if out else {}


def read_secret(secret_id, region):
    """The secret's current value, or None when the secret or its value does not exist yet."""
    proc = RUN(
        ["aws", "secretsmanager", "get-secret-value", "--secret-id", secret_id,
         "--region", region, "--query", "SecretString", "--output", "text"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        if "ResourceNotFoundException" in proc.stderr:
            return None
        raise StepError("reading secret %s failed: %s" % (secret_id, proc.stderr.strip()[-300:]))
    value = proc.stdout.rstrip("\n")
    return value or None


def http(method, url, headers=None, body=None, timeout=30):
    """(status, parsed JSON or raw bytes). Never raises on HTTP status; the caller decides."""
    data = None
    headers = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with URLOPEN(req, timeout=timeout) as resp:
            status, raw = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    try:
        return status, json.loads(raw) if raw else {}
    except ValueError:
        return status, raw


class Vercel:
    def __init__(self, token, team_id, project_id):
        self.token, self.team_id, self.project_id = token, team_id, project_id

    def call(self, method, path, body=None, ok=(200, 201)):
        sep = "&" if "?" in path else "?"
        url = "%s%s%steamId=%s" % (VERCEL_API, path, sep, urllib.parse.quote(self.team_id))
        status, data = http(method, url, {"Authorization": "Bearer " + self.token}, body)
        if status not in ok:
            err = data.get("error", {}) if isinstance(data, dict) else {}
            raise StepError("Vercel %s %s -> %s %s" % (method, path.split("?")[0], status, err.get("code", "")))
        return data

    def project(self):
        return self.call("GET", "/v9/projects/%s" % self.project_id)

    def envs(self):
        return self.call("GET", "/v10/projects/%s/env" % self.project_id).get("envs", [])

    def decrypted(self, env_id):
        """The plain value, or None when Vercel will not decrypt it (sensitive/integration)."""
        try:
            data = self.call("GET", "/v1/projects/%s/env/%s" % (self.project_id, env_id))
        except StepError:
            return None
        if data.get("decrypted") is False or data.get("type") == "sensitive":
            return None
        return data.get("value") or None


def vercel_from_args(args):
    token = read_secret(args.vercel_token_secret, args.region)
    if not token:
        raise StepError("secret %s has no value" % args.vercel_token_secret)
    return Vercel(token, args.team_id, args.project_id)


def production_value(vercel, envs, key):
    """Decrypted Production value of `key`, or None."""
    for env in envs:
        if env.get("key") == key and "production" in (env.get("target") or []) and not env.get("gitBranch"):
            return vercel.decrypted(env["id"])
    return None


# --------------------------------------------------------------------------------------
# Source: one games-repo commit as a CodeBuild-ready zip (games-mp-poller makes it)
# --------------------------------------------------------------------------------------


def check_ref(ref):
    if not re.fullmatch(r"[0-9a-f]{40}", ref or ""):
        raise StepError("source ref must be a full 40-hex commit sha, got %r" % ref)


def repack_zipball(raw, ref, driver_source, extra=None):
    """GitHub's zipball nests everything under `<owner>-<repo>-<sha7>/`. CodeBuild wants the
    repo at the zip root, so strip that folder, keep file modes, and add this driver, the ref it
    was built from and any `extra` {path: bytes} under .games-mp/. Anything the repo itself has
    under .games-mp/ is dropped: that directory is ours."""
    src = zipfile.ZipFile(io.BytesIO(raw))
    names = [n for n in src.namelist() if n]
    top = names[0].split("/", 1)[0] + "/"
    if not all(n.startswith(top) for n in names):
        raise StepError("unexpected zipball layout")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            rel = info.filename[len(top):]
            if not rel or rel == ".games-mp/" or rel.startswith(".games-mp/"):
                continue
            new = zipfile.ZipInfo(rel, info.date_time)
            # GitHub zipballs carry no Unix modes (all zero), and unzip would apply mode 0
            # literally: explicit 755 directories, 644 files (755 if marked executable).
            mode = (info.external_attr >> 16) & 0o777 if info.create_system == 3 else 0
            if info.is_dir():
                new.external_attr = (0o40755 << 16) | 0x10
            else:
                new.external_attr = (0o100755 if mode & 0o111 else 0o100644) << 16
            new.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(new, b"" if info.is_dir() else src.read(info))
        ours = {DRIVER_PATH: driver_source, ".games-mp/SOURCE_REF": (ref + "\n").encode()}
        ours.update(extra or {})
        for path, data in sorted(ours.items()):
            if not path.startswith(".games-mp/"):
                raise StepError("extra source files go under .games-mp/, not %s" % path)
            info = zipfile.ZipInfo(path, (2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o100755 if path == DRIVER_PATH else 0o100644) << 16
            dst.writestr(info, data)
    return out.getvalue()


# --------------------------------------------------------------------------------------
# codebuild-images (inside CodeBuild)
# --------------------------------------------------------------------------------------


def missing_tags(expect, region, refresh_before=None):
    """{repo: tag} entries of `expect` that ECR does not have yet. With `refresh_before` (a UTC
    datetime), an image pushed before it is deleted and counted as missing, so it is pushed
    again (see PREVIEW_REFRESH_DAYS)."""
    missing = {}
    for repo, tag in sorted(expect.items()):
        found = aws("ecr", "describe-images", "--region", region, "--repository-name", repo,
                    "--image-ids", "imageTag=%s" % tag, check=False)
        if not found or not found.get("imageDetails"):
            missing[repo] = tag
            continue
        pushed = found["imageDetails"][0].get("imagePushedAt")
        if refresh_before is not None and pushed and _when(pushed) < refresh_before:
            log("%s:%s was pushed %s: pushing it again, so it outlives its new release record" % (repo, tag, pushed))
            aws("ecr", "batch-delete-image", "--region", region, "--repository-name", repo,
                "--image-ids", "imageTag=%s" % tag)
            missing[repo] = tag
    return missing


def _when(value):
    """The AWS CLI's timestamp (ISO 8601, or epoch seconds) as an aware UTC datetime."""
    if isinstance(value, (int, float)):
        return datetime.datetime.fromtimestamp(value, datetime.timezone.utc)
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


# A preview image older than this is pushed again when its commit is built again. The poller
# builds a branch's head again when its release record expires (30 days) and ECR expires preview
# images 45 days after the push, so a record never outlives its image.
PREVIEW_REFRESH_DAYS = 14


# Base images named without a registry come from Docker Hub, whose anonymous pull limit
# CodeBuild's shared addresses hit. AWS mirrors the official images in ECR Public.
def docker_hub_library_bases(dockerfile_text):
    """Official Docker Hub images (`node:24-alpine`) a Dockerfile builds FROM, skipping
    earlier build stages, `scratch`, and anything that names a namespace or registry."""
    stages, bases = set(), []
    for m in re.finditer(r"(?im)^[ \t]*FROM[ \t]+(?:--platform=\S+[ \t]+)?(\S+)(?:[ \t]+AS[ \t]+(\S+))?", dockerfile_text):
        ref, stage = m.group(1), m.group(2)
        if "/" not in ref and ref.lower() not in stages and ref.lower() != "scratch" and ref not in bases:
            bases.append(ref)
        if stage:
            stages.add(stage.lower())
    return bases


def parse_dry_run(text):
    """What `mp-images.mjs --push --dry-run` would do: {name: (repo, tag, dockerfile)} from its
    `[name] $ docker build ... -f <dockerfile> ...` and `name: <registry>/<repo>:<tag>` lines."""
    dockerfiles, out = {}, {}
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r"\[([a-z0-9-]+)\] \$ docker build .*?-f (\S+)", line)
        if m:
            dockerfiles[m.group(1)] = m.group(2)
            continue
        m = re.fullmatch(r"([a-z0-9-]+): \S+?/(games/[a-z0-9-]+):(\S+)", line)
        if m:
            out[m.group(1)] = (m.group(2), m.group(3), dockerfiles.get(m.group(1)))
    return out


def dockerfile_inputs(dockerfile, root="."):
    """SHA-256 over a Dockerfile and every file its COPY/ADD instructions take from the build
    context (never from another stage): whether a rebuilt image is the same program. The main
    release rolls the router only when this changes."""
    with open(os.path.join(root, dockerfile)) as f:
        text = re.sub(r"\\\n", " ", f.read())
    files = {dockerfile}
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) < 2 or parts[0].upper() not in ("COPY", "ADD"):
            continue
        rest = parts[1].strip()
        flags = []
        while rest.startswith("--"):
            flag, _, rest = rest.partition(" ")
            flags.append(flag)
            rest = rest.strip()
        if any(f.startswith("--from") for f in flags):
            continue
        args = json.loads(rest) if rest.startswith("[") else rest.split()
        for pattern in args[:-1]:
            matches = sorted(glob.glob(os.path.join(root, pattern), recursive=True))
            if not matches:
                raise StepError("%s: COPY source %s matches nothing" % (dockerfile, pattern))
            for m in matches:
                walk = [os.path.join(d, n) for d, _, names in os.walk(m) for n in names] if os.path.isdir(m) else [m]
                files.update(os.path.relpath(w, root) for w in walk)
    digest = hashlib.sha256()
    for rel in sorted(files):
        with open(os.path.join(root, rel), "rb") as f:
            digest.update(rel.encode() + b"\0" + hashlib.sha256(f.read()).digest())
    return digest.hexdigest()


def write_exports(values, path=EXPORTS_PATH):
    """Shell assignments the buildspec sources, so CodeBuild exports them (its
    exported-variables); games-mp-release reads them with BatchGetBuilds."""
    with open(path, "w") as f:
        for key, value in sorted(values.items()):
            f.write("%s=%s\n" % (key, shlex.quote(value)))


def source_commit():
    commit = os.environ.get("GAMES_MP_COMMIT", "")
    check_ref(commit)
    with open(".games-mp/SOURCE_REF") as f:
        if f.read().strip() != commit:
            raise StepError("the source zip is not commit %s" % commit)
    return commit


def cmd_codebuild_images(args):
    """Builds and pushes one commit's images. The channel is the project's own setting:
    main     every image scripts/mp-images.mjs defines, into games/* (router included);
    preview  engines only, into games-preview/<game>-engine: a preview never ships a router.
    Tags are the repo's own (`<sha12>`, `<simVersion>-<sha12>`); a tag ECR already has is not
    rebuilt (tags are immutable: it is already pushed)."""
    channel = os.environ.get("GAMES_MP_CHANNEL")
    if channel not in ("main", "preview"):
        raise StepError("GAMES_MP_CHANNEL must be main or preview")
    commit = source_commit()
    sha12 = commit[:12]
    account, region = os.environ["ACCOUNT_ID"], os.environ.get("AWS_REGION", "us-west-1")
    registry = "%s.dkr.ecr.%s.amazonaws.com" % (account, region)
    script = ["node", "scripts/mp-images.mjs", "--sha", sha12]
    dry = RUN(script + ["--account", account, "--region", region, "--push", "--dry-run"],
              capture_output=True, text=True)
    if dry.returncode != 0:
        raise StepError("mp-images.mjs --dry-run failed: %s" % dry.stderr.strip()[-500:])
    targets = {}
    for name, (repo, tag, dockerfile) in sorted(parse_dry_run(dry.stdout).items()):
        source = repo
        engine = re.fullmatch(r"games/([a-z0-9-]+)-engine", repo)
        if tag != sha12 and not (engine and tag.endswith("-" + sha12)):
            raise StepError("mp-images.mjs tags %s %s:%s, not with commit %s" % (name, repo, tag, sha12))
        if channel == "preview":
            if not engine:
                continue
            repo = "games-preview/%s-engine" % engine.group(1)
        targets[name] = (repo, tag, dockerfile, "%s:%s" % (source, tag))
    if not targets:
        raise StepError("mp-images.mjs defines no images to build")
    images = {repo: tag for repo, tag, _, _ in targets.values()}
    refresh = None
    if channel == "preview":
        refresh = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=PREVIEW_REFRESH_DAYS)
    todo = missing_tags(images, region, refresh)
    names = [name for name, (repo, _, _, _) in sorted(targets.items()) if repo in todo]
    if names:
        password = aws("ecr", "get-login-password", "--region", region, parse=False)
        if RUN(["docker", "login", "--username", "AWS", "--password-stdin", registry],
               input=password, capture_output=True, text=True).returncode != 0:
            raise StepError("docker login to %s failed" % registry)
    for name in names:
        with open(targets[name][2]) as f:
            for ref in docker_hub_library_bases(f.read()):
                mirror = "public.ecr.aws/docker/library/%s" % ref
                if RUN(["docker", "pull", "--platform", "linux/arm64", mirror]).returncode == 0:
                    RUN(["docker", "tag", mirror, ref], check=True)
                else:
                    log("mirror pull failed for %s; docker build will try Docker Hub" % ref)
    for name in names:
        repo, tag, _, local = targets[name]
        log("building %s and pushing %s:%s" % (name, repo, tag))
        if RUN(script + ["--only", name]).returncode != 0:
            raise StepError("mp-images.mjs --only %s failed" % name)
        # mp-images.mjs builds it as games/<...>:<tag>; push it under the channel's repository.
        remote = "%s/%s:%s" % (registry, repo, tag)
        for cmd in (["docker", "tag", local, remote], ["docker", "push", remote]):
            if RUN(cmd).returncode != 0:
                raise StepError("%s failed" % " ".join(cmd[:2]))
    still = missing_tags(images, region)
    if still:
        raise StepError("pushed, but ECR still lacks %s" % still)
    exports = {"GAMES_MP_IMAGES": json.dumps(images, sort_keys=True)}
    if channel == "main":
        router = [df for repo, _, df, _ in targets.values() if repo == "games/mp-router"]
        if len(router) != 1:
            raise StepError("a main build must build exactly one router image")
        exports["GAMES_MP_ROUTER_INPUTS"] = dockerfile_inputs(router[0])
        migrations = mp_migrations(".")
        exports["GAMES_MP_SCHEMA_REVISION"] = str(migrations[-1][0] if migrations else 0)
    write_exports(exports)
    log("built %s (%s): %s" % (commit, channel, json.dumps(images, sort_keys=True)))


# --------------------------------------------------------------------------------------
# Vercel
# --------------------------------------------------------------------------------------


def cmd_vercel_oidc(args):
    v = vercel_from_args(args)
    want = {"enabled": True, "issuerMode": "team"}
    cur = v.project().get("oidcTokenConfig") or {}
    if cur.get("enabled") is True and cur.get("issuerMode") == "team":
        log("Vercel OIDC already on, Team issuer mode")
        return
    log("Vercel OIDC was %s; setting Team issuer mode" % json.dumps(cur, sort_keys=True))
    v.call("PATCH", "/v9/projects/%s" % v.project_id, {"oidcTokenConfig": want})
    cur = v.project().get("oidcTokenConfig") or {}
    if not (cur.get("enabled") is True and cur.get("issuerMode") == "team"):
        raise StepError("Vercel OIDC still %s after PATCH" % json.dumps(cur, sort_keys=True))
    log("Vercel OIDC now on, Team issuer mode (verified)")


def preview_targets(envs, key):
    """(Preview-only variables named `key`, problem). A variable that reaches Preview together
    with another environment or a branch is a problem: editing it would change Production or
    Development."""
    same = [e for e in envs if e.get("key") == key and "preview" in (e.get("target") or [])]
    mixed = [e for e in same if sorted(e.get("target") or []) != ["preview"] or e.get("gitBranch")]
    if mixed:
        return same, ("%s already reaches Preview together with another environment or branch; "
                      "refusing to edit it (that would change Production/Development)" % key)
    return same, None


def cmd_preview_supabase(args):
    """Copies each KEY:TYPE from Production to a Preview-ONLY variable. Writes only variables
    whose target is exactly ["preview"]; anything else with the same key is left alone, and a
    variable that spans Preview and another environment stops the step."""
    v = vercel_from_args(args)
    envs = v.envs()
    for spec in args.keys.split(","):
        key, vtype = spec.split(":")
        if vtype not in ("encrypted", "sensitive"):
            raise StepError("bad type for %s" % key)
        value = production_value(v, envs, key)
        if value is None:
            # The plan-time preflight proved this decrypts; failing here means it changed since.
            raise StepError("%s: Production value missing or no longer decryptable" % key)
        same, problem = preview_targets(envs, key)
        if problem:
            raise StepError(problem)
        body = {"value": value, "type": vtype, "target": ["preview"],
                "comment": "copied from Production by games-mp bring-up (ejc3/aws)"}
        if same:
            v.call("PATCH", "/v9/projects/%s/env/%s" % (v.project_id, same[0]["id"]), body)
            log("%s: Preview value refreshed" % key)
        else:
            v.call("POST", "/v10/projects/%s/env" % v.project_id, dict(body, key=key))
            log("%s: Preview variable created" % key)


# --------------------------------------------------------------------------------------
# migrate
# --------------------------------------------------------------------------------------


def pg_env(url, ca_path):
    """libpq environment for `url`, forcing certificate-verified TLS whatever the URL says."""
    u = urllib.parse.urlsplit(url)
    if u.scheme not in ("postgres", "postgresql") or not u.hostname:
        raise StepError("database URL is not a postgres:// URL")
    env = {
        "PGHOST": u.hostname,
        "PGPORT": str(u.port or 5432),
        "PGUSER": urllib.parse.unquote(u.username or ""),
        "PGPASSWORD": urllib.parse.unquote(u.password or ""),
        "PGDATABASE": (u.path or "/postgres").lstrip("/") or "postgres",
        "PGSSLMODE": "verify-full",
        "PGSSLROOTCERT": ca_path,
        "PGCONNECT_TIMEOUT": "15",
        "PGAPPNAME": "games-mp-bringup",
    }
    if not env["PGUSER"] or not env["PGPASSWORD"]:
        raise StepError("database URL has no user or password")
    return env


def check_ca(ca_path):
    with open(ca_path) as f:
        der = ssl.PEM_cert_to_DER_cert(f.read())
    if hashlib.sha256(der).hexdigest() != SUPABASE_ROOT_SHA256:
        raise StepError("%s is not the pinned Supabase Root 2021 CA" % ca_path)


MIGRATIONS_DIR = "supabase/migrations"
# How an mp migration records its revision: the first inserts the marker row, later ones update it.
_REVISION = re.compile(
    r"INSERT INTO mp_private\.schema_revision \(id, revision\) VALUES \(1, (\d+)\)"
    r"|UPDATE mp_private\.schema_revision SET revision = (\d+) WHERE id = 1")


def migration_revision(sql):
    found = _REVISION.findall(sql)
    if len(found) != 1:
        raise StepError("an mp migration must set mp_private.schema_revision exactly once")
    for other in ("skyhook_private", "site_private"):
        if other in sql:
            raise StepError("an mp migration mentions %s; refusing to run it" % other)
    return int(found[0][0] or found[0][1])


def mp_migrations(root="."):
    """[(revision, path, sql)]: the repo's mp migrations (the files under supabase/migrations
    that set mp_private.schema_revision), in file-name order, which must be revisions 1..n.
    The other migrations there (the site's, Skyhook's) are applied by hand and never here."""
    folder = os.path.join(root, MIGRATIONS_DIR)
    out = []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not name.endswith(".sql"):
            continue
        with open(os.path.join(folder, name)) as f:
            sql = f.read()
        if "mp_private.schema_revision" not in sql or not _REVISION.search(sql):
            continue
        out.append((migration_revision(sql), "%s/%s" % (MIGRATIONS_DIR, name), sql))
    revisions = [r for r, _, _ in out]
    if revisions != list(range(1, len(out) + 1)):
        raise StepError("mp migrations must set revisions 1..n in file-name order, found %s" % revisions)
    return out


# Two statements, not one CASE: PostgreSQL resolves every relation a statement names when it
# parses it, so a query that mentions mp_private.schema_revision fails on a fresh database
# even inside a branch that would not run.
PRESENCE_SQL = (
    "SELECT (to_regclass('mp_private.schema_revision') IS NOT NULL)::int, "
    "(EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'mp_private'))::int"
)
MARKER_SQL = "SELECT coalesce(max(revision), -2) FROM mp_private.schema_revision WHERE id = 1"


def current_revision(env):
    """0 = never applied; -1 = mp_private exists without the marker table; -2 = empty marker;
    otherwise the recorded revision."""
    table, schema = (int(x) for x in psql(env, "-A", "-t", "-F", ",", "-c", PRESENCE_SQL).strip().split(","))
    if not table:
        return -1 if schema else 0
    return int(psql(env, "-A", "-t", "-c", MARKER_SQL).strip())


def psql(env, *args):
    proc = RUN(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", *args], capture_output=True, text=True,
               env=dict(os.environ, **env))
    if proc.returncode != 0:
        # psql errors are SQL errors or connection failures; libpq never echoes the password.
        raise StepError("psql failed: %s" % proc.stderr.strip()[-800:])
    return proc.stdout


def apply_mp_migrations(env, migrations):
    """Brings mp_private to the last of `migrations`, one file at a time, each verified.
    Returns the revision it ends at. Refuses a database NEWER than the migrations (a main
    commit older than what is deployed must never run against it) and a missing marker."""
    want = migrations[-1][0] if migrations else 0
    have = current_revision(env)
    log("database %s: mp_private revision %s, this commit's migrations reach %s" % (env["PGHOST"], have, want))
    if have < 0:
        raise StepError("mp_private exists but its schema_revision marker is %s; fix by hand, not by re-running"
                        % ("missing" if have == -1 else "empty"))
    if have > want:
        raise StepError("database is at mp revision %d, newer than this commit's %d: this commit is older "
                        "than what is deployed and must not run against it" % (have, want))
    for revision, path, sql in migrations[have:]:
        with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
            f.write(sql)
            sql_path = f.name
        try:
            psql(env, "-f", sql_path)
        finally:
            os.unlink(sql_path)
        now = current_revision(env)
        if now != revision:
            raise StepError("%s ran but mp_private.schema_revision is %s, expected %s" % (path, now, revision))
        log("%s applied; mp_private at revision %d (verified)" % (path, now))
    return want


def cmd_codebuild_migrate(args):
    """games-mp-migrate: a main commit's mp migrations, from its source zip, against the
    Supabase database in games/mp-db-url (injected by the buildspec, never on a command line).
    Runs no code of the repo's: only psql with its .sql files. Exports GAMES_MP_DB_REVISION."""
    commit = source_commit()
    url = os.environ.pop("GAMES_MP_DB_URL", "")
    if not url:
        raise StepError("GAMES_MP_DB_URL is empty (secret games/mp-db-url)")
    check_ca(CA_PATH)
    env = pg_env(url, os.path.abspath(CA_PATH))
    revision = apply_mp_migrations(env, mp_migrations("."))
    write_exports({"GAMES_MP_DB_REVISION": str(revision)})
    log("commit %s: mp_private at revision %d" % (commit, revision))


def cmd_sync_db_url(args):
    """Copies the Supabase integration's own database URL (Production POSTGRES_URL_NON_POOLING,
    decrypted from Vercel) into the secret games-mp-migrate reads, when it differs. The value
    goes from memory to Secrets Manager on stdin: never argv, the disk or Terraform state."""
    v = vercel_from_args(args)
    url = production_value(v, v.envs(), args.url_key)
    if not url:
        raise StepError("Vercel %s (Production) is missing or no longer decryptable" % args.url_key)
    pg_env(url, "-")  # a postgres:// URL with a user and password, or stop here
    if read_secret(args.secret_id, args.region) == url:
        log("%s already holds the current database URL" % args.secret_id)
        return
    aws("secretsmanager", "put-secret-value", "--region", args.region, "--secret-id", args.secret_id,
        "--secret-string", "file:///dev/stdin", "--query", "VersionId", input_text=url, parse=False)
    if read_secret(args.secret_id, args.region) != url:
        raise StepError("%s does not hold the database URL after writing it" % args.secret_id)
    log("%s updated from Vercel %s (verified)" % (args.secret_id, args.url_key))


def cmd_router_live(args):
    """Once: the router task definition runs games/mp-router:live, which games-mp-release moves
    on every main release that changes the router. Before the first one, `live` is the image the
    service already runs; on a platform built from nothing, the first main release creates it
    (games-mp-poller starts that build as soon as it exists), so this waits for it."""
    def has_live():
        found = aws("ecr", "describe-images", "--region", args.region, "--repository-name", args.repository,
                    "--image-ids", "imageTag=%s" % args.tag, check=False)
        return bool(found and found.get("imageDetails"))

    if has_live():
        log("%s:%s exists" % (args.repository, args.tag))
        return
    svc = aws("ecs", "describe-services", "--region", args.region, "--cluster", args.cluster,
              "--services", args.service).get("services") or []
    td = svc[0].get("taskDefinition") if svc and svc[0].get("status") == "ACTIVE" else None
    if td:
        image = aws("ecs", "describe-task-definition", "--region", args.region, "--task-definition", td,
                    )["taskDefinition"]["containerDefinitions"][0]["image"]
        tag = image.rsplit(":", 1)[1] if ":" in image.rsplit("/", 1)[-1] else ""
        if not re.fullmatch(r"[0-9a-f]{12}", tag):
            raise StepError("mp-router runs %s, not a <sha12> image, and %s has no `%s` tag" % (image, args.repository, args.tag))
        img = aws("ecr", "batch-get-image", "--region", args.region, "--repository-name", args.repository,
                  "--image-ids", "imageTag=%s" % tag)["images"][0]
        aws("ecr", "put-image", "--region", args.region, "--repository-name", args.repository,
            "--image-tag", args.tag, "--image-manifest", img["imageManifest"],
            "--image-manifest-media-type", img.get("imageManifestMediaType") or
            "application/vnd.docker.distribution.manifest.v2+json")
        log("%s:%s now names the running router image (%s)" % (args.repository, args.tag, tag))
        return
    deadline = CLOCK() + args.timeout
    log("no mp-router service yet: waiting for the first main release to create %s:%s" % (args.repository, args.tag))
    while not has_live():
        if CLOCK() > deadline:
            raise StepError("%s:%s never appeared; check games-mp-poller and games-mp-release logs"
                            % (args.repository, args.tag))
        SLEEP(args.poll)
    log("%s:%s exists" % (args.repository, args.tag))


def cmd_releases_bootstrap(args):
    """Once: production's current release (current#main in games-mp-releases) before the first
    automatic one, so production launches never pause for it. For each game, the newest ACTIVE
    revision of its production family whose engine image is <its repository>:<sim>-<sha12> and
    that carries MP_TOKEN_VERIFIER (what Terraform registered). Written only if the item is
    missing, with the games that have such a revision (a game added before its first image has
    none, and games-mp-launch refuses it until a release has it); with none at all (a platform
    built from nothing) nothing is written, and the first main release creates it."""
    games = json.loads(args.games)
    found = aws("dynamodb", "get-item", "--region", args.region, "--table-name", args.table,
                "--key", json.dumps({"id": {"S": "current#main"}}), "--consistent-read")
    if found.get("Item"):
        log("current#main exists: %s" % found["Item"].get("commit", {}).get("S"))
        return
    current, commits = {}, set()
    for game, where in sorted(games.items()):
        arns = aws("ecs", "list-task-definitions", "--region", args.region, "--family-prefix", where["family"],
                   "--status", "ACTIVE", "--sort", "DESC").get("taskDefinitionArns") or []
        for arn in arns:
            if arn.rsplit("/", 1)[-1].rsplit(":", 1)[0] != where["family"]:
                continue
            td = aws("ecs", "describe-task-definition", "--region", args.region, "--task-definition", arn)["taskDefinition"]
            c = td["containerDefinitions"]
            m = re.fullmatch(re.escape(where["repositoryUrl"]) + r":(.+)-([0-9a-f]{12})", c[0].get("image", "")) if len(c) == 1 else None
            marker = {"name": "MP_TOKEN_VERIFIER", "value": "ed25519-v2"} in (c[0].get("environment") or [])
            if m and marker and c[0].get("name") == "engine":
                current[game] = {"M": {"taskDefinition": {"S": arn}, "simVersion": {"S": m.group(1)}}}
                commits.add(m.group(2))
                break
    missing = sorted(set(games) - set(current))
    if not current:
        log("no registered revision for any game: the first main release will create current#main")
        return
    if missing:
        log("no registered revision for %s yet: left out of current#main until a release has it"
            % ", ".join(missing))
    item = {"id": {"S": "current#main"}, "seq": {"N": "0"}, "games": {"M": current},
            "commit": {"S": ",".join(sorted(commits))}, "bootstrap": {"BOOL": True}}
    aws("dynamodb", "put-item", "--region", args.region, "--table-name", args.table, "--item", json.dumps(item),
        "--condition-expression", "attribute_not_exists(id)")
    log("current#main bootstrapped from the registered revisions: %s"
        % ", ".join("%s=%s" % (g, v["M"]["taskDefinition"]["S"].rsplit("/", 1)[-1]) for g, v in sorted(current.items())))


# --------------------------------------------------------------------------------------
# preflight (plan time, read-only)
# --------------------------------------------------------------------------------------
#
# Runs as Terraform `data "external"` during every plan on the jumpbox. It only READS: the
# two credentials from Secrets Manager, the Vercel project and its env (decrypting the
# values later steps copy or use, then discarding them), the token's own metadata, and
# whether the GitHub read token can read main. Every problem is reported at
# once, and any problem fails the plan, so an apply never starts with a step that cannot
# finish. It prints no secret and returns none to Terraform (nothing sensitive in state).


def token_scope(vercel, team_id, now_ms):
    """('team'|'user'|'unverifiable', problem). Vercel tokens carry scopes (a user, or one
    team) and an expiry, but no per-endpoint permissions, so this proves the token is live
    and covers the team, not that a write will be accepted."""
    status, data = http("GET", "%s/v5/user/tokens/current" % VERCEL_API,
                        {"Authorization": "Bearer " + vercel.token})
    if status != 200 or not isinstance(data, dict) or "token" not in data:
        return "unverifiable", None
    tok = data["token"]
    if tok.get("revokedAt") or tok.get("leakedAt"):
        return "", "the Vercel token (vercel-api-token) is revoked or marked leaked; store a new one"
    if tok.get("expiresAt") and tok["expiresAt"] < now_ms + 7 * 86400 * 1000:
        return "", "the Vercel token (vercel-api-token) expires within 7 days; store a new one"
    scopes = tok.get("scopes") or []
    if any(sc.get("type") == "team" and sc.get("teamId") == team_id for sc in scopes):
        return "team", None
    # No scopes at all is an unrestricted user token (seen on a CLI login, 2026-09-27).
    if not scopes or any(sc.get("type") == "user" for sc in scopes):
        return "user", None
    return "", "the Vercel token (vercel-api-token) is not scoped to team %s" % team_id


def cmd_preflight(_args):
    global LOG_STREAM
    LOG_STREAM = sys.stderr
    q = json.load(sys.stdin)
    region, team_id, project_id = q["region"], q["team_id"], q["project_id"]
    problems, result = [], {}

    token = read_secret(q["vercel_token_secret"], region)
    if not token:
        problems.append("secret %s has no value (the Vercel API token)" % q["vercel_token_secret"])
    else:
        v = Vercel(token, team_id, project_id)
        scope, problem = token_scope(v, team_id, int(time.time() * 1000))
        result["token_scope"] = scope
        if problem:
            problems.append(problem)
        envs = None
        try:
            project = v.project()  # also carries oidcTokenConfig and protectionBypass
            if project.get("id") != project_id:
                problems.append("Vercel returned project %r, expected %s" % (project.get("id"), project_id))
            # Read into the result, not into `problems`: this proves the setting is
            # observable, it does not assert it is already correct (cmd_vercel_oidc's own
            # job, run at apply time, is to fix it if not). Feeding this into
            # terraform_data.games_mp_vercel_oidc's triggers_replace is what makes a plan
            # notice OIDC drift at all; a constant trigger never would.
            result["oidc_state"] = json.dumps(project.get("oidcTokenConfig") or {}, sort_keys=True)
            envs = v.envs()
        except StepError as e:
            problems.append("cannot read the Vercel project or its env with vercel-api-token: %s" % e)
        if envs is not None:
            for key in [k for k in q.get("copy_keys", "").split(",") if k]:
                if production_value(v, envs, key) is None:
                    problems.append("vercel-api-token cannot decrypt %s on Production, which Preview's Supabase "
                                    "copy needs; use a token of a team member who can read the integration's "
                                    "env vars" % key)
                _, problem = preview_targets(envs, key)
                if problem:
                    problems.append(problem)
            # sync-db-url copies it into games/mp-db-url for games-mp-migrate.
            url = production_value(v, envs, q["url_key"])
            if url is None:
                problems.append(
                    "vercel-api-token cannot decrypt %s on Production (the Supabase integration's database "
                    "URL games-mp-migrate uses). Store a token of a colton-games team member in secret "
                    "vercel-api-token" % q["url_key"])
            else:
                try:
                    env = pg_env(url, "-")
                    result["db_host"] = env["PGHOST"]
                except StepError as e:
                    problems.append("%s on Production: %s" % (q["url_key"], e))

    # games-mp-poller reads main and the open pull requests with this token.
    pat = read_secret(q["github_pat_secret"], region)
    if not pat:
        problems.append("secret %s has no value (GitHub read credential for %s)" % (q["github_pat_secret"], q["repo"]))
    else:
        status, data = http("GET", "%s/repos/%s/commits/main" % (GITHUB_API, q["repo"]),
                            {"Authorization": "Bearer " + pat, "Accept": "application/vnd.github+json"})
        if status != 200 or not isinstance(data, dict) or not re.fullmatch(r"[0-9a-f]{40}", str(data.get("sha"))):
            problems.append("GitHub: %s main is not readable with %s (HTTP %s)"
                            % (q["repo"], q["github_pat_secret"], status))

    if problems:
        sys.stderr.write("games-mp preflight failed; nothing has been changed:\n" +
                         "".join("  - %s\n" % p for p in problems))
        return 1
    result.setdefault("db_host", "none")
    result.setdefault("oidc_state", "unknown")
    json.dump(result, sys.stdout)
    return 0


# --------------------------------------------------------------------------------------
# wait-healthy
# --------------------------------------------------------------------------------------


def healthz_via(address, host, path="/healthz", timeout=10):
    """GET https://<host><path>, connecting to `address` with SNI and certificate checks for
    `host`. Proves certificate, listener and router without waiting on public DNS caches."""
    ctx = ssl.create_default_context()
    with socket.create_connection((address, 443), timeout=timeout) as raw:
        with ctx.wrap_socket(raw, server_hostname=host) as tls:
            tls.sendall(("GET %s HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % (path, host)).encode())
            data = b""
            while len(data) < 65536:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1]) if head.startswith(b"HTTP/") else 0
    if b"transfer-encoding: chunked" in head.lower():
        body = dechunk(body)
    return status, body


def dechunk(body):
    out = b""
    while body:
        size_line, _, rest = body.partition(b"\r\n")
        size = int(size_line.split(b";")[0].strip() or b"0", 16)
        if size == 0:
            break
        out += rest[:size]
        body = rest[size + 2:]
    return out


def rollout_state(desc, task_definition_arn):
    """'ready', 'wait' or a failure message, from `aws ecs describe-services` output.

    Terraform's UpdateService makes the new deployment PRIMARY at once. A circuit-breaker
    rollback marks it FAILED and makes a deployment of the previous task definition PRIMARY,
    so "PRIMARY runs something else and no deployment of ours is left" is a rollback too.
    'ready' is PRIMARY on this task definition, running its desired count, not FAILED. It does
    not wait for COMPLETED: the old tasks drain at the ALB for up to an hour (live matches keep
    their connections), and ECS calls the rollout complete only once they have stopped."""
    services = desc.get("services") or []
    if not services:
        return "service not found"
    deployments = services[0].get("deployments") or []
    ours = [d for d in deployments if d.get("taskDefinition") == task_definition_arn]
    primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
    if primary is None:
        return "wait"
    if primary.get("taskDefinition") != task_definition_arn:
        # (A release's own new deployment of the same task definition that failed and rolled
        # back leaves a PRIMARY on this task definition: that is judged below, not here.)
        for d in ours:
            if d.get("rolloutState") == "FAILED":
                return "rollout FAILED: %s" % (d.get("rolloutStateReason") or "see the service events")
        if not ours:
            return "the service runs %s, not %s (rolled back or replaced)" % (
                primary.get("taskDefinition"), task_definition_arn)
        return "wait"
    if primary.get("rolloutState") == "FAILED":
        return "rollout FAILED: %s" % (primary.get("rolloutStateReason") or "see the service events")
    want = primary.get("desiredCount", 0)
    if want >= 1 and primary.get("runningCount", 0) >= want:
        return "ready"
    return "wait"


def deployment_ips(args, deployment_id):
    """Private IPv4 addresses of the deployment's running tasks."""
    arns = aws("ecs", "list-tasks", "--region", args.region, "--cluster", args.cluster, "--started-by",
               deployment_id, "--desired-status", "RUNNING").get("taskArns") or []
    if not arns:
        return set()
    tasks = aws("ecs", "describe-tasks", "--region", args.region, "--cluster", args.cluster, "--tasks", *arns)
    return {d["value"] for t in tasks.get("tasks") or [] for a in t.get("attachments") or []
            for d in a.get("details") or [] if d.get("name") == "privateIPv4Address"}


def cmd_wait_healthy(args):
    deadline = CLOCK() + args.timeout
    while True:
        desc = aws("ecs", "describe-services", "--region", args.region, "--cluster", args.cluster, "--services", args.service)
        state = rollout_state(desc, args.task_definition_arn)
        if state == "ready":
            primary = next(d for d in desc["services"][0]["deployments"] if d.get("status") == "PRIMARY")
            ips = deployment_ips(args, primary["id"])
            th = aws("elbv2", "describe-target-health", "--region", args.region, "--target-group-arn", args.target_group_arn)
            healthy = {d["Target"]["Id"] for d in th.get("TargetHealthDescriptions", [])
                       if d["TargetHealth"]["State"] == "healthy"}
            if len(ips) >= primary["desiredCount"] and ips <= healthy:
                log("mp-router deployment %s on %s: %d tasks, all healthy targets"
                    % (primary["id"], args.task_definition_arn.rsplit("/", 1)[-1], len(ips)))
                break
            state = "wait"
        if state != "wait":
            raise StepError(state)
        if CLOCK() > deadline:
            raise StepError("mp-router deployment not running and healthy after %ds; check the service events and "
                            "/games/mp-router logs" % args.timeout)
        SLEEP(args.poll)
    last = ""
    while True:
        try:
            status, body = healthz_via(args.alb_dns, args.host)
            if status == 200 and body.strip() == b"ok":
                log("https://%s/healthz -> 200 ok (via the ALB, certificate verified for %s)" % (args.host, args.host))
                break
            last = "HTTP %s" % status
        except (OSError, ssl.SSLError, ValueError, IndexError) as e:
            last = type(e).__name__
        if CLOCK() > deadline:
            raise StepError("https://%s/healthz never answered 200 ok (last: %s)" % (args.host, last))
        SLEEP(args.poll)
    # Public DNS is informational: a resolver's negative cache must not fail the apply.
    try:
        addrs = socket.getaddrinfo(args.host, 443)
        log("%s resolves (%d addresses)" % (args.host, len(addrs)))
    except OSError:
        log("NOTE: %s does not resolve here yet (DNS caches); the ALB itself is verified" % args.host)


# --------------------------------------------------------------------------------------


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, vercel=False):
        sp.add_argument("--region", default="us-west-1")
        if vercel:
            sp.add_argument("--team-id", required=True)
            sp.add_argument("--project-id", required=True)
            sp.add_argument("--vercel-token-secret", default="vercel-api-token")

    sub.add_parser("codebuild-images")
    sub.add_parser("codebuild-migrate")
    sub.add_parser("preflight")

    o = sub.add_parser("vercel-oidc")
    common(o, vercel=True)

    s = sub.add_parser("preview-supabase")
    common(s, vercel=True)
    s.add_argument("--keys", required=True, help="KEY:encrypted|sensitive,...")

    d = sub.add_parser("sync-db-url")
    common(d, vercel=True)
    d.add_argument("--secret-id", required=True)
    d.add_argument("--url-key", default="POSTGRES_URL_NON_POOLING")

    r = sub.add_parser("router-live")
    common(r)
    r.add_argument("--cluster", required=True)
    r.add_argument("--service", required=True)
    r.add_argument("--repository", required=True)
    r.add_argument("--tag", default="live")
    r.add_argument("--timeout", type=int, default=1800)
    r.add_argument("--poll", type=int, default=20)

    rb = sub.add_parser("releases-bootstrap")
    common(rb)
    rb.add_argument("--table", required=True)
    rb.add_argument("--games", required=True, help="JSON {game: {family, repositoryUrl}}")

    w = sub.add_parser("wait-healthy")
    common(w)
    w.add_argument("--cluster", required=True)
    w.add_argument("--service", required=True)
    w.add_argument("--task-definition-arn", required=True)
    w.add_argument("--target-group-arn", required=True)
    w.add_argument("--alb-dns", required=True)
    w.add_argument("--host", required=True)
    w.add_argument("--timeout", type=int, default=900)
    w.add_argument("--poll", type=int, default=15)

    args = p.parse_args(argv)
    handler = {
        "codebuild-images": cmd_codebuild_images,
        "codebuild-migrate": cmd_codebuild_migrate,
        "preflight": cmd_preflight,
        "vercel-oidc": cmd_vercel_oidc,
        "preview-supabase": cmd_preview_supabase,
        "sync-db-url": cmd_sync_db_url,
        "router-live": cmd_router_live,
        "releases-bootstrap": cmd_releases_bootstrap,
        "wait-healthy": cmd_wait_healthy,
    }[args.cmd]
    try:
        return handler(args) or 0
    except StepError as e:
        log("FAILED %s: %s" % (args.cmd, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())

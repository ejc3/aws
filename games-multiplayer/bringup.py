#!/usr/bin/env python3
"""games-mp bring-up steps that `terraform apply` runs on the jumpbox (and one inside CodeBuild).

Each step is a subcommand, called from a terraform_data local-exec in
games-multiplayer-bringup.tf. Every step is idempotent: it checks the live state first and
only changes what is missing, then verifies. A failed check exits non-zero, which fails the
apply loudly.

    preflight         (terraform plan, data "external") read-only: prove every later step can
                      run, or stop the plan before anything changes
    build             upload the pinned games-repo source to S3, run CodeBuild, wait for it
    codebuild-images  (inside CodeBuild) build and push only the image tags that are missing
    vercel-oidc       make sure the Vercel project issues OIDC tokens in Team issuer mode
    preview-supabase  copy the Supabase URL and server key from Production to Preview
    migrate           apply the pinned mp migration to Supabase once, over verify-full TLS
    wait-healthy      wait for a healthy router target and a 200 `ok` from /healthz

SECRETS NEVER LEAVE MEMORY. Tokens and database credentials are read from Secrets Manager or
the Vercel API into this process, sent only in HTTP headers, request bodies or a child's
environment (never argv, which every local user can read in /proc/<pid>/cmdline), and never
printed. Python standard library only, plus the `aws`, `psql` (migrate), `node` and `docker`
(codebuild-images) executables.

Offline tests: scripts/test-games-mp-bringup.py.
"""

import argparse
import base64
import hashlib
import io
import json
import os
import re
import shutil
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
DRIVER_PATH = ".games-mp/bringup.py"  # where `build` puts this file inside the source zip
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
# Source: the pinned games-repo commit as a CodeBuild-ready zip
# --------------------------------------------------------------------------------------


def check_ref(ref):
    if not re.fullmatch(r"[0-9a-f]{40}", ref or ""):
        raise StepError("source ref must be a full 40-hex commit sha, got %r" % ref)


def repack_zipball(raw, ref, driver_source):
    """GitHub's zipball nests everything under `<owner>-<repo>-<sha7>/`. CodeBuild wants the
    repo at the zip root, so strip that folder, keep file modes, and add this driver and the
    ref it was built from."""
    src = zipfile.ZipFile(io.BytesIO(raw))
    names = [n for n in src.namelist() if n]
    top = names[0].split("/", 1)[0] + "/"
    if not all(n.startswith(top) for n in names):
        raise StepError("unexpected zipball layout")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            rel = info.filename[len(top):]
            if not rel:
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
        drv = zipfile.ZipInfo(DRIVER_PATH, (2026, 1, 1, 0, 0, 0))
        drv.external_attr = 0o100755 << 16
        dst.writestr(drv, driver_source)
        marker = zipfile.ZipInfo(".games-mp/SOURCE_REF", (2026, 1, 1, 0, 0, 0))
        marker.external_attr = 0o100644 << 16
        dst.writestr(marker, ref + "\n")
    return out.getvalue()


def fetch_source(ref, repo, cache_dir, region, pat_secret):
    """Path of the cached, repacked zip for `ref`. Downloads it once per commit."""
    check_ref(ref)
    os.makedirs(cache_dir, mode=0o700, exist_ok=True)
    with open(os.path.abspath(__file__), "rb") as f:
        driver = f.read()
    # Keyed by the driver too: the zip carries this file into CodeBuild.
    path = os.path.join(cache_dir, "%s-%s.zip" % (ref, hashlib.sha256(driver).hexdigest()[:12]))
    if os.path.exists(path):
        return path
    token = read_secret(pat_secret, region)
    if not token:
        raise StepError("secret %s has no value; it is the GitHub read credential" % pat_secret)
    status, raw = http("GET", "%s/repos/%s/zipball/%s" % (GITHUB_API, repo, ref),
                       {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"},
                       timeout=300)
    if status != 200 or not isinstance(raw, (bytes, bytearray)):
        raise StepError("GitHub zipball %s@%s -> %s" % (repo, ref[:12], status))
    data = repack_zipball(raw, ref, driver)
    # A unique temporary name: the build and migrate steps may download the same commit at
    # the same time, and each must publish a complete file atomically.
    fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".%s." % ref[:12], suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    log("source %s@%s: %d bytes" % (repo, ref[:12], len(data)))
    return path


def read_from_source(zip_path, member):
    with zipfile.ZipFile(zip_path) as z:
        try:
            return z.read(member).decode()
        except KeyError:
            raise StepError("%s is not in the pinned source" % member)


# --------------------------------------------------------------------------------------
# build (jumpbox) and codebuild-images (inside CodeBuild)
# --------------------------------------------------------------------------------------


def missing_tags(expect, region):
    """{repo: tag} entries of `expect` that ECR does not have yet."""
    missing = {}
    for repo, tag in sorted(expect.items()):
        found = aws("ecr", "describe-images", "--region", region, "--repository-name", repo,
                    "--image-ids", "imageTag=%s" % tag, check=False)
        if not found or not found.get("imageDetails"):
            missing[repo] = tag
    return missing


def cmd_build(args):
    expect = json.loads(args.expect)
    todo = missing_tags(expect, args.region)
    if not todo:
        log("images already in ECR, nothing to build: %s" % ", ".join("%s:%s" % kv for kv in sorted(expect.items())))
        return
    log("missing images: %s" % ", ".join("%s:%s" % kv for kv in sorted(todo.items())))
    zip_path = fetch_source(args.ref, args.repo, args.cache_dir, args.region, args.github_pat_secret)
    key = "sources/%s.zip" % args.ref
    aws("s3", "cp", zip_path, "s3://%s/%s" % (args.bucket, key), "--region", args.region, "--only-show-errors", parse=False)
    request = {
        "projectName": args.project,
        "sourceLocationOverride": "%s/%s" % (args.bucket, key),
        "environmentVariablesOverride": [
            {"name": "GAMES_MP_EXPECT", "value": json.dumps(expect, sort_keys=True), "type": "PLAINTEXT"},
            {"name": "GAMES_MP_SHA12", "value": args.ref[:12], "type": "PLAINTEXT"},
        ],
    }
    started = aws("codebuild", "start-build", "--region", args.region, "--cli-input-json", "file:///dev/stdin",
                  input_text=json.dumps(request))
    build_id = started["build"]["id"]
    log("CodeBuild %s started" % build_id)
    deadline = CLOCK() + args.timeout
    status = "IN_PROGRESS"
    while True:
        builds = aws("codebuild", "batch-get-builds", "--region", args.region, "--ids", build_id)["builds"]
        status = builds[0]["buildStatus"]
        if status != "IN_PROGRESS":
            break
        if CLOCK() > deadline:
            raise StepError("CodeBuild %s still running after %ds" % (build_id, args.timeout))
        SLEEP(args.poll)
    link = builds[0].get("logs", {}).get("deepLink", "")
    if status != "SUCCEEDED":
        raise StepError("CodeBuild %s ended %s. Logs: %s" % (build_id, status, link))
    still = missing_tags(expect, args.region)
    if still:
        raise StepError("CodeBuild succeeded but ECR still lacks %s" % still)
    log("images built and pushed (%s)" % build_id)


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


def cmd_codebuild_images(args):
    expect = json.loads(os.environ["GAMES_MP_EXPECT"])
    sha12 = os.environ["GAMES_MP_SHA12"]
    account, region = os.environ["ACCOUNT_ID"], os.environ.get("AWS_REGION", "us-west-1")
    base = ["node", "scripts/mp-images.mjs", "--account", account, "--region", region, "--sha", sha12]
    dry = RUN(base + ["--push", "--dry-run"], capture_output=True, text=True)
    if dry.returncode != 0:
        raise StepError("mp-images.mjs --dry-run failed: %s" % dry.stderr.strip()[-500:])
    produced = parse_dry_run(dry.stdout)
    got = {repo: tag for repo, tag, _ in produced.values()}
    if got != expect:
        # Terraform derives the tags (games_mp_sim_versions + the sha); the repo's script is
        # the other half. Disagreeing would push tags no task definition names.
        raise StepError("the pinned source builds %s but Terraform expects %s; fix var.games_mp_sim_versions"
                        % (json.dumps(got, sort_keys=True), json.dumps(expect, sort_keys=True)))
    todo = missing_tags(expect, region)
    names = [name for name, (repo, _, _) in sorted(produced.items()) if repo in todo]
    if not names:
        log("every tag already exists (immutable tags: already pushed)")
        return
    for name in names:
        dockerfile = produced[name][2]
        if not dockerfile:
            continue
        with open(dockerfile) as f:
            for ref in docker_hub_library_bases(f.read()):
                mirror = "public.ecr.aws/docker/library/%s" % ref
                if RUN(["docker", "pull", "--platform", "linux/arm64", mirror]).returncode == 0:
                    RUN(["docker", "tag", mirror, ref], check=True)
                else:
                    log("mirror pull failed for %s; docker build will try Docker Hub" % ref)
    for name in names:
        log("building and pushing %s" % name)
        proc = RUN(base + ["--push", "--login", "--only", name])
        if proc.returncode != 0:
            raise StepError("mp-images.mjs --only %s failed" % name)
    still = missing_tags(expect, region)
    if still:
        raise StepError("pushed, but ECR still lacks %s" % still)


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


def migration_revision(sql):
    found = re.findall(r"INSERT INTO mp_private\.schema_revision \(id, revision\) VALUES \(1, (\d+)\)", sql)
    if len(found) != 1:
        raise StepError("the migration must set mp_private.schema_revision exactly once")
    for other in ("skyhook_private", "site_private"):
        if other in sql:
            raise StepError("the mp migration mentions %s; refusing to run it" % other)
    return int(found[0])


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


def ensure_psql():
    if shutil.which("psql"):
        return
    if shutil.which("apt-get") and RUN(["sudo", "-n", "true"], capture_output=True).returncode == 0:
        log("installing postgresql-client (psql) on this admin box")
        for cmd in (["apt-get", "install", "-y", "-qq", "postgresql-client"],
                    ["apt-get", "update", "-qq"],
                    ["apt-get", "install", "-y", "-qq", "postgresql-client"]):
            RUN(["sudo", "-n", *cmd], capture_output=True, text=True)
            if shutil.which("psql"):
                return
    raise StepError("psql is not installed and could not be installed: sudo apt-get install -y postgresql-client")


def database_url(args):
    """(url, where): the Supabase integration's own URL, decrypted from the Vercel project's
    Production env at apply time. One source; the plan-time preflight proved it decrypts."""
    v = vercel_from_args(args)
    return production_value(v, v.envs(), args.url_key), "Vercel %s (Production)" % args.url_key


def cmd_migrate(args):
    check_ca(args.ca)
    zip_path = fetch_source(args.ref, args.repo, args.cache_dir, args.region, args.github_pat_secret)
    sql = read_from_source(zip_path, args.file)
    want = migration_revision(sql)
    url, where = database_url(args)
    if not url:
        # The plan-time preflight proved this source had a value; failing here means it changed.
        raise StepError("the database URL from %s is gone since the plan; plan again" % where)
    env = pg_env(url, args.ca)
    ensure_psql()
    have = current_revision(env)
    log("database (%s, %s): mp_private revision %s, migration sets %s" % (where, env["PGHOST"], have, want))
    if have == want:
        log("migration already applied; nothing to do")
        return
    if have < 0:
        raise StepError("mp_private exists but its schema_revision marker is %s; fix by hand, not by re-running"
                        % ("missing" if have == -1 else "empty"))
    if have > want:
        # source_ref rolled back to a commit whose migration file targets an OLDER revision
        # than what is already live. Running it would be a no-op at best; deploying THIS
        # source against a newer, possibly incompatible schema is the real danger, so this
        # must fail loudly rather than silently report "already applied" (have >= want did
        # exactly that, which is the bug this replaces).
        raise StepError("database is at revision %d, newer than this migration's target %d; "
                        "games_mp_source_ref is older than what is deployed and must not run against it"
                        % (have, want))
    if have > 0:
        raise StepError("database is at revision %d but this migration starts from nothing; a later "
                        "migration must bring it to %d" % (have, want))
    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as f:
        f.write(sql)
        sql_path = f.name
    try:
        psql(env, "-f", sql_path)
    finally:
        os.unlink(sql_path)
    have = current_revision(env)
    if have != want:
        raise StepError("migration ran but mp_private.schema_revision is %s, expected %s" % (have, want))
    log("migration %s applied; mp_private at revision %d (verified)" % (args.file, have))


# --------------------------------------------------------------------------------------
# preflight (plan time, read-only)
# --------------------------------------------------------------------------------------
#
# Runs as Terraform `data "external"` during every plan on the jumpbox. It only READS: the
# two credentials from Secrets Manager, the Vercel project and its env (decrypting the
# values later steps copy or use, then discarding them), the token's own metadata, the
# pinned commit on GitHub, and whether psql can run here. Every problem is reported at
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
            if q.get("migrate") == "true":
                url = production_value(v, envs, q["url_key"])
                if url is None:
                    problems.append(
                        "vercel-api-token cannot decrypt %s on Production (the Supabase integration's database "
                        "URL the migration uses). Store a token of a colton-games team member in secret "
                        "vercel-api-token, or set games_mp_migrate = false" % q["url_key"])
                else:
                    try:
                        env = pg_env(url, "-")
                        result["db_host"] = env["PGHOST"]
                    except StepError as e:
                        problems.append("%s on Production: %s" % (q["url_key"], e))

    if q.get("build") == "true" or q.get("migrate") == "true":
        pat = read_secret(q["github_pat_secret"], region)
        if not pat:
            problems.append("secret %s has no value (GitHub read credential for %s)" % (q["github_pat_secret"], q["repo"]))
        else:
            status, data = http("GET", "%s/repos/%s/commits/%s" % (GITHUB_API, q["repo"], q["ref"]),
                                {"Authorization": "Bearer " + pat, "Accept": "application/vnd.github+json"})
            if status != 200 or not isinstance(data, dict) or data.get("sha") != q["ref"]:
                problems.append("GitHub: %s@%s is not readable with %s (HTTP %s)"
                                % (q["repo"], q["ref"], q["github_pat_secret"], status))

    if q.get("migrate") == "true" and not shutil.which("psql"):
        can_install = shutil.which("apt-get") and RUN(["sudo", "-n", "true"], capture_output=True).returncode == 0
        if not can_install:
            problems.append("psql is missing and cannot be installed without a password: "
                            "sudo apt-get install -y postgresql-client")

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
    """'done', 'wait' or a failure message, from `aws ecs describe-services` output.

    Terraform's UpdateService makes the new deployment PRIMARY at once. A circuit-breaker
    rollback marks it FAILED and makes a deployment of the previous task definition PRIMARY,
    so "PRIMARY runs something else and no deployment of ours is left" is a rollback too."""
    services = desc.get("services") or []
    if not services:
        return "service not found"
    deployments = services[0].get("deployments") or []
    ours = [d for d in deployments if d.get("taskDefinition") == task_definition_arn]
    for d in ours:
        if d.get("rolloutState") == "FAILED":
            return "rollout FAILED: %s" % (d.get("rolloutStateReason") or "see the service events")
    primary = next((d for d in deployments if d.get("status") == "PRIMARY"), None)
    if primary is None:
        return "wait"
    if primary.get("taskDefinition") != task_definition_arn:
        if not ours:
            return "the service runs %s, not %s (rolled back or replaced)" % (
                primary.get("taskDefinition"), task_definition_arn)
        return "wait"
    if primary.get("rolloutState") == "COMPLETED" and primary.get("runningCount", 0) >= 1:
        return "done"
    return "wait"


def cmd_wait_healthy(args):
    deadline = CLOCK() + args.timeout
    while True:
        desc = aws("ecs", "describe-services", "--region", args.region, "--cluster", args.cluster, "--services", args.service)
        state = rollout_state(desc, args.task_definition_arn)
        if state == "done":
            log("mp-router rollout COMPLETED on %s" % args.task_definition_arn.rsplit("/", 1)[-1])
            break
        if state != "wait":
            raise StepError(state)
        if CLOCK() > deadline:
            raise StepError("mp-router rollout not complete after %ds; check the service events and /games/mp-router logs"
                            % args.timeout)
        SLEEP(args.poll)
    while True:
        th = aws("elbv2", "describe-target-health", "--region", args.region, "--target-group-arn", args.target_group_arn)
        states = [d["TargetHealth"]["State"] for d in th.get("TargetHealthDescriptions", [])]
        if "healthy" in states:
            log("router target healthy (%s)" % ",".join(states))
            break
        if CLOCK() > deadline:
            raise StepError("no healthy mp-router target after %ds (states: %s). Check /games/mp-router logs."
                            % (args.timeout, ",".join(states) or "none registered"))
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

    def common(sp, vercel=False, source=False):
        sp.add_argument("--region", default="us-west-1")
        if vercel:
            sp.add_argument("--team-id", required=True)
            sp.add_argument("--project-id", required=True)
            sp.add_argument("--vercel-token-secret", default="vercel-api-token")
        if source:
            sp.add_argument("--ref", required=True)
            sp.add_argument("--repo", default="CoderColton/colton-games")
            sp.add_argument("--cache-dir", required=True)
            sp.add_argument("--github-pat-secret", default="github-pat-ejc3")

    b = sub.add_parser("build")
    common(b, source=True)
    b.add_argument("--project", required=True)
    b.add_argument("--bucket", required=True)
    b.add_argument("--expect", required=True, help="JSON {repo: tag}")
    b.add_argument("--timeout", type=int, default=2700)
    b.add_argument("--poll", type=int, default=15)

    sub.add_parser("codebuild-images")
    sub.add_parser("preflight")

    o = sub.add_parser("vercel-oidc")
    common(o, vercel=True)

    s = sub.add_parser("preview-supabase")
    common(s, vercel=True)
    s.add_argument("--keys", required=True, help="KEY:encrypted|sensitive,...")

    m = sub.add_parser("migrate")
    common(m, vercel=True, source=True)
    m.add_argument("--file", required=True)
    m.add_argument("--ca", required=True)
    m.add_argument("--url-key", default="POSTGRES_URL_NON_POOLING")

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
        "build": cmd_build,
        "codebuild-images": cmd_codebuild_images,
        "preflight": cmd_preflight,
        "vercel-oidc": cmd_vercel_oidc,
        "preview-supabase": cmd_preview_supabase,
        "migrate": cmd_migrate,
        "wait-healthy": cmd_wait_healthy,
    }[args.cmd]
    try:
        return handler(args) or 0
    except StepError as e:
        log("FAILED %s: %s" % (args.cmd, e))
        return 1


if __name__ == "__main__":
    sys.exit(main())

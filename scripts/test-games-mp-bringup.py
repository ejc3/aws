#!/usr/bin/env python3
"""Offline checks for games-multiplayer/bringup.py: the steps `terraform apply` runs, and the
two CodeBuild runs (image builds and migrations) of the automatic deploys.

Every AWS CLI call, HTTP request, psql run and sleep is replaced by a fake, so this needs
no credentials and no network. It pins the behaviour that makes one apply safe to repeat:
each step checks live state first, changes only what is missing, verifies, never writes a
Vercel variable outside Preview, never puts a secret in argv or output, and fails loudly.

Run from the repo root:  python3 -S -B scripts/test-games-mp-bringup.py
"""
import contextlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import types
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GM = ROOT / "games-multiplayer"
REF = "c76e5bdf083fe32628b5c8fee9e6ab867e291369"
SHA12 = REF[:12]
# Where a main / preview build pushes: engines to the channel's one engine repository as
# <game>_<simVersion>-<sha12> (games are dynamic), the router (main only) to games/mp-router.
EXPECT = {("games/mp-router", SHA12), ("games/engines", "mptest_mptest-1-" + SHA12)}
PREVIEW_EXPECT = {("games-preview/engines", "mptest_mptest-1-" + SHA12)}
DB_PASSWORD = "s3cr3t-p@ss/word"
DB_URL = "postgres://postgres.kmfdnctkbdpcagukwqro:%s@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require" % (
    "s3cr3t-p%40ss%2Fword")
SERVICE_KEY = "sb_secret_DO_NOT_PRINT_1234567890"
VERCEL_TOKEN = "vercel-token-DO-NOT-PRINT"
GITHUB_PAT = "github_pat_DO_NOT_PRINT"

DRY_RUN = """[router] $ docker build --platform linux/arm64 -f server/mp-router/Dockerfile -t games/mp-router:{s} .
[mptest] $ docker build --platform linux/arm64 -f server/mptest/Dockerfile -t games/mptest-engine:mptest-1-{s} .
[router] $ docker push 928413605543.dkr.ecr.us-west-1.amazonaws.com/games/mp-router:{s}
router: 928413605543.dkr.ecr.us-west-1.amazonaws.com/games/mp-router:{s}
mptest: 928413605543.dkr.ecr.us-west-1.amazonaws.com/games/mptest-engine:mptest-1-{s}
""".format(s=SHA12)


def load():
    spec = importlib.util.spec_from_file_location("bringup", GM / "bringup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def done(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class World:
    """One fake of everything bringup.py talks to."""

    def __init__(self, bu):
        self.bu = bu
        self.calls = []            # argv of every RUN
        self.envs = []             # env of every RUN that passed one
        self.ecr = set()           # (repo, tag) present
        self.secrets = {"vercel-api-token": VERCEL_TOKEN, "games/colton-games-read": GITHUB_PAT}
        self.builds = []           # statuses to return in order
        self.http_log = []
        self.vercel_envs = []
        self.decrypt = {}          # env id -> value (None = not decryptable)
        self.oidc = {"enabled": True, "issuerMode": "team"}
        self.oidc_patch_effective = True
        self.db_revision = 0
        self.psql_files = []
        self.clock = 0.0
        self.services = None
        self.target_states = ["healthy"]
        self.dry_run = DRY_RUN
        self.sudo = True
        self.token_meta = (200, {"token": {"scopes": [{"type": "team", "teamId": "team_x", "createdAt": 1}]}})
        self.github_commit = 200
        self.pushed = []           # remote refs pushed
        self.pushed_at = {}        # (repo, tag) -> imagePushedAt
        self.deleted = []
        self.tasks = {}            # deployment id -> [ip]
        self.target_health = {}    # ip -> state
        self.manifests = {}        # (repo, tag) -> manifest
        self.put_images = []
        self.ddb = {}              # id -> item (typed)
        self.task_definitions = {} # arn -> task definition
        bu.RUN = self.run
        bu.http = self.http
        bu.SLEEP = self.sleep
        bu.CLOCK = lambda: self.clock

    def sleep(self, s):
        self.clock += s

    # ---- subprocess
    def run(self, argv, input=None, capture_output=False, text=False, env=None, check=False):
        argv = list(argv)
        self.calls.append(argv)
        if env is not None:
            self.envs.append(env)
        if argv[:3] == ["aws", "secretsmanager", "get-secret-value"]:
            sid = argv[argv.index("--secret-id") + 1]
            if sid in self.secrets:
                return done(self.secrets[sid] + "\n")
            return done("", 255, "An error occurred (ResourceNotFoundException) when calling GetSecretValue")
        if argv[:3] == ["sudo", "-n", "true"]:
            return done("", 0 if self.sudo else 1)
        if argv[:3] == ["aws", "secretsmanager", "put-secret-value"]:
            assert argv[argv.index("--secret-string") + 1] == "file:///dev/stdin"
            self.secrets[argv[argv.index("--secret-id") + 1]] = input
            return done("v1")
        if argv[:3] == ["aws", "ecr", "batch-delete-image"]:
            repo = argv[argv.index("--repository-name") + 1]
            tag = argv[argv.index("--image-ids") + 1].split("=", 1)[1]
            self.deleted.append((repo, tag))
            self.ecr.discard((repo, tag))
            return done("{}")
        if argv[:3] == ["aws", "ecr", "get-login-password"]:
            return done("ecr-password")
        if argv[:2] == ["docker", "login"]:
            assert input == "ecr-password" and "ecr-password" not in argv
            return done("")
        if argv[:2] == ["docker", "push"]:
            ref = argv[2].split("/", 1)[1]
            repo, tag = ref.rsplit(":", 1)
            self.pushed.append(ref)
            self.ecr.add((repo, tag))
            return done("")
        if argv[:3] == ["aws", "ecr", "batch-get-image"]:
            repo = argv[argv.index("--repository-name") + 1]
            tag = argv[argv.index("--image-ids") + 1].split("=", 1)[1]
            return done(json.dumps({"images": [{"imageManifest": self.manifests[(repo, tag)],
                                                "imageManifestMediaType": "application/vnd.oci.image.manifest.v1+json"}]}))
        if argv[:3] == ["aws", "ecr", "put-image"]:
            repo, tag = argv[argv.index("--repository-name") + 1], argv[argv.index("--image-tag") + 1]
            self.put_images.append((repo, tag, argv[argv.index("--image-manifest") + 1]))
            self.ecr.add((repo, tag))
            return done("{}")
        if argv[:3] == ["aws", "ecs", "describe-task-definition"]:
            return done(json.dumps({"taskDefinition": self.task_definitions[argv[argv.index("--task-definition") + 1]]}))
        if argv[:3] == ["aws", "ecs", "list-task-definitions"]:
            prefix = argv[argv.index("--family-prefix") + 1]
            arns = [a for a in sorted(self.task_definitions, key=lambda a: -int(a.rsplit(":", 1)[1]))
                    if a.split("/", 1)[1].startswith(prefix)]
            return done(json.dumps({"taskDefinitionArns": arns}))
        if argv[:3] == ["aws", "dynamodb", "get-item"]:
            key = json.loads(argv[argv.index("--key") + 1])["id"]["S"]
            return done(json.dumps({"Item": self.ddb[key]} if key in self.ddb else {}))
        if argv[:3] == ["aws", "dynamodb", "put-item"]:
            item = json.loads(argv[argv.index("--item") + 1])
            assert argv[argv.index("--condition-expression") + 1] == "attribute_not_exists(id)"
            self.ddb[item["id"]["S"]] = item
            return done("{}")
        if argv[:3] == ["aws", "ecs", "list-tasks"]:
            return done(json.dumps({"taskArns": ["task/" + ip for ip in
                                                 self.tasks.get(argv[argv.index("--started-by") + 1], [])]}))
        if argv[:3] == ["aws", "ecs", "describe-tasks"]:
            arns = argv[argv.index("--tasks") + 1:argv.index("--output")]
            return done(json.dumps({"tasks": [{"attachments": [{"details": [
                {"name": "privateIPv4Address", "value": a.split("/", 1)[1]}]}]} for a in arns]}))
        if argv[:3] == ["aws", "ecr", "describe-images"]:
            repo = argv[argv.index("--repository-name") + 1]
            tag = argv[argv.index("--image-ids") + 1].split("=", 1)[1]
            if (repo, tag) in self.ecr:
                pushed = self.pushed_at.get((repo, tag), "2026-09-27T07:00:00.000000+00:00")
                return done(json.dumps({"imageDetails": [{"imageTags": [tag], "imagePushedAt": pushed}]}))
            return done("", 254, "ImageNotFoundException")
        if argv[:3] == ["aws", "s3", "cp"]:
            return done("")
        if argv[:3] == ["aws", "codebuild", "start-build"]:
            raw = argv[argv.index("--cli-input-json") + 1]
            # What the real CLI v2 does: a file:///dev/stdin input fails to parse.
            if raw.startswith("file://"):
                return done("", 252, "Error parsing parameter 'cli-input-json': Invalid JSON received.")
            self.start_request = json.loads(raw)
            return done(json.dumps({"build": {"id": "games-mp-images:1"}}))
        if argv[:3] == ["aws", "codebuild", "batch-get-builds"]:
            status = self.builds.pop(0) if len(self.builds) > 1 else self.builds[0]
            if status == "SUCCEEDED":
                self.ecr.update(EXPECT)
            return done(json.dumps({"builds": [{"buildStatus": status, "logs": {"deepLink": "https://logs/x"}}]}))
        if argv[:3] == ["aws", "ecs", "describe-services"]:
            return done(json.dumps(self.services.pop(0) if len(self.services) > 1 else self.services[0]))
        if argv[:3] == ["aws", "elbv2", "describe-target-health"]:
            health = self.target_health if isinstance(self.target_health, dict) else self.target_health.pop(0)
            return done(json.dumps({"TargetHealthDescriptions": [
                {"Target": {"Id": ip}, "TargetHealth": {"State": st}} for ip, st in health.items()]}))
        if argv[:2] == ["node", "scripts/mp-images.mjs"]:
            if "--dry-run" in argv:
                return done(self.dry_run)
            assert "--push" not in argv, "the driver pushes, under the channel's repository"
            self.built = getattr(self, "built", []) + [argv[argv.index("--only") + 1]]
            return done("")
        if argv[:2] == ["docker", "pull"] or argv[:2] == ["docker", "tag"]:
            return done("")
        if argv[0] == "psql":
            # db_revision: 0 = fresh, -1 = schema without marker table, -2 = empty marker.
            if "-f" in argv:
                sql = Path(argv[argv.index("-f") + 1]).read_text()
                self.psql_files.append(sql)
                self.db_revision = self.bu.migration_revision(sql)
                return done("")
            sql = argv[argv.index("-c") + 1]
            table = self.db_revision not in (0, -1)
            if "mp_private.schema_revision WHERE" in sql or "FROM mp_private.schema_revision" in sql:
                if not table:
                    # What PostgreSQL does: the relation is resolved at parse time.
                    return done("", 1, 'ERROR:  relation "mp_private.schema_revision" does not exist')
                return done("%d\n" % self.db_revision)
            sep = argv[argv.index("-F") + 1] if "-F" in argv else "|"
            return done("%d%s%d\n" % (int(table), sep, int(self.db_revision != 0)))
        raise AssertionError("unexpected command %r" % argv)

    # ---- HTTP (Vercel and GitHub)
    def http(self, method, url, headers=None, body=None, timeout=30):
        self.http_log.append((method, url, body))
        path = url.split("?")[0]
        if "api.github.com" in url:
            if "/commits/" in url:
                assert headers["Authorization"] == "Bearer " + GITHUB_PAT
                return self.github_commit, ({"sha": REF} if self.github_commit == 200 else {"message": "Not Found"})
            return 200, make_zipball()
        if path.endswith("/v5/user/tokens/current"):
            return self.token_meta
        assert headers["Authorization"] == "Bearer " + VERCEL_TOKEN
        if method == "GET" and re.search(r"/v9/projects/[^/]+$", path):
            return 200, {"id": path.rsplit("/", 1)[1], "oidcTokenConfig": dict(self.oidc)}
        if method == "PATCH" and re.search(r"/v9/projects/[^/]+$", path):
            if self.oidc_patch_effective:
                self.oidc = dict(body["oidcTokenConfig"])
            return 200, {}
        if method == "GET" and path.endswith("/env"):
            return 200, {"envs": [dict(e) for e in self.vercel_envs]}
        m = re.search(r"/v1/projects/[^/]+/env/([^/]+)$", path)
        if method == "GET" and m:
            value = self.decrypt.get(m.group(1))
            if value is None:
                return 200, {"type": "encrypted", "decrypted": False}
            return 200, {"type": "encrypted", "decrypted": True, "value": value}
        if method == "POST" and path.endswith("/env"):
            self.vercel_envs.append(dict(body, id="new-%d" % len(self.vercel_envs)))
            return 201, {}
        m = re.search(r"/v9/projects/[^/]+/env/([^/]+)$", path)
        if method == "PATCH" and m:
            return 200, {}
        raise AssertionError("unexpected HTTP %s %s" % (method, url))


def make_zipball():
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        top = "CoderColton-colton-games-c76e5bd/"
        z.writestr(top, b"")
        # Like GitHub's zipballs: no Unix modes at all.
        info = zipfile.ZipInfo(top + "scripts/mp-images.mjs")
        info.create_system = 0
        info.external_attr = 0
        z.writestr(info, b"#!/usr/bin/env node\n")
        tool = zipfile.ZipInfo(top + "bin/tool")
        tool.create_system = 3
        tool.external_attr = 0o100755 << 16
        z.writestr(tool, b"#!/bin/sh\n")
        z.writestr(top + "supabase/migrations/20260926000000_mp.sql", MIGRATION)
        z.writestr(top + "supabase/migrations/bad.sql", "CREATE SCHEMA skyhook_private;\n" + MIGRATION)
    return out.getvalue()


MIGRATION = """BEGIN;
CREATE SCHEMA mp_private;
CREATE TABLE mp_private.schema_revision (id integer PRIMARY KEY CHECK (id = 1), revision integer NOT NULL);
INSERT INTO mp_private.schema_revision (id, revision) VALUES (1, 1);
COMMIT;
"""
MIGRATION_2 = """BEGIN;
ALTER TABLE mp_private.matches ADD COLUMN note text;
UPDATE mp_private.schema_revision SET revision = 2 WHERE id = 1;
COMMIT;
"""


def args(**kw):
    base = dict(region="us-west-1", team_id="team_x", project_id="prj_x", vercel_token_secret="vercel-api-token",
                ref=REF, repo="CoderColton/colton-games", github_pat_secret="games/colton-games-read")
    base.update(kw)
    return types.SimpleNamespace(**base)


class Base(unittest.TestCase):
    def setUp(self):
        self.bu = load()
        self.w = World(self.bu)
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "cache")
        self.out = io.StringIO()
        self._redirect = contextlib.redirect_stdout(self.out)
        self._redirect.__enter__()

    def tearDown(self):
        self._redirect.__exit__(None, None, None)
        self.tmp.cleanup()

    def assertNoSecretsLeaked(self):
        printed = self.out.getvalue()
        argv = " ".join(" ".join(c) for c in self.w.calls)
        for secret in (VERCEL_TOKEN, GITHUB_PAT, DB_PASSWORD, "s3cr3t-p%40ss%2Fword", SERVICE_KEY):
            self.assertNotIn(secret, printed)
            self.assertNotIn(secret, argv)


class SourceTests(Base):
    def test_ref_must_be_a_full_sha(self):
        for bad in ("main", REF[:12], REF.upper(), REF + "0"):
            with self.assertRaises(self.bu.StepError):
                self.bu.check_ref(bad)
        self.bu.check_ref(REF)

    def test_repack_strips_top_dir_keeps_modes_and_adds_our_files(self):
        data = self.bu.repack_zipball(make_zipball(), REF, b"driver", extra={self.bu.CA_PATH: b"ca"})
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        self.assertIn("scripts/mp-images.mjs", names)
        self.assertFalse(any(n.startswith("CoderColton-") for n in names))
        self.assertEqual(z.getinfo("scripts/mp-images.mjs").external_attr >> 16, 0o100644)
        self.assertEqual(z.getinfo("bin/tool").external_attr >> 16, 0o100755)
        self.assertTrue(all(z.getinfo(n).external_attr >> 16 == 0o40755 for n in names if n.endswith("/")))
        self.assertEqual(z.read(".games-mp/bringup.py"), b"driver")
        self.assertEqual(z.read(".games-mp/SOURCE_REF").decode().strip(), REF)
        self.assertEqual(z.read(".games-mp/supabase-root-2021-ca.crt"), b"ca")

    def test_the_repos_own_games_mp_directory_never_reaches_codebuild(self):
        # CodeBuild runs .games-mp/bringup.py: a commit must not be able to supply its own.
        raw = io.BytesIO(make_zipball())
        with zipfile.ZipFile(raw, "a") as z:
            z.writestr("CoderColton-colton-games-c76e5bd/.games-mp/bringup.py", b"evil")
            z.writestr("CoderColton-colton-games-c76e5bd/.games-mp/SOURCE_REF", b"0" * 40)
        data = self.bu.repack_zipball(raw.getvalue(), REF, b"driver")
        z = zipfile.ZipFile(io.BytesIO(data))
        self.assertEqual([n for n in z.namelist() if n.startswith(".games-mp/")],
                         [".games-mp/SOURCE_REF", ".games-mp/bringup.py"])
        self.assertEqual(z.read(".games-mp/bringup.py"), b"driver")
        with self.assertRaises(self.bu.StepError):
            self.bu.repack_zipball(make_zipball(), REF, b"driver", extra={"bringup.py": b"x"})


class CodeBuildImagesTests(Base):
    def setUp(self):
        super().setUp()
        os.environ.update(GAMES_MP_COMMIT=REF, GAMES_MP_CHANNEL="main", ACCOUNT_ID="928413605543",
                          AWS_REGION="us-west-1", GAMES_MP_ENGINE_REPOSITORY="games/engines",
                          GAMES_MP_MAX_CPU="4096", GAMES_MP_MAX_MEMORY="8192")
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        for d, text in (("server/mp-router", "FROM node:24-alpine\nCOPY lib/multiplayer/token.mjs lib/x.mjs ./lib/\n"
                                             "COPY --chown=node server/mp-router/*.mjs \\\n  ./server/\n"),
                        ("server/mptest", "FROM node:24-alpine AS deps\nFROM deps AS x\nFROM node:24-alpine\n"),
                        ("lib/multiplayer", None), (".games-mp", None), ("supabase/migrations", None)):
            os.makedirs(d)
            if text:
                Path(d, "Dockerfile").write_text(text)
        for f in ("lib/multiplayer/token.mjs", "lib/x.mjs", "server/mp-router/router.mjs", "server/mp-router/server.mjs"):
            Path(f).write_text("// " + f)
        Path(".games-mp/SOURCE_REF").write_text(REF + "\n")
        Path("supabase/migrations/20260926000000_mp.sql").write_text(MIGRATION)
        Path("supabase/migrations/20260921000000_skyhook.sql").write_text("CREATE SCHEMA skyhook_private;")

    def tearDown(self):
        os.chdir(self.cwd)
        super().tearDown()

    def exports(self):
        return dict(re.findall(r"^([A-Z_]+)=(.*)$", Path(".games-mp/exports.sh").read_text(), re.M))

    def test_parsers_match_the_real_script_output(self):
        got = self.bu.parse_dry_run(DRY_RUN)
        self.assertEqual(got["router"], ("games/mp-router", SHA12, "server/mp-router/Dockerfile"))
        self.assertEqual(got["mptest"], ("games/mptest-engine", "mptest-1-" + SHA12, "server/mptest/Dockerfile"))
        self.assertEqual(self.bu.docker_hub_library_bases("FROM node:24-alpine AS deps\nFROM deps\nFROM scratch\n"
                                                          "FROM public.ecr.aws/x/y:1\nFROM node:24-alpine\n"),
                         ["node:24-alpine"])

    def test_main_builds_only_the_missing_tag_and_exports_what_it_built(self):
        self.w.ecr.add(("games/mp-router", SHA12))
        self.bu.cmd_codebuild_images(None)
        self.assertEqual(self.w.built, ["mptest"])
        self.assertEqual(self.w.pushed, ["games/engines:mptest_mptest-1-" + SHA12])
        pulls = [c for c in self.w.calls if c[:2] == ["docker", "pull"]]
        self.assertEqual(pulls[0][-1], "public.ecr.aws/docker/library/node:24-alpine")
        ex = self.exports()
        # No mp-engine.json: the contract's 2 vCPU / 4 GB.
        self.assertEqual(json.loads(ex["GAMES_MP_ENGINES"].strip("'")), {"mptest": ["mptest-1", 2048, 4096]})
        self.assertNotIn("GAMES_MP_IMAGES", ex)
        self.assertRegex(ex["GAMES_MP_ROUTER_INPUTS"], r"^[0-9a-f]{64}$")
        self.assertEqual(ex["GAMES_MP_SCHEMA_REVISION"], "1")
        self.assertNoSecretsLeaked()

    def test_preview_pushes_engines_only_to_the_preview_repositories(self):
        os.environ.update(GAMES_MP_CHANNEL="preview", GAMES_MP_ENGINE_REPOSITORY="games-preview/engines")
        self.bu.cmd_codebuild_images(None)
        self.assertEqual(self.w.built, ["mptest"], "never the router")
        self.assertEqual(self.w.pushed, ["games-preview/engines:mptest_mptest-1-" + SHA12])
        self.assertFalse(any(r.startswith("games/") for r, _ in self.w.ecr))
        tags = [c for c in self.w.calls if c[:2] == ["docker", "tag"] and "games-preview" in c[-1]]
        self.assertEqual(tags[0][2], "games/mptest-engine:mptest-1-" + SHA12)
        ex = self.exports()
        self.assertEqual(json.loads(ex["GAMES_MP_ENGINES"].strip("'")), {"mptest": ["mptest-1", 2048, 4096]})
        self.assertNotIn("GAMES_MP_ROUTER_INPUTS", ex)
        # The project's repository setting must be its own channel's.
        os.environ["GAMES_MP_ENGINE_REPOSITORY"] = "games/engines"
        with self.assertRaisesRegex(self.bu.StepError, "not the preview engine repository"):
            self.bu.cmd_codebuild_images(None)

    def test_a_stale_preview_image_is_pushed_again_a_fresh_one_or_main_is_not(self):
        os.environ.update(GAMES_MP_CHANNEL="preview", GAMES_MP_ENGINE_REPOSITORY="games-preview/engines")
        key = ("games-preview/engines", "mptest_mptest-1-" + SHA12)
        self.w.ecr.add(key)
        self.w.pushed_at[key] = "2020-01-01T00:00:00+00:00"
        self.bu.cmd_codebuild_images(None)
        self.assertEqual(self.w.deleted, [key])
        self.assertEqual(self.w.pushed, ["games-preview/engines:mptest_mptest-1-" + SHA12])
        # Pushed just now: left alone.
        import datetime as _dt
        self.w.pushed_at[key] = _dt.datetime.now(_dt.timezone.utc).isoformat()
        self.bu.cmd_codebuild_images(None)
        self.assertEqual(len(self.w.deleted), 1)
        # Production images are never deleted, however old.
        os.environ.update(GAMES_MP_CHANNEL="main", GAMES_MP_ENGINE_REPOSITORY="games/engines")
        for repo, tag in EXPECT:
            self.w.ecr.add((repo, tag))
            self.w.pushed_at[(repo, tag)] = "2020-01-01T00:00:00+00:00"
        self.bu.cmd_codebuild_images(None)
        self.assertEqual(len(self.w.deleted), 1)

    def test_everything_present_builds_nothing(self):
        self.w.ecr.update(EXPECT)
        self.bu.cmd_codebuild_images(None)
        self.assertEqual(getattr(self.w, "built", []), [])
        self.assertFalse(any(c[:2] == ["docker", "login"] for c in self.w.calls))

    def test_a_tag_without_this_commit_or_another_source_fails_before_building(self):
        self.w.dry_run = DRY_RUN.replace("mptest-1-" + SHA12, "mptest-1-0123456789ab")
        with self.assertRaisesRegex(self.bu.StepError, "not <simVersion>-" + SHA12):
            self.bu.cmd_codebuild_images(None)
        self.assertFalse(any("--only" in c for c in self.w.calls))
        Path(".games-mp/SOURCE_REF").write_text("0" * 40 + "\n")
        with self.assertRaisesRegex(self.bu.StepError, "not commit"):
            self.bu.cmd_codebuild_images(None)

    def test_a_new_game_needs_only_the_games_repo(self):
        # A second engine in mp-images.mjs, sized by its own mp-engine.json: no Terraform list.
        os.makedirs("server/starfall-arena")
        Path("server/starfall-arena/Dockerfile").write_text("FROM node:24-alpine\n")
        Path("server/starfall-arena/mp-engine.json").write_text('{"cpu": 4096, "memory": 8192}')
        self.w.dry_run = DRY_RUN + (
            "[starfall-arena] $ docker build --platform linux/arm64 -f server/starfall-arena/Dockerfile "
            "-t games/starfall-arena-engine:arena-3-{s} .\n"
            "starfall-arena: 928413605543.dkr.ecr.us-west-1.amazonaws.com/games/starfall-arena-engine:arena-3-{s}\n"
        ).format(s=SHA12)
        self.bu.cmd_codebuild_images(None)
        self.assertIn("games/engines:starfall-arena_arena-3-" + SHA12, self.w.pushed)
        self.assertEqual(json.loads(self.exports()["GAMES_MP_ENGINES"].strip("'")),
                         {"mptest": ["mptest-1", 2048, 4096], "starfall-arena": ["arena-3", 4096, 8192]})

    def test_a_commit_cannot_escape_the_bounds_or_name_another_family(self):
        os.makedirs("server/big")
        Path("server/big/Dockerfile").write_text("FROM node:24-alpine\n")
        line = ("[{n}] $ docker build --platform linux/arm64 -f server/big/Dockerfile -t games/{n}-engine:v-{s} .\n"
                "{n}: 928413605543.dkr.ecr.us-west-1.amazonaws.com/games/{n}-engine:v-{s}\n")
        cases = {
            "cpu above the maximum": ("big", '{"cpu": 8192, "memory": 16384}', "not a Fargate size within"),
            "memory above the maximum": ("big", '{"cpu": 4096, "memory": 16384}', "not a Fargate size within"),
            "not a Fargate size": ("big", '{"cpu": 2048, "memory": 3000}', "not a Fargate size within"),
            "a string": ("big", '{"cpu": "2048"}', "not a Fargate size within"),
            "another setting": ("big", '{"cpu": 2048, "memory": 4096, "taskRoleArn": "x"}', "must be"),
            "not JSON": ("big", "{", "not JSON"),
            "the router's family": ("mp-router", None, "not a valid game id"),
            "another game's preview family": ("preview-mptest", None, "not a valid game id"),
            "the repositories' own name": ("engines", None, "not a valid game id"),
        }
        for why, (name, size, message) in cases.items():
            with self.subTest(why):
                if size is None:
                    Path("server/big/mp-engine.json").unlink(missing_ok=True)
                else:
                    Path("server/big/mp-engine.json").write_text(size)
                self.w.dry_run = DRY_RUN + line.format(n=name, s=SHA12)
                self.w.pushed.clear()
                with self.assertRaisesRegex(self.bu.StepError, message):
                    self.bu.cmd_codebuild_images(None)
                self.assertEqual(self.w.pushed, [], "refused before any push")
        # Anything that is neither an engine nor the router is refused too.
        self.w.dry_run = DRY_RUN + "x: 928413605543.dkr.ecr.us-west-1.amazonaws.com/games/other:v-%s\n" % SHA12
        with self.assertRaisesRegex(self.bu.StepError, "neither an engine nor the router"):
            self.bu.cmd_codebuild_images(None)

    def test_router_inputs_follow_exactly_the_files_its_dockerfile_copies(self):
        before = self.bu.dockerfile_inputs("server/mp-router/Dockerfile")
        self.assertEqual(self.bu.dockerfile_inputs("server/mp-router/Dockerfile"), before)
        Path("server/mptest/engine.mjs").write_text("// engine only")
        self.assertEqual(self.bu.dockerfile_inputs("server/mp-router/Dockerfile"), before, "an engine file")
        for changed in ("server/mp-router/router.mjs", "lib/multiplayer/token.mjs", "server/mp-router/Dockerfile"):
            with self.subTest(changed):
                old = Path(changed).read_text()
                Path(changed).write_text(old + "\n// changed")
                self.assertNotEqual(self.bu.dockerfile_inputs("server/mp-router/Dockerfile"), before)
                Path(changed).write_text(old)
        Path("server/mp-router/Dockerfile").write_text("FROM x\nCOPY missing.mjs ./\n")
        with self.assertRaisesRegex(self.bu.StepError, "matches nothing"):
            self.bu.dockerfile_inputs("server/mp-router/Dockerfile")

    def test_the_router_inputs_of_the_real_dockerfile_shape(self):
        # colton-games' router Dockerfile: two COPY lines, no stage copies.
        Path("server/mp-router/Dockerfile").write_text(
            "FROM node:24-alpine\nENV A=1\nWORKDIR /app\n"
            "COPY lib/multiplayer/token.mjs lib/x.mjs ./lib/multiplayer/\n"
            "COPY server/mp-router/router.mjs server/mp-router/server.mjs ./server/mp-router/\n"
            "COPY --from=deps /build/node_modules ./node_modules\nUSER node\n")
        self.assertRegex(self.bu.dockerfile_inputs("server/mp-router/Dockerfile"), r"^[0-9a-f]{64}$")


class VercelTests(Base):
    def test_oidc_already_team_mode_is_left_alone(self):
        self.bu.cmd_vercel_oidc(args())
        self.assertFalse(any(m == "PATCH" for m, _, _ in self.w.http_log))

    def test_oidc_global_mode_is_patched_and_verified(self):
        self.w.oidc = {"enabled": True, "issuerMode": "global"}
        self.bu.cmd_vercel_oidc(args())
        patches = [b for m, _, b in self.w.http_log if m == "PATCH"]
        self.assertEqual(patches, [{"oidcTokenConfig": {"enabled": True, "issuerMode": "team"}}])
        self.assertEqual(self.w.oidc["issuerMode"], "team")

    def test_oidc_patch_that_does_not_stick_fails(self):
        self.w.oidc = {"enabled": False}
        self.w.oidc_patch_effective = False
        with self.assertRaisesRegex(self.bu.StepError, "still"):
            self.bu.cmd_vercel_oidc(args())

    def prod_envs(self):
        self.w.vercel_envs = [
            {"id": "e1", "key": "SUPABASE_URL", "target": ["development", "production"]},
            {"id": "e2", "key": "SUPABASE_SECRET_KEY", "target": ["development", "production"]},
        ]
        self.w.decrypt = {"e1": "https://kmfdnctkbdpcagukwqro.supabase.co", "e2": SERVICE_KEY}

    def writes(self):
        return [(m, u, b) for m, u, b in self.w.http_log if m in ("POST", "PATCH")]

    def test_preview_copy_creates_preview_only_variables(self):
        self.prod_envs()
        self.bu.cmd_preview_supabase(args(keys="SUPABASE_URL:encrypted,SUPABASE_SECRET_KEY:sensitive"))
        writes = self.writes()
        self.assertEqual(len(writes), 2)
        for m, u, b in writes:
            self.assertEqual(m, "POST")
            self.assertEqual(b["target"], ["preview"])
        self.assertEqual({b["key"]: b["type"] for _, _, b in writes},
                         {"SUPABASE_URL": "encrypted", "SUPABASE_SECRET_KEY": "sensitive"})
        self.assertNoSecretsLeaked()

    def test_preview_copy_refreshes_its_own_variable(self):
        self.prod_envs()
        self.w.vercel_envs.append({"id": "p1", "key": "SUPABASE_SECRET_KEY", "target": ["preview"]})
        self.bu.cmd_preview_supabase(args(keys="SUPABASE_SECRET_KEY:sensitive"))
        (m, u, b), = self.writes()
        self.assertEqual(m, "PATCH")
        self.assertTrue(u.split("?")[0].endswith("/env/p1"))
        self.assertEqual(b["target"], ["preview"])

    def test_preview_copy_never_edits_a_variable_shared_with_production(self):
        self.prod_envs()
        self.w.vercel_envs.append({"id": "x", "key": "SUPABASE_URL", "target": ["preview", "production"]})
        with self.assertRaisesRegex(self.bu.StepError, "refusing"):
            self.bu.cmd_preview_supabase(args(keys="SUPABASE_URL:encrypted"))
        self.assertEqual(self.writes(), [])

    def test_undecryptable_production_value_fails_without_writing(self):
        # The preflight proves this at plan time; at apply it is a hard failure, not a skip.
        self.prod_envs()
        self.w.decrypt["e2"] = None
        with self.assertRaisesRegex(self.bu.StepError, "SUPABASE_SECRET_KEY"):
            self.bu.cmd_preview_supabase(args(keys="SUPABASE_SECRET_KEY:sensitive"))
        self.assertEqual(self.writes(), [])


class MigrateTests(Base):
    CA = str(GM / "supabase-root-2021-ca.crt")

    def setUp(self):
        super().setUp()
        self.w.vercel_envs = [{"id": "pg", "key": "POSTGRES_URL_NON_POOLING", "target": ["development", "production"]}]
        self.w.decrypt = {"pg": DB_URL}
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        os.makedirs(".games-mp")
        os.makedirs("supabase/migrations")
        Path(".games-mp/SOURCE_REF").write_text(REF + "\n")
        Path(self.bu.CA_PATH).write_text(Path(self.CA).read_text())
        Path("supabase/migrations/20260921000000_skyhook.sql").write_text("CREATE SCHEMA skyhook_private;")
        Path("supabase/migrations/20260926000000_mp.sql").write_text(MIGRATION)
        os.environ.update(GAMES_MP_COMMIT=REF, GAMES_MP_DB_URL=DB_URL)

    def tearDown(self):
        os.chdir(self.cwd)
        os.environ.pop("GAMES_MP_DB_URL", None)
        super().tearDown()

    def migrate(self):
        os.environ["GAMES_MP_DB_URL"] = DB_URL
        self.bu.cmd_codebuild_migrate(None)
        return dict(re.findall(r"^([A-Z_]+)=(.*)$", Path(".games-mp/exports.sh").read_text(), re.M))

    def test_pinned_ca_and_tampering(self):
        self.bu.check_ca(self.CA)
        bad = Path(self.tmp.name, "bad.crt")
        bad.write_text(Path(self.CA).read_text().replace("MII", "MIJ", 1))
        with self.assertRaises(Exception):
            self.bu.check_ca(str(bad))

    def test_pg_env_forces_verify_full_and_decodes_credentials(self):
        env = self.bu.pg_env(DB_URL, "/ca.crt")
        self.assertEqual(env["PGSSLMODE"], "verify-full")
        self.assertEqual(env["PGSSLROOTCERT"], "/ca.crt")
        self.assertEqual(env["PGPASSWORD"], DB_PASSWORD)
        self.assertEqual(env["PGUSER"], "postgres.kmfdnctkbdpcagukwqro")
        self.assertEqual(env["PGHOST"], "aws-0-us-east-1.pooler.supabase.com")
        with self.assertRaises(self.bu.StepError):
            self.bu.pg_env("mysql://a:b@h/db", "/ca")

    def test_revision_marker_is_required_and_other_schemas_refused(self):
        self.assertEqual(self.bu.migration_revision(MIGRATION), 1)
        self.assertEqual(self.bu.migration_revision(MIGRATION_2), 2)
        with self.assertRaises(self.bu.StepError):
            self.bu.migration_revision("CREATE SCHEMA mp_private;")
        with self.assertRaisesRegex(self.bu.StepError, "skyhook_private"):
            self.bu.migration_revision("-- skyhook_private\n" + MIGRATION)

    def test_only_mp_migrations_in_order_one_to_n(self):
        self.assertEqual([r for r, _, _ in self.bu.mp_migrations(".")], [1])
        Path("supabase/migrations/20261001000000_mp_note.sql").write_text(MIGRATION_2)
        self.assertEqual([(r, p) for r, p, _ in self.bu.mp_migrations(".")],
                         [(1, "supabase/migrations/20260926000000_mp.sql"),
                          (2, "supabase/migrations/20261001000000_mp_note.sql")])
        Path("supabase/migrations/20250101000000_early.sql").write_text(MIGRATION_2)
        with self.assertRaisesRegex(self.bu.StepError, "1..n"):
            self.bu.mp_migrations(".")

    def test_fresh_database_gets_each_migration_once_verified(self):
        Path("supabase/migrations/20261001000000_mp_note.sql").write_text(MIGRATION_2)
        self.assertEqual(self.migrate()["GAMES_MP_DB_REVISION"], "2")
        self.assertEqual(self.w.psql_files, [MIGRATION, MIGRATION_2])
        self.assertTrue(all(e["PGSSLMODE"] == "verify-full" for e in self.w.envs))
        self.assertTrue(all(e["PGSSLROOTCERT"].endswith(self.bu.CA_PATH) for e in self.w.envs))
        self.assertIn("revision 2 (verified)", self.out.getvalue())
        self.assertNoSecretsLeaked()
        self.assertNotIn("GAMES_MP_DB_URL", os.environ, "taken out of the environment before psql runs")
        # A second run finds it applied and runs nothing.
        self.assertEqual(self.migrate()["GAMES_MP_DB_REVISION"], "2")
        self.assertEqual(len(self.w.psql_files), 2)

    def test_a_database_one_behind_gets_only_the_new_one(self):
        self.w.db_revision = 1
        Path("supabase/migrations/20261001000000_mp_note.sql").write_text(MIGRATION_2)
        self.migrate()
        self.assertEqual(self.w.psql_files, [MIGRATION_2])

    def test_partial_or_newer_states_are_refused(self):
        for rev, msg in ((-1, "missing"), (-2, "empty"), (2, "newer than this commit")):
            with self.subTest(rev=rev):
                self.w.db_revision = rev
                with self.assertRaisesRegex(self.bu.StepError, msg):
                    self.migrate()
        self.assertEqual(self.w.psql_files, [])

    def test_the_database_url_is_copied_from_vercel_on_stdin_only_when_it_changed(self):
        sync = args(secret_id="games/mp-db-url", url_key="POSTGRES_URL_NON_POOLING")
        self.bu.cmd_sync_db_url(sync)
        self.assertEqual(self.w.secrets["games/mp-db-url"], DB_URL)
        puts = [c for c in self.w.calls if "put-secret-value" in c]
        self.assertEqual(len(puts), 1)
        self.bu.cmd_sync_db_url(sync)
        self.assertEqual(len([c for c in self.w.calls if "put-secret-value" in c]), 1, "unchanged: no write")
        self.assertNoSecretsLeaked()
        self.w.decrypt = {"pg": None}
        with self.assertRaisesRegex(self.bu.StepError, "no longer decryptable"):
            self.bu.cmd_sync_db_url(sync)


class BootstrapTests(Base):
    REPO_URL = "928413605543.dkr.ecr.us-west-1.amazonaws.com/games/mptest-engine"
    GAMES = {"mptest": {"family": "games-mptest", "repositoryUrl": REPO_URL}}

    def td(self, n, image, marker=True, family="games-mptest"):
        arn = "arn:aws:ecs:us-west-1:928413605543:task-definition/%s:%d" % (family, n)
        env = [{"name": "MP_TOKEN_VERIFIER", "value": "ed25519-v2"}] if marker else []
        self.w.task_definitions[arn] = {"taskDefinitionArn": arn, "containerDefinitions": [
            {"name": "engine", "image": image, "environment": env}]}
        return arn

    def test_current_release_is_the_newest_verifying_production_revision_once(self):
        self.td(3, "%s:mptest-1-%s" % (self.REPO_URL, SHA12))
        self.td(4, "%s:mptest-1-%s" % (self.REPO_URL, "a" * 12), marker=False)
        self.td(9, "%s:mptest-1-%s" % (self.REPO_URL, "b" * 12), family="games-mptest2")
        rb = args(table="games-mp-releases", games=json.dumps(self.GAMES))
        self.bu.cmd_releases_bootstrap(rb)
        item = self.w.ddb["current#main"]
        self.assertEqual(item["games"]["M"]["mptest"]["M"]["taskDefinition"]["S"],
                         "arn:aws:ecs:us-west-1:928413605543:task-definition/games-mptest:3")
        self.assertEqual(item["games"]["M"]["mptest"]["M"]["simVersion"]["S"], "mptest-1")
        self.assertEqual(item["seq"]["N"], "0", "any automatic release outranks it")
        # Written once: a second apply leaves whatever the releases made of it.
        self.w.ddb["current#main"]["commit"] = {"S": "later"}
        self.bu.cmd_releases_bootstrap(rb)
        self.assertEqual(self.w.ddb["current#main"]["commit"]["S"], "later")

    def test_nothing_registered_writes_nothing(self):
        self.bu.cmd_releases_bootstrap(args(table="games-mp-releases", games=json.dumps(self.GAMES)))
        self.assertEqual(self.w.ddb, {})


class RouterLiveTests(Base):
    def rargs(self):
        return args(cluster="games", service="mp-router", repository="games/mp-router", tag="live",
                    timeout=60, poll=20)

    def test_live_starts_as_the_image_the_service_runs(self):
        td = "arn:aws:ecs:us-west-1:1:task-definition/games-mp-router:4"
        self.w.services = [{"services": [{"status": "ACTIVE", "taskDefinition": td}]}]
        self.w.task_definitions[td] = {"containerDefinitions": [
            {"image": "928413605543.dkr.ecr.us-west-1.amazonaws.com/games/mp-router:" + SHA12}]}
        self.w.manifests[("games/mp-router", SHA12)] = '{"m": 1}'
        self.bu.cmd_router_live(self.rargs())
        self.assertEqual(self.w.put_images, [("games/mp-router", "live", '{"m": 1}')])
        self.bu.cmd_router_live(self.rargs())
        self.assertEqual(len(self.w.put_images), 1, "once")

    def test_without_a_service_it_waits_for_the_first_release(self):
        self.w.services = [{"services": [], "failures": [{"reason": "MISSING"}]}]
        with self.assertRaisesRegex(self.bu.StepError, "never appeared"):
            self.bu.cmd_router_live(self.rargs())
        self.assertEqual(self.w.put_images, [])


class PreflightTests(Base):
    QUERY = {"region": "us-west-1", "team_id": "team_x", "project_id": "prj_x",
             "vercel_token_secret": "vercel-api-token",
             "copy_keys": "SUPABASE_URL,NEXT_PUBLIC_SUPABASE_URL,SUPABASE_SECRET_KEY",
             "url_key": "POSTGRES_URL_NON_POOLING",
             "repo": "CoderColton/colton-games", "github_pat_secret": "games/colton-games-read"}

    def setUp(self):
        super().setUp()
        self.w.vercel_envs = [
            {"id": "e1", "key": "SUPABASE_URL", "target": ["development", "production"]},
            {"id": "e3", "key": "NEXT_PUBLIC_SUPABASE_URL", "target": ["development", "production"]},
            {"id": "e2", "key": "SUPABASE_SECRET_KEY", "target": ["development", "production"]},
            {"id": "pg", "key": "POSTGRES_URL_NON_POOLING", "target": ["development", "production"]},
        ]
        self.w.decrypt = {"e1": "https://x.supabase.co", "e3": "https://x.supabase.co", "e2": SERVICE_KEY, "pg": DB_URL}

    def run_preflight(self, **overrides):
        q = dict(self.QUERY, **overrides)
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdin
        sys.stdin = io.StringIO(json.dumps(q))
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.bu.main(["preflight"])
        finally:
            sys.stdin = old
        for secret in (VERCEL_TOKEN, GITHUB_PAT, DB_PASSWORD, SERVICE_KEY, "s3cr3t-p%40ss%2Fword"):
            self.assertNotIn(secret, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_all_good_returns_strings_only_and_makes_no_writes(self):
        code, out, err = self.run_preflight()
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result, {"token_scope": "team", "db_host": "aws-0-us-east-1.pooler.supabase.com",
                                   "oidc_state": '{"enabled": true, "issuerMode": "team"}'})
        self.assertTrue(all(isinstance(v, str) for v in result.values()))
        self.assertEqual([m for m, _, _ in self.w.http_log if m != "GET"], [])
        self.assertFalse(any(c[:2] in (["aws", "s3"], ["aws", "codebuild"]) or "put-secret-value" in c
                             for c in self.w.calls))

    def test_undecryptable_database_url_fails_the_plan(self):
        self.w.decrypt["pg"] = None
        code, out, err = self.run_preflight()
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("nothing has been changed", err)
        self.assertIn("cannot decrypt POSTGRES_URL_NON_POOLING", err)

    def test_non_postgres_database_url_fails_the_plan(self):
        self.w.decrypt["pg"] = "https://not-a-database"
        code, _, err = self.run_preflight()
        self.assertEqual(code, 1)
        self.assertIn("not a postgres:// URL", err)

    def test_every_problem_is_reported_at_once(self):
        self.w.decrypt["e2"] = None
        self.w.vercel_envs.append({"id": "m", "key": "SUPABASE_URL", "target": ["preview", "production"]})
        self.w.github_commit = 404
        self.w.token_meta = (200, {"token": {"revokedAt": 1, "scopes": []}})
        code, _, err = self.run_preflight()
        self.assertEqual(code, 1)
        for needle in ("cannot decrypt SUPABASE_SECRET_KEY", "refusing to edit", "GitHub:", "revoked"):
            self.assertIn(needle, err)

    def test_token_scope_rules(self):
        now = 10 ** 13
        v = self.bu.Vercel(VERCEL_TOKEN, "team_x", "prj_x")
        cases = [
            ((200, {"token": {"scopes": [{"type": "team", "teamId": "team_x"}]}}), ("team", None)),
            ((200, {"token": {"scopes": [{"type": "user"}]}}), ("user", None)),
            ((200, {"token": {"type": "token", "scopes": []}}), ("user", None)),
            ((403, {"error": {"code": "forbidden"}}), ("unverifiable", None)),
        ]
        for meta, want in cases:
            self.w.token_meta = meta
            self.assertEqual(self.bu.token_scope(v, "team_x", now), want)
        for tok, needle in (({"scopes": [{"type": "team", "teamId": "other"}]}, "not scoped"),
                            ({"expiresAt": now + 1000, "scopes": [{"type": "user"}]}, "expires"),
                            ({"leakedAt": 1, "scopes": [{"type": "user"}]}, "leaked")):
            self.w.token_meta = (200, {"token": tok})
            self.assertIn(needle, self.bu.token_scope(v, "team_x", now)[1])

    def test_unreadable_project_fails(self):
        orig = self.w.http

        def forbidden(method, url, headers=None, body=None, timeout=30):
            if "/v9/projects/" in url:
                return 403, {"error": {"code": "forbidden"}}
            return orig(method, url, headers, body, timeout)
        self.bu.http = forbidden
        code, _, err = self.run_preflight()
        self.assertEqual(code, 1)
        self.assertIn("cannot read the Vercel project", err)

    def test_the_poller_token_must_read_main(self):
        del self.w.secrets["games/colton-games-read"]
        code, _, err = self.run_preflight()
        self.assertEqual(code, 1)
        self.assertIn("games/colton-games-read has no value", err)


class HealthTests(Base):
    TD = "arn:aws:ecs:us-west-1:1:task-definition/games-mp-router:7"
    OLD = "arn:aws:ecs:us-west-1:1:task-definition/games-mp-router:6"

    def svc(self, *deployments):
        return {"services": [{"deployments": list(deployments)}]}

    def dep(self, td, status="PRIMARY", rollout="IN_PROGRESS", running=2, desired=2, id="ecs-svc/new"):
        return {"id": id, "status": status, "taskDefinition": td, "rolloutState": rollout,
                "runningCount": running, "desiredCount": desired}

    def test_rollout_states(self):
        rs = self.bu.rollout_state
        self.assertEqual(rs(self.svc(self.dep(self.TD, running=1)), self.TD), "wait")
        # Ready before COMPLETED: the old tasks drain for up to an hour after this.
        self.assertEqual(rs(self.svc(self.dep(self.TD), self.dep(self.OLD, "ACTIVE", "COMPLETED", id="ecs-svc/old")),
                            self.TD), "ready")
        self.assertIn("FAILED", rs(self.svc(self.dep(self.OLD), self.dep(self.TD, "ACTIVE", "FAILED")), self.TD))
        self.assertIn("rolled back", rs(self.svc(self.dep(self.OLD, rollout="COMPLETED")), self.TD))
        self.assertIn("FAILED", rs(self.svc(self.dep(self.TD, rollout="FAILED")), self.TD))
        self.assertEqual(rs({"services": []}, self.TD), "service not found")

    def hargs(self):
        return args(cluster="games", service="mp-router", task_definition_arn=self.TD, target_group_arn="tg",
                    alb_dns="alb.example", host="play.cc-games.app", timeout=120, poll=15)

    def test_waits_for_the_new_tasks_to_be_healthy_targets_and_healthz(self):
        self.w.services = [self.svc(self.dep(self.TD, running=0)), self.svc(self.dep(self.TD))]
        self.w.tasks = {"ecs-svc/new": ["10.0.66.5", "10.0.67.5"]}
        # The old tasks are draining; one new one is still initial at first.
        self.w.target_health = [{"10.0.66.9": "draining", "10.0.66.5": "healthy", "10.0.67.5": "initial"},
                                {"10.0.66.9": "draining", "10.0.66.5": "healthy", "10.0.67.5": "healthy"}]
        answers = [OSError("refused"), (503, b"no"), (200, b"ok\n")]

        def fake(address, host):
            self.assertEqual((address, host), ("alb.example", "play.cc-games.app"))
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a
        self.bu.healthz_via = fake
        self.bu.socket = types.SimpleNamespace(getaddrinfo=lambda *a: [1])
        self.bu.cmd_wait_healthy(self.hargs())
        self.assertIn("all healthy targets", self.out.getvalue())
        self.assertIn("200 ok", self.out.getvalue())

    def test_never_healthy_fails_loudly(self):
        self.w.services = [self.svc(self.dep(self.TD))]
        self.w.tasks = {"ecs-svc/new": ["10.0.66.5", "10.0.67.5"]}
        self.w.target_health = {"10.0.66.5": "unhealthy", "10.0.67.5": "healthy"}
        with self.assertRaisesRegex(self.bu.StepError, "not running and healthy"):
            self.bu.cmd_wait_healthy(self.hargs())

    def test_dechunk(self):
        self.assertEqual(self.bu.dechunk(b"2\r\nok\r\n0\r\n\r\n"), b"ok")


class TerraformWiringTests(unittest.TestCase):
    def setUp(self):
        self.tf = (ROOT / "games-multiplayer.tf").read_text()
        self.bu = (ROOT / "games-multiplayer-bringup.tf").read_text()

    def block(self, text, kind, name):
        return re.search(r'resource "%s" "%s" \{.*?\n\}' % (kind, name), text, re.S).group()

    def test_no_image_commit_is_pinned_in_terraform(self):
        for name in ("games_mp_source_ref", "games_mp_sim_versions", "games_mp_build", "mp_engine_image_tags",
                     "mp_router_image_tag", "games_mp_migrate"):
            for text in (self.tf, self.bu):
                self.assertNotIn('variable "%s"' % name, text)
        td = self.block(self.tf, "aws_ecs_task_definition", "games_mp_router")
        self.assertIn('image        = "${aws_ecr_repository.games_mp["games/mp-router"].repository_url}:'
                      '${local.mp_router_live_tag}"', td)
        self.assertIn('mp_router_live_tag = "live"', self.tf)
        self.assertIn("depends_on = [terraform_data.games_mp_router_live]", td)

    def test_codebuild_role_holds_no_credentials(self):
        policy = self.block(self.bu, "aws_iam_role_policy", "games_mp_codebuild")
        for forbidden in ("secretsmanager", "iam:", "ecs:", "sts:"):
            self.assertNotIn(forbidden, policy)
        # Main's code, pushing only production repositories (the engine repository and the
        # router's); never the preview ones.
        self.assertIn("Resource = local.games_mp_production_repo_arns", policy)
        self.assertIn('"${aws_s3_bucket.games_mp_build.arn}/sources/main/*"', policy)
        project = self.block(self.bu, "aws_codebuild_project", "games_mp_images")
        self.assertIn('type      = "S3"', project)
        self.assertRegex(project, r'name  = "GAMES_MP_CHANNEL"\n      value = "main"')
        self.assertIn('"ARM_CONTAINER"', project)
        self.assertIn("privileged_mode             = true", project)

    def test_development_gets_no_vercel_env_and_mp_api_is_production_only(self):
        env_block = self.bu[self.bu.index("games_mp_vercel_env = merge("):self.bu.index("games_mp_vercel_secret_values = {")]
        self.assertNotIn('"development"', env_block)
        self.assertIn('"MP_API/production"', env_block)
        self.assertNotIn('"MP_API/preview"', env_block)

    def test_every_step_calls_a_real_subcommand(self):
        subcommands = set(re.findall(r'"([a-z-]+)": cmd_', (GM / "bringup.py").read_text()))
        deploy = (ROOT / "games-multiplayer-deploy.tf").read_text()
        used = set(re.findall(r"bringup\} ([a-z-]+) ", self.bu + deploy))
        self.assertTrue(used)
        self.assertLessEqual(used, subcommands)
        self.assertIn("python3 .games-mp/bringup.py codebuild-images", (GM / "buildspec.yml").read_text())
        self.assertIn("python3 .games-mp/bringup.py codebuild-migrate", (GM / "buildspec-migrate.yml").read_text())

    def test_preflight_runs_at_plan_time_and_gates_every_step(self):
        data = re.search(r'data "external" "games_mp_preflight" \{.*?\n\}', self.bu, re.S).group()
        # A managed-resource reference in the query would defer the read to apply time.
        self.assertNotRegex(data, r"\b(aws|vercel|terraform_data|random)_[a-z0-9_]+\.")
        deploy = (ROOT / "games-multiplayer-deploy.tf").read_text()
        for text, kind, name in ((self.bu, "terraform_data", "games_mp_vercel_oidc"),
                                 (self.bu, "terraform_data", "games_mp_preview_supabase"),
                                 (deploy, "terraform_data", "games_mp_db_url"),
                                 (self.bu, "vercel_project_environment_variable", "games_mp"),
                                 (self.bu, "vercel_project_protection_bypass", "games_mp")):
            self.assertIn("data.external.games_mp_preflight", self.block(text, kind, name))
        self.assertNotIn("supabase-db-url", self.bu)

    def test_oidc_recheck_is_retriggered_by_live_drift_not_a_constant(self):
        # Regression: triggers_replace used to be {project, mode}, both literals that never
        # change, so the OIDC fix-it provisioner would never run a second time no matter how
        # far Vercel's setting drifted after the first apply. It must depend on something the
        # plan-time preflight actually reads live, so a plan notices drift and reruns it.
        oidc = self.block(self.bu, "terraform_data", "games_mp_vercel_oidc")
        self.assertIn("data.external.games_mp_preflight.result.oidc_state", oidc)

    def test_source_download_uses_only_the_colton_games_read_token(self):
        # github-pat-ejc3 is the dev boxes' credential: every dev box can read it, and as a
        # fine-grained ejc3 token it cannot reach CoderColton's personal repo at all.
        self.assertNotIn('"github-pat-ejc3"', self.bu)  # never a value; the comments explain why
        self.assertIn("github_pat_secret   = local.games_mp_github_read_secret", self.bu)
        deploy = (ROOT / "games-multiplayer-deploy.tf").read_text()
        self.assertIn("TOKEN_SECRET    = aws_secretsmanager_secret.games_mp_github_read.name", deploy)
        policy = self.block(self.bu, "aws_secretsmanager_secret_policy", "games_mp_github_read")
        # Administration and the poller only: no CodeBuild role, no dev box.
        self.assertIn("concat(local.games_mp_admin_principals, [aws_iam_role.games_mp_poller.arn])", policy)

    def test_a_key_rotation_is_a_new_router_task_definition(self):
        # The router's public keys are in its environment, so a rotation changes the task
        # definition ARN, which is what the health step tracks (a rollback cannot pass for
        # the new keys).
        td = self.block(self.tf, "aws_ecs_task_definition", "games_mp_router")
        self.assertIn('{ name = "MP_TOKEN_PUBLIC_KEYS", value = local.games_mp_token_public_keys }', td)
        healthy = self.block(self.bu, "terraform_data", "games_mp_healthy")
        self.assertIn("aws_ecs_task_definition.games_mp_router[0].arn", healthy)


def all_tf():
    return {p.name: p.read_text() for p in sorted(ROOT.glob("*.tf"))}


def local_expr(text, name):
    """The expression of `local.<name>` (from `  name =` up to the next top-level local)."""
    m = re.search(r"^  %s\s*=\s*(.*?)(?=^  (?:#|[a-z_]+\s*=)|^\}\s*$)" % re.escape(name), text, re.S | re.M)
    if not m:
        raise AssertionError("local.%s not found" % name)
    return m.group(1)


class JoinTokenKeyTests(unittest.TestCase):
    """Ed25519 join tokens: the lobby holds each environment's private keys, the router
    only public ones, and nothing on AWS can read a private key (ejc3/aws#173)."""

    def setUp(self):
        self.tf = (ROOT / "games-multiplayer.tf").read_text()
        self.bu = (ROOT / "games-multiplayer-bringup.tf").read_text()
        self.block = TerraformWiringTests.block.__get__(self)

    def test_keys_are_ed25519_one_pair_per_environment_and_generation(self):
        key = self.block(self.bu, "tls_private_key", "games_mp_token")
        self.assertIn('algorithm = "ED25519"', key)
        self.assertIn("for_each  = local.games_mp_token_pairs", key)
        self.assertIn('games_mp_token_envs = ["production", "preview"]', self.bu)
        pairs = local_expr(self.bu, "games_mp_token_pairs")
        self.assertIn("setproduct(local.games_mp_token_envs, var.games_mp_token_kids)", pairs)
        self.assertIn('"${pair[0]}-${pair[1]}"', pairs)
        # The shared HMAC key is gone for good.
        self.assertNotIn('"random_bytes" "games_mp_token_key"', self.bu)
        for name, text in all_tf().items():
            self.assertNotIn("MP_TOKEN_KEYS", re.sub(r"#[^\n]*", "", text), name)
            self.assertNotIn("games/mp-token-keys", re.sub(r"#[^\n]*", "", text), name)

    def test_the_router_gets_public_keys_only(self):
        public = local_expr(self.bu, "games_mp_token_public_keys")
        self.assertIn("local.games_mp_token_public_keys_by_env[env]", public)
        self.assertIn("contains(var.mp_router_envs, env)", public)
        by_env = local_expr(self.bu, "games_mp_token_public_keys_by_env")
        self.assertIn("local.games_mp_token_public_der[", by_env)
        for text in (public, by_env):
            for forbidden in ("private", "signing", "tls_private_key"):
                self.assertNotIn(forbidden, text)
        public_der = local_expr(self.bu, "games_mp_token_public_der")
        self.assertIn(".public_key_pem", public_der)
        self.assertNotRegex(public_der, r"private_key_(pem|openssh|pem_pkcs8)")
        # Each public key is bound to its own environment: "<env>:<env>-<kid>:<der>".
        self.assertIn('"${env}:${env}-${kid}:${local.games_mp_token_public_der["${env}-${kid}"]}"', by_env)

        td = re.sub(r"#[^\n]*", "", self.block(self.tf, "aws_ecs_task_definition", "games_mp_router"))
        self.assertNotRegex(td, r"\bsecrets\s*=")
        self.assertNotIn("secretsmanager", td)
        self.assertNotIn("SIGNING", td)
        for token in re.findall(r"local\.[a-z_]+", td):
            self.assertNotIn("private", token)
            self.assertNotIn("signing", token)
        # Nor any engine revision: they get only their environment's public keys, at launch.
        templates = local_expr((ROOT / "games-multiplayer-deploy.tf").read_text(), "games_mp_engine_template")
        self.assertNotIn("games_mp_token", templates)

    def test_private_keys_reach_only_the_vercel_lobby(self):
        # private_key_pem is read in exactly one place, and what is built from it flows only
        # into the Vercel secret values.
        uses = [(n, m.start()) for n, t in all_tf().items()
                for m in re.finditer(r"tls_private_key\.games_mp_token\[[^\]]*\]\.private_key_pem", t)]
        self.assertEqual([n for n, _ in uses], ["games-multiplayer-bringup.tf"])
        self.assertIn("private_key_pem", local_expr(self.bu, "games_mp_token_private_der"))
        refs = {n: len(re.findall(r"local\.games_mp_token_private_der\b", t)) for n, t in all_tf().items()}
        self.assertEqual({n: c for n, c in refs.items() if c}, {"games-multiplayer-bringup.tf": 1})
        self.assertIn("local.games_mp_token_private_der[", local_expr(self.bu, "games_mp_token_signing_keys"))
        signing = [(n, len(re.findall(r"local\.games_mp_token_signing_keys\b", t))) for n, t in all_tf().items()]
        self.assertEqual([x for x in signing if x[1]], [("games-multiplayer-bringup.tf", 2)])
        secret_values = local_expr(self.bu, "games_mp_vercel_secret_values")
        self.assertIn('"MP_TOKEN_SIGNING_KEYS/production"   = local.games_mp_token_signing_keys["production"]', secret_values)
        self.assertIn('"MP_TOKEN_SIGNING_KEYS/preview"      = local.games_mp_token_signing_keys["preview"]', secret_values)
        self.assertNotIn("output", "".join(re.findall(r'output "[^"]*" \{[^}]*games_mp_token[^}]*\}', self.bu)))

    def test_each_vercel_environment_gets_only_its_own_signing_keys(self):
        env = local_expr(self.bu, "games_mp_vercel_env")
        rows = dict(re.findall(r'"(MP_TOKEN_SIGNING_KEYS/[a-z]+)"\s*=\s*(\{[^}]*\})', env))
        self.assertEqual(sorted(rows), ["MP_TOKEN_SIGNING_KEYS/preview", "MP_TOKEN_SIGNING_KEYS/production"])
        self.assertEqual(rows["MP_TOKEN_SIGNING_KEYS/production"],
                         '{ targets = ["production"], sensitive = true, value = null }')
        self.assertEqual(rows["MP_TOKEN_SIGNING_KEYS/preview"],
                         '{ targets = ["preview"], sensitive = true, value = null }')
        signing = local_expr(self.bu, "games_mp_token_signing_keys")
        # An environment's value lists only that environment's keys.
        self.assertIn('for env in local.games_mp_token_envs : env => join(",", [', signing)
        self.assertIn('"${env}-${kid}:${local.games_mp_token_private_der["${env}-${kid}"]}"', signing)

    def test_the_router_role_cannot_read_any_secret(self):
        role = self.block(self.tf, "aws_iam_role_policy", "games_mp_router_execution")
        self.assertNotIn("secretsmanager", role)
        self.assertNotIn("kms:", role)
        self.assertNotIn("ssm:", role)
        actions = re.findall(r'Action\s*=\s*(\[[^\]]*\]|"[^"]*")', role)
        self.assertEqual(sorted(re.findall(r'"([a-z0-9]+:[A-Za-z*]+)"', " ".join(actions))), sorted([
            "ecr:GetAuthorizationToken", "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage",
            "ecr:GetDownloadUrlForLayer", "logs:CreateLogStream", "logs:PutLogEvents"]))
        task = self.block(self.tf, "aws_iam_role", "games_mp_router_task")
        self.assertIn("Deliberately has no policies", task)
        for name, text in all_tf().items():
            self.assertNotRegex(text, r'role\s*=\s*aws_iam_role\.games_mp_router_task\.', name)
            self.assertNotIn("aws_iam_role.games_mp_router_execution.arn]", text, name)


if __name__ == "__main__":
    unittest.main(verbosity=2)

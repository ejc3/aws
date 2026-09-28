#!/usr/bin/env python3
"""Offline checks for games-multiplayer/bringup.py, the steps `terraform apply` runs.

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
EXPECT = {"games/mp-router": SHA12, "games/mptest-engine": "mptest-1-" + SHA12}
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
        if argv[:3] == ["aws", "ecr", "describe-images"]:
            repo = argv[argv.index("--repository-name") + 1]
            tag = argv[argv.index("--image-ids") + 1].split("=", 1)[1]
            if (repo, tag) in self.ecr:
                return done(json.dumps({"imageDetails": [{"imageTags": [tag]}]}))
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
                self.ecr.update(EXPECT.items())
            return done(json.dumps({"builds": [{"buildStatus": status, "logs": {"deepLink": "https://logs/x"}}]}))
        if argv[:3] == ["aws", "ecs", "describe-services"]:
            return done(json.dumps(self.services.pop(0) if len(self.services) > 1 else self.services[0]))
        if argv[:3] == ["aws", "elbv2", "describe-target-health"]:
            st = self.target_states.pop(0) if len(self.target_states) > 1 else self.target_states[0]
            return done(json.dumps({"TargetHealthDescriptions": [{"TargetHealth": {"State": st}}]}))
        if argv[:2] == ["node", "scripts/mp-images.mjs"]:
            if "--dry-run" in argv:
                return done(self.dry_run)
            name = argv[argv.index("--only") + 1]
            repo = {"router": "games/mp-router", "mptest": "games/mptest-engine"}[name]
            self.ecr.add((repo, EXPECT[repo]))
            return done("")
        if argv[:2] == ["docker", "pull"] or argv[:2] == ["docker", "tag"]:
            return done("")
        if argv[0] == "psql":
            # db_revision: 0 = fresh, -1 = schema without marker table, -2 = empty marker.
            if "-f" in argv:
                self.psql_files.append(Path(argv[argv.index("-f") + 1]).read_text())
                self.db_revision = 1
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
                sha = url.rsplit("/", 1)[1]
                return self.github_commit, ({"sha": sha} if self.github_commit == 200 else {"message": "Not Found"})
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

    def test_repack_strips_top_dir_keeps_modes_and_adds_the_driver(self):
        data = self.bu.repack_zipball(make_zipball(), REF, b"driver")
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        self.assertIn("scripts/mp-images.mjs", names)
        self.assertFalse(any(n.startswith("CoderColton-") for n in names))
        self.assertEqual(z.getinfo("scripts/mp-images.mjs").external_attr >> 16, 0o100644)
        self.assertEqual(z.getinfo("bin/tool").external_attr >> 16, 0o100755)
        self.assertTrue(all(z.getinfo(n).external_attr >> 16 == 0o40755 for n in names if n.endswith("/")))
        self.assertEqual(z.read(".games-mp/bringup.py"), b"driver")
        self.assertEqual(z.read(".games-mp/SOURCE_REF").decode().strip(), REF)

    def test_concurrent_downloads_each_publish_a_complete_file(self):
        # Two steps racing on an empty cache: every temporary file is unique and no .tmp is left.
        seen = []
        real_mkstemp = self.bu.tempfile.mkstemp

        def spy(**kw):
            fd, path = real_mkstemp(**kw)
            seen.append(path)
            return fd, path
        self.bu.tempfile = types.SimpleNamespace(mkstemp=spy, NamedTemporaryFile=tempfile.NamedTemporaryFile)
        p = self.bu.fetch_source(REF, "CoderColton/colton-games", self.cache, "us-west-1", "games/colton-games-read")
        os.unlink(p)
        self.bu.fetch_source(REF, "CoderColton/colton-games", self.cache, "us-west-1", "games/colton-games-read")
        self.assertEqual(len(set(seen)), 2)
        self.assertEqual([n for n in os.listdir(self.cache) if n.endswith(".tmp")], [])

    def test_source_is_downloaded_once_per_commit(self):
        p1 = self.bu.fetch_source(REF, "CoderColton/colton-games", self.cache, "us-west-1", "games/colton-games-read")
        p2 = self.bu.fetch_source(REF, "CoderColton/colton-games", self.cache, "us-west-1", "games/colton-games-read")
        self.assertEqual(p1, p2)
        self.assertEqual(len([h for h in self.w.http_log if "github" in h[1]]), 1)
        self.assertNoSecretsLeaked()


class BuildTests(Base):
    def build_args(self):
        return args(project="games-mp-images", bucket="games-mp-build-1", cache_dir=self.cache,
                    expect=json.dumps(EXPECT), timeout=600, poll=15)

    def test_existing_tags_skip_codebuild(self):
        self.w.ecr.update(EXPECT.items())
        self.bu.cmd_build(self.build_args())
        self.assertFalse(any(c[:3] == ["aws", "codebuild", "start-build"] for c in self.w.calls))
        self.assertFalse(any("github" in h[1] for h in self.w.http_log))

    def test_missing_tags_upload_build_wait_verify(self):
        self.w.builds = ["IN_PROGRESS", "IN_PROGRESS", "SUCCEEDED"]
        self.bu.cmd_build(self.build_args())
        cp = [c for c in self.w.calls if c[:3] == ["aws", "s3", "cp"]][0]
        self.assertEqual(cp[4], "s3://games-mp-build-1/sources/%s.zip" % REF)
        req = self.w.start_request
        self.assertEqual(req["sourceLocationOverride"], "games-mp-build-1/sources/%s.zip" % REF)
        env = {e["name"]: e["value"] for e in req["environmentVariablesOverride"]}
        self.assertEqual(json.loads(env["GAMES_MP_EXPECT"]), EXPECT)
        self.assertEqual(env["GAMES_MP_SHA12"], SHA12)
        self.assertNoSecretsLeaked()

    def test_failed_build_fails_with_the_log_link(self):
        self.w.builds = ["FAILED"]
        with self.assertRaisesRegex(self.bu.StepError, "FAILED.*https://logs/x"):
            self.bu.cmd_build(self.build_args())

    def test_success_without_the_tags_is_still_a_failure(self):
        self.w.builds = ["SUCCEEDED"]
        orig = self.w.run

        def no_push(argv, **kw):
            res = orig(argv, **kw)
            if argv[:3] == ["aws", "codebuild", "batch-get-builds"]:
                self.w.ecr.clear()
            return res
        self.bu.RUN = no_push
        with self.assertRaisesRegex(self.bu.StepError, "still lacks"):
            self.bu.cmd_build(self.build_args())

    def test_a_build_that_never_ends_times_out(self):
        self.w.builds = ["IN_PROGRESS"]
        with self.assertRaisesRegex(self.bu.StepError, "still running"):
            self.bu.cmd_build(self.build_args())


class CodeBuildImagesTests(Base):
    def setUp(self):
        super().setUp()
        os.environ.update(GAMES_MP_EXPECT=json.dumps(EXPECT), GAMES_MP_SHA12=SHA12, ACCOUNT_ID="928413605543",
                          AWS_REGION="us-west-1")
        self.cwd = os.getcwd()
        os.chdir(self.tmp.name)
        for d, text in (("server/mp-router", "FROM node:24-alpine\n"),
                        ("server/mptest", "FROM node:24-alpine AS deps\nFROM deps AS x\nFROM node:24-alpine\n")):
            os.makedirs(d)
            Path(d, "Dockerfile").write_text(text)

    def tearDown(self):
        os.chdir(self.cwd)
        super().tearDown()

    def test_parsers_match_the_real_script_output(self):
        got = self.bu.parse_dry_run(DRY_RUN)
        self.assertEqual(got["router"], ("games/mp-router", SHA12, "server/mp-router/Dockerfile"))
        self.assertEqual(got["mptest"], ("games/mptest-engine", "mptest-1-" + SHA12, "server/mptest/Dockerfile"))
        self.assertEqual(self.bu.docker_hub_library_bases("FROM node:24-alpine AS deps\nFROM deps\nFROM scratch\n"
                                                          "FROM public.ecr.aws/x/y:1\nFROM node:24-alpine\n"),
                         ["node:24-alpine"])

    def test_builds_only_the_missing_tag(self):
        self.w.ecr.add(("games/mp-router", SHA12))
        self.bu.cmd_codebuild_images(None)
        builds = [c for c in self.w.calls if c[:2] == ["node", "scripts/mp-images.mjs"] and "--dry-run" not in c]
        self.assertEqual(len(builds), 1)
        self.assertEqual(builds[0][builds[0].index("--only") + 1], "mptest")
        self.assertIn("--sha", builds[0])
        pulls = [c for c in self.w.calls if c[:2] == ["docker", "pull"]]
        self.assertEqual(pulls[0][-1], "public.ecr.aws/docker/library/node:24-alpine")

    def test_everything_present_builds_nothing(self):
        self.w.ecr.update(EXPECT.items())
        self.bu.cmd_codebuild_images(None)
        self.assertEqual([c for c in self.w.calls if c[:2] == ["node", "scripts/mp-images.mjs"] and "--dry-run" not in c], [])

    def test_sim_version_disagreement_fails_before_building(self):
        self.w.dry_run = DRY_RUN.replace("mptest-1-", "mptest-2-")
        with self.assertRaisesRegex(self.bu.StepError, "games_mp_sim_versions"):
            self.bu.cmd_codebuild_images(None)
        self.assertFalse(any("--only" in c for c in self.w.calls))


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

    def margs(self, **kw):
        return args(cache_dir=self.cache, file="supabase/migrations/20260926000000_mp.sql", ca=self.CA,
                    url_key="POSTGRES_URL_NON_POOLING", **kw)

    def setUp(self):
        super().setUp()
        self.bu.shutil = types.SimpleNamespace(which=lambda name: "/usr/bin/" + name)
        self.w.vercel_envs = [{"id": "pg", "key": "POSTGRES_URL_NON_POOLING", "target": ["development", "production"]}]
        self.w.decrypt = {"pg": DB_URL}

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
        with self.assertRaises(self.bu.StepError):
            self.bu.migration_revision("CREATE SCHEMA mp_private;")
        with self.assertRaisesRegex(self.bu.StepError, "skyhook_private"):
            self.bu.migration_revision("-- skyhook_private\n" + MIGRATION)

    def test_fresh_database_gets_the_migration_once_and_is_verified(self):
        self.bu.cmd_migrate(self.margs())
        self.assertEqual(self.w.psql_files, [MIGRATION])
        self.assertTrue(all(e["PGSSLMODE"] == "verify-full" for e in self.w.envs))
        self.assertIn("revision 1 (verified)", self.out.getvalue())
        self.assertNoSecretsLeaked()
        # A second apply finds it applied and runs nothing.
        self.bu.cmd_migrate(self.margs())
        self.assertEqual(len(self.w.psql_files), 1)
        self.assertIn("already applied", self.out.getvalue())

    def test_partial_or_later_states_are_refused(self):
        for rev, msg in ((-1, "missing"), (-2, "empty")):
            self.w.db_revision = rev
            with self.assertRaisesRegex(self.bu.StepError, msg):
                self.bu.cmd_migrate(self.margs())
        self.assertEqual(self.w.psql_files, [])

    def test_newer_database_revision_than_this_migration_is_refused(self):
        # Regression: `have >= want` used to treat a database already past this migration's
        # target as "already applied" and return quietly. That is a rollback: source_ref
        # points at an OLDER commit than what is actually deployed, and running (or silently
        # skipping) its migration against a newer, possibly incompatible schema must fail
        # loudly instead of reporting success.
        self.w.db_revision = 2
        with self.assertRaisesRegex(self.bu.StepError, "newer than this migration"):
            self.bu.cmd_migrate(self.margs())
        self.assertEqual(self.w.psql_files, [])

    def test_url_that_stops_decrypting_after_the_plan_fails(self):
        self.w.decrypt = {"pg": None}
        with self.assertRaisesRegex(self.bu.StepError, "gone since the plan"):
            self.bu.cmd_migrate(self.margs())
        self.assertEqual(self.w.psql_files, [])


class PreflightTests(Base):
    QUERY = {"region": "us-west-1", "team_id": "team_x", "project_id": "prj_x",
             "vercel_token_secret": "vercel-api-token",
             "copy_keys": "SUPABASE_URL,NEXT_PUBLIC_SUPABASE_URL,SUPABASE_SECRET_KEY",
             "url_key": "POSTGRES_URL_NON_POOLING",
             "migrate": "true", "build": "true", "repo": "CoderColton/colton-games", "ref": REF,
             "github_pat_secret": "games/colton-games-read"}

    def setUp(self):
        super().setUp()
        self.bu.shutil = types.SimpleNamespace(which=lambda name: "/usr/bin/" + name)
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
        self.bu.shutil = types.SimpleNamespace(which=lambda name: None)
        code, _, err = self.run_preflight()
        self.assertEqual(code, 1)
        for needle in ("cannot decrypt SUPABASE_SECRET_KEY", "refusing to edit", "GitHub:", "revoked", "psql is missing"):
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

    def test_migrate_and_build_off_skip_their_checks(self):
        self.w.decrypt["pg"] = None
        self.w.github_commit = 404
        self.bu.shutil = types.SimpleNamespace(which=lambda name: None)
        code, out, err = self.run_preflight(migrate="false", build="false")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["db_host"], "none")


class HealthTests(Base):
    TD = "arn:aws:ecs:us-west-1:1:task-definition/games-mp-router:7"
    OLD = "arn:aws:ecs:us-west-1:1:task-definition/games-mp-router:6"

    def svc(self, *deployments):
        return {"services": [{"deployments": list(deployments)}]}

    def test_rollout_states(self):
        rs = self.bu.rollout_state
        self.assertEqual(rs(self.svc({"status": "PRIMARY", "taskDefinition": self.TD, "rolloutState": "IN_PROGRESS"}), self.TD), "wait")
        self.assertEqual(rs(self.svc({"status": "PRIMARY", "taskDefinition": self.TD, "rolloutState": "COMPLETED", "runningCount": 1}), self.TD), "done")
        self.assertIn("FAILED", rs(self.svc({"status": "PRIMARY", "taskDefinition": self.OLD, "rolloutState": "IN_PROGRESS"},
                                            {"status": "ACTIVE", "taskDefinition": self.TD, "rolloutState": "FAILED"}), self.TD))
        self.assertIn("rolled back", rs(self.svc({"status": "PRIMARY", "taskDefinition": self.OLD, "rolloutState": "COMPLETED", "runningCount": 1}), self.TD))
        self.assertEqual(rs({"services": []}, self.TD), "service not found")

    def hargs(self):
        return args(cluster="games", service="mp-router", task_definition_arn=self.TD, target_group_arn="tg",
                    alb_dns="alb.example", host="play.cc-games.app", timeout=120, poll=15)

    def test_waits_for_rollout_target_and_healthz(self):
        self.w.services = [self.svc({"status": "PRIMARY", "taskDefinition": self.TD, "rolloutState": "IN_PROGRESS"}),
                           self.svc({"status": "PRIMARY", "taskDefinition": self.TD, "rolloutState": "COMPLETED", "runningCount": 1})]
        self.w.target_states = ["initial", "healthy"]
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
        self.assertIn("200 ok", self.out.getvalue())

    def test_never_healthy_fails_loudly(self):
        self.w.services = [self.svc({"status": "PRIMARY", "taskDefinition": self.TD, "rolloutState": "COMPLETED", "runningCount": 1})]
        self.w.target_states = ["unhealthy"]
        with self.assertRaisesRegex(self.bu.StepError, "no healthy mp-router target"):
            self.bu.cmd_wait_healthy(self.hargs())

    def test_dechunk(self):
        self.assertEqual(self.bu.dechunk(b"2\r\nok\r\n0\r\n\r\n"), b"ok")


class TerraformWiringTests(unittest.TestCase):
    def setUp(self):
        self.tf = (ROOT / "games-multiplayer.tf").read_text()
        self.bu = (ROOT / "games-multiplayer-bringup.tf").read_text()

    def block(self, text, kind, name):
        return re.search(r'resource "%s" "%s" \{.*?\n\}' % (kind, name), text, re.S).group()

    def test_task_definitions_wait_for_the_build(self):
        for name in ("games_mp_router", "games_engine"):
            self.assertIn("terraform_data.games_mp_build", self.block(self.tf, "aws_ecs_task_definition", name))

    def test_tags_are_derived_from_the_pinned_commit(self):
        self.assertIn("games_mp_sha12 = substr(var.games_mp_source_ref, 0, 12)", self.tf)
        self.assertIn('"${var.games_mp_sim_versions[id]}-${local.games_mp_sha12}"', self.tf)
        self.assertRegex(self.tf, r'default\s*=\s*"1eb83917d994f362fe1e0efe0e06cd022137781b"')

    def test_codebuild_role_holds_no_credentials(self):
        policy = self.block(self.bu, "aws_iam_role_policy", "games_mp_codebuild")
        for forbidden in ("secretsmanager", "iam:", "ecs:", "sts:"):
            self.assertNotIn(forbidden, policy)
        project = self.block(self.bu, "aws_codebuild_project", "games_mp_images")
        self.assertIn('type      = "S3"', project)
        self.assertIn('"ARM_CONTAINER"', project)
        self.assertIn("privileged_mode             = true", project)

    def test_development_gets_no_vercel_env_and_mp_api_is_production_only(self):
        env_block = self.bu[self.bu.index("games_mp_vercel_env = merge("):self.bu.index("games_mp_vercel_secret_values = {")]
        self.assertNotIn('"development"', env_block)
        self.assertIn('"MP_API/production"', env_block)
        self.assertNotIn('"MP_API/preview"', env_block)

    def test_every_step_calls_a_real_subcommand(self):
        subcommands = set(re.findall(r'"([a-z-]+)": cmd_', (GM / "bringup.py").read_text()))
        used = set(re.findall(r"bringup\} ([a-z-]+) ", self.bu))
        self.assertTrue(used)
        self.assertLessEqual(used, subcommands)
        self.assertIn("codebuild-images", (GM / "buildspec.yml").read_text())

    def test_preflight_runs_at_plan_time_and_gates_every_step(self):
        data = re.search(r'data "external" "games_mp_preflight" \{.*?\n\}', self.bu, re.S).group()
        # A managed-resource reference in the query would defer the read to apply time.
        self.assertNotRegex(data, r"\b(aws|vercel|terraform_data|random)_[a-z0-9_]+\.")
        for kind, name in (("terraform_data", "games_mp_build"), ("terraform_data", "games_mp_vercel_oidc"),
                           ("terraform_data", "games_mp_preview_supabase"), ("terraform_data", "games_mp_migration"),
                           ("vercel_project_environment_variable", "games_mp"),
                           ("vercel_project_protection_bypass", "games_mp")):
            self.assertIn("data.external.games_mp_preflight", self.block(self.bu, kind, name))
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
        for step in ("games_mp_build", "games_mp_migration"):
            self.assertIn("--github-pat-secret ${local.games_mp_github_read_secret}",
                          self.block(self.bu, "terraform_data", step))
        policy = self.block(self.bu, "aws_secretsmanager_secret_policy", "games_mp_github_read")
        self.assertIn("local.games_mp_admin_principals", policy)

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
        # Nor any engine: they never see a token.
        engine = self.block(self.tf, "aws_ecs_task_definition", "games_engine")
        self.assertNotIn("games_mp_token", engine)

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

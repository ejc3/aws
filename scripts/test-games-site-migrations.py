#!/usr/bin/env python3
"""The site's migrations in games-multiplayer/bringup.py (games-mp-migrate), against a real
PostgreSQL: every file under supabase/migrations that is not a multiplayer one is applied in
file-name order, each in one transaction with its row in the ledger
(migrations_private.applied), and anything that does not fit is refused before the first
write.

The cluster is this test's own: initdb in a temporary directory, listening on a Unix socket
in that directory only (no TCP port), stopped and removed at the end. It never connects to an
existing database. PostgreSQL 16+ server binaries (initdb, pg_ctl, psql) are required: on
PATH, under /usr/lib/postgresql/<version>/bin, or in the directory SKYHOOK_POSTGRES_BIN names
(the variable the games repository's own SQL tests use).

Run from the repo root:  python3 -S -B scripts/test-games-site-migrations.py
"""
import contextlib
import glob
import hashlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GM = ROOT / "games-multiplayer"
COMMIT = "c76e5bdf083fe32628b5c8fee9e6ab867e291369"
OTHER_COMMIT = "0123456789abcdef0123456789abcdef01234567"
PORT = "55432"  # names the socket file inside the test's own directory; nothing listens on TCP


def load():
    spec = importlib.util.spec_from_file_location("bringup", GM / "bringup.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def postgres_bin():
    on_path = shutil.which("initdb")
    candidates = [os.environ.get("SKYHOOK_POSTGRES_BIN"), os.path.dirname(on_path) if on_path else None]
    candidates += sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    for candidate in candidates:
        if candidate and all(os.path.exists(os.path.join(candidate, tool)) for tool in ("initdb", "pg_ctl", "psql")):
            return candidate
    raise RuntimeError("PostgreSQL server binaries are required (initdb, pg_ctl, psql). Install PostgreSQL 16 or "
                       "set SKYHOOK_POSTGRES_BIN to their directory. This test starts its own temporary cluster.")


CLUSTER = {}


def setUpModule():
    bin_dir = postgres_bin()
    # Short path: a Unix socket's path is limited to about 100 bytes.
    directory = tempfile.mkdtemp(prefix="gsm-", dir="/tmp")
    CLUSTER.update(dir=directory, bin=bin_dir, path=os.environ.get("PATH", ""))
    os.environ["PATH"] = bin_dir + os.pathsep + CLUSTER["path"]
    data = os.path.join(directory, "data")
    quiet = dict(stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    subprocess.run(["initdb", "-D", data, "-U", "postgres", "--auth=trust", "--no-locale", "-E", "UTF8"],
                   check=True, **quiet)
    subprocess.run(["pg_ctl", "-D", data, "-l", os.path.join(directory, "postgres.log"), "-w", "start", "-o",
                    "-F -h '' -k '%s' -p %s -c shared_buffers=16MB -c max_connections=20" % (directory, PORT)],
                   check=True, **quiet)
    CLUSTER["running"] = True
    # The roles a Supabase project has, so the ledger's grants can be checked against them.
    admin("postgres", "CREATE ROLE anon NOLOGIN; CREATE ROLE authenticated NOLOGIN; "
                      "CREATE ROLE service_role NOLOGIN BYPASSRLS;")


def tearDownModule():
    if CLUSTER.get("running"):
        subprocess.run(["pg_ctl", "-D", os.path.join(CLUSTER["dir"], "data"), "-m", "immediate", "-w", "stop"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if CLUSTER.get("dir", "").startswith("/tmp/gsm-"):
        shutil.rmtree(CLUSTER["dir"], ignore_errors=True)
    if "path" in CLUSTER:
        os.environ["PATH"] = CLUSTER["path"]


def admin(database, sql):
    """Runs `sql` as the test itself (not through the driver) and returns its output."""
    proc = subprocess.run(["psql", "-X", "-q", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-h", CLUSTER["dir"], "-p", PORT,
                           "-U", "postgres", "-d", database, "-c", sql], capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError("psql: %s" % proc.stderr.strip())
    return proc.stdout.strip()


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


# Three files "applied by hand before the ledger existed": a made-up shop schema, its second
# step, and an unrelated forum schema. They keep a revision marker the way the real ones do.
SHOP = """-- The shop: one table and its readiness function.
BEGIN;
CREATE SCHEMA shop_private;
CREATE TABLE shop_private.schema_revision (id integer PRIMARY KEY CHECK (id = 1), revision integer NOT NULL);
CREATE TABLE shop_private.items (id integer PRIMARY KEY, title text NOT NULL);
CREATE FUNCTION public.shop_ready() RETURNS boolean LANGUAGE plpgsql AS $$
BEGIN
  RETURN true;  -- a body with its own BEGIN and END; and a COMMIT; in a comment
END;
$$;
INSERT INTO shop_private.schema_revision (id, revision) VALUES (1, 1);
COMMIT;
"""
SHOP_ENVIRONMENTS = """BEGIN;
ALTER TABLE shop_private.items ADD COLUMN environment text NOT NULL DEFAULT 'production';
CREATE FUNCTION public.shop_ready_scoped(p_environment text) RETURNS boolean LANGUAGE sql AS $fn$ SELECT true $fn$;
UPDATE shop_private.schema_revision SET revision = 2 WHERE id = 1;
COMMIT;
"""
FORUM = """BEGIN;
CREATE SCHEMA forum_private;
CREATE TABLE forum_private.schema_revision (id integer PRIMARY KEY CHECK (id = 1), revision integer NOT NULL);
CREATE TABLE forum_private.posts (id integer PRIMARY KEY, body text NOT NULL);
INSERT INTO forum_private.schema_revision (id, revision) VALUES (1, 1);
COMMIT;
"""
BASELINE = {
    "20260101000000_shop.sql": {"sha256": sha(SHOP), "creates": [
        ("table", "shop_private.schema_revision"), ("table", "shop_private.items"),
        ("function", "public.shop_ready"), ("revision", "shop_private.schema_revision", 1)]},
    "20260102000000_shop_environments.sql": {"sha256": sha(SHOP_ENVIRONMENTS), "creates": [
        ("column", "shop_private.items.environment"), ("function", "public.shop_ready_scoped"),
        ("revision", "shop_private.schema_revision", 2)]},
    "20260103000000_forum.sql": {"sha256": sha(FORUM), "creates": [
        ("table", "forum_private.posts"), ("revision", "forum_private.schema_revision", 1)]},
}
# Files that arrive after the ledger: without transaction statements, and with the pair.
SAVES = """CREATE SCHEMA saves_private;
CREATE TABLE saves_private.saves (player text PRIMARY KEY, data text NOT NULL DEFAULT 'it''s; fine');
"""
CLUBS = """BEGIN;
CREATE TABLE saves_private.clubs (id integer PRIMARY KEY);
COMMIT;
"""
# Multiplayer migrations, in the form bringup.py already applies.
MP_1 = """BEGIN;
CREATE SCHEMA mp_private;
CREATE TABLE mp_private.schema_revision (id integer PRIMARY KEY CHECK (id = 1), revision integer NOT NULL);
CREATE TABLE mp_private.matches (id integer PRIMARY KEY);
INSERT INTO mp_private.schema_revision (id, revision) VALUES (1, 1);
COMMIT;
"""
MP_2 = """BEGIN;
ALTER TABLE mp_private.matches ADD COLUMN note text;
UPDATE mp_private.schema_revision SET revision = 2 WHERE id = 1;
COMMIT;
"""
ALL = {
    "20260101000000_shop.sql": SHOP,
    "20260102000000_shop_environments.sql": SHOP_ENVIRONMENTS,
    "20260103000000_forum.sql": FORUM,
    "20260201000000_mp.sql": MP_1,
    "20260301000000_saves.sql": SAVES,
    "20260302000000_mp_note.sql": MP_2,
    "20260401000000_clubs.sql": CLUBS,
}
SITE = [name for name in sorted(ALL) if "_mp" not in name]


class Base(unittest.TestCase):
    count = 0

    def setUp(self):
        self.bu = load()
        self.bu.SITE_BASELINE = BASELINE
        Base.count += 1
        self.db = "t%d" % Base.count
        admin("postgres", "CREATE DATABASE %s" % self.db)
        self.env = {"PGHOST": CLUSTER["dir"], "PGPORT": PORT, "PGUSER": "postgres", "PGDATABASE": self.db,
                    "PGAPPNAME": "test-games-site-migrations"}
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "supabase/migrations"))
        self.files(ALL)
        self.out = io.StringIO()
        self.runs = []  # ("read", sql) for every read, ("write", script) for every file psql was given
        real = self.bu.RUN

        def recording(argv, **kw):
            if argv[0] == "psql" and "-f" in argv:
                self.runs.append(("write", Path(argv[argv.index("-f") + 1]).read_text()))
            elif argv[0] == "psql":
                self.runs.append(("read", [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]))
            return real(argv, **kw)

        self.bu.RUN = recording

    def tearDown(self):
        self.tmp.cleanup()

    def files(self, contents):
        folder = Path(self.root, "supabase/migrations")
        for old in folder.glob("*.sql"):
            old.unlink()
        for name, text in contents.items():
            (folder / name).write_text(text)

    def sql(self, text):
        return admin(self.db, text)

    def by_hand(self, *names):
        for name in names:
            self.sql(ALL[name])

    def migrate(self, commit=COMMIT):
        self.runs.clear()
        with contextlib.redirect_stdout(self.out):
            return self.bu.migrate(self.env, commit, self.root)

    def refused(self, pattern, commit=COMMIT):
        """The run is refused with `pattern`, and it wrote nothing: no file reached psql."""
        with self.assertRaisesRegex(self.bu.StepError, pattern):
            self.migrate(commit)
        self.assertEqual([kind for kind, _ in self.runs if kind == "write"], [])

    def ledger(self):
        if self.sql("SELECT to_regclass('migrations_private.applied') IS NULL") == "t":
            return None
        rows = self.sql("SELECT name, sha256, commit, baseline FROM migrations_private.applied "
                        "ORDER BY name COLLATE \"C\"")
        return [tuple(row.split("|")) for row in rows.splitlines()]

    def exists(self, relation):
        return self.sql("SELECT to_regclass('%s') IS NOT NULL" % relation) == "t"

    def writes(self):
        return [script for kind, script in self.runs if kind == "write"]


class FreshDatabaseTests(Base):
    def test_a_fresh_database_gets_every_file_in_name_order_each_recorded(self):
        exports = self.migrate()
        self.assertEqual(self.ledger(), [(name, sha(ALL[name]), COMMIT, "f") for name in SITE])
        for relation in ("shop_private.items", "forum_private.posts", "saves_private.saves", "saves_private.clubs"):
            self.assertTrue(self.exists(relation), relation)
        self.assertEqual(self.sql("SELECT revision FROM shop_private.schema_revision"), "2")
        self.assertEqual(self.sql("SELECT shop_ready() AND shop_ready_scoped('preview')"), "t")
        # What the release compares: the database now holds exactly this commit's files.
        files = [(name, sha(ALL[name])) for name in SITE]
        self.assertEqual(exports["GAMES_SITE_DB_MIGRATIONS"], self.bu.site_state(files))
        self.assertRegex(exports["GAMES_SITE_DB_MIGRATIONS"], r"^5:[0-9a-f]{64}$")
        self.assertEqual(exports["GAMES_SITE_DB_NEWEST"], "20260401000000_clubs.sql")
        self.assertIn("20260401000000_clubs.sql applied and recorded", self.out.getvalue())

    def test_two_runs_change_nothing_the_second_time(self):
        first = self.migrate()
        before = self.sql("SELECT string_agg(name || applied_at::text, ',' ORDER BY name) FROM migrations_private.applied")
        second = self.migrate()
        self.assertEqual(second, first)
        self.assertEqual(self.writes(), [], "the second run gives psql no file at all")
        after = self.sql("SELECT string_agg(name || applied_at::text, ',' ORDER BY name) FROM migrations_private.applied")
        self.assertEqual(after, before)
        self.assertEqual(self.sql("SELECT revision FROM mp_private.schema_revision"), "2")

    def test_a_later_commit_gets_only_its_new_file(self):
        self.files({name: ALL[name] for name in ALL if name != "20260401000000_clubs.sql"})
        self.migrate()
        self.assertFalse(self.exists("saves_private.clubs"))
        self.files(ALL)
        self.migrate(OTHER_COMMIT)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.ledger()[-1], ("20260401000000_clubs.sql", sha(CLUBS), OTHER_COMMIT, "f"))
        self.assertTrue(self.exists("saves_private.clubs"))

    def test_the_ledger_is_out_of_reach_of_the_sites_database_roles(self):
        self.migrate()
        for role in ("anon", "authenticated", "service_role", "public"):
            self.assertEqual(self.sql("SELECT has_schema_privilege('%s', 'migrations_private', 'USAGE') OR "
                                      "has_table_privilege('%s', 'migrations_private.applied', "
                                      "'SELECT, INSERT, UPDATE, DELETE')" % (role, role)), "f", role)
        self.assertEqual(self.sql("SELECT relrowsecurity FROM pg_class "
                                  "WHERE oid = 'migrations_private.applied'::regclass"), "t")


class BaselineTests(Base):
    """A database whose first files were applied by hand, before any ledger."""

    def test_hand_applied_files_are_recorded_after_their_probes_and_never_run_again(self):
        self.by_hand("20260101000000_shop.sql", "20260102000000_shop_environments.sql", "20260103000000_forum.sql")
        self.sql("INSERT INTO shop_private.items (id, title) VALUES (7, 'kept')")
        self.migrate()
        ledger = self.ledger()
        self.assertEqual([(name, flag) for name, _, _, flag in ledger],
                         [("20260101000000_shop.sql", "t"), ("20260102000000_shop_environments.sql", "t"),
                          ("20260103000000_forum.sql", "t"), ("20260301000000_saves.sql", "f"),
                          ("20260401000000_clubs.sql", "f")])
        self.assertEqual(ledger[0][1], sha(SHOP))
        # Not one statement of a hand-applied file reached psql again, and its data is untouched.
        for script in self.writes():
            self.assertNotIn("CREATE SCHEMA shop_private", script)
            self.assertNotIn("CREATE SCHEMA forum_private", script)
        self.assertEqual(self.sql("SELECT title FROM shop_private.items WHERE id = 7"), "kept")
        self.assertIn("recorded as applied by hand", self.out.getvalue())

    def test_every_read_before_the_first_write_is_in_a_read_only_transaction(self):
        self.by_hand("20260101000000_shop.sql", "20260102000000_shop_environments.sql", "20260103000000_forum.sql")
        self.migrate()
        first_write = [kind for kind, _ in self.runs].index("write")
        reads = [statements for kind, statements in self.runs[:first_write] if kind == "read"]
        self.assertGreaterEqual(len(reads), 2)
        site_reads = [s for s in reads if "mp_private" not in " ".join(s)]
        self.assertTrue(site_reads)
        for statements in site_reads:
            self.assertEqual((statements[0], statements[-1]), ("BEGIN TRANSACTION READ ONLY", "COMMIT"), statements)

    def test_a_partly_present_file_stops_the_run_naming_what_is_missing(self):
        self.by_hand("20260101000000_shop.sql", "20260102000000_shop_environments.sql", "20260103000000_forum.sql")
        self.sql("DROP FUNCTION public.shop_ready_scoped(text); ALTER TABLE shop_private.items DROP COLUMN environment")
        self.refused(r"20260102000000_shop_environments\.sql.*missing: column shop_private\.items\.environment, "
                     r"function public\.shop_ready_scoped")
        self.assertIsNone(self.ledger())
        self.assertFalse(self.exists("saves_private.saves"))
        self.assertFalse(self.exists("mp_private.schema_revision"), "the multiplayer migrations did not run either")

    def test_an_absent_file_older_than_a_present_one_stops_the_run(self):
        self.by_hand("20260103000000_forum.sql")
        self.refused(r"20260101000000_shop\.sql.*20260103000000_forum\.sql.*missing: table shop_private\.schema_revision")
        self.assertIsNone(self.ledger())
        self.assertFalse(self.exists("shop_private.items"))

    def test_a_hand_applied_file_that_no_longer_matches_its_recorded_content_is_refused(self):
        self.by_hand("20260101000000_shop.sql", "20260102000000_shop_environments.sql", "20260103000000_forum.sql")
        self.files(dict(ALL, **{"20260103000000_forum.sql": FORUM + "-- tidied\n"}))
        self.refused(r"20260103000000_forum\.sql.*changed")
        self.assertIsNone(self.ledger())

    def test_only_the_first_step_applied_by_hand_gets_the_rest_from_the_job(self):
        # In order: the shop's first file is present, everything after it is still to do.
        self.by_hand("20260101000000_shop.sql")
        self.migrate()
        self.assertEqual([(name, flag) for name, _, _, flag in self.ledger()][:2],
                         [("20260101000000_shop.sql", "t"), ("20260102000000_shop_environments.sql", "f")])
        self.assertEqual(self.sql("SELECT revision FROM shop_private.schema_revision"), "2")


class RefusalTests(Base):
    def test_an_applied_file_whose_content_changed_is_refused(self):
        self.migrate()
        before = self.ledger()
        self.files(dict(ALL, **{"20260301000000_saves.sql": SAVES + "CREATE TABLE saves_private.more (id integer);\n",
                                "20260501000000_later.sql": "CREATE TABLE saves_private.later (id integer);\n"}))
        self.refused(r"20260301000000_saves\.sql.*changed", OTHER_COMMIT)
        self.assertEqual(self.ledger(), before)
        self.assertFalse(self.exists("saves_private.later"), "nothing after it was applied either")

    def test_a_database_newer_than_the_commit_is_refused(self):
        self.migrate()
        before = self.ledger()
        self.files({name: ALL[name] for name in ALL if name != "20260401000000_clubs.sql"})
        self.refused(r"20260401000000_clubs\.sql.*this commit does not have", OTHER_COMMIT)
        self.assertEqual(self.ledger(), before)

    def test_a_new_file_named_before_the_newest_applied_one_is_refused(self):
        self.migrate()
        before = self.ledger()
        self.files(dict(ALL, **{"20260315000000_forgotten.sql": "CREATE TABLE saves_private.forgotten (id integer);\n"}))
        self.refused(r"20260315000000_forgotten\.sql sorts before 20260401000000_clubs\.sql", OTHER_COMMIT)
        self.assertEqual(self.ledger(), before)
        self.assertFalse(self.exists("saves_private.forgotten"))

    def test_a_failing_migration_rolls_back_whole_and_leaves_the_ledger_untouched(self):
        # No BEGIN of its own: the job's transaction is what undoes the first statement.
        self.files(dict(ALL, **{
            "20260501000000_half.sql": "CREATE TABLE saves_private.half (id integer);\n"
                                       "INSERT INTO saves_private.no_such_table VALUES (1);\n",
            "20260601000000_after.sql": "CREATE TABLE saves_private.after (id integer);\n"}))
        with self.assertRaisesRegex(self.bu.StepError, "no_such_table"):
            self.migrate()
        self.assertFalse(self.exists("saves_private.half"), "its first statement was rolled back with the rest")
        self.assertFalse(self.exists("saves_private.after"), "and nothing after it ran")
        # The files before it stay applied and recorded; the failed one has no row.
        self.assertEqual([name for name, _, _, _ in self.ledger()], SITE)

    def test_a_file_and_its_ledger_row_commit_or_fail_together(self):
        self.migrate()
        # Make the ledger refuse one name: if the row were written in a transaction of its own,
        # the file's table would already be committed when the row fails.
        self.sql("""CREATE FUNCTION public.refuse_row() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN RAISE EXCEPTION 'the ledger refuses this row'; END $$;
                    CREATE TRIGGER refuse BEFORE INSERT ON migrations_private.applied FOR EACH ROW
                    WHEN (NEW.name = '20260501000000_together.sql') EXECUTE FUNCTION public.refuse_row()""")
        for shape, text in (("no transaction statements", "CREATE TABLE saves_private.together (id integer);\n"),
                            ("its own BEGIN and COMMIT", "BEGIN;\nCREATE TABLE saves_private.together (id integer);\nCOMMIT;\n")):
            with self.subTest(shape):
                self.files(dict(ALL, **{"20260501000000_together.sql": text}))
                with self.assertRaisesRegex(self.bu.StepError, "the ledger refuses this row"):
                    self.migrate(OTHER_COMMIT)
                self.assertFalse(self.exists("saves_private.together"))
        self.assertEqual([name for name, _, _, _ in self.ledger()], SITE)

    def test_a_file_that_would_end_its_own_transaction_is_refused_before_anything_runs(self):
        bad = {
            "a stray COMMIT": "BEGIN;\nCREATE TABLE a (id integer);\nCOMMIT;\nCREATE TABLE b (id integer);\nCOMMIT;\n",
            "a ROLLBACK": "CREATE TABLE a (id integer);\nROLLBACK;\n",
            "BEGIN without COMMIT": "BEGIN;\nCREATE TABLE a (id integer);\n",
            "a savepoint": "SAVEPOINT s;\nCREATE TABLE a (id integer);\n",
            "a psql command": "CREATE TABLE a (id integer);\n\\! id\n",
            "an unterminated string": "CREATE TABLE a (note text DEFAULT 'oops);\n",
            "an unterminated dollar quote": "DO $x$ BEGIN NULL; END;\n",
        }
        for label, text in bad.items():
            with self.subTest(label):
                self.files(dict(ALL, **{"20260501000000_bad.sql": text}))
                self.refused(r"20260501000000_bad\.sql")
        self.assertIsNone(self.ledger())
        self.assertFalse(self.exists("mp_private.schema_revision"))

    def test_transaction_words_inside_bodies_strings_and_comments_are_not_statements(self):
        text = """-- COMMIT; ROLLBACK; in a comment
/* BEGIN; in a block comment */
CREATE TABLE saves_private.words (note text DEFAULT 'COMMIT; it''s fine', "end;" text DEFAULT E'a\\'b; ROLLBACK;');
CREATE FUNCTION public.words() RETURNS integer LANGUAGE plpgsql AS $body$
BEGIN
  IF true THEN RETURN 1; END IF;
  RETURN 2;
END;
$body$;
DO $$ BEGIN PERFORM 1; END $$;
"""
        self.files(dict(ALL, **{"20260501000000_words.sql": text}))
        self.migrate()
        self.assertEqual(self.sql("SELECT words()"), "1")
        self.assertEqual(self.ledger()[-1][0], "20260501000000_words.sql")

    def test_a_site_file_cannot_touch_the_multiplayer_schema_or_carry_another_name(self):
        self.files(dict(ALL, **{"20260501000000_sneaky.sql": "ALTER TABLE mp_private.matches ADD COLUMN x integer;\n"}))
        self.refused(r"20260501000000_sneaky\.sql.*mp_private")
        self.files(dict(ALL, **{"clubs-final.sql": "CREATE TABLE c (id integer);\n"}))
        self.refused(r"clubs-final\.sql.*14 digits")


class MultiplayerTests(Base):
    """The multiplayer migrations keep their own mechanism and run first."""

    def test_multiplayer_files_are_applied_by_revision_before_any_site_file(self):
        exports = self.migrate()
        self.assertEqual(exports["GAMES_MP_DB_REVISION"], "2")
        self.assertEqual(self.sql("SELECT revision FROM mp_private.schema_revision"), "2")
        writes = self.writes()
        # Exactly the two files, unwrapped, as before; then the ledger and the site files.
        self.assertEqual(writes[:2], [MP_1, MP_2])
        self.assertTrue(all("migrations_private" in script for script in writes[2:]))
        self.assertEqual(len(writes), 2 + 1 + len(SITE))
        self.assertEqual([name for name, _, _, _ in self.ledger()], SITE, "no multiplayer file is in the ledger")

    def test_the_multiplayer_refusals_still_hold(self):
        self.migrate()
        self.files({name: ALL[name] for name in ALL if name != "20260302000000_mp_note.sql"})
        with self.assertRaisesRegex(self.bu.StepError, "newer than this commit"):
            self.migrate(OTHER_COMMIT)
        self.assertEqual(self.sql("SELECT revision FROM mp_private.schema_revision"), "2")

    def test_a_refused_site_state_leaves_the_multiplayer_revision_where_it_was(self):
        self.files({name: ALL[name] for name in ALL if name != "20260302000000_mp_note.sql"})
        self.migrate()
        self.assertEqual(self.sql("SELECT revision FROM mp_private.schema_revision"), "1")
        self.files(dict(ALL, **{"20260301000000_saves.sql": SAVES + "-- edited\n"}))
        self.refused("changed", OTHER_COMMIT)
        self.assertEqual(self.sql("SELECT revision FROM mp_private.schema_revision"), "1")

    def test_what_the_images_build_reports_is_what_the_migration_reaches(self):
        files = self.bu.site_migrations(self.root)
        self.assertEqual([name for name, _, _ in files], SITE)
        self.assertEqual([r for r, _, _ in self.bu.mp_migrations(self.root)], [1, 2])
        self.assertEqual(self.migrate()["GAMES_SITE_DB_MIGRATIONS"],
                         self.bu.site_state([(name, digest) for name, digest, _ in files]))


class RealBaselineTests(unittest.TestCase):
    """The list in bringup.py itself: short, explicit, and well formed."""

    def test_it_names_the_four_files_applied_by_hand_with_their_content_and_probes(self):
        baseline = load().SITE_BASELINE
        self.assertEqual(sorted(baseline), [
            "20260921000000_skyhook_leaderboard.sql", "20260921010000_skyhook_environments.sql",
            "20260922000000_site_community.sql", "20260922100000_site_ratings_poll.sql"])
        name = re.compile(r"[a-z_][a-z0-9_]*")
        for file, entry in baseline.items():
            self.assertRegex(entry["sha256"], r"^[0-9a-f]{64}$", file)
            kinds = [probe[0] for probe in entry["creates"]]
            self.assertIn("revision", kinds, file)
            self.assertIn("function", kinds, file)
            for probe in entry["creates"]:
                self.assertIn(probe[0], ("table", "column", "function", "revision"), file)
                parts = probe[1].split(".")
                self.assertEqual(len(parts), 3 if probe[0] == "column" else 2, probe)
                self.assertTrue(all(name.fullmatch(part) for part in parts), probe)
                self.assertNotIn("mp_private", probe[1])
        revisions = {file: [p[2] for p in entry["creates"] if p[0] == "revision"] for file, entry in baseline.items()}
        self.assertEqual(list(revisions.values()), [[3], [4], [1], [2]])


if __name__ == "__main__":
    unittest.main(verbosity=2)

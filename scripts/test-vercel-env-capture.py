#!/usr/bin/env python3
"""scripts/vercel-env-capture.py: the pure parts, the generated route executed for real in Node, and the safeguards that must
never be removed (staged production deploys only, a 401 check before anything is read, deletion by deployment id)."""
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "scripts" / "vercel-env-capture.py").read_text()
spec = importlib.util.spec_from_file_location("capture", ROOT / "scripts" / "vercel-env-capture.py")
cap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cap)
TF = (ROOT / "vercel-env-secrets.tf").read_text()


class NameTests(unittest.TestCase):
    ENVS = [
        {"key": "AUTH_SECRET", "target": ["production"], "type": "sensitive"},
        {"key": "PLAIN", "target": ["production", "preview"], "type": "encrypted"},
        {"key": "ONLY_PREVIEW", "target": ["preview"], "type": "encrypted"},
        {"key": "VERCEL_ENV", "target": ["production"], "type": "encrypted"},
        {"key": "TURBO_REMOTE_ONLY", "target": ["production"], "type": "encrypted"},
        {"key": "SYS", "target": ["production"], "type": "system"},
        {"key": "BRANCHY", "target": ["preview"], "type": "encrypted", "gitBranch": "x"},
        {"key": "lowercase", "target": ["production"], "type": "encrypted"},
    ]

    def test_only_the_projects_own_variables_for_that_target_are_named(self):
        self.assertEqual(cap.variable_names(self.ENVS, "production"), ["AUTH_SECRET", "PLAIN"])
        self.assertEqual(cap.variable_names(self.ENVS, "preview"), ["ONLY_PREVIEW", "PLAIN"])

    def test_a_deployment_is_needed_only_when_a_sensitive_variable_exists_and_never_for_development(self):
        self.assertTrue(cap.needs_deployment(self.ENVS, "production"))
        self.assertFalse(cap.needs_deployment(self.ENVS, "preview"))
        dev = [{"key": "A", "target": ["development"], "type": "sensitive"}]
        self.assertFalse(cap.needs_deployment(dev, "development"))

    def test_a_placeholder_or_empty_value_is_never_storable(self):
        missing, bad, extra = cap.validate_record({"A": "[SENSITIVE]", "B": "", "C": "ok"}, ["A", "B", "C"])
        self.assertEqual(sorted(bad), ["A", "B"])
        self.assertEqual(cap.validate_record({"C": "ok"}, ["C"]), ([], [], []))

    def test_dotenv_values_are_unquoted_like_vercel_writes_them(self):
        self.assertEqual(cap.parse_dotenv('A="x\\ny"\nB=plain\n# c\n'), {"A": "x\ny", "B": "plain"})


class RouteTests(unittest.TestCase):
    TOKEN = "one-time-token-for-the-test"

    def run_route(self, header_token, env):
        route = cap.render_route(["ALPHA", "BETA", "MISSING"], cap.token_hash(self.TOKEN))
        d = Path(tempfile.mkdtemp())
        (d / "route.mjs").write_text(route)
        runner = """
import { GET } from './route.mjs';
Object.assign(process.env, %s);
const headers = %s;
const r = await GET(new Request('https://x.invalid/api/dump', { headers }));
console.log(JSON.stringify({ status: r.status, body: await r.text(), cache: r.headers.get('cache-control') }));
""" % (json.dumps(env), json.dumps({"x-dump-token": header_token} if header_token is not None else {}))
        (d / "run.mjs").write_text(runner)
        out = subprocess.run(["node", str(d / "run.mjs")], capture_output=True, text=True, timeout=60)
        shutil.rmtree(d, ignore_errors=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_the_right_token_returns_exactly_the_named_variables_that_exist(self):
        r = self.run_route(self.TOKEN, {"ALPHA": "a1", "BETA": "b2", "OTHER_SECRET": "must-not-leak"})
        self.assertEqual(r["status"], 200)
        self.assertEqual(json.loads(r["body"]), {"ALPHA": "a1", "BETA": "b2"})
        self.assertEqual(r["cache"], "no-store")

    def test_a_wrong_or_missing_token_gets_a_404_and_no_variable(self):
        for tok in ("wrong", "", None):
            r = self.run_route(tok, {"ALPHA": "secret-alpha"})
            self.assertEqual(r["status"], 404, tok)
            self.assertNotIn("secret-alpha", r["body"])

    def test_the_route_source_carries_names_and_a_hash_never_the_token_or_a_value(self):
        src = cap.render_route(["ALPHA"], cap.token_hash(self.TOKEN))
        self.assertIn(cap.token_hash(self.TOKEN), src)
        self.assertNotIn(self.TOKEN, src)
        self.assertNotRegex(src, r"console\.|fetch\(|process\.env\)")

    def test_it_refuses_to_render_for_a_vercel_bookkeeping_name_or_a_bad_digest(self):
        with self.assertRaises(AssertionError):
            cap.render_route(["VERCEL_URL"], cap.token_hash("x"))
        with self.assertRaises(AssertionError):
            cap.render_route(["ALPHA"], "not-a-digest")

    def test_the_throwaway_app_overrides_the_sites_own_install_and_build(self):
        v = json.loads(cap.render_vercel_json())
        self.assertEqual(v["installCommand"], "npm install --no-audit --no-fund")
        self.assertEqual(v["buildCommand"], "npm run build")


class SafeguardTests(unittest.TestCase):
    def test_a_production_capture_is_staged_and_never_promoted(self):
        self.assertIn('["--prod", "--skip-domain"]', SRC)
        self.assertNotRegex(SRC, r'"--prod"\]')   # --prod is never used without --skip-domain
        self.assertIn('dep.get("aliasAssigned") or dep.get("alias")', SRC)

    def test_nothing_is_read_until_an_unauthenticated_request_has_been_refused(self):
        anon = SRC.index('anon != "401"')
        read = SRC.index('"/api/dump", "--deployment"')
        self.assertLess(anon, read)
        self.assertIn("is NOT protected; deleting it", SRC)
        self.assertIn('sso.get("deploymentType") not in ("all", "prod_deployment_urls_and_all_previews")', SRC)

    def test_the_deployment_is_deleted_by_its_id_never_by_project_name(self):
        self.assertIn('assert deployment.startswith("dpl_")', SRC)
        self.assertIn('vercel(["remove", deployment, "--yes", "--scope", scope], check=False)', SRC)
        self.assertNotRegex(SRC, r'\["remove", (project|name)\b')
        self.assertIn("STILL EXISTS", SRC)  # it confirms the deletion

    def test_values_are_only_ever_printed_as_names(self):
        printed = re.findall(r'print\((.*?)\)\n', SRC)
        self.assertTrue(all("record[" not in p and "values[" not in p for p in printed), printed)
        self.assertIn('", ".join(sorted(record))', SRC)

    def test_the_work_directory_is_private_memory_and_removed(self):
        self.assertIn('tempfile.mkdtemp(dir="/dev/shm"', SRC)
        self.assertIn("os.chmod(work, 0o700)", SRC)
        self.assertIn("shutil.rmtree(work, ignore_errors=True)", SRC)

    def test_the_sites_match_the_containers_terraform_creates(self):
        tf_sites = dict(re.findall(r'"([a-z-]+)"\s*=\s*\[([^\]]*)\]', TF.split("vercel_env_targets", 1)[1].split("vercel_env_records", 1)[0]))
        self.assertEqual(sorted(tf_sites), sorted(cap.SITES))
        for site, targets in tf_sites.items():
            for t in re.findall(r'"(\w+)"', targets):
                self.assertIn(t, cap.TARGETS, site)


if __name__ == "__main__":
    unittest.main()

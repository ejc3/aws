#!/usr/bin/env python3
"""The staging Workers' Cloudflare envelope (workers-stage.tf): one Worker-native Access application covering every Worker, each
Worker's URL switches owned by Terraform, and no URL ever on for a Worker the Access application does not cover."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TF = (ROOT / "workers-stage.tf").read_text()


def code(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def block(kind, *labels):
    head = "%s %s {" % (kind, " ".join('"%s"' % l for l in labels))
    start = code(TF).index(head)
    body = code(TF)[start:]
    depth, seen = 0, False
    for i, ch in enumerate(body):
        depth += ch == "{"
        depth -= ch == "}"
        seen = seen or depth > 0
        if seen and depth == 0:
            return body[:i + 1]


WORKERS = re.findall(r'^\s+"([a-z0-9-]+-stage)"\s*=\s*\{\s*id\s*=\s*"([0-9a-f]+)",\s*urls\s*=\s*(true|false)\s*\}', code(TF), re.M)


class MapTests(unittest.TestCase):
    def test_there_is_at_least_one_worker_and_names_and_ids_are_unique(self):
        self.assertTrue(WORKERS)
        self.assertEqual(len({n for n, _, _ in WORKERS}), len(WORKERS))
        self.assertEqual(len({i for _, i, _ in WORKERS}), len(WORKERS))

    def test_each_id_is_a_cloudflare_worker_tag(self):
        for name, wid, _ in WORKERS:
            self.assertRegex(wid, r"^[0-9a-f]{32}$", name)

    def test_the_colton_games_worker_is_not_in_this_map(self):
        # It has its own Access application, gates and Workers Builds path (cloudflare.tf).
        self.assertNotIn("colton-games-stage", [n for n, _, _ in WORKERS])


class AccessTests(unittest.TestCase):
    ACCESS = block("resource", "cloudflare_zero_trust_access_application", "workers_stage")

    def test_every_worker_is_a_worker_native_destination_by_immutable_id(self):
        self.assertIn('type      = "worker"', self.ACCESS)
        self.assertIn("worker_id = local.workers_stage[name].id", self.ACCESS)
        self.assertIn("for name in local.workers_stage_names", self.ACCESS)
        self.assertNotRegex(self.ACCESS, r"\buri\b|domain|cloudflare_worker\.")  # no hostname app, no cycle through the Worker

    def test_it_reuses_the_family_allowlist_and_the_service_token_policy(self):
        self.assertIn("cloudflare_zero_trust_access_policy.cc_games_allowed.id", self.ACCESS)
        self.assertIn("cloudflare_zero_trust_access_policy.cc_games_service.id", self.ACCESS)

    def test_it_cannot_be_destroyed_while_urls_may_be_on(self):
        self.assertIn("prevent_destroy = true", self.ACCESS)


class UrlSwitchTests(unittest.TestCase):
    SUB = block("resource", "cloudflare_workers_script_subdomain", "stage")

    def test_both_url_switches_follow_one_flag(self):
        self.assertIn("enabled          = each.value.urls", self.SUB)
        self.assertIn("previews_enabled = each.value.urls", self.SUB)

    def test_access_exists_before_any_url_can_be_enabled(self):
        self.assertRegex(self.SUB, r"depends_on\s*=\s*\[[^\]]*cloudflare_zero_trust_access_application\.workers_stage")
        self.assertIn("cloudflare_workers_subdomain.cc_games", self.SUB)

    def test_only_the_url_switches_are_managed_never_the_whole_worker(self):
        # A whole-Worker update sends back Cloudflare's own observability defaults and the API refuses it.
        self.assertNotRegex(code(TF), r'resource\s+"cloudflare_worker"')
        self.assertIn("script_name      = each.key", self.SUB)

    def test_every_worker_is_adopted_not_created(self):
        imp = re.search(r"import \{(.*?)\n\}", code(TF), re.S).group(1)
        self.assertIn("for_each = local.workers_stage", imp)
        self.assertIn("to       = cloudflare_workers_script_subdomain.stage[each.key]", imp)
        self.assertIn('id       = "${var.cloudflare_account_id}/${each.key}"', imp)

    def test_the_earlier_whole_worker_resources_are_forgotten_without_destroying_a_worker(self):
        rm = re.search(r"removed \{(.*?)\n\}", code(TF), re.S).group(1)
        self.assertIn("from = cloudflare_worker.stage", rm)
        self.assertIn("destroy = false", rm)

    def test_nothing_in_the_file_shells_out_or_calls_the_api_directly(self):
        self.assertNotRegex(code(TF), r"local-exec|provisioner|curl|api\.cloudflare\.com")


class ColtonGamesStageTests(unittest.TestCase):
    """colton-games-stage (cloudflare.tf) follows the same rule: only its URL switches are managed, never the whole Worker."""

    CF = (ROOT / "cloudflare.tf").read_text()

    def test_it_manages_only_the_url_switches_and_waits_for_access(self):
        self.assertNotRegex(self.CF, r'resource\s+"cloudflare_worker"\s+"colton_games_stage"')
        self.assertIn('resource "cloudflare_workers_script_subdomain" "colton_games_stage"', self.CF)
        body = self.CF[self.CF.index('resource "cloudflare_workers_script_subdomain" "colton_games_stage"'):][:900]
        self.assertIn("enabled          = local.colton_games_worker_urls_enabled", body)
        self.assertIn("previews_enabled = local.colton_games_worker_urls_enabled", body)
        self.assertIn("cloudflare_zero_trust_access_application.colton_games_stage", body)
        self.assertIn("prevent_destroy = true", body)

    def test_the_old_whole_worker_object_is_forgotten_not_destroyed(self):
        rm = re.search(r"removed \{\s*from = cloudflare_worker\.colton_games_stage(.*?)\n\}", self.CF, re.S).group(1)
        self.assertIn("destroy = false", rm)

    def test_the_gated_workers_builds_resources_wait_on_the_new_resource(self):
        self.assertNotIn("cloudflare_worker.colton_games_stage]", self.CF)
        self.assertGreaterEqual(self.CF.count("depends_on = [cloudflare_workers_script_subdomain.colton_games_stage]"), 2)


if __name__ == "__main__":
    unittest.main()

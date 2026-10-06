#!/usr/bin/env python3
"""The staging Workers' Cloudflare envelope (workers-stage.tf): one Worker-native Access application covering every Worker, each
Worker adopted so its URL switches belong to Terraform, and no URL ever on for a Worker the Access application does not cover."""
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


class WorkerEnvelopeTests(unittest.TestCase):
    WORKER = block("resource", "cloudflare_worker", "stage")

    def test_both_url_switches_follow_one_flag(self):
        self.assertIn("enabled          = each.value.urls", self.WORKER)
        self.assertIn("previews_enabled = each.value.urls", self.WORKER)

    def test_access_exists_before_any_url_can_be_enabled(self):
        self.assertRegex(self.WORKER, r"depends_on\s*=\s*\[[^\]]*cloudflare_zero_trust_access_application\.workers_stage")
        self.assertIn("cloudflare_workers_subdomain.cc_games", self.WORKER)

    def test_the_worker_cannot_be_destroyed_and_wrangler_keeps_its_observability(self):
        self.assertIn("prevent_destroy = true", self.WORKER)
        self.assertIn("ignore_changes = [observability]", self.WORKER)

    def test_every_worker_is_adopted_not_created(self):
        imp = re.search(r"import \{(.*?)\n\}", code(TF), re.S).group(1)
        self.assertIn("for_each = local.workers_stage", imp)
        self.assertIn("to       = cloudflare_worker.stage[each.key]", imp)
        self.assertIn('id       = "${var.cloudflare_account_id}/${each.value.id}"', imp)

    def test_nothing_in_the_file_shells_out_or_calls_the_api_directly(self):
        self.assertNotRegex(code(TF), r"local-exec|provisioner|curl|api\.cloudflare\.com")


if __name__ == "__main__":
    unittest.main()

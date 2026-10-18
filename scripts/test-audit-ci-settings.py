#!/usr/bin/env python3
"""scripts/audit-ci-settings.py's workflow parsing and its handling of failed reads. Offline: no GitHub call."""
import importlib.util
import os
import unittest
import urllib.error
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("audit", os.path.join(HERE, "audit-ci-settings.py"))
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


class TriggerTests(unittest.TestCase):
    def test_a_block_form_on_lists_every_event_not_only_the_first(self):
        text = "on:\n  push:\n    branches: [main]\n  pull_request_target:\n    types: [opened]\njobs:\n  a:\n"
        self.assertEqual(audit.triggers(text), {"push", "pull_request_target"})

    def test_inline_forms(self):
        self.assertEqual(audit.triggers("on: [push, workflow_run]\n"), {"push", "workflow_run"})
        self.assertEqual(audit.triggers("on: pull_request\n"), {"pull_request"})

    def test_a_pull_request_with_no_types_runs_on_the_default_set(self):
        self.assertEqual(audit.pr_types("on:\n  pull_request:\n  push:\n", "pull_request"), audit.DEFAULT_PR_TYPES)
        self.assertNotIn("ready_for_review", audit.DEFAULT_PR_TYPES)
        listed = "on:\n  pull_request:\n    types: [opened, ready_for_review]\n"
        self.assertEqual(audit.pr_types(listed, "pull_request"), {"opened", "ready_for_review"})


class IndentationTests(unittest.TestCase):
    def test_an_on_map_indented_four_spaces_lists_its_events(self):
        text = "on:\n    push:\n        branches: [main]\n    pull_request:\n        types: [opened]\njobs:\n  a:\n"
        self.assertEqual(audit.triggers(text), {"push", "pull_request"})

    def test_types_are_read_at_any_indentation(self):
        text = "on:\n    pull_request:\n        types:\n            - opened\n            - ready_for_review\n    push:\n"
        self.assertEqual(audit.pr_types(text, "pull_request"), {"opened", "ready_for_review"})

    def test_a_sibling_events_types_are_not_borrowed(self):
        text = "on:\n  pull_request:\n  pull_request_target:\n    types: [opened, ready_for_review]\n"
        self.assertEqual(audit.pr_types(text, "pull_request"), audit.DEFAULT_PR_TYPES)


class DraftGapTests(unittest.TestCase):
    SKIP = "jobs:\n  a:\n    if: github.event.pull_request.draft == false\n"

    def test_pull_request_target_skipping_drafts_needs_ready_for_review(self):
        text = "on:\n  pull_request_target:\n    types: [opened, synchronize]\n" + self.SKIP
        self.assertEqual(audit.draft_gaps({".github/workflows/g.yml": text}), ["g.yml (pull_request_target)"])

    def test_both_events_covered_is_no_gap(self):
        text = ("on:\n  pull_request:\n    types: [opened, ready_for_review]\n"
                "  pull_request_target:\n    types: [opened, ready_for_review]\n" + self.SKIP)
        self.assertEqual(audit.draft_gaps({".github/workflows/g.yml": text}), [])

    def test_pull_request_without_ready_for_review_is_still_a_gap(self):
        text = "on:\n  pull_request:\n" + self.SKIP
        self.assertEqual(audit.draft_gaps({".github/workflows/ci.yml": text}), ["ci.yml"])


class PinTests(unittest.TestCase):
    SHA = "a" * 40

    def test_the_owners_own_actions_are_exempt(self):
        text = ("steps:\n  - uses: ejc3/some-action@main\n  - uses: dolphin-labs-hq/x/.github/workflows/y.yml@v1\n"
                "  - uses: CoderColton/z@v2\n")
        self.assertEqual(audit.unpinned(text), set())

    def test_github_and_other_third_party_actions_must_be_pinned(self):
        text = f"steps:\n  - uses: actions/checkout@v4\n  - uses: cloudflare/wrangler-action@{self.SHA} # v3\n  - uses: pnpm/action-setup@v4\n"
        self.assertEqual(audit.unpinned(text), {"actions/checkout@v4", "pnpm/action-setup@v4"})

    def test_local_and_docker_references_are_not_actions_to_pin(self):
        self.assertEqual(audit.unpinned("  - uses: ./local\n  - uses: docker://alpine:3\n"), set())


class OwnerTokenTests(unittest.TestCase):
    def run_main(self, owner, app_token):
        lines = []
        with mock.patch.object(audit.sys, "argv", ["audit", owner]), \
                mock.patch.object(audit, "token", return_value=app_token), \
                mock.patch.object(audit, "user_token", return_value="personal") as user, \
                mock.patch.object(audit, "audit_org_runner_groups"), \
                mock.patch.object(audit, "paged", return_value=([], 200)) as paged, \
                mock.patch("builtins.print", side_effect=lambda *a, **k: lines.append(" ".join(map(str, a)))):
            audit.main()
        return lines, user, paged

    def test_a_failed_app_token_for_an_app_owner_is_reported_not_replaced(self):
        lines, user, paged = self.run_main("dolphin-labs-hq", None)
        user.assert_not_called()
        paged.assert_not_called()
        self.assertTrue(any("minting failed" in l for l in lines), lines)

    def test_an_owner_without_the_app_is_read_with_the_callers_login(self):
        lines, user, paged = self.run_main("CoderColton", None)
        user.assert_called_once()
        self.assertEqual(paged.call_args[0][0], "personal")
        self.assertTrue(paged.call_args[0][1].startswith("/user/repos"))


class ReadFailureTests(unittest.TestCase):
    def test_a_transport_error_is_status_zero_not_an_exception(self):
        for error in (urllib.error.URLError("no route"), TimeoutError("slow"), ConnectionResetError()):
            with mock.patch.object(audit.urllib.request, "urlopen", side_effect=error):
                self.assertEqual(audit.get("t", "/repos/o/r"), (0, None))

    def test_an_unreadable_workflow_file_is_reported_not_dropped(self):
        listing = [{"type": "file", "name": "ci.yml", "path": ".github/workflows/ci.yml"}]
        replies = {"/repos/o/r/contents/.github/workflows": (200, listing),
                   "/repos/o/r/contents/.github/workflows/ci.yml": (403, None)}
        with mock.patch.object(audit, "get", side_effect=lambda tok, path: replies[path]):
            self.assertEqual(audit.workflows("t", "o/r"), ({}, [".github/workflows/ci.yml (403)"]))

    def test_no_workflows_directory_is_no_files_and_no_error(self):
        with mock.patch.object(audit, "get", return_value=(404, None)):
            self.assertEqual(audit.workflows("t", "o/r"), ({}, []))


if __name__ == "__main__":
    unittest.main()

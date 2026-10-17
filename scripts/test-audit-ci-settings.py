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

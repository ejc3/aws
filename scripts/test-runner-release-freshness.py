#!/usr/bin/env python3
"""Offline release/version/deadline and schedule-boundary regression checks."""

import contextlib
import importlib.util
import io
from pathlib import Path
from unittest import mock
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("freshness", Path(__file__).with_name("check-runner-release-freshness.py"))
freshness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(freshness)


def release(tag="v2.337.0", published="2026-08-26T14:33:29Z", **extra):
    return {"tag_name": tag, "published_at": published, "draft": False, "prerelease": False, **extra}


class FreshnessTests(unittest.TestCase):
    def test_extracts_real_literal_pin(self):
        pin = freshness.pinned_version((ROOT / "runner-autoscale.tf").read_text())
        self.assertEqual(len(freshness.version(pin)), 3)

    def test_pin_must_be_unique_literal_in_runner_body(self):
        for body in ('RUNNER_VERSION=$(curl example)', 'RUNNER_VERSION="latest"',
                     'RUNNER_VERSION="2.337.0"\nRUNNER_VERSION="2.338.0"'):
            with self.assertRaises(ValueError):
                freshness.pinned_version("  runner_user_data = <<-EOF\n" + body + "\nEOF")

    def test_app_runner_pin_is_watched_and_matches_fcvm(self):
        app = freshness.pinned_app_version((ROOT / "runner-app" / "bootstrap.sh").read_text())
        fcvm = freshness.pinned_version((ROOT / "runner-autoscale.tf").read_text())
        self.assertEqual(app, fcvm, "both fleets must pin one runner version: the check watches it for both")
        self.assertEqual(freshness.pinned_versions(), fcvm)
        for body in ("RUNNER_VERSION=$(curl example)", "RUNNER_VERSION='latest'",
                     "RUNNER_VERSION='2.337.0'\nRUNNER_VERSION='2.338.0'", ""):
            with self.assertRaises(ValueError):
                freshness.pinned_app_version(body)

    def test_current_stable_passes_despite_newer_draft_and_prerelease(self):
        self.assertEqual(freshness.assess("2.337.0", [release(),
            release("v2.338.0", draft=True), release("v2.338.0-rc1", prerelease=True)])[0], 0)

    def test_numeric_not_lexical_version_order(self):
        code, notice = freshness.assess("2.9.0", [release("v2.9.0"), release("v2.10.0")])
        self.assertEqual(code, 1)
        self.assertIn("behind stable 2.10.0", notice)

    def test_new_release_does_not_extend_first_missed_deadline(self):
        code, notice = freshness.assess("2.337.0", [
            release("v2.339.0", "2026-09-20T04:00:00Z"), release(),
            release("v2.338.0", "2026-09-01T04:00:00Z")])
        self.assertEqual(code, 1)
        self.assertIn("2026-10-01 04:00 UTC", notice)
        self.assertIn("critical", notice)
        self.assertIn("both official asset digests", notice)

    def test_every_release_kind_including_patch_requires_update(self):
        for tag in ("v2.337.1", "v2.338.0", "v3.0.0"):
            self.assertEqual(freshness.assess("2.337.0", [release(), release(tag)])[0], 1)

    def test_missing_pin_or_invalid_history_fails_closed(self):
        for payload in ([], {}, [release("v2.338.0")], [release()] * 101,
                        [{"tag_name": "v2.337.0"}], [release(draft="false")],
                        [release(published=None)], [release(published="2026-09-08")],
                        [release("v2.337.0\n::error::untrusted")]):
            with self.assertRaises((ValueError, TypeError)):
                freshness.assess("2.337.0", payload)

    def test_public_fetch_is_anonymous_bounded_and_timed(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"[]"
        with mock.patch.object(freshness.urllib.request, "urlopen", return_value=response) as call:
            self.assertEqual(freshness.fetch_releases(), [])
        request = call.call_args.args[0]
        self.assertEqual(request.full_url, freshness.RELEASES_API)
        self.assertFalse(request.has_header("Authorization"))
        self.assertEqual(call.call_args.kwargs["timeout"], 20)
        response.__enter__.return_value.read.assert_called_once_with(freshness.MAX_RESPONSE_BYTES + 1)

    def test_oversized_fetch_fails_closed(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"x" * (freshness.MAX_RESPONSE_BYTES + 1)
        with mock.patch.object(freshness.urllib.request, "urlopen", return_value=response):
            with self.assertRaises(ValueError):
                freshness.fetch_releases()

    def test_unknown_status_is_failure_without_echoing_raw_exception(self):
        output = io.StringIO()
        with mock.patch.object(freshness, "fetch_releases", side_effect=OSError("untrusted response")), contextlib.redirect_stdout(output):
            self.assertEqual(freshness.main(), 2)
        self.assertIn("freshness unknown", output.getvalue())
        self.assertNotIn("untrusted response", output.getvalue())

    def test_schedule_is_main_only_hosted_and_has_no_write_credentials(self):
        text = (ROOT / ".github/workflows/runner-release-freshness.yml").read_text()
        self.assertIn("cron: '17 9 * * *'", text)
        self.assertIn("github.event_name == 'schedule'", text)
        self.assertIn("github.ref == 'refs/heads/main'", text)
        self.assertIn("github.repository == 'ejc3/aws'", text)
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertIn("timeout-minutes: 3", text)
        self.assertIn("contents: read", text)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("fetch-depth: 0", text)
        for forbidden in ("pull_request:", "workflow_dispatch:", "push:", "id-token:",
                          "secrets.", "aws-actions/", "self-hosted", "terraform apply"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main(verbosity=2)

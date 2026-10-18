#!/usr/bin/env python3
"""search-fetch-usage: parsing of each provider's response, publishing, and failure handling, without network."""
import importlib.util
import io
import json
import os
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.modules.setdefault("boto3", types.SimpleNamespace(client=lambda *a, **k: None))
spec = importlib.util.spec_from_file_location("usage", ROOT / "scripts" / "search-fetch-usage.py")
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)
TF = (ROOT / "search-fetch-usage.tf").read_text()


class FakeCW:
    def __init__(self):
        self.calls = []

    def put_metric_data(self, Namespace, MetricData):
        assert len(MetricData) <= 20
        self.calls.append((Namespace, MetricData))


class Parsing(unittest.TestCase):
    def test_deepseek_balance_strings_become_numbers_per_currency(self):
        resp = {"is_available": True, "balance_infos": [
            {"currency": "USD", "total_balance": "97.50", "granted_balance": "0.00", "topped_up_balance": "97.50"}]}
        got = usage.deepseek("k", get=lambda url, h: resp)
        self.assertIn(("Available", 1.0, {}), got)
        self.assertIn(("BalanceTotal", 97.5, {"Currency": "USD"}), got)
        self.assertIn(("BalanceToppedUp", 97.5, {"Currency": "USD"}), got)

    def test_deepseek_sends_the_key_as_a_bearer_header_not_in_the_url(self):
        seen = {}
        usage.deepseek("secret-k", get=lambda url, h: seen.update(url=url, h=h) or {"is_available": False})
        self.assertNotIn("secret-k", seen["url"])
        self.assertEqual(seen["h"], {"Authorization": "Bearer secret-k"})

    def test_scrapingdog_derives_remaining_and_fraction_and_ignores_the_echoed_key(self):
        resp = {"requestLimit": 1000000, "requestUsed": 250000, "threadCount": 7, "concurrency_limit": 50,
                "validity": 21, "apiKey": "k", "email": "someone@example.com"}
        got = dict((n, v) for n, v, d in usage.scrapingdog("k", get=lambda url, h=None: resp))
        self.assertEqual(got["CreditsRemaining"], 750000)
        self.assertAlmostEqual(got["CreditsRemainingFraction"], 0.75)
        self.assertEqual(got["ThreadsInFlight"], 7)
        self.assertEqual(got["ConcurrencyLimit"], 50)
        self.assertNotIn("apiKey", got)

    def test_scrapingdog_with_no_known_fields_is_an_error(self):
        with self.assertRaises(ValueError):
            usage.scrapingdog("k", get=lambda url, h=None: {"something": "else"})

    def test_browserbase_usage_concurrency_and_running_sessions(self):
        def get(url, h):
            self.assertEqual(h, {"X-BB-API-Key": "k"})
            if url.endswith("/usage"):
                return {"browserMinutes": 120, "proxyBytes": 1048576}
            if url.endswith("/v1/projects"):
                return [{"id": "other", "concurrency": 3}, {"id": "proj", "concurrency": 25}]
            return [{"id": "s1"}, {"id": "s2"}]
        got = dict((n, v) for n, v, d in usage.browserbase("k", "proj", get=get))
        self.assertEqual(got, {"BrowserMinutes": 120, "ProxyBytes": 1048576, "ConcurrencyLimit": 25, "SessionsRunning": 2})


    def test_browserbase_usage_without_its_fields_is_an_error_even_if_sessions_answer(self):
        def get(url, h):
            if url.endswith("/usage"):
                return {"renamed": 1}
            return []
        with self.assertRaises(ValueError):
            usage.browserbase("k", "proj", get=get)


class Handler(unittest.TestCase):
    def test_publishes_every_provider_and_providerup(self):
        cw = FakeCW()
        out = usage.lambda_handler({}, None, sm=object(), cw=cw, fetchers={
            "deepseek": lambda: [("Available", 1.0, {})], "scrapingdog": lambda: [("CreditsUsed", 5.0, {})]})
        data = [d for _, batch in cw.calls for d in batch]
        self.assertEqual(out["published"], 4)
        self.assertTrue(all(ns == "SearchFetch" for ns, _ in cw.calls))
        ups = {d["Dimensions"][0]["Value"]: d["Value"] for d in data if d["MetricName"] == "ProviderUp"}
        self.assertEqual(ups, {"deepseek": 1.0, "scrapingdog": 1.0})

    def test_a_failed_provider_publishes_the_rest_then_fails_without_logging_the_error_text(self):
        cw = FakeCW()

        def boom():
            raise RuntimeError("https://api.scrapingdog.com/account?api_key=SECRET")
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(RuntimeError):
            usage.lambda_handler({}, None, sm=object(), cw=cw, fetchers={"scrapingdog": boom, "deepseek": lambda: [("Available", 1.0, {})]})
        data = [d for _, batch in cw.calls for d in batch]
        ups = {d["Dimensions"][0]["Value"]: d["Value"] for d in data if d["MetricName"] == "ProviderUp"}
        self.assertEqual(ups, {"scrapingdog": 0.0, "deepseek": 1.0})
        self.assertNotIn("SECRET", buf.getvalue())

    def test_batches_of_twenty(self):
        cw = FakeCW()
        usage.lambda_handler({}, None, sm=object(), cw=cw, fetchers={"x": lambda: [(f"M{i}", 1.0, {}) for i in range(45)]})
        self.assertEqual([len(b) for _, b in cw.calls], [20, 20, 6])


class Terraform(unittest.TestCase):
    def test_role_reads_only_three_keys_and_publishes_only_its_namespace(self):
        self.assertIn('aws_secretsmanager_secret.search_fetch_api_key["deepseek"].arn', TF)
        self.assertIn('aws_secretsmanager_secret.search_fetch_api_key["scrapingdog"].arn', TF)
        self.assertNotIn('search_fetch_api_key["exa"]', TF)
        self.assertIn('Condition = { StringEquals = { "cloudwatch:namespace" = local.search_fetch_usage_namespace } }', TF)

    def test_it_is_a_reader_of_the_secrets_it_needs(self):
        ai = (ROOT / "dev-ai-services.tf").read_text()
        self.assertIn("aws_iam_role.search_fetch_usage.arn]", ai)
        self.assertIn("search_fetch_api_key_readers = local.browserbase_readers", ai)

    def test_alarms_and_dashboard_exist(self):
        for name in ("search-fetch-usage-errors", "search-fetch-usage-not-running", "search-fetch-deepseek-balance-low",
                     "search-fetch-scrapingdog-credits-low"):
            self.assertIn(f'alarm_name          = "{name}"', TF)
        self.assertIn('dashboard_name = "search-fetch"', TF)


if __name__ == "__main__":
    unittest.main()

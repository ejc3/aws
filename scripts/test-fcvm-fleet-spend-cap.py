#!/usr/bin/env python3
"""Exercise the fleet spending guard without AWS credentials or a clock on the wall."""
import importlib.util
import io
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

source = Path(__file__).with_name("fcvm-fleet-spend-cap.py")
spec = importlib.util.spec_from_file_location("fleet_spend_cap", source)
cap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cap)

ENV = {
    "ACCOUNT_ID": "000000000001",
    "BUDGET_NAME": "fleet-daily",
    "LIMIT_USD": "750",
    "STATE_BUCKET": "coordinator-state",
    "STATE_KEY": "spend-cap.json",
    "ROLE_NAME": "coordinator",
    "POLICY_ARN": "arn:aws:iam::000000000001:policy/spend-cap",
}
DAY = datetime(2026, 11, 19, tzinfo=timezone.utc)
OTHER_POLICY = "arn:aws:iam::000000000001:policy/ordinary-fleet-permissions"


def cost(day, actual="750.00", actual_unit="USD", limit="750", limit_unit="USD"):
    row = {"TimePeriod": {"Start": day, "End": day + timedelta(days=1)},
           "BudgetedAmount": {"Amount": limit, "Unit": limit_unit}}
    if actual is not None:
        row["ActualAmount"] = {"Amount": actual, "Unit": actual_unit}
    return row


class Budget:
    def __init__(self):
        self.pages = [[]]
        self.requests = []

    def describe_budget_performance_history(self, *, AccountId, BudgetName, TimePeriod, MaxResults, NextToken=None):
        assert AccountId == ENV["ACCOUNT_ID"] and BudgetName == ENV["BUDGET_NAME"]
        self.requests.append(TimePeriod)
        page = int(NextToken or "0")
        result = {"BudgetPerformanceHistory": {
            "BudgetName": BudgetName, "BudgetType": "COST", "TimeUnit": "DAILY",
            "BudgetedAndActualAmountsList": self.pages[page],
        }}
        if page + 1 < len(self.pages):
            result["NextToken"] = str(page + 1)
        return result


class Store:
    def __init__(self):
        self.state = {"blocked_on": "", "billed_days": []}
        self.after_put = None

    def get_object(self, *, Bucket, Key):
        assert (Bucket, Key) == (ENV["STATE_BUCKET"], ENV["STATE_KEY"])
        return {"Body": io.BytesIO(json.dumps(self.state).encode())}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        assert (Bucket, Key) == (ENV["STATE_BUCKET"], ENV["STATE_KEY"])
        self.state = json.loads(Body)
        if self.after_put:
            self.after_put()
        return {}


class RolePolicies:
    def __init__(self, store):
        self.store = store
        self.attached = {OTHER_POLICY}
        self.changes = []
        self.fail_after_change = False

    def get_paginator(self, name):
        assert name == "list_attached_role_policies"
        return self

    def paginate(self, *, RoleName):
        assert RoleName == ENV["ROLE_NAME"]
        # In production the guard need not be on the first page.
        yield {"AttachedPolicies": [{"PolicyArn": OTHER_POLICY}]}
        yield {"AttachedPolicies": [{"PolicyArn": arn} for arn in self.attached if arn != OTHER_POLICY]}

    def attach_role_policy(self, *, RoleName, PolicyArn):
        assert (RoleName, PolicyArn) == (ENV["ROLE_NAME"], ENV["POLICY_ARN"])
        self.changes.append(("attach", self.store.state.copy()))
        self.attached.add(PolicyArn)
        if self.fail_after_change:
            raise ConnectionError("lost IAM acknowledgement")
        return {}

    def detach_role_policy(self, *, RoleName, PolicyArn):
        assert (RoleName, PolicyArn) == (ENV["ROLE_NAME"], ENV["POLICY_ARN"])
        self.changes.append(("detach", self.store.state.copy()))
        self.attached.remove(PolicyArn)
        if self.fail_after_change:
            raise ConnectionError("lost IAM acknowledgement")
        return {}


class SpendCapTests(unittest.TestCase):
    def setUp(self):
        self.budget = Budget()
        self.store = Store()
        self.iam = RolePolicies(self.store)
        self.yesterday = DAY - timedelta(days=1)

    def sync(self, now=DAY, *, expiry_only=False):
        return cap.reconcile(ENV, self.budget, self.store, self.iam, now, expiry_only=expiry_only)

    def test_only_complete_days_count_and_paginated_history_trips_at_exact_limit(self):
        self.budget.pages = [
            [cost(DAY, "8000"), cost(DAY - timedelta(days=4), "8000"), cost(self.yesterday, "749.99")],
            [cost(DAY - timedelta(days=3), "750")],
        ]
        self.sync()
        self.assertEqual(len(self.budget.requests), 2)
        self.assertEqual(self.budget.requests[0], {"Start": DAY - timedelta(days=3), "End": DAY})
        self.assertEqual(self.iam.attached, {OTHER_POLICY, ENV["POLICY_ARN"]})
        self.assertEqual(self.store.state["blocked_on"], DAY.date().isoformat())
        billed = self.store.state["billed_days"]
        self.assertIn((DAY - timedelta(days=3)).date().isoformat(), billed)
        self.assertNotIn(DAY.date().isoformat(), billed)
        self.assertNotIn((DAY - timedelta(days=4)).date().isoformat(), billed)
        self.assertEqual(self.iam.changes, [("attach", self.store.state)])

    def test_missing_actual_can_be_reported_later(self):
        self.budget.pages = [[cost(self.yesterday, actual=None)]]
        self.sync()
        self.assertEqual(self.iam.attached, {OTHER_POLICY})
        self.assertNotIn(self.yesterday.date().isoformat(), self.store.state["billed_days"])
        self.budget.pages = [[cost(self.yesterday, actual="803.70")]]
        self.sync()
        self.assertIn(ENV["POLICY_ARN"], self.iam.attached)

    def test_today_and_history_before_window_cannot_trip_a_healthy_budget(self):
        self.budget.pages = [[cost(DAY, "8000"), cost(DAY - timedelta(days=4), "8000"),
                              cost(self.yesterday, "749.99")]]
        self.sync()
        self.assertEqual(self.iam.attached, {OTHER_POLICY})
        self.assertFalse(self.iam.changes)

    def test_two_late_overages_in_one_page_share_one_detection_day(self):
        billed = [DAY - timedelta(days=n) for n in (2, 3)]
        self.budget.pages = [[cost(day, "850") for day in billed]]
        self.sync()
        self.assertCountEqual(self.store.state["billed_days"], [day.date().isoformat() for day in billed])
        self.assertEqual([name for name, _ in self.iam.changes], ["attach"])
        self.sync(DAY + timedelta(days=1))
        self.assertEqual(self.iam.attached, {OTHER_POLICY})

    def test_redelivered_overage_cannot_extend_denial_into_next_day(self):
        self.budget.pages = [[cost(self.yesterday)]]
        self.sync()
        self.sync()
        tomorrow = DAY + timedelta(days=1)
        self.sync(tomorrow)
        self.sync(tomorrow)
        self.assertEqual([name for name, _ in self.iam.changes], ["attach", "detach"])
        self.assertEqual(self.iam.attached, {OTHER_POLICY})

    def test_older_overage_first_seen_later_still_trips_once(self):
        self.budget.pages = [[cost(self.yesterday)]]
        self.sync()
        older = DAY - timedelta(days=2)
        # The newer billed day has already triggered. The older day is first reported two
        # UTC midnights after it ended and warrants its own, bounded decision today.
        self.budget.pages = [[cost(self.yesterday), cost(older), cost(older)]]
        tomorrow = DAY + timedelta(days=1)
        self.sync(tomorrow)
        self.sync(tomorrow)
        self.assertEqual(self.store.state["blocked_on"], tomorrow.date().isoformat())
        days = self.store.state["billed_days"]
        self.assertEqual(days.count(older.date().isoformat()), 1)
        self.assertEqual(days.count(self.yesterday.date().isoformat()), 1)
        self.assertIn(ENV["POLICY_ARN"], self.iam.attached)
        self.sync(tomorrow + timedelta(days=1))
        self.assertEqual(self.iam.attached, {OTHER_POLICY})

    def test_expiry_only_detaches_without_reading_billing(self):
        self.budget.pages = [[cost(self.yesterday)]]
        self.sync()
        requests = len(self.budget.requests)
        tomorrow = DAY + timedelta(days=1)
        self.sync(tomorrow, expiry_only=True)
        self.assertEqual(len(self.budget.requests), requests)
        self.assertEqual(self.iam.attached, {OTHER_POLICY})

    def test_expired_deny_lifts_even_on_billing_outage(self):
        self.budget.pages = [[cost(self.yesterday)]]
        self.sync()
        with patch.object(self.budget, "describe_budget_performance_history", side_effect=ConnectionError("unavailable")):
            with self.assertRaises(ConnectionError):
                self.sync(DAY + timedelta(days=1))
        self.assertEqual(self.iam.attached, {OTHER_POLICY})

    def test_billing_with_another_currency_or_limit_cannot_attach_deny(self):
        for kwargs in [{"actual_unit": "EUR"}, {"limit_unit": "EUR"}, {"limit": "500"}]:
            with self.subTest(kwargs=kwargs):
                self.budget.pages = [[cost(self.yesterday, actual="850", **kwargs)]]
                with self.assertRaisesRegex(ValueError, "limit or currency"):
                    self.sync()
                self.assertEqual(self.iam.attached, {OTHER_POLICY})
                self.assertNotIn(self.yesterday.date().isoformat(), self.store.state["billed_days"])

    def test_history_for_another_budget_period_or_type_cannot_attach_deny(self):
        response = self.budget.describe_budget_performance_history(
            AccountId=ENV["ACCOUNT_ID"], BudgetName=ENV["BUDGET_NAME"], TimePeriod={}, MaxResults=100)
        response["BudgetPerformanceHistory"]["BudgetedAndActualAmountsList"] = [cost(self.yesterday)]
        for key, value in [("BudgetName", "a-different-fleet"), ("TimeUnit", "MONTHLY"), ("BudgetType", "USAGE")]:
            with self.subTest(field=key), patch.dict(response["BudgetPerformanceHistory"], {key: value}):
                with patch.object(self.budget, "describe_budget_performance_history", return_value=response):
                    with self.assertRaises(ValueError):
                        self.sync()
                self.assertEqual(self.iam.attached, {OTHER_POLICY})
                self.assertEqual(self.store.state["billed_days"], [])

    def test_missing_history_unit_checks_budget_definition_before_using_daily_rows(self):
        self.budget.pages = [[cost(self.yesterday)]]
        response = self.budget.describe_budget_performance_history(
            AccountId=ENV["ACCOUNT_ID"], BudgetName=ENV["BUDGET_NAME"], TimePeriod={}, MaxResults=100)
        del response["BudgetPerformanceHistory"]["TimeUnit"]
        definition = {"BudgetName": ENV["BUDGET_NAME"], "BudgetType": "COST", "TimeUnit": "DAILY"}
        with patch.object(self.budget, "describe_budget_performance_history", return_value=response), \
                patch.object(self.budget, "describe_budget", create=True, return_value={"Budget": definition}) as details:
            for field, bad_value in [("TimeUnit", "MONTHLY"), ("BudgetName", "other"), ("BudgetType", "USAGE")]:
                with self.subTest(field=field), patch.dict(definition, {field: bad_value}):
                    with self.assertRaises(ValueError):
                        self.sync()
                    self.assertEqual(self.iam.attached, {OTHER_POLICY})
            self.sync()
            details.assert_called_with(AccountId=ENV["ACCOUNT_ID"], BudgetName=ENV["BUDGET_NAME"])
        self.assertIn(ENV["POLICY_ARN"], self.iam.attached)

    def test_billing_response_crossing_midnight_is_evaluated_in_the_new_window(self):
        self.budget.pages = [[cost(self.yesterday, "800")]]
        query = self.budget.describe_budget_performance_history
        clock = Mock(return_value=DAY + timedelta(days=1, microseconds=-1))

        def late_response(**args):
            clock.return_value = DAY + timedelta(days=1)
            return query(**args)

        with patch.object(self.budget, "describe_budget_performance_history", side_effect=late_response):
            self.sync(clock)
        self.assertFalse(self.iam.changes)
        self.assertEqual(self.store.state["billed_days"], [])
        self.sync(clock)
        self.assertEqual(self.store.state["blocked_on"], clock.return_value.date().isoformat())
        self.assertEqual([name for name, _ in self.iam.changes], ["attach"])

    def test_retry_after_s3_committed_without_ack(self):
        self.budget.pages = [[cost(self.yesterday)]]
        self.store.after_put = Mock(side_effect=ConnectionError("lost S3 acknowledgement"))
        with self.assertRaises(ConnectionError):
            self.sync()
        self.assertEqual(self.store.state["blocked_on"], DAY.date().isoformat())
        self.assertFalse(self.iam.changes)
        self.store.after_put = None
        self.budget.pages = [[]]
        self.sync()
        self.assertEqual(self.iam.changes, [("attach", self.store.state)])
        self.assertIn(ENV["POLICY_ARN"], self.iam.attached)

    def test_retries_converge_after_iam_applied_without_ack(self):
        self.budget.pages = [[cost(self.yesterday)]]
        self.iam.fail_after_change = True
        with self.assertRaises(ConnectionError):
            self.sync()
        self.iam.fail_after_change = False
        self.sync()
        self.iam.fail_after_change = True
        with self.assertRaises(ConnectionError):
            self.sync(DAY + timedelta(days=1))
        self.iam.fail_after_change = False
        self.sync(DAY + timedelta(days=1))
        self.assertEqual(self.iam.attached, {OTHER_POLICY})
        self.assertEqual([name for name, _ in self.iam.changes], ["attach", "detach"])

    def test_crossing_midnight_while_saving_never_attaches_an_expired_decision(self):
        self.budget.pages = [[cost(self.yesterday)]]
        clock = Mock(return_value=DAY + timedelta(days=1, microseconds=-1))

        def cross_midnight():
            clock.return_value = DAY + timedelta(days=1)

        self.store.after_put = cross_midnight
        self.sync(clock)
        self.assertEqual(self.iam.attached, {OTHER_POLICY})
        self.assertFalse(self.iam.changes)
        self.sync(clock)
        self.assertFalse(self.iam.changes)


class Fleet:
    """Budgets, S3 and IAM for several stacks at once, keyed by budget, state key and role."""

    def __init__(self):
        self.pages = {}
        self.states = {}
        self.attached = {}
        self.broken_budgets = set()

    def describe_budget_performance_history(self, *, AccountId, BudgetName, TimePeriod, MaxResults, NextToken=None):
        if BudgetName in self.broken_budgets:
            raise ConnectionError("billing outage")
        return {"BudgetPerformanceHistory": {
            "BudgetName": BudgetName, "BudgetType": "COST", "TimeUnit": "DAILY",
            "BudgetedAndActualAmountsList": self.pages.get(BudgetName, []),
        }}

    def get_object(self, *, Bucket, Key):
        state = self.states.setdefault(Key, {"blocked_on": "", "billed_days": []})
        return {"Body": io.BytesIO(json.dumps(state).encode())}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.states[Key] = json.loads(Body)
        return {}

    def get_paginator(self, name):
        return self

    def paginate(self, *, RoleName):
        yield {"AttachedPolicies": [{"PolicyArn": arn} for arn in self.attached.get(RoleName, set())]}

    def attach_role_policy(self, *, RoleName, PolicyArn):
        self.attached.setdefault(RoleName, set()).add(PolicyArn)

    def detach_role_policy(self, *, RoleName, PolicyArn):
        self.attached[RoleName].discard(PolicyArn)


STACKS_ENV = {
    **ENV,
    "STATE_KEY": "spend-cap/daily.json", "ROLE_NAME": "stage-coordinator",
    "STACKS": json.dumps([
        {"budget": "fleet-daily", "state_key": "spend-cap/daily.json", "role": "stage-coordinator"},
        {"budget": "fleet-prod-daily", "state_key": "spend-cap/prod-daily.json", "role": "prod-coordinator"},
    ]),
}


class StackTests(unittest.TestCase):
    """fcvm-fleet-budget.tf: each fleet stack has its own budget, decision record and coordinator role."""

    def setUp(self):
        self.fleet = Fleet()

    def run_all(self, now=DAY, expiry_only=False):
        cap.reconcile_all(STACKS_ENV, self.fleet, self.fleet, self.fleet, now, expiry_only=expiry_only)

    def test_current_worker_reconciles_staging_with_previous_configuration(self):
        self.fleet.pages[ENV["BUDGET_NAME"]] = [cost(DAY - timedelta(days=1))]
        cap.reconcile_all(ENV, self.fleet, self.fleet, self.fleet, DAY)
        self.assertEqual(self.fleet.attached, {ENV["ROLE_NAME"]: {ENV["POLICY_ARN"]}})
        self.assertEqual(self.fleet.states[ENV["STATE_KEY"]]["blocked_on"], DAY.date().isoformat())

    def test_one_stacks_overage_blocks_only_that_stacks_coordinator(self):
        self.fleet.pages["fleet-prod-daily"] = [cost(DAY - timedelta(days=1), "751")]
        self.fleet.pages["fleet-daily"] = [cost(DAY - timedelta(days=1), "12.40")]
        self.run_all()
        self.assertEqual(self.fleet.attached.get("prod-coordinator"), {ENV["POLICY_ARN"]})
        self.assertFalse(self.fleet.attached.get("stage-coordinator"))
        self.assertEqual(self.fleet.states["spend-cap/prod-daily.json"]["blocked_on"], DAY.date().isoformat())
        self.assertEqual(self.fleet.states["spend-cap/daily.json"]["blocked_on"], "")
        # At the next UTC midnight the block lifts, with no billing read.
        self.fleet.broken_budgets = {"fleet-daily", "fleet-prod-daily"}
        self.run_all(DAY + timedelta(days=1), expiry_only=True)
        self.assertFalse(self.fleet.attached["prod-coordinator"])

    def test_a_failing_stack_does_not_skip_the_next_and_the_run_still_fails(self):
        self.fleet.broken_budgets = {"fleet-daily"}
        self.fleet.pages["fleet-prod-daily"] = [cost(DAY - timedelta(days=1), "900")]
        with patch("builtins.print"):
            with self.assertRaisesRegex(RuntimeError, "fleet-daily"):
                self.run_all()
        self.assertEqual(self.fleet.attached.get("prod-coordinator"), {ENV["POLICY_ARN"]})

    def test_stalled_stage_request_leaves_time_to_reconcile_production(self):
        original_read = self.fleet.describe_budget_performance_history
        def delayed(**request):
            if request["BudgetName"] == "fleet-daily":
                time.sleep(1)
            return original_read(**request)

        self.fleet.describe_budget_performance_history = delayed
        self.fleet.pages["fleet-prod-daily"] = [cost(DAY - timedelta(days=1), "900")]
        with self.assertRaisesRegex(RuntimeError, "fleet-daily"), patch("builtins.print"):
            cap.reconcile_all(STACKS_ENV, self.fleet, self.fleet, self.fleet, DAY,
                              remaining_ms=lambda: 1200)
        self.assertEqual(self.fleet.attached.get("prod-coordinator"), {ENV["POLICY_ARN"]})

    def test_stacks_are_read_from_the_environment_and_none_is_an_error(self):
        listed = cap.stacks(STACKS_ENV)
        self.assertEqual([(s["BUDGET_NAME"], s["STATE_KEY"], s["ROLE_NAME"]) for s in listed], [
            ("fleet-daily", "spend-cap/daily.json", "stage-coordinator"),
            ("fleet-prod-daily", "spend-cap/prod-daily.json", "prod-coordinator"),
        ])
        for s in listed:
            self.assertEqual((s["ACCOUNT_ID"], s["LIMIT_USD"], s["POLICY_ARN"]), (ENV["ACCOUNT_ID"], "750", ENV["POLICY_ARN"]))
        with self.assertRaises(ValueError):
            cap.stacks({**STACKS_ENV, "STACKS": "[]"})


if __name__ == "__main__":
    unittest.main(verbosity=2, buffer=True)

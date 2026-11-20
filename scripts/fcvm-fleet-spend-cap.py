"""Reconcile the fleet's daily, retrospective launch cap against AWS Budgets."""

import json
import os
import signal
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal


UTC = timezone.utc
LOOKBACK = timedelta(days=3)
DAY = timedelta(days=1)


def over_budget_days(budgets, env, today):
    """Only complete UTC days with an actual USD reading for this specific budget count."""
    start = datetime.combine(today - LOOKBACK, time.min, UTC)
    end = datetime.combine(today, time.min, UTC)
    request = {
        "AccountId": env["ACCOUNT_ID"], "BudgetName": env["BUDGET_NAME"],
        "TimePeriod": {"Start": start, "End": end}, "MaxResults": 100,
    }
    overages = set()
    while True:
        page = budgets.describe_budget_performance_history(**request)
        history = page["BudgetPerformanceHistory"]
        if (history["BudgetName"], history["TimeUnit"], history["BudgetType"]) != (env["BUDGET_NAME"], "DAILY", "COST"):
            raise ValueError("Unexpected daily fleet budget in billing history")
        for entry in history.get("BudgetedAndActualAmountsList", []):
            period = entry["TimePeriod"]
            since = period["Start"].astimezone(UTC)
            if not start <= since < end:
                continue  # Budgets also returns periods overlapping its requested bounds.
            if since.time() != time.min or period["End"] != since + DAY:
                raise ValueError("Unexpected billing period boundaries")
            actual = entry.get("ActualAmount")
            if actual is None:
                continue  # A missing actual regularly occurs on yesterday's bill.
            limit = entry["BudgetedAmount"]
            if (actual["Unit"], limit["Unit"]) != ("USD", "USD") or Decimal(limit["Amount"]) != Decimal(env["LIMIT_USD"]):
                raise ValueError("Unexpected daily fleet budget limit or currency")
            if Decimal(actual["Amount"]) >= Decimal(limit["Amount"]):
                overages.add(since.date().isoformat())
        if not page.get("NextToken"):
            return overages
        request["NextToken"] = page["NextToken"]


def enforce(iam, env, blocked_on, clock):
    role = env["ROLE_NAME"]
    policy = env["POLICY_ARN"]
    policies = iam.get_paginator("list_attached_role_policies").paginate(RoleName=role)
    attached = any(p["PolicyArn"] == policy for page in policies for p in page["AttachedPolicies"])
    # A slow API call or an old queued event must not attach yesterday's decision.
    should_block = blocked_on == clock().astimezone(UTC).date().isoformat()
    if attached != should_block:
        method = iam.attach_role_policy if should_block else iam.detach_role_policy
        method(RoleName=role, PolicyArn=policy)
        print(json.dumps({"event": "spend_cap_changed", "attached": should_block, "blocked_on": blocked_on}))


def reconcile(env, budgets, s3, iam, now, expiry_only=False):
    clock = now if callable(now) else lambda: now
    today = clock().astimezone(UTC).date()
    key = {"Bucket": env["STATE_BUCKET"], "Key": env["STATE_KEY"]}
    state = json.loads(s3.get_object(**key)["Body"].read())
    blocked_on = state["blocked_on"]
    handled = {date.fromisoformat(day) for day in state["billed_days"]}
    # Persisted intent is applied first so billing outages never prevent expiration or crash recovery.
    enforce(iam, env, blocked_on, clock)
    if expiry_only:
        return
    previously_billed = {day.isoformat() for day in handled if day >= today - LOOKBACK}
    observed = over_budget_days(budgets, env, today)
    if clock().astimezone(UTC).date() != today:
        return  # Let the next poll evaluate the new window with fresh cost data.
    new_overages = observed - previously_billed
    decision = {
        "blocked_on": today.isoformat() if new_overages else blocked_on,
        "billed_days": sorted(previously_billed | observed),
    }
    if decision != state:
        # The function has one reserved execution and is the only runtime writer of this S3 key.
        # Save before changing IAM so retries preserve the original UTC expiry after a lost reply.
        s3.put_object(**key, Body=json.dumps(decision).encode(), ContentType="application/json")
        print(json.dumps({"event": "spend_cap_decision", **decision}))
        enforce(iam, env, decision["blocked_on"], clock)


def stacks(env):
    """Each fleet stack's settings for reconcile(): its budget, decision record and coordinator role (STACKS, a JSON
    list), with the account, limit, bucket and deny policy they share."""
    shared = {key: env[key] for key in ("ACCOUNT_ID", "LIMIT_USD", "STATE_BUCKET", "POLICY_ARN")}
    # During rollback, the old environment can arrive before the old handler.
    if "STACKS" not in env:
        return [{**shared, **{key: env[key] for key in ("BUDGET_NAME", "STATE_KEY", "ROLE_NAME")}}]
    listed = json.loads(env["STACKS"])
    if not listed:
        raise ValueError("No fleet stack to reconcile")
    return [{**shared, "BUDGET_NAME": s["budget"], "STATE_KEY": s["state_key"], "ROLE_NAME": s["role"]} for s in listed]


@contextmanager
def stack_deadline(remaining_ms, stack_count):
    if remaining_ms is None:
        yield
        return

    # Leave one second to report failures, and divide the remaining Lambda time fairly.
    seconds = (remaining_ms() / 1000 - 1) / stack_count
    if seconds <= 0:
        raise TimeoutError("Insufficient time to reconcile a fleet budget")

    def expired(signum, frame):
        raise TimeoutError("Fleet budget reconciliation exceeded its share of the Lambda timeout")

    prior_handler = signal.signal(signal.SIGALRM, expired)
    try:
        signal.setitimer(signal.ITIMER_REAL, seconds)
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prior_handler)


def reconcile_all(env, budgets, s3, iam, now, expiry_only=False, remaining_ms=None):
    """Reconcile every stack; one stack's failure does not skip the next one's, and the run still fails."""
    failed = []
    listed = stacks(env)
    for i, stack in enumerate(listed):
        try:
            with stack_deadline(remaining_ms, len(listed) - i):
                reconcile(stack, budgets, s3, iam, now, expiry_only=expiry_only)
        except Exception as error:  # noqa: BLE001 - reported, then raised below
            print(json.dumps({"event": "spend_cap_failed", "budget": stack["BUDGET_NAME"], "error": type(error).__name__}))
            failed.append(stack["BUDGET_NAME"])
    if failed:
        raise RuntimeError("Spend cap reconciliation failed for " + ", ".join(failed))


def handler(event, context):
    import boto3
    from botocore.config import Config

    config = Config(connect_timeout=2, read_timeout=4, retries={"total_max_attempts": 1})
    reconcile_all(os.environ, boto3.client("budgets", region_name="us-east-1", config=config),
                  boto3.client("s3", config=config), boto3.client("iam", config=config),
                  lambda: datetime.now(UTC), expiry_only=event == {"action": "expire"},
                  remaining_ms=context.get_remaining_time_in_millis)

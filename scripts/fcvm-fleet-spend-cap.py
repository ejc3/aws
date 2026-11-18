"""Reconcile the fleet's daily, retrospective launch cap against AWS Budgets."""

import json
import os
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


def handler(event, context):
    import boto3

    reconcile(os.environ, boto3.client("budgets", region_name="us-east-1"), boto3.client("s3"),
              boto3.client("iam"), lambda: datetime.now(UTC), expiry_only=event == {"action": "expire"})
